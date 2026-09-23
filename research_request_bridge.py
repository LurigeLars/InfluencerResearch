#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creator_registry import normalize_registration_request

BRIDGE_VERSION = "0.6.0"
BRIDGE_NAME = "instagramresearch-control-v1"
BRIDGE_STATE_SCHEMA_VERSION = 1
BRIDGE_STATE_UPGRADE_SOURCE_VERSION = "0.5.0"
BRIDGE_STATE_ALLOWED_KEYS = {
    "schema_version", "bridge_version", "paused", "processed_request_ids",
    "last_request_id", "last_action", "last_result", "last_exit_code", "updated_at",
}
BRIDGE_STATE_PRESERVED_KEYS = (
    "paused", "processed_request_ids", "last_request_id", "last_action",
    "last_result", "last_exit_code",
)
BRIDGE_STATE_UPGRADE_BACKUP_NAME = "research_bridge_state.pre_v0.6.0-upgrade.json"
BRIDGE_STATE_UPGRADE_TRANSACTION_NAME = "research_bridge_state.pre_v0.6.0-upgrade.transaction.json"
BRIDGE_STATE_UPGRADE_TRANSACTION_SCHEMA_VERSION = 2
BRIDGE_STATE_UPGRADE_TRANSACTION_ALLOWED_KEYS = {
    "schema_version", "transaction_id", "source_version", "target_version",
    "source_sha256", "source_size", "current_sha256", "current_size",
    "pending_sha256", "pending_size", "phase",
}
BRIDGE_STATE_UPGRADE_TRANSACTION_PHASES = {
    "PREPARING", "PREPARED", "WRITE_INTENT", "MIGRATED",
    "COMMIT_CLEANUP", "RESTORE_CLEANUP",
}
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 30.0
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_REQUEST_TTL_SECONDS = 30 * 60
ALLOWED_ACTIONS = {
    "RUN_TIKTOK_SYNC",
    "REGISTER_CREATOR",
    "RUN_CREATOR_EVALUATION",
    "RUN_CREATOR_MONITOR",
    "RUN_CREATOR_RECENT_CHECK",
    "APPLY_RESEARCH_DECISIONS",
    "STOP_CURRENT",
    "PAUSE",
    "RESUME",
    "HEALTH",
}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
COMMON_REQUEST_KEYS = {
    "schema_version", "bridge", "state", "request_id", "action",
    "issued_by", "issued_at", "expires_at", "reason"
}
CREATOR_REQUEST_KEYS = {"creator_key", "sample_size", "source_platform", "max_new"}
REGISTRATION_REQUEST_KEYS = {"creator_key", "display_name", "verification_methods", "verification_refs", "sources"}
CREATOR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a JSON string")
    if not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError("timestamp must include an explicit UTC/offset suffix")
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError("timestamp offset missing")
    return dt.astimezone(timezone.utc)


def _atomic_replace_with_retry(src: Path, dst: Path, timeout_seconds: float = 1.0) -> None:
    """Atomically replace dst, waiting briefly for a security reader to release its handle.

    Windows readers intentionally deny FILE_SHARE_WRITE while reading bounded JSON bytes from
    one handle.  A concurrent atomic replacement may therefore receive ACCESS_DENIED or
    SHARING_VIOLATION until that short read completes.  Retry only those Windows sharing
    failures for a bounded interval; never fall back to an in-place/non-atomic write.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if os.name != "nt" or winerror not in (5, 32) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _atomic_replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, obj: dict) -> None:
    atomic_bytes(path, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _open_json_read_handle(path: Path):
    """Open one stable read handle; on Windows deny in-place writers but allow atomic replace."""
    if os.name != "nt":
        return path.open("rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    invalid_handle = wintypes.HANDLE(-1).value
    handle = create_file(
        str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, None, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == invalid_handle:
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FileNotFoundError(err, os.strerror(err), str(path))
        raise OSError(err, f"CreateFileW failed for JSON input: {path}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = msvcrt.open_osfhandle(handle, flags)
    except Exception:
        close_handle(handle)
        raise
    # open_osfhandle transfers ownership of the native HANDLE to the fd.
    return os.fdopen(fd, "rb", closefd=True)



def _strict_json_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def _reject_json_constant(value: str):
    raise ValueError(f"Non-standard JSON numeric constant: {value}")

def read_bounded_bytes(path: Path) -> bytes:
    fh = _open_json_read_handle(path)
    with fh:
        data = fh.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f"JSON file exceeds {MAX_JSON_BYTES} byte limit: {path}")
    return data


def _parse_json_bytes(data: bytes):
    return json.loads(
        data.decode("utf-8-sig"),
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )


def _read_json_snapshot(path: Path):
    try:
        data = read_bounded_bytes(path)
    except FileNotFoundError:
        return None
    return data, _parse_json_bytes(data)


def load_json(path: Path, default=None):
    snapshot = _read_json_snapshot(path)
    return default if snapshot is None else snapshot[1]


def _validate_persisted_bridge_state(persisted: dict, *, allowed_versions: set[str]) -> None:
    if not isinstance(persisted, dict):
        raise RuntimeError("Persisted bridge state is not an object.")
    keys = set(persisted)
    unknown = keys - BRIDGE_STATE_ALLOWED_KEYS
    missing = BRIDGE_STATE_ALLOWED_KEYS - keys
    if unknown:
        raise RuntimeError(f"Persisted bridge state has unexpected fields: {sorted(unknown)}")
    if missing:
        raise RuntimeError(f"Persisted bridge state is missing required fields: {sorted(missing)}")
    schema_version = persisted["schema_version"]
    if type(schema_version) is not int or schema_version != BRIDGE_STATE_SCHEMA_VERSION:
        raise RuntimeError("Persisted bridge state schema mismatch.")
    bridge_version = persisted["bridge_version"]
    if not isinstance(bridge_version, str) or bridge_version not in allowed_versions:
        raise RuntimeError("Persisted bridge state version mismatch.")
    if not isinstance(persisted["paused"], bool):
        raise RuntimeError("Persisted bridge paused flag is invalid.")
    processed_ids = persisted["processed_request_ids"]
    if not isinstance(processed_ids, list) or len(processed_ids) > 100:
        raise RuntimeError("Persisted processed_request_ids is invalid.")
    if any(not isinstance(x, str) or not REQUEST_ID_RE.fullmatch(x) for x in processed_ids):
        raise RuntimeError("Persisted processed_request_ids contains an invalid request id.")
    if len(set(processed_ids)) != len(processed_ids):
        raise RuntimeError("Persisted processed_request_ids contains duplicates.")
    last_request_id = persisted["last_request_id"]
    if last_request_id is not None and (not isinstance(last_request_id, str) or not REQUEST_ID_RE.fullmatch(last_request_id)):
        raise RuntimeError("Persisted last_request_id is invalid.")
    last_action = persisted["last_action"]
    if last_action is not None and (not isinstance(last_action, str) or last_action not in ALLOWED_ACTIONS):
        raise RuntimeError("Persisted last_action is invalid.")
    last_result = persisted["last_result"]
    if last_result is not None and (not isinstance(last_result, str) or len(last_result) > 100):
        raise RuntimeError("Persisted last_result is invalid.")
    last_exit_code = persisted["last_exit_code"]
    if last_exit_code is not None and (type(last_exit_code) is not int):
        raise RuntimeError("Persisted last_exit_code is invalid.")
    if not isinstance(persisted["updated_at"], str) or len(persisted["updated_at"]) > 128:
        raise RuntimeError("Persisted updated_at type is invalid.")
    try:
        updated_at = parse_iso(persisted["updated_at"])
    except Exception as exc:
        raise RuntimeError("Persisted updated_at is invalid.") from exc
    if updated_at is None or updated_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise RuntimeError("Persisted updated_at is missing or implausibly in the future.")


def _bridge_state_upgrade_backup_path() -> Path:
    local_base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
    return local_base / "recovery" / BRIDGE_STATE_UPGRADE_BACKUP_NAME


def _bridge_state_upgrade_transaction_path() -> Path:
    local_base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
    return local_base / "recovery" / BRIDGE_STATE_UPGRADE_TRANSACTION_NAME


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_upgrade_transaction_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise RuntimeError("Bridge-state upgrade transaction record is not an object.")
    keys = set(record)
    unknown = keys - BRIDGE_STATE_UPGRADE_TRANSACTION_ALLOWED_KEYS
    missing = BRIDGE_STATE_UPGRADE_TRANSACTION_ALLOWED_KEYS - keys
    if unknown or missing:
        raise RuntimeError(
            "Bridge-state upgrade transaction record schema mismatch: "
            f"unexpected={sorted(unknown)} missing={sorted(missing)}"
        )
    if type(record["schema_version"]) is not int or record["schema_version"] != BRIDGE_STATE_UPGRADE_TRANSACTION_SCHEMA_VERSION:
        raise RuntimeError("Bridge-state upgrade transaction schema mismatch.")
    transaction_id = record["transaction_id"]
    if not isinstance(transaction_id, str):
        raise RuntimeError("Bridge-state upgrade transaction_id type is invalid.")
    try:
        parsed = uuid.UUID(transaction_id)
    except Exception as exc:
        raise RuntimeError("Bridge-state upgrade transaction_id is invalid.") from exc
    if str(parsed) != transaction_id:
        raise RuntimeError("Bridge-state upgrade transaction_id is not canonical.")
    if record["source_version"] != BRIDGE_STATE_UPGRADE_SOURCE_VERSION or record["target_version"] != BRIDGE_VERSION:
        raise RuntimeError("Bridge-state upgrade transaction version binding mismatch.")
    for field in ("source_sha256", "current_sha256"):
        value = record[field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RuntimeError(f"Bridge-state upgrade transaction {field} is invalid.")
    for field in ("source_size", "current_size"):
        value = record[field]
        if type(value) is not int or value < 0 or value > MAX_JSON_BYTES:
            raise RuntimeError(f"Bridge-state upgrade transaction {field} is invalid.")
    pending_sha = record["pending_sha256"]
    pending_size = record["pending_size"]
    if pending_sha is not None and (not isinstance(pending_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", pending_sha)):
        raise RuntimeError("Bridge-state upgrade transaction pending_sha256 is invalid.")
    if pending_size is not None and (type(pending_size) is not int or pending_size < 0 or pending_size > MAX_JSON_BYTES):
        raise RuntimeError("Bridge-state upgrade transaction pending_size is invalid.")
    if (pending_sha is None) != (pending_size is None):
        raise RuntimeError("Bridge-state upgrade transaction pending identity is incomplete.")
    phase = record["phase"]
    if phase not in BRIDGE_STATE_UPGRADE_TRANSACTION_PHASES:
        raise RuntimeError("Bridge-state upgrade transaction phase is invalid.")
    if phase == "WRITE_INTENT":
        if pending_sha is None:
            raise RuntimeError("Bridge-state upgrade WRITE_INTENT is missing pending identity.")
    elif pending_sha is not None:
        raise RuntimeError("Bridge-state upgrade transaction has pending identity outside WRITE_INTENT.")


def _read_upgrade_transaction_raw():
    backup_path = _bridge_state_upgrade_backup_path()
    transaction_path = _bridge_state_upgrade_transaction_path()
    backup_snapshot = _read_json_snapshot(backup_path)
    transaction_snapshot = _read_json_snapshot(transaction_path)
    if backup_snapshot is None and transaction_snapshot is None:
        return None

    backup_bytes = backup = None
    if backup_snapshot is not None:
        backup_bytes, backup = backup_snapshot
        _validate_persisted_bridge_state(backup, allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION})

    transaction_bytes = transaction = None
    if transaction_snapshot is not None:
        transaction_bytes, transaction = transaction_snapshot
        _validate_upgrade_transaction_record(transaction)

    if transaction is None:
        raise RuntimeError("Bridge-state upgrade artifacts are incomplete: orphan rollback backup has no transaction journal.")

    if backup_bytes is not None:
        source_sha = _sha256_bytes(backup_bytes)
        if source_sha != transaction["source_sha256"] or len(backup_bytes) != transaction["source_size"]:
            raise RuntimeError("Bridge-state upgrade backup is not bound to the transaction source identity.")

    return {
        "backup_path": backup_path,
        "transaction_path": transaction_path,
        "backup_bytes": backup_bytes,
        "backup": backup,
        "transaction_bytes": transaction_bytes,
        "transaction": transaction,
    }


def _write_upgrade_transaction_record(transaction_path: Path, record: dict) -> None:
    _validate_upgrade_transaction_record(record)
    atomic_json(transaction_path, record)
    snapshot = _read_json_snapshot(transaction_path)
    if snapshot is None:
        raise RuntimeError("Bridge-state upgrade transaction journal read-back is missing.")
    _, check = snapshot
    _validate_upgrade_transaction_record(check)
    if check != record:
        raise RuntimeError("Bridge-state upgrade transaction journal read-back mismatch.")


def _unlink_upgrade_artifact(path: Path) -> None:
    path.unlink()


def _assert_transaction_current_binding(state_bytes: bytes, persisted: dict, transaction: dict, backup_bytes: bytes | None) -> None:
    current_sha = _sha256_bytes(state_bytes)
    if current_sha != transaction["current_sha256"] or len(state_bytes) != transaction["current_size"]:
        raise RuntimeError("Persisted bridge state does not match the bound upgrade transaction generation.")
    if transaction["phase"] in {"PREPARING", "PREPARED"}:
        if persisted["bridge_version"] != BRIDGE_STATE_UPGRADE_SOURCE_VERSION:
            raise RuntimeError("Prepared bridge-state upgrade transaction is not bound to v0.5.0 state.")
        if current_sha != transaction["source_sha256"]:
            raise RuntimeError("Prepared bridge-state transaction source identity drifted.")
        if backup_bytes is not None and state_bytes != backup_bytes:
            raise RuntimeError("Prepared bridge-state transaction source no longer matches its exact rollback backup.")
    elif transaction["phase"] in {"MIGRATED", "COMMIT_CLEANUP"}:
        if persisted["bridge_version"] != BRIDGE_VERSION:
            raise RuntimeError("Migrated bridge-state upgrade transaction is not bound to v0.6.0 state.")


def _assert_preserved_state_fields(before: dict, after: dict) -> None:
    for key in BRIDGE_STATE_PRESERVED_KEYS:
        if before[key] != after[key]:
            raise RuntimeError(f"Persisted bridge state migration changed preserved field: {key}")


def _new_upgrade_transaction_record(source_bytes: bytes) -> dict:
    source_sha = _sha256_bytes(source_bytes)
    return {
        "schema_version": BRIDGE_STATE_UPGRADE_TRANSACTION_SCHEMA_VERSION,
        "transaction_id": str(uuid.uuid4()),
        "source_version": BRIDGE_STATE_UPGRADE_SOURCE_VERSION,
        "target_version": BRIDGE_VERSION,
        "source_sha256": source_sha,
        "source_size": len(source_bytes),
        "current_sha256": source_sha,
        "current_size": len(source_bytes),
        "pending_sha256": None,
        "pending_size": None,
        "phase": "PREPARING",
    }


def _recover_upgrade_transaction_artifacts(state_path: Path, state_snapshot=...):
    """Recover only exact self-generated intermediate journal states.

    Ambiguous or identity-mismatched artifacts remain fail-closed. Every mutation below
    is justified by exact canonical-state, source-backup and journal identities. When a
    caller already captured canonical bytes, reuse that exact generation throughout the
    bounded journal-only recovery loop rather than re-opening the decision-critical path.
    """
    if state_snapshot is ...:
        state_snapshot = _read_json_snapshot(state_path)
    for _ in range(8):
        raw = _read_upgrade_transaction_raw()
        if raw is None:
            return None
        transaction = raw["transaction"]
        phase = transaction["phase"]
        if state_snapshot is None:
            raise RuntimeError("Persisted bridge state is missing while an upgrade transaction is pending.")
        state_bytes, persisted = state_snapshot
        _validate_persisted_bridge_state(
            persisted,
            allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION, BRIDGE_VERSION},
        )
        state_sha = _sha256_bytes(state_bytes)
        state_size = len(state_bytes)
        backup_bytes = raw["backup_bytes"]

        if phase == "PREPARING":
            if persisted["bridge_version"] != BRIDGE_STATE_UPGRADE_SOURCE_VERSION:
                raise RuntimeError("Preparing bridge-state transaction is not bound to v0.5.0 state.")
            if state_sha != transaction["source_sha256"] or state_size != transaction["source_size"]:
                raise RuntimeError("Preparing bridge-state transaction source identity drifted.")
            if backup_bytes is None:
                atomic_bytes(raw["backup_path"], state_bytes)
                continue
            if backup_bytes != state_bytes:
                raise RuntimeError("Preparing bridge-state rollback backup does not match exact source bytes.")
            updated = dict(transaction)
            updated["phase"] = "PREPARED"
            _write_upgrade_transaction_record(raw["transaction_path"], updated)
            continue

        if phase == "WRITE_INTENT":
            if backup_bytes is None:
                raise RuntimeError("Bridge-state upgrade artifacts are incomplete during WRITE_INTENT.")
            current_matches = (
                state_sha == transaction["current_sha256"] and
                state_size == transaction["current_size"]
            )
            pending_matches = (
                state_sha == transaction["pending_sha256"] and
                state_size == transaction["pending_size"]
            )
            if current_matches:
                if persisted["bridge_version"] == BRIDGE_STATE_UPGRADE_SOURCE_VERSION:
                    if state_sha != transaction["source_sha256"] or state_bytes != backup_bytes:
                        raise RuntimeError("Interrupted bridge-state write source no longer matches rollback backup.")
                    stable_phase = "PREPARED"
                elif persisted["bridge_version"] == BRIDGE_VERSION:
                    stable_phase = "MIGRATED"
                else:
                    raise RuntimeError("Interrupted bridge-state write has unsupported current version.")
                updated = dict(transaction)
                updated["phase"] = stable_phase
                updated["pending_sha256"] = None
                updated["pending_size"] = None
                _write_upgrade_transaction_record(raw["transaction_path"], updated)
                continue
            if pending_matches:
                if persisted["bridge_version"] != BRIDGE_VERSION:
                    raise RuntimeError("Interrupted bridge-state write target identity is not v0.6.0.")
                updated = dict(transaction)
                updated["phase"] = "MIGRATED"
                updated["current_sha256"] = transaction["pending_sha256"]
                updated["current_size"] = transaction["pending_size"]
                updated["pending_sha256"] = None
                updated["pending_size"] = None
                _write_upgrade_transaction_record(raw["transaction_path"], updated)
                continue
            raise RuntimeError("Interrupted bridge-state write cannot be classified against current or pending identity.")

        if phase == "MIGRATED":
            if backup_bytes is None:
                raise RuntimeError("Bridge-state upgrade artifacts are incomplete for migrated transaction.")
            if persisted["bridge_version"] == BRIDGE_VERSION:
                _assert_transaction_current_binding(state_bytes, persisted, transaction, backup_bytes)
                return raw
            if (
                persisted["bridge_version"] == BRIDGE_STATE_UPGRADE_SOURCE_VERSION and
                state_sha == transaction["source_sha256"] and
                state_size == transaction["source_size"] and
                state_bytes == backup_bytes
            ):
                # Crash after exact canonical restore but before RESTORE_CLEANUP journal advance.
                updated = dict(transaction)
                updated["phase"] = "RESTORE_CLEANUP"
                updated["current_sha256"] = transaction["source_sha256"]
                updated["current_size"] = transaction["source_size"]
                _write_upgrade_transaction_record(raw["transaction_path"], updated)
                continue
            raise RuntimeError("Migrated bridge-state transaction canonical generation drifted.")

        if phase == "PREPARED":
            if backup_bytes is None:
                raise RuntimeError("Bridge-state upgrade artifacts are incomplete for prepared transaction.")
            _assert_transaction_current_binding(state_bytes, persisted, transaction, backup_bytes)
            return raw

        if phase == "COMMIT_CLEANUP":
            if persisted["bridge_version"] != BRIDGE_VERSION:
                raise RuntimeError("Commit-cleanup transaction is not bound to v0.6.0 state.")
            if state_sha != transaction["current_sha256"] or state_size != transaction["current_size"]:
                raise RuntimeError("Commit-cleanup canonical generation drifted.")
            if backup_bytes is not None:
                _unlink_upgrade_artifact(raw["backup_path"])
            _unlink_upgrade_artifact(raw["transaction_path"])
            continue

        if phase == "RESTORE_CLEANUP":
            if persisted["bridge_version"] != BRIDGE_STATE_UPGRADE_SOURCE_VERSION:
                raise RuntimeError("Restore-cleanup transaction is not bound to v0.5.0 state.")
            if state_sha != transaction["source_sha256"] or state_size != transaction["source_size"]:
                raise RuntimeError("Restore-cleanup canonical source identity drifted.")
            if backup_bytes is not None and state_bytes != backup_bytes:
                raise RuntimeError("Restore-cleanup canonical bytes differ from rollback backup.")
            if backup_bytes is not None:
                _unlink_upgrade_artifact(raw["backup_path"])
            _unlink_upgrade_artifact(raw["transaction_path"])
            continue

        raise RuntimeError("Bridge-state upgrade transaction recovery reached an unsupported phase.")
    raise RuntimeError("Bridge-state upgrade recovery exceeded bounded transition count.")


def _create_upgrade_transaction(state_path: Path, source_bytes: bytes) -> dict:
    backup_path = _bridge_state_upgrade_backup_path()
    transaction_path = _bridge_state_upgrade_transaction_path()
    if backup_path.exists() or transaction_path.exists():
        raise RuntimeError("Refusing to create bridge-state upgrade transaction over existing artifacts.")
    record = _new_upgrade_transaction_record(source_bytes)
    # Publish recoverable intent before creating the separate rollback backup. From
    # this point onward every interruption boundary has a durable transaction identity.
    _write_upgrade_transaction_record(transaction_path, record)
    atomic_bytes(backup_path, source_bytes)
    raw = _read_upgrade_transaction_raw()
    if raw is None or raw["backup_bytes"] != source_bytes:
        raise RuntimeError("Bridge-state upgrade backup read-back mismatch.")
    updated = dict(record)
    updated["phase"] = "PREPARED"
    _write_upgrade_transaction_record(transaction_path, updated)
    source_state = _parse_json_bytes(source_bytes)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, (source_bytes, source_state))
    if artifacts is None or artifacts["transaction"]["transaction_id"] != record["transaction_id"]:
        raise RuntimeError("Bridge-state upgrade transaction preparation recovery/read-back failed.")
    return artifacts


def _load_runtime_persisted_state(state_path: Path) -> dict:
    state_snapshot = _read_json_snapshot(state_path)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    if state_snapshot is None:
        if artifacts is not None:
            raise RuntimeError("Persisted bridge state is missing while an upgrade transaction is pending.")
        return {}
    state_bytes, persisted = state_snapshot
    _validate_persisted_bridge_state(
        persisted,
        allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION, BRIDGE_VERSION},
    )
    version = persisted["bridge_version"]
    if version == BRIDGE_VERSION:
        if artifacts is not None:
            transaction = artifacts["transaction"]
            if transaction["phase"] != "MIGRATED":
                raise RuntimeError("Current v0.6.0 state is not bound to a migrated upgrade transaction.")
            _assert_transaction_current_binding(state_bytes, persisted, transaction, artifacts["backup_bytes"])
        return persisted

    if artifacts is None:
        raise RuntimeError("Persisted v0.5.0 bridge state is not prepared for v0.6.0 upgrade.")
    transaction = artifacts["transaction"]
    if transaction["phase"] != "PREPARED":
        raise RuntimeError("Persisted v0.5.0 state is not bound to a prepared upgrade transaction.")
    _assert_transaction_current_binding(state_bytes, persisted, transaction, artifacts["backup_bytes"])
    if state_bytes != artifacts["backup_bytes"]:
        raise RuntimeError("Prepared v0.5.0 bridge-state backup does not match persisted state.")
    return persisted


def prepare_persisted_state_upgrade(root: Path) -> dict:
    state_path = root.resolve() / "state" / "research_bridge_state.json"
    state_snapshot = _read_json_snapshot(state_path)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    if state_snapshot is None:
        if artifacts is not None:
            raise RuntimeError("Persisted bridge state is missing while an upgrade transaction is pending.")
        return {"result": "NO_PERSISTED_STATE", "source_version": None, "target_version": BRIDGE_VERSION}

    state_bytes, persisted = state_snapshot
    _validate_persisted_bridge_state(
        persisted,
        allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION, BRIDGE_VERSION},
    )
    source_version = persisted["bridge_version"]
    if source_version == BRIDGE_VERSION:
        if artifacts is None:
            return {"result": "ALREADY_CURRENT", "source_version": BRIDGE_VERSION, "target_version": BRIDGE_VERSION}
        transaction = artifacts["transaction"]
        if transaction["phase"] != "MIGRATED":
            raise RuntimeError("Current v0.6.0 state is not bound to a migrated upgrade transaction.")
        _assert_transaction_current_binding(state_bytes, persisted, transaction, artifacts["backup_bytes"])
        return {
            "result": "ALREADY_MIGRATED_PENDING_COMMIT",
            "source_version": BRIDGE_VERSION,
            "target_version": BRIDGE_VERSION,
            "transaction_id": transaction["transaction_id"],
            "source_sha256": transaction["source_sha256"],
        }

    if artifacts is None:
        artifacts = _create_upgrade_transaction(state_path, state_bytes)
    transaction = artifacts["transaction"]
    if transaction["phase"] != "PREPARED":
        raise RuntimeError("Current v0.5.0 state is not bound to a prepared upgrade transaction.")
    _assert_transaction_current_binding(state_bytes, persisted, transaction, artifacts["backup_bytes"])
    if state_bytes != artifacts["backup_bytes"]:
        raise RuntimeError("Existing bridge-state upgrade backup does not match current v0.5.0 state.")
    return {
        "result": "PREPARED",
        "source_version": BRIDGE_STATE_UPGRADE_SOURCE_VERSION,
        "target_version": BRIDGE_VERSION,
        "backup_path": str(artifacts["backup_path"]),
        "transaction_path": str(artifacts["transaction_path"]),
        "transaction_id": transaction["transaction_id"],
        "source_sha256": transaction["source_sha256"],
    }


def _prepare_upgrade_transaction_state_write(state_path: Path, written_bytes: bytes):
    state_snapshot = _read_json_snapshot(state_path)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    if artifacts is None:
        return None
    if state_snapshot is None:
        raise RuntimeError("Cannot persist bridge state while the bound upgrade transaction current state is missing.")
    state_bytes, persisted = state_snapshot
    _validate_persisted_bridge_state(
        persisted,
        allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION, BRIDGE_VERSION},
    )
    transaction = artifacts["transaction"]
    if transaction["phase"] not in {"PREPARED", "MIGRATED"}:
        raise RuntimeError("Cannot create bridge-state write intent from an unstable transaction phase.")
    _assert_transaction_current_binding(state_bytes, persisted, transaction, artifacts["backup_bytes"])
    target = _parse_json_bytes(written_bytes)
    _validate_persisted_bridge_state(target, allowed_versions={BRIDGE_VERSION})
    updated = dict(transaction)
    updated["phase"] = "WRITE_INTENT"
    updated["pending_sha256"] = _sha256_bytes(written_bytes)
    updated["pending_size"] = len(written_bytes)
    _write_upgrade_transaction_record(artifacts["transaction_path"], updated)
    return transaction["transaction_id"]


def _record_upgrade_transaction_state_write(state_path: Path, written_bytes: bytes, transaction_id: str) -> None:
    state_snapshot = _read_json_snapshot(state_path)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    if artifacts is None:
        raise RuntimeError("Upgrade transaction disappeared after bridge-state persistence.")
    transaction = artifacts["transaction"]
    if transaction["transaction_id"] != transaction_id:
        raise RuntimeError("Upgrade transaction identity changed during bridge-state persistence.")
    if transaction["phase"] != "MIGRATED":
        raise RuntimeError("Upgrade transaction did not recover to MIGRATED after bridge-state persistence.")
    if state_snapshot is None:
        raise RuntimeError("Persisted bridge state disappeared after atomic write.")
    current_bytes, current = state_snapshot
    if current_bytes != written_bytes:
        raise RuntimeError("Persisted bridge state drifted before upgrade transaction binding could advance.")
    _validate_persisted_bridge_state(current, allowed_versions={BRIDGE_VERSION})
    if transaction["current_sha256"] != _sha256_bytes(current_bytes) or transaction["current_size"] != len(current_bytes):
        raise RuntimeError("Upgrade transaction current-state read-back failed after bridge-state persistence.")


def restore_persisted_state_upgrade_backup(root: Path) -> dict:
    state_path = root.resolve() / "state" / "research_bridge_state.json"
    state_snapshot = _read_json_snapshot(state_path)
    try:
        artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    except RuntimeError as exc:
        if (
            state_snapshot is not None
            and "does not match the bound upgrade transaction generation" in str(exc)
            and isinstance(state_snapshot[1], dict)
            and state_snapshot[1].get("bridge_version") == BRIDGE_STATE_UPGRADE_SOURCE_VERSION
        ):
            raw = _read_upgrade_transaction_raw()
            if raw is not None and raw["backup_bytes"] is not None and state_snapshot[0] != raw["backup_bytes"]:
                raise RuntimeError("Refusing bridge-state rollback over a different v0.5.0 state.") from exc
        raise
    if artifacts is None:
        return {"result": "NO_UPGRADE_BACKUP"}
    if state_snapshot is None:
        raise RuntimeError("Refusing bridge-state rollback while canonical state is missing.")
    state_bytes, current = state_snapshot
    _validate_persisted_bridge_state(
        current,
        allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION, BRIDGE_VERSION},
    )
    transaction = artifacts["transaction"]
    source_sha = transaction["source_sha256"]
    transaction_id = transaction["transaction_id"]
    if current["bridge_version"] == BRIDGE_STATE_UPGRADE_SOURCE_VERSION:
        if transaction["phase"] != "PREPARED":
            raise RuntimeError("Refusing v0.5.0 rollback cleanup from a non-prepared transaction.")
        if state_bytes != artifacts["backup_bytes"] or _sha256_bytes(state_bytes) != source_sha:
            raise RuntimeError("Refusing bridge-state rollback over a different v0.5.0 state.")
        updated = dict(transaction)
        updated["phase"] = "RESTORE_CLEANUP"
        updated["current_sha256"] = source_sha
        updated["current_size"] = len(state_bytes)
        _write_upgrade_transaction_record(artifacts["transaction_path"], updated)
        _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
        return {
            "result": "ALREADY_RESTORED",
            "restored_version": BRIDGE_STATE_UPGRADE_SOURCE_VERSION,
            "transaction_id": transaction_id,
        }

    if transaction["phase"] != "MIGRATED":
        raise RuntimeError("Refusing v0.6.0 rollback from a transaction that is not MIGRATED.")
    _assert_transaction_current_binding(state_bytes, current, transaction, artifacts["backup_bytes"])
    atomic_bytes(state_path, artifacts["backup_bytes"])
    restored_snapshot = _read_json_snapshot(state_path)
    if restored_snapshot is None:
        raise RuntimeError("Bridge-state rollback read-back is missing.")
    restored_bytes, restored = restored_snapshot
    if restored_bytes != artifacts["backup_bytes"]:
        raise RuntimeError("Bridge-state rollback byte read-back mismatch.")
    _validate_persisted_bridge_state(restored, allowed_versions={BRIDGE_STATE_UPGRADE_SOURCE_VERSION})
    updated = dict(transaction)
    updated["phase"] = "RESTORE_CLEANUP"
    updated["current_sha256"] = source_sha
    updated["current_size"] = len(restored_bytes)
    _write_upgrade_transaction_record(artifacts["transaction_path"], updated)
    _recover_upgrade_transaction_artifacts(state_path, restored_snapshot)
    return {
        "result": "RESTORED",
        "restored_version": BRIDGE_STATE_UPGRADE_SOURCE_VERSION,
        "transaction_id": transaction_id,
        "source_sha256": source_sha,
    }


def commit_persisted_state_upgrade(root: Path) -> dict:
    state_path = root.resolve() / "state" / "research_bridge_state.json"
    state_snapshot = _read_json_snapshot(state_path)
    artifacts = _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    if artifacts is None:
        if state_snapshot is not None:
            _validate_persisted_bridge_state(state_snapshot[1], allowed_versions={BRIDGE_VERSION})
        return {"result": "NO_UPGRADE_BACKUP"}
    if state_snapshot is None:
        raise RuntimeError("Refusing bridge-state upgrade commit while canonical state is missing.")
    state_bytes, current = state_snapshot
    _validate_persisted_bridge_state(current, allowed_versions={BRIDGE_VERSION})
    transaction = artifacts["transaction"]
    if transaction["phase"] != "MIGRATED":
        raise RuntimeError("Refusing bridge-state upgrade commit before migration is bound.")
    _assert_transaction_current_binding(state_bytes, current, transaction, artifacts["backup_bytes"])
    transaction_id = transaction["transaction_id"]
    updated = dict(transaction)
    updated["phase"] = "COMMIT_CLEANUP"
    _write_upgrade_transaction_record(artifacts["transaction_path"], updated)
    _recover_upgrade_transaction_artifacts(state_path, state_snapshot)
    return {"result": "COMMITTED", "target_version": BRIDGE_VERSION, "transaction_id": transaction_id}

def acquire_single_instance_lock() -> object:
    lock_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "research_request_bridge.lock"
    fh = lock_path.open("a+b")
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit(
                "Another InstagramResearch request bridge instance is already running."
            )
    return fh


class _WindowsLifetimeJob:
    """Bind this bridge process and descendants to a kill-on-root-exit Job Object."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        if os.name != "nt":
            self.handle = None
            return

        import ctypes
        from ctypes import wintypes

        ULONG_PTR = wintypes.WPARAM

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ULONG_PTR),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        get_current = kernel32.GetCurrentProcess
        get_current.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_job(None, None)
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, "CreateJobObjectW failed for bridge lifetime containment")
        try:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not set_info(
                handle,
                self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                err = ctypes.get_last_error()
                raise OSError(err, "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed")
            if not assign(handle, get_current()):
                err = ctypes.get_last_error()
                raise OSError(err, "AssignProcessToJobObject failed for bridge root")
        except Exception:
            close_handle(handle)
            raise

        self._close_handle = close_handle
        self.handle = handle

    def close(self):
        handle = getattr(self, "handle", None)
        if handle:
            self.handle = None
            self._close_handle(handle)



class Bridge:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.app = self.root / "app"
        self.control = self.root / "control"
        self.state_dir = self.root / "state"
        self.request_path = self.control / "research_bridge_request.json"
        self.status_path = self.state_dir / "research_bridge_status.json"
        self.state_path = self.state_dir / "research_bridge_state.json"
        self.log_path = self.root / "logs" / "research_request_bridge.log"

        self.child: subprocess.Popen | None = None
        self.child_log = None
        self.child_request_id: str | None = None
        self.child_started_at: str | None = None
        self.child_action: str | None = None
        self.child_job: str | None = None
        self.last_heartbeat = 0.0

        persisted = _load_runtime_persisted_state(self.state_path)
        self.prepared_upgrade_source_state = (
            dict(persisted)
            if persisted.get("bridge_version") == BRIDGE_STATE_UPGRADE_SOURCE_VERSION
            else None
        )
        if persisted:
            processed_ids = persisted["processed_request_ids"]
        else:
            processed_ids = []
        self.paused = persisted.get("paused", False)
        self.processed = deque(processed_ids[-100:], maxlen=100)
        self.last_request_id = persisted.get("last_request_id")
        self.last_action = persisted.get("last_action")
        self.last_result = persisted.get("last_result")
        self.last_exit_code = persisted.get("last_exit_code")
        self.started_at = now_iso()
        self.incarnation_id = str(uuid.uuid4())
        self.process_pid = os.getpid()
        self.health_status: str | None = None
        self.health_since = self.started_at

        local_base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InstagramResearch"
        self.python = local_base / "venv" / "Scripts" / "python.exe"
        if os.name == "nt" and not self.python.is_file():
            raise RuntimeError(f"Reviewed venv child runtime is unavailable: {self.python}")
        self.taskkill = None
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot")
            if not system_root:
                raise RuntimeError("SystemRoot is missing; refusing nondeterministic taskkill resolution.")
            self.taskkill = Path(system_root) / "System32" / "taskkill.exe"
        self.lifetime_job = _WindowsLifetimeJob()

    def log(self, msg: str):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")

    def persist_state(self):
        state = {
            "schema_version": 1,
            "bridge_version": BRIDGE_VERSION,
            "paused": self.paused,
            "processed_request_ids": list(self.processed),
            "last_request_id": self.last_request_id,
            "last_action": self.last_action,
            "last_result": self.last_result,
            "last_exit_code": self.last_exit_code,
            "updated_at": now_iso(),
        }
        state_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        transaction_id = _prepare_upgrade_transaction_state_write(self.state_path, state_bytes)
        atomic_bytes(self.state_path, state_bytes)
        if transaction_id is not None:
            _record_upgrade_transaction_state_write(self.state_path, state_bytes, transaction_id)

    def current_state(self) -> str:
        if self.child and self.child.poll() is None:
            return "RUNNING"
        if self.paused:
            return "PAUSED"
        return "IDLE"

    def health_snapshot(self) -> tuple[str, str]:
        checks = {
            "root_directory": self.root.is_dir(),
            "app_directory": self.app.is_dir(),
            "control_directory": self.control.is_dir(),
            "state_directory": self.state_dir.is_dir(),
            "python_runtime": self.python.is_file(),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            return "ERROR", "failed_checks=" + ",".join(failed)
        return "OK", "readiness_checks=OK"

    def write_status(self, *, request_id=None, action=None, result=None, detail=None):
        active = self.child and self.child.poll() is None
        heartbeat_at = now_iso()
        health_status, health_detail = self.health_snapshot()
        health_at = now_iso()
        if health_status != self.health_status:
            self.health_status = health_status
            self.health_since = health_at
        status = {
            "schema_version": 2,
            "bridge": BRIDGE_NAME,
            "bridge_version": BRIDGE_VERSION,
            "state": self.current_state(),
            "incarnation_id": self.incarnation_id,
            "process_pid": self.process_pid,
            "heartbeat_at": heartbeat_at,
            "heartbeat_incarnation_id": self.incarnation_id,
            "health_status": health_status,
            "health_at": health_at,
            "health_since": self.health_since,
            "health_incarnation_id": self.incarnation_id,
            "health_detail": health_detail,
            "started_at": self.started_at,
            "paused": self.paused,
            "active_job": self.child_job if active else None,
            "active_pid": self.child.pid if active else None,
            "active_request_id": self.child_request_id if active else None,
            "active_started_at": self.child_started_at if active else None,
            "active_action": self.child_action if active else None,
            "last_request_id": self.last_request_id,
            "last_action": self.last_action,
            "last_result": self.last_result,
            "last_exit_code": self.last_exit_code,
            "request_id": request_id,
            "action": action,
            "result": result,
            "detail": detail,
        }
        atomic_json(self.status_path, status)
        self.last_heartbeat = time.time()

    def mark_processed(self, request_id: str, action: str, result: str, exit_code=None, detail=None):
        if request_id not in self.processed:
            self.processed.append(request_id)
        self.last_request_id = request_id
        self.last_action = action
        self.last_result = result
        self.last_exit_code = exit_code
        self.persist_state()
        self.write_status(request_id=request_id, action=action, result=result, detail=detail)

    def validate_request(self, req: dict):
        if not isinstance(req, dict):
            return False, "REQUEST_NOT_OBJECT"
        if isinstance(req.get("schema_version"), bool) or not isinstance(req.get("schema_version"), int):
            return False, "BAD_SCHEMA_VERSION"
        if not isinstance(req.get("bridge"), str):
            return False, "BAD_BRIDGE"
        if not isinstance(req.get("state"), str):
            return False, "BAD_STATE"
        if not isinstance(req.get("request_id"), str):
            return False, "BAD_REQUEST_ID"
        if not isinstance(req.get("action"), str):
            return False, "ACTION_NOT_ALLOWED"
        for field in ("issued_at", "expires_at"):
            if not isinstance(req.get(field), str) or len(req[field]) > 128:
                return False, "BAD_TIMESTAMP"
        if "issued_by" in req and (not isinstance(req.get("issued_by"), str) or len(req["issued_by"]) > 256):
            return False, "BAD_ISSUED_BY"
        if "reason" in req and req.get("reason") is not None and (not isinstance(req.get("reason"), str) or len(req["reason"]) > 2000):
            return False, "BAD_REASON"
        action = req.get("action", "").upper()
        allowed_keys = set(COMMON_REQUEST_KEYS)
        if action == "REGISTER_CREATOR":
            allowed_keys.update(REGISTRATION_REQUEST_KEYS)
        elif action == "RUN_CREATOR_EVALUATION":
            allowed_keys.update({"creator_key", "sample_size", "source_platform"})
        elif action == "RUN_CREATOR_MONITOR":
            allowed_keys.update({"creator_key", "max_new"})
        elif action == "RUN_CREATOR_RECENT_CHECK":
            allowed_keys.update({"scope", "creator_keys", "window", "lookback_days", "max_items"})
        unknown = set(req) - allowed_keys
        if unknown:
            return False, f"UNKNOWN_FIELDS:{','.join(sorted(unknown))}"
        if req.get("schema_version") != 1:
            return False, "BAD_SCHEMA_VERSION"
        if req.get("bridge") != BRIDGE_NAME:
            return False, "BAD_BRIDGE"
        if req.get("state", "").upper() != "PENDING":
            return False, "NOT_PENDING"
        rid = req.get("request_id", "")
        if not REQUEST_ID_RE.fullmatch(rid):
            return False, "BAD_REQUEST_ID"
        if action not in ALLOWED_ACTIONS:
            return False, "ACTION_NOT_ALLOWED"
        try:
            issued = parse_iso(req.get("issued_at"))
            expires = parse_iso(req.get("expires_at"))
        except Exception:
            return False, "BAD_TIMESTAMP"
        if not issued or not expires:
            return False, "MISSING_TIMESTAMP"
        now = datetime.now(timezone.utc)
        if expires <= issued:
            return False, "BAD_EXPIRY_WINDOW"
        if (expires - issued).total_seconds() > MAX_REQUEST_TTL_SECONDS:
            return False, "EXPIRY_WINDOW_TOO_LONG"
        if expires < now:
            return False, "EXPIRED"
        if issued > now.replace(microsecond=0) and (issued - now).total_seconds() > 300:
            return False, "ISSUED_IN_FUTURE"

        if action == "REGISTER_CREATOR":
            try:
                normalize_registration_request(req)
            except Exception as exc:
                return False, f"REGISTRATION_REJECTED:{exc}"

        if action == "RUN_CREATOR_EVALUATION":
            if not isinstance(req.get("creator_key"), str):
                return False, "BAD_CREATOR_KEY"
            creator_key = req.get("creator_key", "").strip().lower()
            if not CREATOR_KEY_RE.fullmatch(creator_key):
                return False, "BAD_CREATOR_KEY"
            sample_size = req.get("sample_size", 20)
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 1 <= sample_size <= 100:
                return False, "BAD_SAMPLE_SIZE"
            source_platform = str(req.get("source_platform", "")).upper() if req.get("source_platform") else None
            if source_platform not in {None, "YOUTUBE", "TIKTOK"}:
                return False, "BAD_SOURCE_PLATFORM"
            ok_registry, registry_reason = self._validate_registered_creator(creator_key, source_platform=source_platform, require_monitoring=False)
            if not ok_registry:
                return False, registry_reason

        if action == "RUN_CREATOR_MONITOR":
            if req.get("creator_key") is not None and not isinstance(req.get("creator_key"), str):
                return False, "BAD_CREATOR_KEY"
            creator_key = req.get("creator_key", "").strip().lower() if req.get("creator_key") else ""
            if creator_key and not CREATOR_KEY_RE.fullmatch(creator_key):
                return False, "BAD_CREATOR_KEY"
            max_new = req.get("max_new", 10)
            if isinstance(max_new, bool) or not isinstance(max_new, int) or not 1 <= max_new <= 20:
                return False, "BAD_MAX_NEW"
            if creator_key:
                ok_registry, registry_reason = self._validate_registered_creator(creator_key, source_platform=None, require_monitoring=True)
                if not ok_registry:
                    return False, registry_reason

        if action == "RUN_CREATOR_RECENT_CHECK":
            scope = str(req.get("scope", "MONITORED")).upper()
            if scope not in {"MONITORED", "ALL_REGISTERED"}:
                return False, "BAD_SCOPE"
            window = str(req.get("window", "TODAY")).upper()
            if window not in {"TODAY", "LAST_7_DAYS", "LAST_N_DAYS"}:
                return False, "BAD_WINDOW"
            lookback_days = req.get("lookback_days")
            if window == "LAST_N_DAYS":
                if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or not 1 <= lookback_days <= 30:
                    return False, "BAD_LOOKBACK_DAYS"
            elif lookback_days is not None:
                return False, "LOOKBACK_DAYS_NOT_ALLOWED"
            max_items = req.get("max_items", 10)
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
                return False, "BAD_MAX_ITEMS"
            creator_keys = req.get("creator_keys")
            if creator_keys is not None:
                if not isinstance(creator_keys, list) or not 1 <= len(creator_keys) <= 20:
                    return False, "BAD_CREATOR_KEYS"
                normalized = []
                for raw in creator_keys:
                    if not isinstance(raw, str):
                        return False, "BAD_CREATOR_KEYS"
                    key = raw.strip().lower()
                    if not CREATOR_KEY_RE.fullmatch(key) or key in normalized:
                        return False, "BAD_CREATOR_KEYS"
                    ok_registry, registry_reason = self._validate_registered_creator(key, source_platform=None, require_monitoring=False)
                    if not ok_registry:
                        return False, registry_reason
                    normalized.append(key)

        return True, action

    def _validate_registered_creator(self, creator_key: str, *, source_platform: str | None, require_monitoring: bool):
        path = self.control / "creator_registry.json"
        try:
            obj = load_json(path, {}) or {}
        except Exception as exc:
            return False, f"CREATOR_REGISTRY_UNREADABLE:{type(exc).__name__}"
        if not isinstance(obj, dict) or obj.get("schema_version") != 1 or not isinstance(obj.get("creators"), dict):
            return False, "BAD_CREATOR_REGISTRY"
        profile = obj["creators"].get(creator_key)
        if not isinstance(profile, dict) or str(profile.get("status", "ACTIVE")).upper() != "ACTIVE":
            return False, "CREATOR_NOT_REGISTERED"
        verification = profile.get("verification") or {}
        if not isinstance(verification, dict) or str(verification.get("status", "")).upper() != "VERIFIED":
            return False, "CREATOR_NOT_VERIFIED"
        if "monitoring_enabled" in profile and not isinstance(profile.get("monitoring_enabled"), bool):
            return False, "BAD_CREATOR_REGISTRY"
        if require_monitoring and profile.get("monitoring_enabled") is not True:
            return False, "CREATOR_MONITORING_DISABLED"
        raw_sources = profile.get("sources", [])
        if not isinstance(raw_sources, list):
            return False, "BAD_CREATOR_REGISTRY"
        eligible = []
        for source in raw_sources:
            if not isinstance(source, dict):
                return False, "BAD_CREATOR_REGISTRY"
            for flag in ("enabled", "evaluation_enabled", "monitoring_enabled"):
                if flag in source and not isinstance(source.get(flag), bool):
                    return False, "BAD_CREATOR_REGISTRY"
            if source.get("enabled", True) is not True:
                continue
            platform_value = source.get("platform")
            if not isinstance(platform_value, str):
                return False, "BAD_CREATOR_REGISTRY"
            platform = platform_value.upper()
            if source_platform and platform != source_platform:
                continue
            if require_monitoring:
                if source.get("monitoring_enabled") is True and platform in {"YOUTUBE", "TIKTOK"}:
                    eligible.append(source)
            elif source.get("evaluation_enabled", platform in {"YOUTUBE", "TIKTOK"}) is True and platform in {"YOUTUBE", "TIKTOK"}:
                eligible.append(source)
        if not eligible:
            return False, "NO_REGISTERED_ELIGIBLE_SOURCE"
        return True, "OK"

    def _start_fixed_child(
        self,
        *,
        request_id: str,
        action: str,
        job: str,
        script_name: str,
        log_prefix: str,
        extra_args: list[str] | None = None,
    ):
        if self.paused:
            self.mark_processed(request_id, action, "REJECTED_PAUSED", detail="Bridge is paused.")
            return
        if self.child and self.child.poll() is None:
            self.mark_processed(
                request_id,
                action,
                "REJECTED_BUSY",
                detail=f"Active job={self.child_job} request_id={self.child_request_id}",
            )
            return

        script = self.app / script_name
        if not script.is_file():
            self.mark_processed(request_id, action, "FAILED", detail=f"Missing {script}")
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        child_log_path = self.root / "logs" / f"{log_prefix}_{request_id.replace(':','_')}.log"
        self.child_log = child_log_path.open("a", encoding="utf-8", buffering=1)
        cmd = [str(self.python), str(script), "--root", str(self.root)]
        if extra_args:
            cmd.extend(extra_args)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            # Interpreter/script are fixed local paths and every request-derived extra arg is schema-validated.
            # codeql[py/command-line-injection]
            self.child = subprocess.Popen(
                cmd,
                cwd=str(self.app),
                stdout=self.child_log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
            )
        except Exception as exc:
            self.child = None
            if self.child_log:
                self.child_log.close()
                self.child_log = None
            self.mark_processed(
                request_id,
                action,
                "FAILED",
                detail=f"Failed to start fixed job {job}: {type(exc).__name__}: {exc}",
            )
            return
        self.child_request_id = request_id
        self.child_started_at = now_iso()
        self.child_action = action
        self.child_job = job
        if request_id not in self.processed:
            self.processed.append(request_id)
        self.last_request_id = request_id
        self.last_action = action
        self.last_result = "STARTED"
        self.last_exit_code = None
        self.persist_state()
        self.write_status(
            request_id=request_id,
            action=action,
            result="STARTED",
            detail=f"Started {job} PID {self.child.pid}.",
        )
        self.log(f"{action} started request_id={request_id} pid={self.child.pid}")

    def start_creator_registration(self, request_id: str):
        self._start_fixed_child(
            request_id=request_id,
            action="REGISTER_CREATOR",
            job="CREATOR_REGISTRATION",
            script_name="creator_registration.py",
            log_prefix="creator_registration",
            extra_args=["--request-id", request_id],
        )

    def start_tiktok_sync(self, request_id: str):
        self._start_fixed_child(
            request_id=request_id,
            action="RUN_TIKTOK_SYNC",
            job="TIKTOK_SYNC",
            script_name="tiktok_camofox_sync.py",
            log_prefix="tiktok_sync",
        )

    def start_creator_evaluation(self, request_id: str, req: dict):
        extra = ["--creator-key", str(req["creator_key"]).strip().lower(), "--sample-size", str(int(req.get("sample_size", 20)))]
        if req.get("source_platform"):
            extra.extend(["--source-platform", str(req["source_platform"]).upper()])
        self._start_fixed_child(
            request_id=request_id, action="RUN_CREATOR_EVALUATION", job="CREATOR_EVALUATION",
            script_name="influencer_evaluation.py", log_prefix="creator_evaluation", extra_args=extra,
        )

    def start_creator_monitor(self, request_id: str, req: dict):
        extra = ["--max-new", str(int(req.get("max_new", 10)))]
        if req.get("creator_key"):
            extra.extend(["--creator-key", str(req["creator_key"]).strip().lower()])
        self._start_fixed_child(
            request_id=request_id, action="RUN_CREATOR_MONITOR", job="CREATOR_MONITOR",
            script_name="creator_monitor.py", log_prefix="creator_monitor", extra_args=extra,
        )

    def start_creator_recent_check(self, request_id: str, req: dict):
        extra = [
            "--scope", str(req.get("scope", "MONITORED")).upper(),
            "--window", str(req.get("window", "TODAY")).upper(),
            "--max-items", str(int(req.get("max_items", 10))),
        ]
        if req.get("lookback_days") is not None:
            extra.extend(["--lookback-days", str(int(req["lookback_days"]))])
        if req.get("creator_keys"):
            extra.extend(["--creator-keys", ",".join(str(x).strip().lower() for x in req["creator_keys"])])
        self._start_fixed_child(
            request_id=request_id, action="RUN_CREATOR_RECENT_CHECK", job="CREATOR_RECENT_CHECK",
            script_name="creator_recent_check.py", log_prefix="creator_recent_check", extra_args=extra,
        )

    def start_apply_research_decisions(self, request_id: str):
        self._start_fixed_child(
            request_id=request_id,
            action="APPLY_RESEARCH_DECISIONS",
            job="RESEARCH_APPLY",
            script_name="apply_research_decisions.py",
            log_prefix="research_apply",
        )

    def stop_current(self, request_id: str):
        if not self.child or self.child.poll() is not None:
            self.mark_processed(request_id, "STOP_CURRENT", "NO_ACTIVE_JOB")
            return
        pid = self.child.pid
        stopped_action = self.child_action
        stopped_job = self.child_job
        self.log(
            f"STOP_CURRENT request_id={request_id} target_pid={pid} "
            f"active_action={stopped_action}"
        )
        try:
            if os.name == "nt":
                if self.taskkill is None or not self.taskkill.is_file():
                    raise RuntimeError(f"Reviewed taskkill path is unavailable: {self.taskkill}")
                # taskkill is a reviewed fixed path and pid is the numeric PID of self.child.
                # codeql[py/command-line-injection]
                completed = subprocess.run(
                    [str(self.taskkill), "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "").strip()
                    raise RuntimeError(
                        f"taskkill failed for PID {pid}: exit={completed.returncode}; detail={detail[:500]}"
                    )
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"taskkill reported success but PID {pid} is still running after verification window."
                    ) from exc
            else:
                self.child.terminate()
                try:
                    self.child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
            exit_code = self.child.poll()
            if exit_code is None:
                raise RuntimeError(f"STOP_CURRENT could not prove PID {pid} exited.")
            if self.child_log:
                self.child_log.close()
                self.child_log = None
            self.child = None
            self.child_request_id = None
            self.child_started_at = None
            self.child_action = None
            self.child_job = None
            self.mark_processed(
                request_id,
                "STOP_CURRENT",
                "STOPPED",
                exit_code=exit_code,
                detail=(
                    f"Stopped bridge-started process tree rooted at PID {pid}; "
                    f"job={stopped_job} action={stopped_action}."
                ),
            )
        except Exception as exc:
            self.mark_processed(request_id, "STOP_CURRENT", "FAILED", detail=str(exc))

    def handle_request(self, req: dict):
        ok, action_or_reason = self.validate_request(req)
        rid = str(req.get("request_id", "")) if isinstance(req, dict) else ""
        if not ok:
            if action_or_reason == "NOT_PENDING":
                return
            if rid and REQUEST_ID_RE.fullmatch(rid) and rid not in self.processed:
                self.mark_processed(
                    rid,
                    str(req.get("action", "")).upper() or "UNKNOWN",
                    "REJECTED",
                    detail=action_or_reason,
                )
            return

        action = action_or_reason
        if rid in self.processed:
            return

        if action == "RUN_TIKTOK_SYNC":
            self.start_tiktok_sync(rid)
        elif action == "REGISTER_CREATOR":
            self.start_creator_registration(rid)
        elif action == "RUN_CREATOR_EVALUATION":
            self.start_creator_evaluation(rid, req)
        elif action == "RUN_CREATOR_MONITOR":
            self.start_creator_monitor(rid, req)
        elif action == "RUN_CREATOR_RECENT_CHECK":
            self.start_creator_recent_check(rid, req)
        elif action == "APPLY_RESEARCH_DECISIONS":
            self.start_apply_research_decisions(rid)
        elif action == "STOP_CURRENT":
            self.stop_current(rid)
        elif action == "PAUSE":
            self.paused = True
            self.mark_processed(rid, action, "PAUSED")
        elif action == "RESUME":
            self.paused = False
            self.mark_processed(rid, action, "RESUMED")
        elif action == "HEALTH":
            self.mark_processed(
                rid,
                action,
                "OK",
                detail=f"Bridge state={self.current_state()} version={BRIDGE_VERSION}",
            )

    def _apply_detail(self, rc: int) -> str:
        path = self.state_dir / "research_apply_status.json"
        try:
            s = load_json(path, {}) or {}
        except Exception as exc:
            return f"Exit code {rc}; apply status unreadable: {type(exc).__name__}: {exc}"
        if not s:
            return f"Exit code {rc}; research_apply_status.json missing/empty."
        fields = [
            f"apply_result={s.get('result')}",
            f"applied={s.get('applied')}",
            f"already_applied={s.get('already_applied')}",
            f"followups_opened={s.get('followups_opened')}",
            f"remaining_queue={s.get('remaining_queue')}",
        ]
        return f"Exit code {rc}; " + " ".join(fields)

    def _creator_registration_detail(self, rc: int) -> str:
        path = self.state_dir / "creator_registration_status.json"
        try:
            s = load_json(path, {}) or {}
        except Exception as exc:
            return f"Exit code {rc}; registration status unreadable: {type(exc).__name__}: {exc}"
        return (
            f"Exit code {rc}; registration_state={s.get('state')} result={s.get('result')} "
            f"creator_key={s.get('creator_key')} registry_changed={s.get('registry_changed')} "
            f"error={s.get('error')}"
        )

    def _creator_eval_detail(self, rc: int) -> str:
        path = self.state_dir / "creator_evaluation_status.json"
        try:
            s = load_json(path, {}) or {}
        except Exception as exc:
            return f"Exit code {rc}; creator status unreadable: {type(exc).__name__}: {exc}"
        return (
            f"Exit code {rc}; evaluation_state={s.get('state')} "
            f"evaluation_run_id={s.get('evaluation_run_id')} "
            f"completed={s.get('completed_count', s.get('evaluation_items_count'))} "
            f"failures={s.get('failure_count', len(s.get('failures', []) if isinstance(s.get('failures'), list) else []))}"
        )

    def _creator_monitor_detail(self, rc: int) -> str:
        path = self.state_dir / "creator_monitor_status.json"
        try:
            s = load_json(path, {}) or {}
        except Exception as exc:
            return f"Exit code {rc}; monitor status unreadable: {type(exc).__name__}: {exc}"
        return (
            f"Exit code {rc}; monitor_state={s.get('state')} result_count={s.get('result_count')} "
            f"error_count={s.get('error_count')}"
        )

    def _creator_recent_check_detail(self, rc: int) -> str:
        path = self.state_dir / "creator_recent_check_status.json"
        try:
            s = load_json(path, {}) or {}
        except Exception as exc:
            return f"Exit code {rc}; recent-check status unreadable: {type(exc).__name__}: {exc}"
        return (
            f"Exit code {rc}; recent_state={s.get('state')} recent_found={s.get('recent_found_count')} "
            f"selected={s.get('selected_for_ingestion_count')} queued_for_analysis={s.get('queued_for_analysis_count')} "
            f"deferred={s.get('deferred_due_to_cap_count')} error_count={s.get('error_count')}"
        )

    def poll_child(self):
        if not self.child:
            return
        rc = self.child.poll()
        if rc is None:
            return
        rid = self.child_request_id
        action = self.child_action or "UNKNOWN"
        job = self.child_job
        self.last_request_id = rid
        self.last_action = action
        self.last_exit_code = rc
        self.last_result = "DONE" if rc == 0 else "FAILED"
        if action == "APPLY_RESEARCH_DECISIONS":
            detail = self._apply_detail(rc)
        elif action == "REGISTER_CREATOR":
            detail = self._creator_registration_detail(rc)
        elif action == "RUN_CREATOR_EVALUATION":
            detail = self._creator_eval_detail(rc)
        elif action == "RUN_CREATOR_MONITOR":
            detail = self._creator_monitor_detail(rc)
        elif action == "RUN_CREATOR_RECENT_CHECK":
            detail = self._creator_recent_check_detail(rc)
        else:
            detail = f"Exit code {rc}"
        self.log(f"{action} finished request_id={rid} exit_code={rc}")
        if self.child_log:
            self.child_log.close()
            self.child_log = None
        self.child = None
        self.child_request_id = None
        self.child_started_at = None
        self.child_action = None
        self.child_job = None
        self.persist_state()
        self.write_status(
            request_id=rid,
            action=action,
            result=self.last_result,
            detail=f"job={job}; {detail}",
        )

    def run(self):
        self.log(f"bridge startup version={BRIDGE_VERSION} root={self.root}")
        self.persist_state()
        if self.prepared_upgrade_source_state is not None:
            migrated = load_json(self.state_path, None)
            _validate_persisted_bridge_state(migrated, allowed_versions={BRIDGE_VERSION})
            _assert_preserved_state_fields(self.prepared_upgrade_source_state, migrated)
            self.log(
                "bridge state migration 0.5.0->0.6.0 PASS "
                "preserved=paused,processed_request_ids,last_request_id,last_action,last_result,last_exit_code "
                "reset=bridge_version,updated_at"
            )
        self.write_status(result="BRIDGE_STARTED")

        while True:
            try:
                self.poll_child()
                req = load_json(self.request_path, None)
                if req is not None:
                    self.handle_request(req)
                if time.time() - self.last_heartbeat >= HEARTBEAT_SECONDS:
                    self.write_status()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self.log(f"loop_error {type(exc).__name__}: {exc}")
                try:
                    self.write_status(
                        result="BRIDGE_ERROR",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
            time.sleep(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ops = ap.add_mutually_exclusive_group()
    ops.add_argument("--prepare-state-upgrade", action="store_true")
    ops.add_argument("--restore-state-upgrade-backup", action="store_true")
    ops.add_argument("--commit-state-upgrade", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    if args.prepare_state_upgrade:
        print(json.dumps(prepare_persisted_state_upgrade(root), sort_keys=True))
        return
    if args.restore_state_upgrade_backup:
        print(json.dumps(restore_persisted_state_upgrade_backup(root), sort_keys=True))
        return
    if args.commit_state_upgrade:
        print(json.dumps(commit_persisted_state_upgrade(root), sort_keys=True))
        return
    _lock = acquire_single_instance_lock()
    Bridge(root).run()


if __name__ == "__main__":
    main()
