import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from provision_auth import CloudClient, ProvisionError, SessionStore, SignInRequired


class MemoryStore:
    def __init__(self, account=None):
        self.account = dict(account or {})

    def load(self):
        return dict(self.account)

    def save(self, account):
        self.account = dict(account)


class CredentialStorageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_account_is_encrypted_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.dpapi'
            store = SessionStore(path)
            account = {'token': 'synthetic-private-token', 'password': 'synthetic-private-password'}
            store.save(account)
            self.assertEqual(store.load(), account)
            self.assertNotIn(b'synthetic-private-token', path.read_bytes())
            self.assertNotIn(b'synthetic-private-password', path.read_bytes())

    def test_credentials_cannot_be_sent_to_an_unapproved_host(self):
        for endpoint in ['http://api-eu.provisionisr-app.com', 'https://example.invalid',
                         'https://api-eu.provisionisr-app.com.evil.invalid',
                         'https://user:password@api-eu.provisionisr-app.com']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ProvisionError):
                CloudClient(MemoryStore(), api_base=endpoint)

    def test_sign_in_is_requested_without_any_phone_fallback(self):
        with self.assertRaises(SignInRequired):
            CloudClient(MemoryStore()).new_ticket('synthetic-camera')


@unittest.skipUnless(os.environ.get('PROVISION_TEST_URL'), 'Start the local Postman simulation')
class SimulatedAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.base = os.environ['PROVISION_TEST_URL'].rstrip('/')
        urllib.request.urlopen(self.base + '/__test/reset').close()
        self.store = MemoryStore()
        self.client = CloudClient(self.store, self.base, timeout=2, allow_local_test=True)

    def calls(self):
        with urllib.request.urlopen(self.base + '/__test/requests') as response:
            return json.load(response)

    def test_login_then_new_ticket_from_desktop(self):
        ticket = self.client.sign_in('owner@example.invalid', 'synthetic-password', 'synthetic-camera')
        self.assertGreater(ticket['data']['expirationTime'], time.time() * 1000)
        self.assertNotIn('password', self.store.account)
        self.client.new_ticket('synthetic-camera')
        calls = self.calls()
        self.assertEqual([item['path'] for item in calls], [
            '/account/login/', '/device/getWebrtcTicket', '/device/getWebrtcTicket'])
        self.assertTrue(calls[-1]['hasAuthorization'])
        self.assertTrue(all(item['tenantId'] == 'provisionhome' for item in calls))
        self.assertEqual(calls[-1]['serialNumber'], 'synthetic-camera')

    def test_account_metadata_is_serialized_from_private_configuration(self):
        client = CloudClient(self.store, self.base, allow_local_test=True, country_no='CA', time_zone='Etc/UTC')
        client.sign_in('owner@example.invalid', 'synthetic-password', 'synthetic-camera')
        self.assertTrue(all(call['countryNo'] == 'CA' and call['timeZone'] == 'Etc/UTC' for call in self.calls()))

    def test_expired_account_reauthenticates_once_with_remembered_credentials(self):
        self.store.account = {'token': 'expired-account-token', 'api_base': self.base,
                              'email': 'owner@example.invalid', 'password': 'synthetic-password'}
        self.client.new_ticket('synthetic-camera')
        self.assertEqual([item['path'] for item in self.calls()], [
            '/device/getWebrtcTicket', '/account/login/', '/device/getWebrtcTicket'])
        self.assertEqual(self.store.account['token'], 'synthetic-account-token')

    def test_unremembered_expired_login_requests_desktop_sign_in(self):
        self.store.account = {'token': 'expired-account-token', 'api_base': self.base}
        with self.assertRaises(SignInRequired):
            self.client.new_ticket('synthetic-camera')
        self.assertEqual(len(self.calls()), 1)

    def test_rejected_login_does_not_replace_saved_account(self):
        self.store.account = {'token': 'keep-existing'}
        with self.assertRaises(ProvisionError) as raised:
            self.client.sign_in('owner@example.invalid', 'wrong', 'synthetic-camera')
        self.assertNotIn('wrong', str(raised.exception))
        self.assertEqual(self.store.account['token'], 'keep-existing')

    def test_server_failures_and_invalid_login_responses(self):
        for password in ['http-error', 'non-json', 'missing-token']:
            with self.subTest(password=password), self.assertRaises(ProvisionError):
                self.client.authenticate('owner@example.invalid', password)

    def test_login_redirect_is_not_followed(self):
        with self.assertRaisesRegex(ProvisionError, 'redirect'):
            self.client.authenticate('owner@example.invalid', 'redirect')
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_tickets_are_rejected(self):
        for token in ['expired-ticket', 'untrusted-signal', 'bad-ticket']:
            with self.subTest(token=token), self.assertRaises(ProvisionError):
                self.client.fetch_ticket('synthetic-camera', token)

    def test_launcher_refresh_uses_http_even_without_adb(self):
        import camera_client
        self.store.account = {'token': 'synthetic-account-token', 'api_base': self.base}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ticket.json'
            with patch.object(camera_client, 'TICKET', path), \
                 patch.object(camera_client, 'cloud_client', return_value=self.client), \
                 patch('camera_client.subprocess.run', side_effect=AssertionError('Phone access is forbidden')):
                ticket = camera_client.ensure_ticket(force=True)
            self.assertTrue(path.is_file())
            self.assertEqual(ticket['id'], 'synthetic-viewer')
        self.assertEqual(self.calls()[-1]['path'], '/device/getWebrtcTicket')

    def test_directional_controls_use_expected_pitch_and_yaw(self):
        self.store.account = {'token': 'synthetic-account-token', 'api_base': self.base}
        for direction in ['left', 'right', 'up', 'down', 'stop']:
            self.client.rotate('synthetic-camera', direction)
        self.assertEqual([(call['pitch'], call['yaw']) for call in self.calls()],
                         [(0, 1), (0, -1), (-1, 0), (1, 0), (0, 0)])
        with self.assertRaises(ProvisionError):
            self.client.rotate('synthetic-camera', 'invalid')
        self.assertEqual(len(self.calls()), 5)

    def test_each_camera_fetches_and_keeps_its_own_ticket(self):
        from camera_client import CameraAccess
        from camera_config import CameraConfig
        self.store.account = {'token': 'synthetic-account-token', 'api_base': self.base}
        with tempfile.TemporaryDirectory() as directory, \
             patch('camera_client.cloud_client', return_value=self.client):
            first = CameraAccess(CameraConfig('First', 'camera-a', '192.0.2.10'), directory)
            second = CameraAccess(CameraConfig('Second', 'camera-b', '192.0.2.11'), directory)
            first.ensure_ticket()
            second.ensure_ticket()
            self.assertTrue(first.ticket_path.exists())
            self.assertTrue(second.ticket_path.exists())
            first.ensure_ticket()
        self.assertEqual([call['serialNumber'] for call in self.calls()], ['camera-a', 'camera-b'])

    def test_simultaneous_expired_accounts_reauthenticate_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        gate = threading.Barrier(2)
        thread_state = threading.local()
        class ConcurrentStore(MemoryStore):
            def load(self):
                snapshot = super().load()
                if not getattr(thread_state, 'loaded', False):
                    thread_state.loaded = True
                    gate.wait(timeout=3)
                return snapshot
        store = ConcurrentStore({'token': 'expired-account-token', 'api_base': self.base,
                                 'email': 'owner@example.invalid', 'password': 'synthetic-password',
                                 'expires_at': time.time() - 60})
        def fetch(serial):
            return CloudClient(store, self.base, timeout=2, allow_local_test=True).new_ticket(serial)
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(fetch, ['camera-a', 'camera-b']))
        self.assertEqual(len(replies), 2)
        paths = [item['path'] for item in self.calls()]
        self.assertEqual(paths.count('/account/login/'), 1)
        self.assertEqual(paths.count('/device/getWebrtcTicket'), 2)


if __name__ == '__main__':
    unittest.main()
