"""Short-lived Jev credential lease in the macOS login Keychain."""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from .errors import JgError


MAX_LEASE_SECONDS = 48 * 60 * 60
_SERVICE = b"jev-git-graph:Jev_Key:48h-cache"
_ACCOUNT = b"HomeLab"
_MARKER = "jev-git-graph-credential-lease-v1"
_NOT_FOUND = -25300


class KeychainLease:
    def __init__(self, service: bytes = _SERVICE, account: bytes = _ACCOUNT):
        if sys.platform != "darwin":
            raise JgError("Jev Keychain cache requires macOS")
        self.service, self.account = service, account
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        self.security.SecKeychainAddGenericPassword.argtypes = [
            pointer, uint, pointer, uint, pointer, uint, pointer, ctypes.POINTER(pointer)]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainFindGenericPassword.argtypes = [
            pointer, uint, pointer, uint, pointer, ctypes.POINTER(uint), ctypes.POINTER(pointer),
            ctypes.POINTER(pointer)]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [pointer]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [pointer]

    def _find(self) -> tuple[bytes, ctypes.c_void_p] | None:
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(self.account), self.account,
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == _NOT_FOUND:
            return None
        if status != 0:
            raise JgError(f"Jev Keychain lookup failed (OSStatus {status})")
        try:
            value = ctypes.string_at(data, length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, data)
        return value, item

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise JgError("Jev Keychain cache has an invalid envelope") from exc
        if (not isinstance(value, dict) or value.get("kind") != _MARKER
                or not isinstance(value.get("token"), str)
                or not isinstance(value.get("issued_at"), int)
                or not isinstance(value.get("expires_at"), int)
                or value["expires_at"] - value["issued_at"] > MAX_LEASE_SECONDS
                or value["expires_at"] <= value["issued_at"]):
            raise JgError("Jev Keychain cache has an invalid lease")
        return value

    def clear(self) -> bool:
        found = self._find()
        if found is None:
            return False
        raw, item = found
        try:
            self._decode(raw)
            status = self.security.SecKeychainItemDelete(item)
            if status != 0:
                raise JgError(f"Jev Keychain cache deletion failed (OSStatus {status})")
            return True
        finally:
            self.core.CFRelease(item)

    def store(self, token: str, *, issued_at: int | None = None, hours: int = 48) -> int:
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(char in token for char in "\r\n\x00")):
            raise JgError("Jev credential is empty or malformed")
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 48:
            raise JgError("Jev credential cache duration must be 1 to 48 hours")
        issued = int(time.time()) if issued_at is None else issued_at
        expires = issued + hours * 3600
        self.clear()
        payload = json.dumps({"kind": _MARKER, "issued_at": issued,
                              "expires_at": expires, "token": token}, separators=(",", ":")).encode()
        item = ctypes.c_void_p()
        status = self.security.SecKeychainAddGenericPassword(
            None, len(self.service), self.service, len(self.account), self.account,
            len(payload), payload, ctypes.byref(item))
        if status != 0:
            raise JgError(f"Jev Keychain cache write failed (OSStatus {status})")
        if item:
            self.core.CFRelease(item)
        return expires

    def read(self, *, now: int | None = None) -> tuple[str, int] | None:
        found = self._find()
        if found is None:
            return None
        raw, item = found
        try:
            value = self._decode(raw)
        finally:
            self.core.CFRelease(item)
        current = int(time.time()) if now is None else now
        if current < value["issued_at"] - 300 or current >= value["expires_at"]:
            self.clear()
            return None
        return value["token"], value["expires_at"]


def resolve_provider_token() -> str:
    explicit = os.environ.get("TYPESAFE_API_KEY")
    if explicit:
        return explicit
    cached = KeychainLease().read()
    if cached is None:
        raise JgError("Jev credential is unavailable; run `jg credential cache` once")
    return cached[0]


def credential_status() -> str:
    cached = KeychainLease().read()
    if cached is None:
        return "Jev Keychain cache: absent or expired"
    expires = datetime.fromtimestamp(cached[1], UTC).isoformat()
    return f"Jev Keychain cache: active until {expires}"


def serve_cache_form(*, hours: int = 48, timeout_seconds: int = 600) -> str:
    """Accept one credential via a transient loopback form without logging it."""
    lease = KeychainLease()
    nonce = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _headers(self, status: int, length: int):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; style-src 'unsafe-inline'")
            self.send_header("Content-Length", str(length))
            self.end_headers()

        def do_GET(self):
            if self.path != f"/{nonce}" or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self._headers(404, 0)
                return
            page = ("<!doctype html><meta charset=utf-8><title>Jev Keychain cache</title>"
                    "<h1>Cache Jev_Key for 48 hours</h1><p>Paste the key from Passwords. "
                    "It is stored in macOS Keychain and is never shown here again.</p>"
                    f"<form method=post action='/{nonce}'><input type=password name=token required "
                    "autocomplete=off autofocus><button>Store for 48 hours</button></form>").encode()
            self._headers(200, len(page))
            self.wfile.write(page)

        def do_POST(self):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if (self.path != f"/{nonce}" or self.headers.get("Host") != expected_host
                    or self.headers.get("Origin") != f"http://{expected_host}"
                    or self.headers.get("Content-Type") != "application/x-www-form-urlencoded"):
                self._headers(403, 0)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._headers(400, 0)
                return
            if not 1 <= length <= 8192:
                self._headers(413, 0)
                return
            try:
                fields = parse_qs(self.rfile.read(length).decode("utf-8", "strict"), strict_parsing=True)
            except (UnicodeError, ValueError):
                self._headers(400, 0)
                return
            values = fields.get("token", [])
            if len(values) != 1:
                self._headers(400, 0)
                return
            try:
                self.server.cached_until = lease.store(values[0], hours=hours)
            except JgError:
                self._headers(400, 0)
                return
            page = b"<!doctype html><meta charset=utf-8><h1>Jev key cached for 48 hours</h1>"
            self._headers(200, len(page))
            self.wfile.write(page)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    url = f"http://127.0.0.1:{server.server_port}/{nonce}"
    print(f"Transient Jev Keychain handoff: {url}", flush=True)
    deadline = time.monotonic() + timeout_seconds
    try:
        while not getattr(server, "cached_until", None) and time.monotonic() < deadline:
            server.handle_request()
        if not getattr(server, "cached_until", None):
            raise JgError("Jev Keychain handoff timed out without storing a key")
        return credential_status()
    finally:
        server.server_close()
