"""Select, watch, and control configured Provision Home cameras."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from provision_auth import CloudClient, LoginCancelled, ProvisionError, SessionStore, SignInRequired, atomic_write
from camera_config import CameraConfig, load_account_settings, load_cameras


ROOT = Path(__file__).resolve().parent
STATE_ROOT = Path(os.environ.get("PROVISION_STATE_DIR", str(Path.home() / ".cache" / "camera-inspection")))


def load_camera_settings(path=None, environ=None):
    """Compatibility helper for the original single-camera configuration."""
    cameras = load_cameras(path, environ)
    return (cameras[0].serial, cameras[0].camera_ip) if cameras else ("", "")


SERIAL, CAMERA_IP = load_camera_settings()
TICKET = (CameraConfig("Camera", SERIAL, CAMERA_IP).ticket_path(STATE_ROOT)
          if SERIAL else STATE_ROOT / "private" / "camera-ticket.json")
RECEIVER = ROOT / "provisionprobe.exe"


def cloud_client(timeout=15):
    return CloudClient(SessionStore(STATE_ROOT / "private" / "account-session.dpapi"),
                       timeout=timeout, **load_account_settings())


def ticket_data(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("data", data)


def refresh_ticket() -> None:
    document = cloud_client().new_ticket(SERIAL)
    atomic_write(TICKET, json.dumps(document).encode("utf-8"))
    print("Requested a new camera session ticket directly from Provision.", file=sys.stderr)


def ensure_ticket(force: bool = False, login=None) -> dict:
    if login is None:
        from desktop_login import login_window
        login = lambda: login_window(cloud_client(), SERIAL, TICKET)
    return _ensure_ticket(TICKET, refresh_ticket, force, login)


def _ensure_ticket(path, refresh, force, login):
    from camera_session import ticket_needs_renewal
    try:
        current = ticket_data(path) if path.exists() else {}
    except (OSError, ValueError, AttributeError):
        current = {}
    if force or ticket_needs_renewal(current, time.time()):
        try:
            refresh()
        except SignInRequired:
            if not login():
                raise LoginCancelled("Sign-in cancelled") from None
            if not path.exists() or ticket_needs_renewal(ticket_data(path), time.time()):
                refresh()
    return ticket_data(path)


class CameraAccess:
    def __init__(self, profile, state_root=None):
        self.profile = profile
        self.ticket_path = profile.ticket_path(STATE_ROOT if state_root is None else state_root)

    def refresh_ticket(self):
        document = cloud_client().new_ticket(self.profile.serial)
        atomic_write(self.ticket_path, json.dumps(document).encode("utf-8"))

    def ensure_ticket(self, force=False, login=None):
        if login is None:
            from desktop_login import login_window
            login = lambda: login_window(cloud_client(), self.profile.serial, self.ticket_path)
        return _ensure_ticket(self.ticket_path, self.refresh_ticket, force, login)


def parse_resolution(value):
    try:
        width, height = value.lower().split("x")
        if not (1 <= int(width) <= 8192 and 1 <= int(height) <= 8192):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("Use WIDTHxHEIGHT with dimensions between 1 and 8192") from None
    return value.lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="sign in on this computer using a local password window")
    mode.add_argument("--watch", action="store_true", help="open the video and camera controls window")
    mode.add_argument("--sample", type=Path, help="save a bounded HEVC sample without overwriting files")
    mode.add_argument("--check", action="store_true", help="check ticket lifetime without connecting to the camera")
    parser.add_argument("--refresh", action="store_true", help="request a fresh streaming ticket directly from Provision")
    parser.add_argument("--seconds", type=int, default=20, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--resolution", type=parse_resolution, default="2304x1296", help="requested resolution; default is verified full resolution")
    parser.add_argument("--size", default="large", help="app stream size request; default is full resolution")
    parser.add_argument("--camera", help="configured camera name for --sample, --check, or --login")
    args = parser.parse_args()
    args.cameras = load_cameras()
    if not args.cameras:
        parser.error("Copy camera.example.json to camera.local.json and set your camera's serial and IP, "
                     "or set PROVISION_SERIAL and PROVISION_CAMERA_IP")
    if args.watch or not (args.sample or args.check or args.login):
        from desktop_viewer import watch
        return watch(args)
    selected = [profile for profile in args.cameras if profile.name.casefold() == (args.camera or "").casefold()]
    if not args.camera and len(args.cameras) == 1:
        selected = list(args.cameras)
    if len(selected) != 1:
        parser.error("Choose a configured camera by name with --camera")
    profile = selected[0]
    access = CameraAccess(profile)
    if args.login:
        from desktop_login import login_window
        if login_window(cloud_client(), profile.serial, access.ticket_path):
            print("Desktop sign-in succeeded. A new camera ticket was obtained without the phone.")
            return 0
        return 1
    data = access.ensure_ticket(args.refresh)
    if args.check:
        remaining = max(0, int((data["expirationTime"] - time.time() * 1000) / 1000))
        print(f"{profile.name} ({profile.camera_ip}): cached session ticket valid for {remaining} seconds")
        return 0
    command = [str(RECEIVER), "-ticket", str(access.ticket_path), "-serial", profile.serial,
               "-target-ip", profile.camera_ip, "-resolution", args.resolution, "-size", args.size]
    if args.sample:
        return subprocess.run(command + ["-duration", f"{args.seconds}s", "-out", str(args.sample)]).returncode
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except LoginCancelled:
        sys.exit(0)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Camera client: {error}", file=sys.stderr)
        sys.exit(1)
