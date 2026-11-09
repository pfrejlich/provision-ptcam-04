"""Provision Home account login and WebRTC tickets, independent of Android."""

import base64
import ctypes
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


API_BASE = "https://api-eu.provisionisr-app.com"
ACCOUNT_REFRESH_LOCK = threading.RLock()
AUTH_ERRORS = {-1022, -1023, -1024, -1025, -1026, -1027}
APP = {
    "versionName": "1.3.5(e0aaba)", "bundle": "com.provisionhome.app",
    "timeZone": "UTC", "appName": "Provision Home",
    "tenantId": "provisionhome", "env": "prod-k8s", "version": 210003005,
    "appType": "Android",
}


class ProvisionError(RuntimeError):
    """An intentionally credential-free error for callers and user interfaces."""


class SignInRequired(ProvisionError):
    pass


class LoginCancelled(ProvisionError):
    pass


class ApiError(ProvisionError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Provision rejected the request (code {code})")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise ProvisionError("Account credential storage currently requires Windows DPAPI")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(b"provision-camera-account-v1")
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(entropy_buffer.value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise ProvisionError("Windows could not unlock the saved account; sign in again")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


class SessionStore:
    """Persist account tokens and optional remembered credentials for this Windows user."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
        except (ValueError, OSError) as error:
            raise ProvisionError("Saved account data is unreadable; sign in again") from error

    def save(self, account: dict) -> None:
        atomic_write(self.path, _dpapi(json.dumps(account).encode("utf-8")))


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise ProvisionError("Provision returned a redirect; account credentials were not forwarded")


def token_expiration(token: str) -> float:
    """Use a token's expiry as a cache hint; the server remains the authority."""
    try:
        payload = token.removeprefix("Bearer ").split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp", 0))
    except (ValueError, KeyError, IndexError, TypeError):
        return 0


class CloudClient:
    def __init__(self, store: SessionStore, api_base: str = API_BASE, timeout: float = 15,
                 allow_local_test: bool = False, country_no: str = "US", time_zone: str = "UTC"):
        parsed = urllib.parse.urlsplit(api_base)
        official = (parsed.scheme == "https" and parsed.hostname in {
            "api-eu.provisionisr-app.com", "api-us.provisionisr-app.com"})
        local_test = (allow_local_test and parsed.scheme == "http" and
                      parsed.hostname in {"127.0.0.1", "localhost"})
        if (not official and not local_test) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProvisionError("The account API must use an approved Provision HTTPS endpoint")
        self.store, self.api_base, self.timeout = store, api_base.rstrip("/"), timeout
        self.country_no, self.time_zone = country_no, time_zone
        self.opener = urllib.request.build_opener(
            NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, path: str, fields: dict, authorization: str = "") -> dict:
        payload = {"countryNo": self.country_no, "language": "en",
                   "app": dict(APP, timeZone=self.time_zone), **fields}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(self.api_base + path, json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProvisionError("Provision returned an oversized response")
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in {401, 403}:
                raise SignInRequired("Provision requires a fresh account sign-in") from None
            raise ProvisionError(f"Provision returned HTTP {code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise ProvisionError("Could not reach Provision within the request timeout; retry when connected") from None
        try:
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError
            result = document.get("result", document.get("base", {}).get("result"))
            if not isinstance(result, int):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ProvisionError("Provision returned an invalid JSON response") from None
        if result != 0:
            if result in AUTH_ERRORS:
                raise SignInRequired("Provision requires a fresh account sign-in")
            raise ApiError(result)
        return document

    def authenticate(self, email: str, password: str) -> dict:
        if not email.strip() or not password:
            raise ProvisionError("Enter your Provision Home email and password")
        document = self.request("/account/login/", {"email": email.strip(), "password": password, "loginType": 0})
        data = document.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        token = token.get("token") if isinstance(token, dict) else None
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise ProvisionError("Provision sign-in response did not contain an account token")
        return {"token": token, "expires_at": token_expiration(token), "api_base": self.api_base}

    def fetch_ticket(self, serial: str, token: str) -> dict:
        document = self.request("/device/getWebrtcTicket", {
            "serialNumber": serial, "requestId": uuid.uuid4().hex,
            "supportUnlimitedWebsocket": True, "list": [],
        }, token)
        return self.validate_ticket(document)

    @staticmethod
    def validate_ticket(document: dict) -> dict:
        data = document.get("data")
        if not isinstance(data, dict) or not data.get("accessToken") or not data.get("id"):
            raise ProvisionError("Provision returned an incomplete streaming ticket")
        endpoint = urllib.parse.urlsplit(data.get("signalServer", ""))
        if endpoint.scheme != "wss" or not (endpoint.hostname or "").endswith(".smartvideogo.com"):
            raise ProvisionError("Provision returned an unexpected signaling endpoint")
        try:
            expiry = float(data["expirationTime"])
        except (KeyError, TypeError, ValueError):
            raise ProvisionError("Provision streaming ticket has no valid expiration") from None
        if expiry <= time.time() * 1000:
            raise ProvisionError("Provision returned an expired streaming ticket")
        return document

    def sign_in(self, email: str, password: str, serial: str, remember: bool = False) -> dict:
        with ACCOUNT_REFRESH_LOCK:
            return self._sign_in(email, password, serial, remember)

    def _sign_in(self, email: str, password: str, serial: str, remember: bool) -> dict:
        account = self.authenticate(email, password)
        ticket = self.fetch_ticket(serial, account["token"])
        if remember:
            account.update(email=email.strip(), password=password)
        self.store.save(account)
        return ticket

    def _load_account(self):
        try:
            account = self.store.load()
        except ProvisionError:
            raise SignInRequired("Please sign in to Provision Home again") from None
        if not account:
            raise SignInRequired("Please sign in to Provision Home")
        if account.get("api_base") != self.api_base:
            raise SignInRequired("Saved sign-in belongs to another region; sign in again")
        return account

    def authorized_request(self, path: str, fields: dict) -> dict:
        account = self._load_account()
        expired = bool(account.get("expires_at") and account["expires_at"] <= time.time() + 30)
        if not expired and account.get("token"):
            try:
                return self.request(path, fields, account["token"])
            except SignInRequired:
                pass
        # Healthy requests remain parallel; only account replacement is serialized.
        with ACCOUNT_REFRESH_LOCK:
            latest = self._load_account()
            if latest != account and latest.get("token"):
                try:
                    return self.request(path, fields, latest["token"])
                except SignInRequired:
                    pass
            return self._renew_account_request(path, fields, latest)

    def _renew_account_request(self, path, fields, account):
        if not account.get("email") or not account.get("password"):
            raise SignInRequired("Please sign in to Provision Home again")
        try:
            refreshed = self.authenticate(account["email"], account["password"])
        except ApiError:
            raise SignInRequired("Saved sign-in was rejected; please sign in again") from None
        response = self.request(path, fields, refreshed["token"])
        refreshed.update(email=account["email"], password=account["password"])
        self.store.save(refreshed)
        return response

    def new_ticket(self, serial: str) -> dict:
        document = self.authorized_request("/device/getWebrtcTicket", {
            "serialNumber": serial, "requestId": uuid.uuid4().hex,
            "supportUnlimitedWebsocket": True, "list": [],
        })
        return self.validate_ticket(document)

    def rotate(self, serial: str, direction: str) -> dict:
        # RockerControlManager negates joystick Y for pitch and X for yaw.
        vectors = {"left": (0.0, 1.0), "right": (0.0, -1.0),
                   "up": (-1.0, 0.0), "down": (1.0, 0.0), "stop": (0.0, 0.0)}
        if direction not in vectors:
            raise ProvisionError("Choose left, right, up, down, or stop")
        pitch, yaw = vectors[direction]
        return self.authorized_request("/device/rotate", {"serialNumber": serial, "pitch": pitch, "yaw": yaw})
