"""Desktop behavior checks; opt in with PROVISION_GUI_TESTS=1 on Windows."""

import io
import os
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from camera_config import CameraConfig
from desktop_viewer import Viewer, fit_size


class FakePlayback:
    def __init__(self, viewer, profile):
        self.profile = profile
        self.access = SimpleNamespace(ticket_path=profile.ticket_path(viewer.camera.STATE_ROOT))
        self.session = SimpleNamespace(frames=queue.Queue(), stop=threading.Event(),
                                       restart=threading.Event(), reconnect=Mock())
        self.motion = SimpleNamespace(busy=False, request=Mock(), cancel=threading.Event())
        self.notify = lambda kind, value: viewer.events.put((self, kind, value))
        self.started = threading.Event()
        self.closed = threading.Event()

    def start(self):
        self.started.set()

    def cancel(self):
        self.session.stop.set()

    def close(self):
        self.cancel()
        self.closed.set()


class FrameSizingTests(unittest.TestCase):
    def test_maximum_fit_preserves_aspect_ratio_and_handles_minimization(self):
        self.assertEqual(fit_size((2304, 1296), (1600, 1000)), (1600, 900))
        self.assertEqual(fit_size((2304, 1296), (1600, 600)), (1066, 600))
        self.assertEqual(fit_size((2304, 1296), (3840, 2160)), (3840, 2160))
        self.assertEqual(fit_size((2304, 1296), (0, 0)), (1, 1))


@unittest.skipUnless(os.environ.get('PROVISION_GUI_TESTS'), 'Enable desktop GUI tests')
class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch('desktop_viewer.Playback', FakePlayback),
                        patch('desktop_viewer.shutil.which', return_value='test-ffmpeg'),
                        patch('camera_client.cloud_client', side_effect=AssertionError('Unexpected cloud request'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        args = SimpleNamespace(refresh=False, resolution='2304x1296', size='large', cameras=(
            CameraConfig('Camera A', 'camera-a', '192.0.2.10'),
            CameraConfig('Camera B', 'camera-b', '192.0.2.11')))
        self.viewer = Viewer(args)
        self.viewer.root.attributes('-alpha', 0)
        self.viewer.root.update()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.viewer.close()
        self.viewer.switcher.close()

    def until(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.viewer.root.update()
            time.sleep(0.01)
        self.assertTrue(condition())

    def select(self, index):
        self.viewer.selector.current(index)
        self.viewer.selector.event_generate('<<ComboboxSelected>>')
        self.until(lambda: self.viewer.playback is not None and self.viewer.playback.started.is_set())
        return self.viewer.playback

    def frame(self, playback, color='blue'):
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (2304, 1296), color).save(image, format='PNG')
        playback.session.frames.put(image.getvalue())
        before = self.viewer.displayed_frames
        self.until(lambda: self.viewer.displayed_frames > before)

    def station(self):
        self.viewer.selector.current(len(self.viewer.cameras))
        self.viewer.selector.event_generate('<<ComboboxSelected>>')
        self.until(lambda: len(self.viewer.tiles) == 2 and all(playback.started.is_set() for playback in self.viewer.tiles))
        return list(self.viewer.tiles)

    def test_security_station_displays_both_and_controls_only_focused_tile(self):
        first, second = self.station()
        self.assertIsNone(self.viewer.playback)
        self.assertFalse(self.viewer.connected)
        self.frame(first, 'red')
        self.frame(second, 'blue')
        self.assertTrue(all(tile.connected for tile in self.viewer.tiles.values()))
        self.assertFalse(any(tile.loading_active for tile in self.viewer.tiles.values()))
        self.assertIn('2/2', self.viewer.status.get())
        self.viewer.tiles[second].canvas.event_generate('<Button-1>', x=10, y=10)
        self.viewer.direction_buttons['left'].invoke()
        second.motion.request.assert_called_once_with('left')
        first.motion.request.assert_not_called()

    def test_station_reconnect_and_cancellation_leave_other_stream_running(self):
        first, second = self.station()
        self.frame(first)
        self.frame(second)
        first.notify('offline', None)
        self.until(lambda: not self.viewer.tiles[first].connected)
        self.assertTrue(self.viewer.tiles[first].loading_active)
        self.frame(second)
        self.assertTrue(self.viewer.tiles[second].connected)
        first.notify('cancelled', 'Sign-in cancelled')
        self.until(first.session.stop.is_set)
        self.assertFalse(second.session.stop.is_set())
        self.frame(second)

    def test_leaving_station_stops_every_previous_stream(self):
        first, second = self.station()
        selected = self.select(0)
        self.assertTrue(first.closed.is_set())
        self.assertTrue(second.closed.is_set())
        self.assertEqual(len(self.viewer.tiles), 1)
        self.assertIs(self.viewer.playback, selected)

    def test_station_opens_only_one_login_window_for_two_workers(self):
        first, second = self.station()
        observed = self.viewer.authentication.snapshot()
        outcomes = []
        def request(playback):
            outcomes.append(self.viewer.authentication.request(playback.notify, playback.session.stop, observed))
        with patch('desktop_login.login_window', return_value=True) as prompt, \
             patch('camera_client.cloud_client', return_value=Mock()):
            workers = [threading.Thread(target=request, args=(playback,)) for playback in (first, second)]
            for worker in workers:
                worker.start()
            self.until(lambda: len(outcomes) == 2)
            for worker in workers:
                worker.join(timeout=1)
            prompt.assert_called_once()
        self.assertEqual(outcomes, [True, True])

    def test_healthy_tile_keeps_rendering_during_modal_sign_in(self):
        first, second = self.station()
        self.frame(second)
        before = self.viewer.tiles[second].frames
        observed = []
        def prompt(*args, **kwargs):
            from PIL import Image
            import tkinter as tk
            encoded = io.BytesIO()
            Image.new('RGB', (640, 360), 'blue').save(encoded, format='PNG')
            second.session.frames.put(encoded.getvalue())
            done = tk.BooleanVar(master=self.viewer.root, value=False)
            self.viewer.root.after(200, lambda: done.set(True))
            self.viewer.root.wait_variable(done)
            observed.append(self.viewer.tiles[second].frames > before)
            return False
        request = {'event': threading.Event(), 'success': False}
        with patch('desktop_login.login_window', side_effect=prompt), \
             patch('camera_client.cloud_client', return_value=Mock()):
            first.notify('login', request)
            self.until(request['event'].is_set)
        self.assertEqual(observed, [True])

    def test_idle_selection_loading_animation_and_first_frame(self):
        self.assertIsNone(self.viewer.session)
        self.assertFalse(self.viewer.connected)
        self.assertEqual(self.viewer.selection.get(), '')
        playback = self.select(0)
        self.assertTrue(self.viewer.loading_active)
        before = self.viewer.loading_bar['value']
        self.until(lambda: self.viewer.loading_bar['value'] != before)
        self.frame(playback)
        self.assertTrue(self.viewer.connected)
        self.assertFalse(self.viewer.loading_active)
        self.assertEqual(len(self.viewer.buttons), 5)

    def test_resize_uses_available_space_without_restarting_stream(self):
        playback = self.select(0)
        self.frame(playback)
        self.viewer.root.geometry('850x650')
        self.viewer.root.update()
        self.viewer.render()
        small = self.viewer.rendered_size
        self.viewer.root.geometry('1400x900')
        self.viewer.root.update()
        self.viewer.render()
        large = self.viewer.rendered_size
        self.assertGreater(large[0], small[0])
        self.assertGreater(large[1], small[1])
        self.assertEqual(large, fit_size((2304, 1296),
                         (self.viewer.video.winfo_width(), self.viewer.video.winfo_height())))
        self.assertIs(self.viewer.playback, playback)
        self.assertFalse(playback.closed.is_set())

    def test_switch_closes_old_camera_and_ignores_old_frames_and_login(self):
        first = self.select(0)
        self.frame(first, 'red')
        second = self.select(1)
        self.assertTrue(first.closed.is_set())
        stale = {'event': threading.Event(), 'success': False}
        first.notify('login', stale)
        first.notify('status', 'obsolete status')
        self.frame(second, 'blue')
        self.assertTrue(stale['event'].is_set())
        self.assertEqual(self.viewer.source_image.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(self.viewer.selection.get(), 'Camera B')
        self.assertIn('Camera B', self.viewer.status.get())

    def test_login_appears_automatically_and_cancellation_returns_to_selection(self):
        playback = self.select(0)
        request = {'event': threading.Event(), 'success': False}
        with patch('desktop_login.login_window', return_value=False) as prompt, \
             patch('camera_client.cloud_client', return_value=Mock()):
            playback.notify('login', request)
            self.until(request['event'].is_set)
            prompt.assert_called_once()
            self.assertFalse(request['success'])
            playback.notify('cancelled', 'Sign-in cancelled. Select a camera to try again.')
            self.until(lambda: self.viewer.playback is None)
        self.assertEqual(self.viewer.selection.get(), '')
        self.assertFalse(self.viewer.loading_active)
        self.assertTrue(playback.session.stop.is_set())
        self.select(0)
