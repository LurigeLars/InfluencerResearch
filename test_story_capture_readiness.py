from __future__ import annotations

import unittest

import story_capture_readiness as scr


class FakeItem:
    def __init__(self, states):
        self.states = states
        self.index = 0

    def is_visible(self):
        return True

    def bounding_box(self):
        return {"width": 400, "height": 700}

    def evaluate(self, script):
        state = self.states[min(self.index, len(self.states) - 1)]
        return dict(state)


class FakeLocator:
    def __init__(self, item=None):
        self.item = item

    def count(self):
        return 1 if self.item is not None else 0

    def nth(self, index):
        return self.item


class FakePage:
    def __init__(self, video_states):
        self.url = "https://www.instagram.com/stories/example/123/"
        self.video = FakeItem(video_states)
        self.waits = []

    def locator(self, selector):
        if selector == "video":
            return FakeLocator(self.video)
        return FakeLocator()

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.video.index += 1


class StoryCaptureReadinessTests(unittest.TestCase):
    def test_waits_until_video_has_decoded_dimensions(self):
        page = FakePage([
            {"url": "https://cdn.example/x.mp4", "readyState": 1, "width": 0, "height": 0},
            {"url": "https://cdn.example/x.mp4", "readyState": 2, "width": 1080, "height": 1920},
        ])
        result = scr.wait_for_story_media_ready(page, max_wait_ms=500, poll_ms=25)
        self.assertTrue(result["ready"])
        self.assertFalse(result["timed_out"])
        self.assertGreaterEqual(result["attempts"], 2)
        self.assertEqual(result["media"]["kind"], "video")

    def test_times_out_instead_of_accepting_loading_video(self):
        page = FakePage([
            {"url": "https://cdn.example/x.mp4", "readyState": 1, "width": 0, "height": 0},
        ])
        result = scr.wait_for_story_media_ready(page, max_wait_ms=1, poll_ms=25)
        self.assertFalse(result["ready"])
        self.assertTrue(result["timed_out"])


if __name__ == "__main__":
    unittest.main()
