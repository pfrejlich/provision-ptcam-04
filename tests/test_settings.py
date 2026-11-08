import json
from pathlib import Path
import tempfile
import unittest

from camera_client import load_camera_settings
from camera_config import load_account_settings, load_cameras


class CameraSettingsTests(unittest.TestCase):
    def test_account_metadata_has_neutral_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_account_settings(Path(directory) / 'missing.json', {}),
                             {'country_no': 'US', 'time_zone': 'UTC'})

    def test_account_metadata_comes_from_private_file_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera.local.json'
            path.write_text(json.dumps({'account': {'country_no': 'ca', 'time_zone': 'Etc/UTC'}}), encoding='utf-8')
            self.assertEqual(load_account_settings(path, {}), {'country_no': 'CA', 'time_zone': 'Etc/UTC'})
            self.assertEqual(load_account_settings(path, {'PROVISION_COUNTRY': 'GB', 'PROVISION_TIME_ZONE': 'UTC'}),
                             {'country_no': 'GB', 'time_zone': 'UTC'})
            for invalid in [{'country_no': ''}, {'country_no': 42}, {'time_zone': None}]:
                path.write_text(json.dumps({'account': invalid}), encoding='utf-8')
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    load_account_settings(path, {})

    def test_multiple_cameras_have_names_and_isolated_ticket_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera.local.json'
            path.write_text(json.dumps({'cameras': [
                {'name': 'Camera A', 'serial': 'camera-a', 'camera_ip': '192.0.2.10'},
                {'name': 'Camera B', 'serial': 'camera-b', 'camera_ip': '192.0.2.11'}]}), encoding='utf-8')
            cameras = load_cameras(path, {})
            self.assertEqual([profile.name for profile in cameras], ['Camera A', 'Camera B'])
            self.assertNotEqual(cameras[0].ticket_path(directory), cameras[1].ticket_path(directory))
            self.assertEqual(cameras[0].ticket_path(directory), load_cameras(path, {})[0].ticket_path(directory))
            with self.assertRaises(ValueError):
                load_cameras(path, {'PROVISION_SERIAL': 'ambiguous-override'})

    def test_duplicate_cameras_and_invalid_addresses_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera.local.json'
            first = {'name': 'Camera A', 'serial': 'camera-a', 'camera_ip': '192.0.2.10'}
            for second in [dict(first, serial='other'), dict(first, name='Other'),
                           dict(first, name='Other', serial='other', camera_ip='invalid')]:
                path.write_text(json.dumps({'cameras': [first, second]}), encoding='utf-8')
                with self.subTest(second=second), self.assertRaises(ValueError):
                    load_cameras(path, {})

    def test_private_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera.local.json'
            path.write_text(json.dumps({'serial': 'local-camera', 'camera_ip': '192.0.2.10'}), encoding='utf-8')
            self.assertEqual(load_camera_settings(path, {}), ('local-camera', '192.0.2.10'))
            self.assertEqual(load_camera_settings(path, {'PROVISION_SERIAL': 'another-camera',
                                                       'PROVISION_CAMERA_IP': '192.0.2.11'}),
                             ('another-camera', '192.0.2.11'))

    def test_no_configuration_contains_no_camera_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_camera_settings(Path(directory) / 'missing.json', {}), ('', ''))

    def test_invalid_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'camera.local.json'
            for data in [[], {'serial': 123}, {'camera_ip': None}]:
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.subTest(data=data), self.assertRaises(ValueError):
                    load_camera_settings(path, {})
