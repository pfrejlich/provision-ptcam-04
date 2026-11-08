"""Private, named camera profiles and isolated streaming-ticket locations."""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class CameraConfig:
    name: str
    serial: str
    camera_ip: str

    def ticket_path(self, state_root):
        key = hashlib.sha256(self.serial.encode("utf-8")).hexdigest()
        return Path(state_root) / "private" / "tickets" / (key + ".json")


def load_cameras(path=None, environ=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parent / "camera.local.json"
    environ = os.environ if environ is None else environ
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("Camera configuration must be a JSON object")
    entries = settings.get("cameras")
    if entries is None:
        entries = [settings] if settings else []
    if not isinstance(entries, list):
        raise ValueError("cameras must be a list of named camera configurations")
    if "PROVISION_SERIAL" in environ or "PROVISION_CAMERA_IP" in environ:
        if len(entries) > 1:
            raise ValueError("Use camera.local.json for multiple cameras; remove the single-camera environment overrides")
        original = entries[0] if entries and isinstance(entries[0], dict) else {}
        entries = [dict(original, serial=environ.get("PROVISION_SERIAL", original.get("serial", "")),
                        camera_ip=environ.get("PROVISION_CAMERA_IP", original.get("camera_ip", "")))]
    cameras, names, serials = [], set(), set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("Each camera must be a JSON object")
        values = [entry.get("name", f"Camera {index}"),
                  entry.get("serial", ""), entry.get("camera_ip", "")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"Camera {index} needs a name, serial, and IP address")
        name, serial, address = (value.strip() for value in values)
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise ValueError(f"Camera {index} needs a valid IP address") from None
        if name.casefold() in names or serial in serials:
            raise ValueError("Camera names and serials must be unique")
        names.add(name.casefold())
        serials.add(serial)
        cameras.append(CameraConfig(name, serial, address))
    return tuple(cameras)


def load_account_settings(path=None, environ=None):
    """Load optional account metadata from ignored local settings."""
    path = Path(path) if path is not None else Path(__file__).resolve().parent / "camera.local.json"
    environ = os.environ if environ is None else environ
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("account", {}), dict):
        raise ValueError("Account configuration must be a JSON object")
    account = settings.get("account", {})
    country = environ.get("PROVISION_COUNTRY", account.get("country_no", "US"))
    zone = environ.get("PROVISION_TIME_ZONE", account.get("time_zone", "UTC"))
    if not isinstance(country, str) or len(country) != 2 or not country.isascii() or not country.isalpha():
        raise ValueError("Account country_no must be a two-letter country code")
    if not isinstance(zone, str) or not zone.strip() or len(zone) > 64 or "\r" in zone or "\n" in zone:
        raise ValueError("Account time_zone must be a time-zone name")
    return {"country_no": country.upper(), "time_zone": zone.strip()}
