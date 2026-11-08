import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import camera_client
from camera_session import MotionWorker, StreamSession, png_frames, ticket_needs_renewal
from provision_auth import LoginCancelled, ProvisionError, SignInRequired


def ticket(expiration=None):
    return {"id": "test-viewer", "accessToken": "test-token",
            "expirationTime": expiration or (time.time() + 1800) * 1000}


class AutomaticTicketTests(unittest.TestCase):
    def test_renewal_begins_before_expiration(self):
        self.assertFalse(ticket_needs_renewal(ticket(1070000), 1000))
        self.assertTrue(ticket_needs_renewal(ticket(1059000), 1000))
        self.assertTrue(ticket_needs_renewal({"expirationTime": 9999999999999}, 1000))
        self.assertTrue(ticket_needs_renewal(ticket(float('nan')), 1000))

    def test_missing_sign_in_opens_prompt_and_resumes_original_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ticket.json'
            prompted = []
            def login():
                prompted.append(True)
                path.write_text(json.dumps(ticket()), encoding='utf8')
                return True
            with patch.object(camera_client, 'TICKET', path), \
                 patch.object(camera_client, 'refresh_ticket', side_effect=SignInRequired('Sign in')):
                received = camera_client.ensure_ticket(login=login)
            self.assertEqual(received['id'], 'test-viewer')
            self.assertEqual(len(prompted), 1)

    def test_cancelled_login_does_not_loop_or_retry_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(camera_client, 'TICKET', Path(directory) / 'missing.json'), \
                 patch.object(camera_client, 'refresh_ticket', side_effect=SignInRequired('Sign in')) as refresh:
                with self.assertRaises(LoginCancelled):
                    camera_client.ensure_ticket(login=lambda: False)
                refresh.assert_called_once()

    def test_corrupt_cache_is_replaced_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ticket.json'
            path.write_text('{broken', encoding='utf8')
            def refresh():
                path.write_text(json.dumps(ticket()), encoding='utf8')
            with patch.object(camera_client, 'TICKET', path), patch.object(camera_client, 'refresh_ticket', side_effect=refresh):
                self.assertEqual(camera_client.ensure_ticket()['id'], 'test-viewer')

    def test_active_viewer_renews_without_closing(self):
        requested = []
        session = StreamSession(lambda force: requested.append(force) or ticket(), [], '', Path('unused.log'), lambda *_: None)
        connections = []
        def stream(_):
            connections.append(True)
            if len(connections) == 2:
                session.stop.set()
                return 'closed'
            return 'renew'
        session._stream = stream
        session._run()
        self.assertEqual(requested, [False, True])
        self.assertEqual(len(connections), 2)


class MovementTests(unittest.TestCase):
    def test_click_moves_then_stops(self):
        commands = []
        motion = MotionWorker(commands.append, lambda *_: None, pulse=0)
        self.assertTrue(motion.request('left'))
        motion.thread.join(timeout=1)
        self.assertEqual(commands, ['left', 'stop'])
        motion.close()

    def test_close_interrupts_hold_and_sends_stop(self):
        commands, started = [], threading.Event()
        def move(direction):
            commands.append(direction)
            if direction != 'stop':
                started.set()
        motion = MotionWorker(move, lambda *_: None, pulse=30)
        motion.request('up')
        self.assertTrue(started.wait(1))
        motion.close()
        self.assertEqual(commands, ['up', 'stop'])
        self.assertFalse(motion.request('right'))

    def test_ambiguous_failure_still_attempts_stop(self):
        commands, events = [], []
        def move(direction):
            commands.append(direction)
            if direction == 'right':
                raise ProvisionError('Timeout')
        motion = MotionWorker(move, lambda *args: events.append(args), pulse=0)
        motion.request('right')
        motion.thread.join(timeout=1)
        self.assertEqual(commands, ['right', 'stop'])
        self.assertTrue(any(event[0] == 'motion_error' for event in events))
        motion.close()

    def test_authentication_failure_requests_sign_in_without_replaying_movement(self):
        events, commands = [], []
        def move(direction):
            commands.append(direction)
            raise SignInRequired('Sign in')
        motion = MotionWorker(move, lambda *args: events.append(args), pulse=0)
        motion.request('down')
        motion.thread.join(timeout=1)
        self.assertEqual(commands, ['down', 'stop'])
        self.assertEqual(sum(event[0] == 'needs_login' for event in events), 1)
        motion.close()


class VideoFramingTests(unittest.TestCase):
    def test_complete_png_and_truncated_next_frame(self):
        frame = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 0) + b'IEND' + b'\x00' * 4
        self.assertEqual(list(png_frames(io.BytesIO(frame + frame[:-2]))), [frame])

    def test_unbounded_frame_is_rejected(self):
        stream = io.BytesIO(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 0x7fffffff) + b'IDAT')
        with self.assertRaises(ValueError):
            list(png_frames(stream))
