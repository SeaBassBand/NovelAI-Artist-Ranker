#!/usr/bin/env python3
"""Local-only NovelAI API credential storage for the Artist Ranker.

Secrets are stored as a Windows Generic Credential. This module deliberately
has no file-backed fallback: a token must never be written to JSON, source,
logs, browser storage, Android storage, or backup archives.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Optional, Tuple

CREDENTIAL_TARGET = "NovelAI Artist Ranker/NovelAI API"
CREDENTIAL_USERNAME = "NovelAI persistent API token"
NOVELAI_SUBSCRIPTION_URL = "https://api.novelai.net/user/subscription"


class CredentialStoreError(RuntimeError):
    """Base error for secure credential operations."""


class CredentialStoreUnavailable(CredentialStoreError):
    """Raised when Windows Credential Manager is unavailable."""


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    backend: str
    available: bool


if os.name == "nt":
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _cred_write = _advapi32.CredWriteW
    _cred_write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _cred_write.restype = wintypes.BOOL
    _cred_read = _advapi32.CredReadW
    _cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    _cred_read.restype = wintypes.BOOL
    _cred_delete = _advapi32.CredDeleteW
    _cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _cred_delete.restype = wintypes.BOOL
    _cred_free = _advapi32.CredFree
    _cred_free.argtypes = [wintypes.LPVOID]
    _cred_free.restype = None

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168


def _require_windows() -> None:
    if os.name != "nt":
        raise CredentialStoreUnavailable(
            "Windows Credential Manager is required for NovelAI API-key storage."
        )


def _normalized_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError("Enter a NovelAI persistent API token.")
    if any(character.isspace() for character in token):
        raise ValueError("The API token must not contain spaces or line breaks.")
    if len(token) < 24:
        raise ValueError("The API token is unexpectedly short.")
    return token


def save_api_key(value: Any) -> None:
    """Write or replace the token in Windows Credential Manager."""
    _require_windows()
    token = _normalized_token(value)
    blob_bytes = token.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(blob_bytes)).from_buffer_copy(blob_bytes)
    credential = CREDENTIALW()
    credential.Flags = 0
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = CREDENTIAL_TARGET
    credential.Comment = "Stored locally by NovelAI Artist Ranker"
    credential.CredentialBlobSize = len(blob_bytes)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.AttributeCount = 0
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = CREDENTIAL_USERNAME
    if not _cred_write(ctypes.byref(credential), 0):
        error_code = ctypes.get_last_error()
        raise CredentialStoreError(
            f"Windows Credential Manager could not save the credential (error {error_code})."
        )


def read_api_key() -> str:
    """Read the token without logging, masking, or copying it to persistent app state."""
    _require_windows()
    pointer = PCREDENTIALW()
    if not _cred_read(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error_code = ctypes.get_last_error()
        if error_code == ERROR_NOT_FOUND:
            return ""
        raise CredentialStoreError(
            f"Windows Credential Manager could not read the credential (error {error_code})."
        )
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize or 0)
        if size <= 0 or not credential.CredentialBlob:
            return ""
        raw = ctypes.string_at(credential.CredentialBlob, size)
        return raw.decode("utf-16-le").rstrip("\x00")
    finally:
        _cred_free(pointer)


def delete_api_key() -> bool:
    """Delete the token. Returns False when no credential existed."""
    _require_windows()
    if _cred_delete(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0):
        return True
    error_code = ctypes.get_last_error()
    if error_code == ERROR_NOT_FOUND:
        return False
    raise CredentialStoreError(
        f"Windows Credential Manager could not delete the credential (error {error_code})."
    )


def credential_status() -> CredentialStatus:
    if os.name != "nt":
        return CredentialStatus(False, "Windows Credential Manager", False)
    try:
        return CredentialStatus(bool(read_api_key()), "Windows Credential Manager", True)
    except CredentialStoreError:
        return CredentialStatus(False, "Windows Credential Manager", False)


def is_configured() -> bool:
    return credential_status().configured


def _normalize_host(value: Any) -> str:
    host = str(value or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    return host.strip("[]")


def _local_host_addresses() -> set[str]:
    """Collect loopback and active addresses assigned to this computer."""
    addresses = {"127.0.0.1", "::1", "localhost"}
    hostnames = {socket.gethostname(), socket.getfqdn()}
    for hostname in hostnames:
        if not hostname:
            continue
        addresses.add(_normalize_host(hostname))
        try:
            for _family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(hostname, None):
                if sockaddr:
                    addresses.add(_normalize_host(sockaddr[0]))
        except OSError:
            pass
        try:
            _canonical, aliases, ipv4_values = socket.gethostbyname_ex(hostname)
            addresses.update(_normalize_host(alias) for alias in aliases if alias)
            addresses.update(_normalize_host(value) for value in ipv4_values if value)
        except OSError:
            pass

    # A UDP connect selects the currently active outbound interface without sending
    # application data. This catches machines whose hostname resolves only to loopback.
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        addresses.add(_normalize_host(probe.getsockname()[0]))
    except OSError:
        pass
    finally:
        if probe is not None:
            probe.close()
    return {value for value in addresses if value}


def is_local_request(request: Any) -> bool:
    """Accept only requests that actually originate from the same PC."""
    client = getattr(request, "client", None)
    host = _normalize_host(getattr(client, "host", ""))
    return bool(host and host in _local_host_addresses())


def require_local_request(request: Any) -> None:
    if not is_local_request(request):
        raise PermissionError(
            "API-key management is restricted to the local PC. Open the ranker at "
            "http://127.0.0.1 on the computer running it."
        )


def test_api_key(value: Any = None, *, timeout: float = 12.0) -> Tuple[bool, str]:
    """Validate a supplied or stored token without returning account data."""
    token = _normalized_token(value) if str(value or "").strip() else read_api_key()
    if not token:
        return False, "No NovelAI API key is configured."
    request = urllib.request.Request(
        NOVELAI_SUBSCRIPTION_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "NovelAI-Artist-Ranker/1.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(3.0, float(timeout))) as response:
            status = int(getattr(response, "status", 0) or 0)
            # Read a bounded body so the connection can close cleanly, but never return it.
            response.read(4096)
            if 200 <= status < 300:
                return True, "NovelAI accepted the API key."
            return False, f"NovelAI returned HTTP {status}."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "NovelAI rejected the API key. Create a new persistent token and try again."
        if exc.code == 429:
            return False, "NovelAI rate-limited the test. The key was not changed."
        return False, f"NovelAI returned HTTP {exc.code}; the key was not changed."
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"Could not reach NovelAI to test the key: {type(reason).__name__}."


__all__ = [
    "CredentialStoreError",
    "CredentialStoreUnavailable",
    "CredentialStatus",
    "CREDENTIAL_TARGET",
    "credential_status",
    "delete_api_key",
    "is_configured",
    "is_local_request",
    "read_api_key",
    "require_local_request",
    "save_api_key",
    "test_api_key",
]
