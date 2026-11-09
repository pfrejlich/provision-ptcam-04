import queue
import threading
import unittest

from camera_session import LoginCoordinator, PlaybackGroup
from camera_tile import grid_shape


class StationLayoutTests(unittest.TestCase):
    def test_grid_uses_landscape_portrait_and_four_camera_space(self):
        self.assertEqual(grid_shape(2, (1200, 600)), (1, 2))
        self.assertEqual(grid_shape(2, (600, 1200)), (2, 1))
        self.assertEqual(grid_shape(4, (1600, 900)), (2, 2))
        for count in range(1, 10):
            rows, columns = grid_shape(count, (1200, 800))
            self.assertGreaterEqual(rows * columns, count)

    def test_group_cancels_every_stream_before_waiting_for_cleanup(self):
        events = []
        class Playback:
            def __init__(self, name):
                self.name = name
            def start(self):
                events.append('start ' + self.name)
            def cancel(self):
                events.append('cancel ' + self.name)
            def close(self):
                events.append('close ' + self.name)
        group = PlaybackGroup([Playback('a'), Playback('b')])
        group.start()
        group.close()
        self.assertEqual(events, ['start a', 'start b', 'cancel a', 'cancel b', 'close a', 'close b'])


class SharedSignInTests(unittest.TestCase):
    def test_two_streams_share_one_prompt_and_success(self):
        coordinator = LoginCoordinator()
        requests = queue.Queue()
        observed = coordinator.snapshot()
        results = []
        gate = threading.Barrier(3)
        def work():
            gate.wait(timeout=2)
            results.append(coordinator.request(lambda _, request: requests.put(request), threading.Event(), observed))
        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait(timeout=2)
        request = requests.get(timeout=2)
        coordinator.finish(request, True)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True, True])
        self.assertTrue(requests.empty())
        self.assertTrue(coordinator.request(lambda *_: self.fail('Redundant login'), threading.Event(), observed))

    def test_cancellation_does_not_reopen_until_new_selection(self):
        coordinator = LoginCoordinator()
        observed = coordinator.snapshot()
        outcomes = []
        def cancel(_, request):
            coordinator.finish(request, False)
        self.assertFalse(coordinator.request(cancel, threading.Event(), observed))
        self.assertFalse(coordinator.request(lambda *_: self.fail('Repeated login'), threading.Event(), observed))
        coordinator.reset()
        self.assertFalse(coordinator.request(lambda *_: self.fail('Stale view login'), threading.Event(), observed))
        def accept(_, request):
            outcomes.append(True)
            coordinator.finish(request, True)
        self.assertTrue(coordinator.request(accept, threading.Event(), coordinator.snapshot()))
        self.assertEqual(outcomes, [True])
