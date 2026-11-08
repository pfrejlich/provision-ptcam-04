import threading
import unittest

from camera_session import PlaybackSwitcher


class FakePlayback:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.allow_close = threading.Event()
        self.allow_close.set()

    def start(self):
        self.started.set()

    def cancel(self):
        self.cancelled.set()

    def close(self):
        self.cancel()
        if not self.allow_close.wait(2):
            raise RuntimeError('Test close gate timed out')
        self.closed.set()


class CameraSwitchingTests(unittest.TestCase):
    def test_rapid_selection_starts_only_latest_after_old_stream_closes(self):
        switcher = PlaybackSwitcher()
        first, middle, last = FakePlayback(), FakePlayback(), FakePlayback()
        switcher.select(first)
        self.assertTrue(first.started.wait(1))
        first.allow_close.clear()
        try:
            switcher.select(middle)
            self.assertTrue(first.cancelled.wait(1))
            switcher.select(last)
            self.assertFalse(last.started.is_set())
            first.allow_close.set()
            self.assertTrue(last.started.wait(1))
            self.assertTrue(first.closed.is_set())
            self.assertFalse(middle.started.is_set())
        finally:
            first.allow_close.set()
            switcher.close()
        self.assertTrue(last.closed.is_set())

    def test_closing_prevents_a_pending_camera_from_starting(self):
        switcher = PlaybackSwitcher()
        first, second = FakePlayback(), FakePlayback()
        switcher.select(first)
        self.assertTrue(first.started.wait(1))
        first.allow_close.clear()
        switcher.select(second)
        self.assertTrue(first.cancelled.wait(1))
        closer = threading.Thread(target=switcher.close)
        closer.start()
        try:
            self.assertTrue(second.cancelled.wait(1))
        finally:
            first.allow_close.set()
            closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(second.started.is_set())
