from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
QUEUE_SRC = BASE / "research_queue_b038_candidate.py"
YOUTUBE_SRC = BASE / "youtube_creator_evaluation_b038_candidate.py"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def make_item(root: Path, shortcode: str, *, creator: str, published_at, run_id=None, creator_eval=False) -> dict:
    transcript_rel = Path("output") / creator / "transcripts" / f"{shortcode}.txt"
    transcript = " ".join([f"{shortcode}_word_{i}" for i in range(60)])
    transcript_path = root / transcript_rel
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(transcript, encoding="utf-8")
    return {
        "schema_version": 1,
        "source_platform": "YOUTUBE",
        "source_type": "VIDEO",
        "source_id": shortcode.removeprefix("yt_"),
        "creator": creator,
        "url": f"https://www.youtube.com/watch?v={shortcode.removeprefix('yt_')}",
        "caption": shortcode,
        "published_at": published_at,
        "downloaded_at": "2026-08-29T10:00:00+00:00",
        "download_status": "DONE",
        "transcribed_at": "2026-08-29T10:01:00+00:00",
        "transcription_status": "DONE",
        "transcript_txt": str(transcript_rel),
        "research_status": "PENDING",
        "evaluation_mode": "CREATOR_EVALUATION" if creator_eval else None,
        "evaluation_run_id": run_id,
        "evaluation_source_profile": "https://www.youtube.com/@BKForex" if creator_eval else None,
        "evaluation_sample_size": 2 if creator_eval else None,
        "permanent_source": False if creator_eval else True,
        "creator_verification": {"status": "WEB_VERIFIED"} if creator_eval else None,
    }


def test_starvation_regression() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app").mkdir(parents=True)
        shutil.copy2(QUEUE_SRC, root / "app" / "research_queue.py")
        items = {}
        for i in range(105):
            shortcode = f"yt_old{i:03d}"
            items[shortcode] = make_item(
                root,
                shortcode,
                creator="allowed",
                published_at=f"2026-08-{28 - (i % 20):02d}T12:{i % 60:02d}:00+00:00",
            )
        targets = ["yt_7ZH2isWsdjc", "yt_12DtB9Rxr-g"]
        for shortcode in targets:
            items[shortcode] = make_item(
                root,
                shortcode,
                creator="kathylien",
                published_at=None,
                run_id="B038-TEST",
                creator_eval=True,
            )
        write_json(root / "state" / "manifest.json", {"schema_version": 1, "items": items})
        write_json(root / "state" / "research_decisions.json", {"schema_version": 2, "items": {}})
        write_json(root / "control" / "research_screening.json", {"max_queue_items": 100, "creators": ["allowed"]})

        cmd = [sys.executable, str(root / "app" / "research_queue.py"), "--root", str(root)]
        for target in targets:
            cmd += ["--must-include-shortcode", target]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        queue = json.loads((root / "state" / "research_queue.json").read_text(encoding="utf-8"))
        shortcodes = [x["shortcode"] for x in queue["items"]]
        assert len(shortcodes) == 100
        assert all(target in shortcodes for target in targets), shortcodes[-10:]
        assert queue["must_include_requested"] == targets
        assert queue["must_include_eligible"] == targets
        assert queue["must_include_queued"] == targets
        assert queue["must_include_missing"] == []
        # Preserve ordinary published_at-desc display ordering after admission reservation.
        keys = [(str(x.get("published_at") or ""), str(x.get("shortcode") or "")) for x in queue["items"]]
        assert keys == sorted(keys, reverse=True)
        print("PASS starvation regression: 2 null-date evaluation items admitted within 100-item cap")


def test_fail_closed_invalid_and_overcap() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app").mkdir(parents=True)
        shutil.copy2(QUEUE_SRC, root / "app" / "research_queue.py")
        item_a = make_item(root, "yt_targetA", creator="kathy", published_at=None, run_id="R", creator_eval=True)
        item_b = make_item(root, "yt_targetB", creator="kathy", published_at=None, run_id="R", creator_eval=True)
        manifest_path = root / "state" / "manifest.json"
        queue_path = root / "state" / "research_queue.json"
        write_json(manifest_path, {"schema_version": 1, "items": {"yt_targetA": item_a, "yt_targetB": item_b}})
        write_json(root / "state" / "research_decisions.json", {"schema_version": 2, "items": {}})
        write_json(root / "control" / "research_screening.json", {"max_queue_items": 1})
        write_json(queue_path, {"sentinel": "unchanged"})
        before_manifest = manifest_path.read_bytes()
        before_queue = queue_path.read_bytes()

        bad = subprocess.run(
            [sys.executable, str(root / "app" / "research_queue.py"), "--root", str(root), "--must-include-shortcode", "../escape"],
            capture_output=True, text=True, timeout=60,
        )
        assert bad.returncode != 0
        assert manifest_path.read_bytes() == before_manifest
        assert queue_path.read_bytes() == before_queue

        over = subprocess.run(
            [sys.executable, str(root / "app" / "research_queue.py"), "--root", str(root),
             "--must-include-shortcode", "yt_targetA", "--must-include-shortcode", "yt_targetB"],
            capture_output=True, text=True, timeout=60,
        )
        assert over.returncode != 0
        assert "MUST_INCLUDE_EXCEEDS_QUEUE_CAP" in over.stderr
        assert manifest_path.read_bytes() == before_manifest
        assert queue_path.read_bytes() == before_queue
        print("PASS fail-closed invalid/over-cap admission with zero source-of-truth writes")



def test_generic_queue_semantics_unchanged() -> None:
    with tempfile.TemporaryDirectory() as td:
        base_root = Path(td)
        def setup(root: Path) -> None:
            (root / "app").mkdir(parents=True)
            items = {}
            for i in range(105):
                shortcode = f"yt_generic{i:03d}"
                items[shortcode] = make_item(
                    root, shortcode, creator="allowed",
                    published_at=f"2026-08-{28 - (i % 20):02d}T11:{i % 60:02d}:00+00:00",
                )
            for shortcode in ["yt_nullA", "yt_nullB"]:
                items[shortcode] = make_item(
                    root, shortcode, creator="kathy", published_at=None,
                    run_id="R", creator_eval=True,
                )
            write_json(root / "state" / "manifest.json", {"schema_version": 1, "items": items})
            write_json(root / "state" / "research_decisions.json", {"schema_version": 2, "items": {}})
            write_json(root / "control" / "research_screening.json", {"max_queue_items": 100, "creators": ["allowed"]})
        old_root = base_root / "old"
        new_root = base_root / "new"
        setup(old_root); setup(new_root)
        shutil.copy2(BASE / "research_queue.py", old_root / "app" / "research_queue.py")
        shutil.copy2(QUEUE_SRC, new_root / "app" / "research_queue.py")
        old = subprocess.run([sys.executable, str(old_root / "app" / "research_queue.py"), "--root", str(old_root)], capture_output=True, text=True, timeout=60)
        new = subprocess.run([sys.executable, str(new_root / "app" / "research_queue.py"), "--root", str(new_root)], capture_output=True, text=True, timeout=60)
        assert old.returncode == 0 and new.returncode == 0, (old.stderr, new.stderr)
        oq = json.loads((old_root / "state" / "research_queue.json").read_text(encoding="utf-8"))
        nq = json.loads((new_root / "state" / "research_queue.json").read_text(encoding="utf-8"))
        assert [x["shortcode"] for x in oq["items"]] == [x["shortcode"] for x in nq["items"]]
        assert oq["count"] == nq["count"] == 100
        print("PASS generic queue selection/order unchanged when must-include is unused")

def test_delivery_dispositions() -> None:
    mod = load_module("b038_youtube", YOUTUBE_SRC)
    completed = ["yt_queued", "yt_dup", "yt_final", "yt_missing"]
    manifest = {"items": {
        "yt_queued": {},
        "yt_dup": {"duplicate_of": "yt_canonical"},
        "yt_final": {},
        "yt_missing": {},
    }}
    decisions = {"items": {"yt_final": {"decision": "RESEARCH"}}}
    queue = {"items": [{"shortcode": "yt_queued"}]}
    result = mod.delivery_dispositions(completed, manifest=manifest, decisions=decisions, queue=queue)
    assert result["dispositions"] == {
        "yt_queued": "QUEUED",
        "yt_dup": "DUPLICATE",
        "yt_final": "FINALIZED",
        "yt_missing": "UNDELIVERED",
    }
    assert result["expected_queue"] == ["yt_queued", "yt_missing"]
    assert result["undelivered"] == ["yt_missing"]
    print("PASS delivery dispositions distinguish queued/duplicate/finalized/undelivered")



def test_delivery_reconciliation_self_heals_and_then_noops() -> None:
    mod = load_module("b038_youtube_reconcile", YOUTUBE_SRC)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app").mkdir(parents=True)
        shutil.copy2(QUEUE_SRC, root / "app" / "research_queue.py")
        items = {
            "yt_recent": make_item(root, "yt_recent", creator="allowed", published_at="2026-08-29T12:00:00+00:00"),
            "yt_targetA": make_item(root, "yt_targetA", creator="kathy", published_at=None, run_id="R", creator_eval=True),
            "yt_targetB": make_item(root, "yt_targetB", creator="kathy", published_at=None, run_id="R", creator_eval=True),
        }
        write_json(root / "state" / "manifest.json", {"schema_version": 1, "items": items})
        write_json(root / "state" / "research_decisions.json", {"schema_version": 2, "items": {}})
        write_json(root / "control" / "research_screening.json", {"max_queue_items": 2, "creators": ["allowed"]})
        write_json(root / "state" / "research_queue.json", {
            "schema_version": 2,
            "items": [{"shortcode": "yt_recent"}],
        })
        targets = ["yt_targetA", "yt_targetB"]
        result1, queue1, delivery1 = mod.reconcile_delivery(root, targets)
        assert result1["ok"] and not delivery1["undelivered"]
        assert set(x["shortcode"] for x in queue1["items"]) == set(targets)
        before = (root / "state" / "research_queue.json").read_bytes()
        result2, queue2, delivery2 = mod.reconcile_delivery(root, targets)
        after = (root / "state" / "research_queue.json").read_bytes()
        assert result2.get("skipped") == "DELIVERY_ALREADY_REACHABLE"
        assert not delivery2["undelivered"]
        assert before == after, "second reconciliation mutated an already-reachable queue"
        print("PASS delivery reconciliation self-heals once and second run is a queue no-op")

def test_ffmpeg_timeout_reaps_ytdlp() -> None:
    mod = load_module("b038_youtube_cleanup", YOUTUBE_SRC)

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.wait_calls = []
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
            self.returncode = -9
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if self.terminated and self.returncode is None:
                self.returncode = 0
                return 0
            if self.returncode is not None:
                return self.returncode
            raise subprocess.TimeoutExpired("yt-dlp", timeout)

    fake = FakeProcess()
    old_popen = mod.subprocess.Popen
    old_run = mod.subprocess.run
    old_ffmpeg = mod._ffmpeg_exe
    old_yt_base = mod._yt_base_args
    try:
        mod._ffmpeg_exe = lambda: "ffmpeg"
        mod._yt_base_args = lambda: (["yt-dlp", "--ignore-config"], {})
        mod.subprocess.Popen = lambda *a, **k: fake
        def timeout_run(*a, **k):
            raise subprocess.TimeoutExpired("ffmpeg", 600)
        mod.subprocess.run = timeout_run
        with tempfile.TemporaryDirectory() as td:
            try:
                mod.capture_visual_evidence(Path(td), "kathy", "https://youtube.test/watch?v=x", "x")
                raise AssertionError("expected ffmpeg TimeoutExpired")
            except subprocess.TimeoutExpired:
                pass
        assert fake.terminated or fake.killed
        assert fake.wait_calls, "yt-dlp child was not waited/reaped"
        assert fake.returncode is not None
        print("PASS ffmpeg timeout deterministically terminates/waits yt-dlp child")
    finally:
        mod.subprocess.Popen = old_popen
        mod.subprocess.run = old_run
        mod._ffmpeg_exe = old_ffmpeg
        mod._yt_base_args = old_yt_base


def main() -> int:
    test_starvation_regression()
    test_fail_closed_invalid_and_overcap()
    test_generic_queue_semantics_unchanged()
    test_delivery_dispositions()
    test_delivery_reconciliation_self_heals_and_then_noops()
    test_ffmpeg_timeout_reaps_ytdlp()
    print("ALL BACKLOG_038 REMEDIATION TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
