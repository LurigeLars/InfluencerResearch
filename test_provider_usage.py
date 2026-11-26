import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_usage as usage
import transcription_backend as tb


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(usage, "LOG_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def events(self):
        files = list(self.root.glob("provider-usage-*.jsonl"))
        return [json.loads(line) for file in files for line in file.read_text().splitlines()]

    def test_duration_and_content_free_schema(self):
        @usage.measure("faster-whisper", "transcription")
        def operation(path):
            return {"text": "PRIVATE TRANSCRIPT", "model": "PRIVATE MODEL", "duration": 120.0}
        result = operation(Path("PRIVATE VIDEO"))
        self.assertEqual(result["text"], "PRIVATE TRANSCRIPT")
        records = self.events()
        self.assertEqual([r["outcome"] for r in records], ["attempt", "completed"])
        self.assertEqual(records[1]["media_seconds"], 120.0)
        self.assertEqual(set(records[1]), {"provider","operation","outcome","fallback","elapsed_seconds","media_seconds"})
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_fallback_and_failures(self):
        @usage.measure("gemini", "transcription")
        def rejected():
            raise RuntimeError("Gemini error with PRIVATE data")
        @usage.measure("ollama", "story_image")
        def empty():
            return {"text": ""}
        @usage.measure("tesseract", "video_frame_ocr")
        def timed_out():
            raise subprocess.TimeoutExpired("PRIVATE command", 1)
        with self.assertRaises(RuntimeError):
            rejected()
        with usage.fallback_usage():
            self.assertEqual(empty(), {"text": ""})
        with self.assertRaises(subprocess.TimeoutExpired):
            timed_out()
        records = self.events()
        self.assertEqual([r["outcome"] for r in records], ["attempt","failed","attempt","empty","attempt","timeout"])
        self.assertEqual([r["fallback"] for r in records], [False,False,True,True,False,False])
        self.assertNotIn("PRIVATE", json.dumps(records))

    def test_rate_limit_and_unknown_duration(self):
        class QuotaError(Exception):
            code = 429
        @usage.measure("gemini", "transcription")
        def quota():
            raise QuotaError("PRIVATE")
        @usage.measure("gemini", "transcription")
        def ok(video_path):
            return {"text":"private", "duration":None}
        with self.assertRaises(QuotaError):
            quota()
        with patch.object(usage, "_duration", return_value=None):
            ok(Path("missing"))
        self.assertEqual(self.events()[1]["outcome"], "rate_limited")
        self.assertIsNone(self.events()[3]["media_seconds"])

    def test_log_failure_and_cap_do_not_change_operation(self):
        @usage.measure("ollama", "story_image")
        def operation():
            return {"text": "ok"}
        with patch.object(usage, "LOG_DIR", self.root / "missing"):
            self.assertEqual(operation(), {"text":"ok"})
        with patch.object(usage, "DAILY_LIMIT", 1):
            self.assertEqual(operation(), {"text":"ok"})
        self.assertEqual(self.events(), [])

    def test_real_transcription_route_marks_local_fallback(self):
        # Replace expensive backend functions with instrumented synthetic work;
        # exercise the real auto routing rather than duplicating it in the test.
        @usage.measure("gemini", "transcription")
        def remote(*args, **kwargs):
            raise TimeoutError("PRIVATE")
        @usage.measure("faster-whisper", "transcription")
        def local(*args, **kwargs):
            return {"text":"ok","duration":90}
        with patch.object(tb, "read_gemini_api_key", return_value="PRIVATE"), \
             patch.object(tb, "transcribe_gemini_bounded", remote), \
             patch.object(tb, "transcribe_faster_whisper", local):
            self.assertEqual(tb.transcribe_video(Path("fixture.mp4"))["fallback_from"], "gemini")
        records = self.events()
        self.assertEqual([(r["provider"],r["outcome"],r["fallback"]) for r in records],
            [("gemini","attempt",False),("gemini","timeout",False),
             ("faster-whisper","attempt",True),("faster-whisper","completed",True)])
