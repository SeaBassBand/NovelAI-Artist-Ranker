#!/usr/bin/env python3
"""Secure LAN pairing, device authorization, and lightweight mDNS advertising.

The NovelAI credential is deliberately outside this module. Pairing tokens authorize
only the ranker's LAN UI/API and are stored as SHA-256 hashes after issuance.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PAIRING_SCHEMA_VERSION = 1
PAIRING_PROTOCOL_VERSION = 1
PAIRING_PROTOCOL_MIN = 1
PAIRING_PROTOCOL_MAX = 1
PAIRING_COOKIE_NAME = "artist_ranker_pair"
PAIRING_TOKEN_PREFIX = "ar1"
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_TTL_SECONDS = 10 * 60
DEFAULT_CLIENT_MODE = "single_active"
CLIENT_MODES = {"single_active", "multi_client"}
WRITE_LEASE_SECONDS = 35
SERVICE_TYPE = "_artist-ranker._tcp.local."
SERVICE_ENUMERATION = "_services._dns-sd._udp.local."
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353


def _atomic_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clean_device_name(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return (text[:80] or "Paired device")


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _constant_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(str(left), str(right))
    except Exception:
        return False


def request_client_ip(request: Any) -> str:
    try:
        client = getattr(request, "client", None)
        host = str(getattr(client, "host", "") or "").strip()
        if host:
            return host
    except Exception:
        pass
    return ""


def is_private_client_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
        return bool(address.is_private or address.is_loopback or address.is_link_local)
    except Exception:
        return False


def request_token(request: Any) -> str:
    try:
        header = str(request.headers.get("authorization", "") or "").strip()
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        alternate = str(request.headers.get("x-artist-ranker-token", "") or "").strip()
        if alternate:
            return alternate
    except Exception:
        pass
    try:
        cookie = str(request.cookies.get(PAIRING_COOKIE_NAME, "") or "").strip()
        if cookie:
            return cookie
    except Exception:
        pass
    try:
        return str(request.query_params.get("pair_token", "") or "").strip()
    except Exception:
        return ""


@dataclass(frozen=True)
class PairingAuth:
    authorized: bool
    local: bool = False
    device_id: str = ""
    device_name: str = ""
    reason: str = ""


class PairingManager:
    """Persistent paired-device registry with ephemeral one-time pairing codes."""

    def __init__(
        self,
        state_file: Path,
        *,
        server_name: str,
        server_version: str,
        port: int,
    ) -> None:
        self.state_file = Path(state_file)
        self.server_name = _clean_device_name(server_name or "Artist Ranker")
        self.server_version = str(server_version or "unknown")
        self.port = int(port)
        self.lock = threading.RLock()
        self._codes: Dict[str, dict] = {}
        self._write_lease: dict = {"device_id": "", "device_name": "", "expires_at": 0.0}
        self.state = self._load_or_create()

    def _default_state(self) -> dict:
        return {
            "schema_version": PAIRING_SCHEMA_VERSION,
            "installation_id": secrets.token_hex(8),
            "server_secret": secrets.token_urlsafe(32),
            "client_mode": DEFAULT_CLIENT_MODE,
            "devices": {},
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def _load_or_create(self) -> dict:
        data: dict = {}
        if self.state_file.is_file():
            try:
                loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        default = self._default_state()
        for key, value in default.items():
            data.setdefault(key, value)
        if not isinstance(data.get("devices"), dict):
            data["devices"] = {}
        if str(data.get("client_mode", "")) not in CLIENT_MODES:
            data["client_mode"] = DEFAULT_CLIENT_MODE
        data["schema_version"] = PAIRING_SCHEMA_VERSION
        self.state = data
        self._save_locked()
        return data

    def _save_locked(self) -> None:
        self.state["updated_at"] = time.time()
        _atomic_json(self.state_file, self.state)

    @property
    def installation_id(self) -> str:
        with self.lock:
            return str(self.state.get("installation_id", ""))

    @property
    def client_mode(self) -> str:
        with self.lock:
            mode = str(self.state.get("client_mode", DEFAULT_CLIENT_MODE))
            return mode if mode in CLIENT_MODES else DEFAULT_CLIENT_MODE

    def set_client_mode(self, mode: str) -> str:
        mode = str(mode or "").strip()
        if mode not in CLIENT_MODES:
            raise ValueError("Unsupported client mode.")
        with self.lock:
            self.state["client_mode"] = mode
            self._write_lease = {"device_id": "", "device_name": "", "expires_at": 0.0}
            self._save_locked()
        return mode

    def _purge_codes_locked(self) -> None:
        now = time.time()
        expired = [code for code, item in self._codes.items() if float(item.get("expires_at", 0.0)) <= now]
        for code in expired:
            self._codes.pop(code, None)

    def issue_code(self, base_urls: Iterable[str], ttl_seconds: int = PAIRING_CODE_TTL_SECONDS) -> dict:
        urls = [str(url).rstrip("/") for url in base_urls if str(url).strip()]
        ttl = max(60, min(3600, int(ttl_seconds or PAIRING_CODE_TTL_SECONDS)))
        with self.lock:
            self._purge_codes_locked()
            for _ in range(30):
                code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(6))
                if code not in self._codes:
                    break
            else:
                raise RuntimeError("Could not allocate a pairing code.")
            expires_at = time.time() + ttl
            item = {
                "code": code,
                "created_at": time.time(),
                "expires_at": expires_at,
                "uses_left": 1,
                "base_urls": urls,
            }
            self._codes[code] = item
        links = [f"{url}/duel?pair={code}" for url in urls]
        return {**item, "links": links, "primary_link": links[0] if links else ""}

    def active_codes(self) -> List[dict]:
        with self.lock:
            self._purge_codes_locked()
            values = []
            for raw in sorted(self._codes.values(), key=lambda value: value["expires_at"]):
                item = dict(raw)
                urls = [str(url).rstrip("/") for url in item.get("base_urls", []) if str(url).strip()]
                links = [f"{url}/duel?pair={item['code']}" for url in urls]
                item["links"] = links
                item["primary_link"] = links[0] if links else ""
                values.append(item)
            return values

    def exchange_code(self, code: str, device_name: str, client_protocol: int) -> Tuple[str, dict]:
        normalized = str(code or "").strip().upper().replace("-", "")
        protocol = int(client_protocol or 0)
        if protocol < PAIRING_PROTOCOL_MIN:
            raise ValueError("App update required: this app protocol is too old for the server.")
        if protocol > PAIRING_PROTOCOL_MAX:
            raise ValueError("Server update required: this app protocol is newer than the server supports.")
        with self.lock:
            self._purge_codes_locked()
            item = self._codes.get(normalized)
            if not item or int(item.get("uses_left", 0)) < 1:
                raise ValueError("That pairing code is invalid, expired, or already used.")
            item["uses_left"] = 0
            self._codes.pop(normalized, None)
            device_id = secrets.token_hex(8)
            raw_token = f"{PAIRING_TOKEN_PREFIX}_{device_id}_{secrets.token_urlsafe(32)}"
            now = time.time()
            device = {
                "device_id": device_id,
                "name": _clean_device_name(device_name),
                "token_hash": _token_hash(raw_token),
                "created_at": now,
                "last_seen_at": now,
                "last_ip": "",
                "client_protocol": protocol,
                "revoked": False,
            }
            self.state["devices"][device_id] = device
            self._save_locked()
            public = self._public_device(device)
        return raw_token, public

    @staticmethod
    def _public_device(device: dict) -> dict:
        return {
            "device_id": str(device.get("device_id", "")),
            "name": str(device.get("name", "Paired device")),
            "created_at": float(device.get("created_at", 0.0) or 0.0),
            "last_seen_at": float(device.get("last_seen_at", 0.0) or 0.0),
            "last_ip": str(device.get("last_ip", "") or ""),
            "client_protocol": int(device.get("client_protocol", 0) or 0),
            "revoked": bool(device.get("revoked", False)),
        }

    def validate_token(self, token: str, *, client_ip: str = "", touch: bool = True) -> Optional[dict]:
        raw = str(token or "").strip()
        if not raw.startswith(PAIRING_TOKEN_PREFIX + "_"):
            return None
        parts = raw.split("_", 2)
        if len(parts) != 3:
            return None
        device_id = parts[1]
        candidate_hash = _token_hash(raw)
        with self.lock:
            device = self.state.get("devices", {}).get(device_id)
            if not isinstance(device, dict) or bool(device.get("revoked", False)):
                return None
            if not _constant_equal(candidate_hash, str(device.get("token_hash", ""))):
                return None
            if touch:
                now = time.time()
                previous = float(device.get("last_seen_at", 0.0) or 0.0)
                ip_changed = bool(client_ip and client_ip != str(device.get("last_ip", "")))
                if now - previous >= 30 or ip_changed:
                    device["last_seen_at"] = now
                    if client_ip:
                        device["last_ip"] = str(client_ip)[:64]
                    self._save_locked()
            return self._public_device(device)

    def authorize_request(self, request: Any, *, local: bool = False) -> PairingAuth:
        if local:
            return PairingAuth(True, local=True, device_id="local", device_name="Local PC")
        token = request_token(request)
        device = self.validate_token(token, client_ip=request_client_ip(request))
        if not device:
            return PairingAuth(False, reason="Pair this device with the ranker PC before continuing.")
        return PairingAuth(
            True,
            local=False,
            device_id=str(device.get("device_id", "")),
            device_name=str(device.get("name", "Paired device")),
        )

    def list_devices(self) -> List[dict]:
        with self.lock:
            devices = [self._public_device(value) for value in self.state.get("devices", {}).values() if isinstance(value, dict)]
        return sorted(devices, key=lambda item: (item["revoked"], -item["last_seen_at"], item["name"].casefold()))

    def revoke_device(self, device_id: str) -> bool:
        with self.lock:
            device = self.state.get("devices", {}).get(str(device_id or ""))
            if not isinstance(device, dict):
                return False
            device["revoked"] = True
            device["token_hash"] = ""
            if self._write_lease.get("device_id") == str(device_id):
                self._write_lease = {"device_id": "", "device_name": "", "expires_at": 0.0}
            self._save_locked()
            return True

    def revoke_all(self) -> int:
        count = 0
        with self.lock:
            for device in self.state.get("devices", {}).values():
                if isinstance(device, dict) and not bool(device.get("revoked", False)):
                    device["revoked"] = True
                    device["token_hash"] = ""
                    count += 1
            self._write_lease = {"device_id": "", "device_name": "", "expires_at": 0.0}
            self._save_locked()
        return count

    def check_write_access(self, auth: PairingAuth) -> Tuple[bool, str]:
        if auth.local or self.client_mode == "multi_client":
            return True, ""
        now = time.time()
        with self.lock:
            lease_id = str(self._write_lease.get("device_id", ""))
            expires = float(self._write_lease.get("expires_at", 0.0) or 0.0)
            if not lease_id or expires <= now or lease_id == auth.device_id:
                self._write_lease = {
                    "device_id": auth.device_id,
                    "device_name": auth.device_name,
                    "expires_at": now + WRITE_LEASE_SECONDS,
                }
                return True, ""
            remaining = max(1, int(round(expires - now)))
            holder = str(self._write_lease.get("device_name", "another paired device"))
            return False, f"{holder} currently has the voting lease. Try again in about {remaining} seconds."

    def lease_status(self) -> dict:
        with self.lock:
            lease = dict(self._write_lease)
        if float(lease.get("expires_at", 0.0) or 0.0) <= time.time():
            return {"active": False, "device_id": "", "device_name": "", "expires_at": 0.0}
        lease["active"] = True
        return lease

    def reset_pairing_identity(self) -> int:
        with self.lock:
            count = len([1 for value in self.state.get("devices", {}).values() if isinstance(value, dict) and not value.get("revoked")])
            replacement = self._default_state()
            replacement["client_mode"] = self.client_mode
            self.state = replacement
            self._codes.clear()
            self._write_lease = {"device_id": "", "device_name": "", "expires_at": 0.0}
            self._save_locked()
        return count

    def handshake(self, client_protocol: Optional[int] = None) -> dict:
        protocol = int(client_protocol or 0) if client_protocol is not None else None
        status = "compatible"
        message = "Compatible."
        compatible = True
        if protocol is not None and protocol < PAIRING_PROTOCOL_MIN:
            status, message, compatible = "app_update_required", "App update required.", False
        elif protocol is not None and protocol > PAIRING_PROTOCOL_MAX:
            status, message, compatible = "server_update_required", "Server update required.", False
        return {
            "service": "NovelAI Artist Ranker",
            "server_name": self.server_name,
            "installation_id": self.installation_id,
            "server_version": self.server_version,
            "port": self.port,
            "duel_path": "/duel",
            "protocol_current": PAIRING_PROTOCOL_VERSION,
            "protocol_min": PAIRING_PROTOCOL_MIN,
            "protocol_max": PAIRING_PROTOCOL_MAX,
            "compatible": compatible,
            "compatibility_status": status,
            "compatibility_message": message,
            "client_mode": self.client_mode,
            "paired_device_count": len([d for d in self.list_devices() if not d["revoked"]]),
        }


# --- Lightweight DNS-SD / mDNS advertisement ---------------------------------

def _dns_name(name: str) -> bytes:
    labels = [label.encode("utf-8") for label in str(name).rstrip(".").split(".") if label]
    return b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"


def _dns_record(name: str, rtype: int, data: bytes, ttl: int = 120, cache_flush: bool = False) -> bytes:
    rrclass = 0x8001 if cache_flush else 1
    return _dns_name(name) + struct.pack("!HHIH", rtype, rrclass, int(ttl), len(data)) + data


def _lan_ipv4_addresses() -> List[str]:
    values = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            value = str(info[4][0] or "")
            if value and is_private_client_ip(value) and not value.startswith("127.") and not value.startswith("169.254."):
                values.add(value)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        value = str(probe.getsockname()[0] or "")
        probe.close()
        if value and is_private_client_ip(value) and not value.startswith("127."):
            values.add(value)
    except OSError:
        pass
    return sorted(values)


class RankerMDNSAdvertiser:
    """Dependency-free DNS-SD announcement compatible with Android NSD discovery."""

    def __init__(self, manager: PairingManager) -> None:
        self.manager = manager
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.last_error = ""
        self.running = False
        self.announcements = 0

    def _packet(self) -> bytes:
        host_base = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in socket.gethostname()).strip("-") or "artist-ranker"
        host = f"{host_base}.local."
        instance = f"{self.manager.server_name}.{SERVICE_TYPE}"
        addresses = _lan_ipv4_addresses()
        txt_values = [
            f"id={self.manager.installation_id}",
            f"protocol={PAIRING_PROTOCOL_VERSION}",
            f"version={self.manager.server_version}",
            "path=/duel",
            f"mode={self.manager.client_mode}",
        ]
        txt = b"".join(bytes([min(255, len(value.encode("utf-8")))]) + value.encode("utf-8")[:255] for value in txt_values)
        records = [
            _dns_record(SERVICE_ENUMERATION, 12, _dns_name(SERVICE_TYPE)),
            _dns_record(SERVICE_TYPE, 12, _dns_name(instance)),
            _dns_record(instance, 33, struct.pack("!HHH", 0, 0, self.manager.port) + _dns_name(host), cache_flush=True),
            _dns_record(instance, 16, txt, cache_flush=True),
        ]
        for address in addresses:
            records.append(_dns_record(host, 1, socket.inet_aton(address), cache_flush=True))
        header = struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0)
        return header + b"".join(records)

    def _send(self, sock: socket.socket) -> None:
        sock.sendto(self._packet(), (MDNS_GROUP, MDNS_PORT))
        self.announcements += 1

    def _run(self) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", MDNS_PORT))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            try:
                membership = struct.pack("=4sl", socket.inet_aton(MDNS_GROUP), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            except OSError:
                pass
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.settimeout(1.0)
            self.running = True
            self._send(sock)
            next_announce = time.monotonic() + 30.0
            service_bytes = SERVICE_TYPE.rstrip(".").encode("utf-8").lower()
            enumeration_bytes = SERVICE_ENUMERATION.rstrip(".").encode("utf-8").lower()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_announce:
                    self._send(sock)
                    next_announce = now + 30.0
                try:
                    data, _peer = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                lowered = bytes(data).lower()
                if service_bytes in lowered or enumeration_bytes in lowered:
                    try:
                        self._send(sock)
                    except OSError:
                        pass
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def start(self) -> "RankerMDNSAdvertiser":
        if self.thread and self.thread.is_alive():
            return self
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="artist-ranker-mdns", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()

    def status(self) -> dict:
        return {
            "running": bool(self.running),
            "last_error": self.last_error,
            "announcements": int(self.announcements),
            "service_type": SERVICE_TYPE,
            "addresses": _lan_ipv4_addresses(),
            "port": self.manager.port,
        }
