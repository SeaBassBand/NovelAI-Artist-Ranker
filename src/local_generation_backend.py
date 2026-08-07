#!/usr/bin/env python3
"""Opt-in local image-generation backends for the Anima test build.

The production ranker remains NovelAI-first.  This module is deliberately
stdlib-only and inactive unless local_generation.json (or an environment
override) selects ``comfyui`` or ``forge``.
"""
from __future__ import annotations

import argparse
import base64
import copy
import ipaddress
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from websockets.sync.client import connect as websocket_connect
except Exception:  # pragma: no cover - local preview remains optional
    websocket_connect = None


BACKENDS = {"novelai", "comfyui", "forge"}
DEFAULT_CONFIG_NAME = "local_generation.json"
CONFIG_ENV = "ARTIST_RANKER_LOCAL_GENERATION_CONFIG"
BACKEND_ENV = "ARTIST_RANKER_GENERATION_BACKEND"
PreviewCallback = Callable[[bytes, Dict[str, Any]], None]
COMMON_ENDPOINTS = (
    ("comfyui", "http://127.0.0.1:8188"),
    ("comfyui", "http://127.0.0.1:8189"),
    ("comfyui", "http://localhost:8188"),
    ("forge", "http://127.0.0.1:7860"),
    ("forge", "http://127.0.0.1:7861"),
    ("forge", "http://localhost:7860"),
)


class LocalGenerationError(RuntimeError):
    """A configuration, connection, or generation failure."""


def format_anima_artist_name(value: str) -> str:
    """Convert one stored Danbooru artist name to Anima's artist syntax."""
    normalized = " ".join(str(value or "").strip().lstrip("@").replace("_", " ").split())
    return f"@{normalized.casefold()}" if normalized else ""


def format_anima_artist_tags(artists: Iterable[str]) -> str:
    """Format artists for Anima, preserving order and removing duplicates."""
    seen = set()
    result = []
    for artist in artists:
        tag = format_anima_artist_name(artist)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return ", ".join(result)


def _json_file(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise LocalGenerationError(f"Local-generation config does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalGenerationError(f"Could not read local-generation JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LocalGenerationError(f"Local-generation JSON must contain an object: {path}")
    return value


def _normalized_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocalGenerationError(f"Backend URL must be an http(s) URL, got {text!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalGenerationError("Backend URL may not contain credentials, a query, or a fragment")
    return text


def _is_loopback_host(hostname: str) -> bool:
    clean = str(hostname or "").strip().rstrip(".").casefold()
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        pass
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(clean, None)}
    except OSError:
        return False
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_loopback for address in addresses)
    except ValueError:
        return False


def _assert_endpoint_allowed(base_url: str, allow_non_loopback: bool) -> None:
    hostname = urllib.parse.urlparse(base_url).hostname or ""
    if not allow_non_loopback and not _is_loopback_host(hostname):
        raise LocalGenerationError(
            f"Refusing non-loopback generation endpoint {hostname!r}. "
            "Keep the service on this PC or explicitly set allow_non_loopback to true."
        )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LocalGenerationError(f"Backend returned HTTP {exc.code} for {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalGenerationError(f"Could not reach local generation backend at {url}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalGenerationError(f"Backend returned invalid JSON for {url}") from exc
    if not isinstance(value, dict):
        raise LocalGenerationError(f"Backend returned a non-object JSON response for {url}")
    return value


def _http_bytes(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LocalGenerationError(f"Backend returned HTTP {exc.code} for {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalGenerationError(f"Could not download generated image from {url}: {exc}") from exc
    if not data:
        raise LocalGenerationError("Backend returned an empty image")
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class LocalGenerationBackend:
    """Loads one test-build configuration and calls the selected local API."""

    def __init__(self, program_dir: Path, config_path: Optional[Path] = None):
        self.program_dir = Path(program_dir).resolve()
        explicit = str(os.environ.get(CONFIG_ENV, "") or "").strip()
        self.config_path = Path(config_path or explicit or (self.program_dir / DEFAULT_CONFIG_NAME)).resolve()
        self.config: Dict[str, Any] = {}
        self.backend = "novelai"
        self.allow_non_loopback = False
        self.timeout = 600.0
        self.poll_interval = 0.5
        self.reload()

    def reload(self) -> None:
        """Reload connection settings without replacing the backend object."""
        self.config = _json_file(self.config_path) if self.config_path.exists() else {}
        configured = str(os.environ.get(BACKEND_ENV, "") or self.config.get("backend", "novelai"))
        self.backend = configured.strip().casefold() or "novelai"
        if self.backend not in BACKENDS:
            raise LocalGenerationError(
                f"Unsupported generation backend {self.backend!r}; choose novelai, comfyui, or forge"
            )
        self.allow_non_loopback = bool(self.config.get("allow_non_loopback", False))
        self.timeout = max(2.0, float(self.config.get("request_timeout_seconds", 600)))
        self.poll_interval = min(10.0, max(0.1, float(self.config.get("poll_interval_seconds", 0.5))))

    def connection_payload(self) -> Dict[str, Any]:
        """Return non-secret connection state for local configuration UIs."""
        sections: Dict[str, Dict[str, Any]] = {}
        for backend, default in (("comfyui", "http://127.0.0.1:8188"), ("forge", "http://127.0.0.1:7861")):
            raw = self.config.get(backend, {})
            raw = raw if isinstance(raw, dict) else {}
            sections[backend] = {"base_url": str(raw.get("base_url", default) or default)}
        return {
            "backend": self.backend,
            "label": self.display_name,
            "local": self.is_local,
            "allow_non_loopback": bool(self.allow_non_loopback),
            "config_path": str(self.config_path),
            "connections": sections,
        }

    def save_connection(self, backend: str, base_url: str, allow_non_loopback: bool = False) -> Dict[str, Any]:
        """Persist a validated ComfyUI/Forge endpoint while preserving workflow settings."""
        selected = str(backend or "").strip().casefold()
        if selected not in {"comfyui", "forge"}:
            raise LocalGenerationError("Choose ComfyUI or Forge / Neo Forge")
        normalized = _normalized_base_url(base_url)
        _assert_endpoint_allowed(normalized, bool(allow_non_loopback))
        config = copy.deepcopy(self.config)
        section = config.get(selected)
        if not isinstance(section, dict):
            section = {}
        section["base_url"] = normalized
        config[selected] = section
        config["backend"] = selected
        config["allow_non_loopback"] = bool(allow_non_loopback)
        _atomic_write(self.config_path, (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        os.environ.pop(BACKEND_ENV, None)
        self.reload()
        return self.connection_payload()

    @staticmethod
    def _probe_endpoint(backend: str, base_url: str, timeout: float = 1.2) -> Optional[Dict[str, Any]]:
        route = "/system_stats" if backend == "comfyui" else "/sdapi/v1/options"
        try:
            payload = _http_json(base_url + route, timeout=timeout)
        except LocalGenerationError:
            return None
        detail = "ComfyUI system API" if backend == "comfyui" else "Forge-compatible WebUI API"
        if backend == "forge":
            checkpoint = str(payload.get("sd_model_checkpoint", "") or "").strip()
            if checkpoint:
                detail += f" · {checkpoint}"
        return {"backend": backend, "label": "ComfyUI" if backend == "comfyui" else "Forge / Neo Forge", "base_url": base_url, "detail": detail}

    def scan_endpoints(self) -> List[Dict[str, Any]]:
        """Probe common local diffuser ports concurrently for one-click setup."""
        candidates = list(COMMON_ENDPOINTS)
        configured = self.connection_payload().get("connections", {})
        for backend in ("comfyui", "forge"):
            base_url = str(configured.get(backend, {}).get("base_url", "") or "")
            if base_url:
                candidates.append((backend, base_url.rstrip("/")))
        candidates = list(dict.fromkeys(candidates))
        found: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            futures = {pool.submit(self._probe_endpoint, backend, url): (backend, url) for backend, url in candidates}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    found.append(result)
        found.sort(key=lambda item: (item["backend"], item["base_url"]))
        return found

    @property
    def is_local(self) -> bool:
        return self.backend in {"comfyui", "forge"}

    @property
    def display_name(self) -> str:
        return {"novelai": "NovelAI", "comfyui": "ComfyUI", "forge": "Forge / Forge Neo"}[self.backend]

    @property
    def configured_cfg_scale(self) -> float:
        """Return the backend's existing CFG as the migration-safe profile default."""
        if not self.is_local:
            return 6.0
        section = self._section()
        key = "cfg" if self.backend == "comfyui" else "cfg_scale"
        try:
            return round(max(0.0, min(10.0, float(section.get(key, 4.5)))), 1)
        except (TypeError, ValueError):
            return 4.5

    def _section(self) -> Dict[str, Any]:
        value = self.config.get(self.backend, {})
        if not isinstance(value, dict):
            raise LocalGenerationError(f"The {self.backend} configuration must be a JSON object")
        return value

    def _base_url(self) -> str:
        defaults = {"comfyui": "http://127.0.0.1:8188", "forge": "http://127.0.0.1:7861"}
        base_url = _normalized_base_url(self._section().get("base_url", defaults.get(self.backend)))
        _assert_endpoint_allowed(base_url, self.allow_non_loopback)
        return base_url

    def healthcheck(self, backend: Optional[str] = None, base_url: Optional[str] = None, allow_non_loopback: Optional[bool] = None) -> str:
        selected = str(backend or self.backend).strip().casefold()
        if selected == "novelai":
            return "NovelAI selected; local backend healthcheck is not applicable."
        if selected not in {"comfyui", "forge"}:
            raise LocalGenerationError("Unsupported local generation backend")
        target = _normalized_base_url(base_url) if base_url else (
            self._base_url() if selected == self.backend else _normalized_base_url(
                (self.config.get(selected, {}) if isinstance(self.config.get(selected), dict) else {}).get(
                    "base_url", "http://127.0.0.1:8188" if selected == "comfyui" else "http://127.0.0.1:7861"
                )
            )
        )
        allowed = self.allow_non_loopback if allow_non_loopback is None else bool(allow_non_loopback)
        _assert_endpoint_allowed(target, allowed)
        route = "/system_stats" if selected == "comfyui" else "/sdapi/v1/options"
        _http_json(target + route, timeout=min(self.timeout, 10.0))
        label = "ComfyUI" if selected == "comfyui" else "Forge / Neo Forge"
        return f"{label} is reachable at {target}."

    def format_artist_tags(self, artists: Iterable[str]) -> str:
        if self.is_local:
            return format_anima_artist_tags(artists)
        return ", ".join(f"artist: {artist}" for artist in artists)

    def generate(
        self,
        *,
        prompt: str,
        output_path: Path,
        negative_prompt: str,
        seed: Optional[int],
        width: int,
        height: int,
        steps: int,
        cfg_scale: float,
        sampler: str,
        scheduler: str,
        preview_callback: Optional[PreviewCallback] = None,
    ) -> None:
        if self.backend == "comfyui":
            self._generate_comfyui(
                prompt=prompt, output_path=output_path, negative_prompt=negative_prompt,
                seed=seed, width=width, height=height, steps=steps,
                cfg_scale=cfg_scale,
                sampler=sampler, scheduler=scheduler,
                preview_callback=preview_callback,
            )
            return
        if self.backend == "forge":
            self._generate_forge(
                prompt=prompt, output_path=output_path, negative_prompt=negative_prompt,
                seed=seed, width=width, height=height, steps=steps,
                cfg_scale=cfg_scale,
                sampler=sampler, scheduler=scheduler,
                preview_callback=preview_callback,
            )
            return
        raise LocalGenerationError("Local generation was requested while NovelAI is selected")

    def _workflow_path(self, section: Dict[str, Any]) -> Path:
        text = str(section.get("workflow_api_file", "") or "").strip()
        if not text:
            raise LocalGenerationError(
                "ComfyUI requires workflow_api_file. Export the loaded Anima workflow with File -> Export (API)."
            )
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self.config_path.parent / candidate
        return candidate.resolve()

    @staticmethod
    def _bind(workflow: Dict[str, Any], binding: Any, value: Any, label: str, *, required: bool = False) -> None:
        if not binding:
            if required:
                raise LocalGenerationError(f"ComfyUI binding {label!r} is required")
            return
        if not isinstance(binding, dict):
            raise LocalGenerationError(f"ComfyUI binding {label!r} must be an object")
        node_id = str(binding.get("node", "") or "").strip()
        input_name = str(binding.get("input", "") or "").strip()
        node = workflow.get(node_id)
        if not node_id or not input_name or not isinstance(node, dict):
            raise LocalGenerationError(f"ComfyUI binding {label!r} points to a missing node or input")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise LocalGenerationError(f"ComfyUI node {node_id!r} has no inputs object")
        inputs[input_name] = value

    def _generate_comfyui(
        self, *, prompt: str, output_path: Path, negative_prompt: str, seed: Optional[int],
        width: int, height: int, steps: int, cfg_scale: float, sampler: str, scheduler: str,
        preview_callback: Optional[PreviewCallback] = None,
    ) -> None:
        section = self._section()
        base_url = self._base_url()
        workflow = copy.deepcopy(_json_file(self._workflow_path(section)))
        bindings = section.get("bindings", {})
        if not isinstance(bindings, dict):
            raise LocalGenerationError("ComfyUI bindings must be a JSON object")
        values = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt,
            "seed": int(seed if seed is not None else uuid.uuid4().int & 0xFFFFFFFF),
            "width": int(width),
            "height": int(height),
            "steps": int(section.get("steps", steps)),
            "cfg": float(cfg_scale),
            "sampler": str(section.get("sampler_name", sampler)),
            "scheduler": str(section.get("scheduler", scheduler)),
        }
        for label, value in values.items():
            self._bind(
                workflow, bindings.get(label), value, label,
                required=label in {"positive_prompt", "negative_prompt", "seed"},
            )
        client_id = uuid.uuid4().hex
        payload: Dict[str, Any] = {"prompt": workflow, "client_id": client_id}
        extra_data = section.get("extra_data")
        if isinstance(extra_data, dict) and extra_data:
            payload["extra_data"] = extra_data
        websocket = None
        if preview_callback is not None and websocket_connect is not None:
            parsed = urllib.parse.urlsplit(base_url)
            websocket_url = urllib.parse.urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, "/ws", f"clientId={client_id}", ""))
            try:
                websocket = websocket_connect(websocket_url, open_timeout=min(self.timeout, 10.0), close_timeout=2.0)
            except Exception:
                websocket = None
        queued = _http_json(base_url + "/prompt", method="POST", payload=payload, timeout=min(self.timeout, 30.0))
        prompt_id = str(queued.get("prompt_id", "") or "").strip()
        if not prompt_id:
            errors = queued.get("node_errors") or queued.get("error") or queued
            raise LocalGenerationError(f"ComfyUI did not accept the workflow: {errors}")

        deadline = time.monotonic() + self.timeout
        history_item: Optional[Dict[str, Any]] = None
        if websocket is not None:
            try:
                while time.monotonic() < deadline:
                    remaining = max(0.1, min(5.0, deadline - time.monotonic()))
                    try:
                        message = websocket.recv(timeout=remaining)
                    except TimeoutError:
                        # Model loading can be quiet for minutes on lower-VRAM
                        # systems.  A receive timeout is only a polling tick;
                        # keep the socket alive for the sampler's later frames.
                        continue
                    if isinstance(message, bytes):
                        if len(message) > 8:
                            event_number = struct.unpack(">I", message[:4])[0]
                            image_bytes = b""
                            if event_number == 1:  # PREVIEW_IMAGE: event + format + image
                                image_bytes = message[8:]
                            elif event_number == 4:  # PREVIEW_IMAGE_WITH_METADATA
                                metadata_size = struct.unpack(">I", message[4:8])[0]
                                image_start = 8 + metadata_size
                                if image_start < len(message):
                                    image_bytes = message[image_start:]
                            if image_bytes:
                                preview_callback(image_bytes, {"backend": "comfyui", "prompt_id": prompt_id, "kind": "preview"})
                        continue
                    event = json.loads(message)
                    event_type = str(event.get("type", "") or "")
                    data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
                    if str(data.get("prompt_id", "") or "") != prompt_id:
                        continue
                    if event_type == "progress":
                        value = int(data.get("value", 0) or 0)
                        maximum = int(data.get("max", 0) or 0)
                        preview_callback(b"", {"backend": "comfyui", "prompt_id": prompt_id, "kind": "progress", "value": value, "max": maximum, "progress": (value / maximum if maximum else 0.0)})
                    elif event_type == "execution_error":
                        raise LocalGenerationError(f"ComfyUI workflow failed: {data}")
                    elif event_type == "executing" and data.get("node") is None:
                        break
            except LocalGenerationError:
                raise
            except Exception:
                # Preview transport is best-effort; generation completion still uses history.
                pass
            finally:
                try:
                    websocket.close()
                except Exception:
                    pass
        while time.monotonic() < deadline:
            history = _http_json(
                base_url + "/history/" + urllib.parse.quote(prompt_id, safe=""),
                timeout=min(30.0, max(1.0, deadline - time.monotonic())),
            )
            candidate = history.get(prompt_id, history if "outputs" in history else None)
            if isinstance(candidate, dict):
                status = candidate.get("status", {})
                if isinstance(status, dict) and status.get("completed") is False:
                    messages = status.get("messages") or []
                    if any(isinstance(item, list) and item and item[0] == "execution_error" for item in messages):
                        raise LocalGenerationError(f"ComfyUI workflow failed: {messages}")
                outputs = candidate.get("outputs")
                if isinstance(outputs, dict) and outputs:
                    history_item = candidate
                    break
            time.sleep(self.poll_interval)
        if history_item is None:
            raise LocalGenerationError(f"ComfyUI timed out after {self.timeout:.0f} seconds")

        outputs = history_item.get("outputs", {})
        output_node = str(section.get("output_node", "") or "").strip()
        output_values = [outputs.get(output_node)] if output_node else list(outputs.values())
        image_info = None
        for output in output_values:
            if not isinstance(output, dict):
                continue
            images = output.get("images")
            if isinstance(images, list) and images and isinstance(images[0], dict):
                image_info = images[0]
                break
        if image_info is None:
            raise LocalGenerationError("ComfyUI completed but did not report an image output")
        query = urllib.parse.urlencode({
            "filename": str(image_info.get("filename", "")),
            "subfolder": str(image_info.get("subfolder", "")),
            "type": str(image_info.get("type", "output")),
        })
        _atomic_write(output_path, _http_bytes(base_url + "/view?" + query, timeout=min(self.timeout, 60.0)))

    def _generate_forge(
        self, *, prompt: str, output_path: Path, negative_prompt: str, seed: Optional[int],
        width: int, height: int, steps: int, cfg_scale: float, sampler: str, scheduler: str,
        preview_callback: Optional[PreviewCallback] = None,
    ) -> None:
        section = self._section()
        base_url = self._base_url()
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seed": int(seed if seed is not None else -1),
            "width": int(width),
            "height": int(height),
            "steps": int(section.get("steps", steps)),
            "cfg_scale": float(cfg_scale),
            "batch_size": 1,
            "n_iter": 1,
        }
        sampler_name = str(section.get("sampler_name", "") or "").strip()
        if sampler_name:
            payload["sampler_name"] = sampler_name
        selected_scheduler = str(section.get("scheduler", "") or "").strip()
        if selected_scheduler:
            payload["scheduler"] = selected_scheduler
        overrides = section.get("override_settings")
        if isinstance(overrides, dict) and overrides:
            payload["override_settings"] = overrides
            payload["override_settings_restore_afterwards"] = True
        stop_preview = threading.Event()
        preview_thread: Optional[threading.Thread] = None
        if preview_callback is not None:
            def poll_preview() -> None:
                last_image = ""
                while not stop_preview.wait(max(0.25, self.poll_interval)):
                    try:
                        progress = _http_json(base_url + "/sdapi/v1/progress?skip_current_image=false", timeout=3.0)
                        encoded_preview = str(progress.get("current_image", "") or "")
                        metadata = {
                            "backend": "forge", "kind": "preview" if encoded_preview else "progress",
                            "progress": float(progress.get("progress", 0.0) or 0.0),
                            "eta_relative": float(progress.get("eta_relative", 0.0) or 0.0),
                        }
                        if encoded_preview and encoded_preview != last_image:
                            last_image = encoded_preview
                            if "," in encoded_preview and encoded_preview.lstrip().startswith("data:"):
                                encoded_preview = encoded_preview.split(",", 1)[1]
                            preview_callback(base64.b64decode(encoded_preview), metadata)
                        else:
                            preview_callback(b"", metadata)
                    except Exception:
                        continue
            preview_thread = threading.Thread(target=poll_preview, daemon=True, name="forge-preview-poller")
            preview_thread.start()
        try:
            response = _http_json(
                base_url + "/sdapi/v1/txt2img", method="POST", payload=payload, timeout=self.timeout
            )
        finally:
            stop_preview.set()
            if preview_thread is not None:
                preview_thread.join(timeout=2.0)
        images = response.get("images")
        if not isinstance(images, list) or not images:
            raise LocalGenerationError("Forge completed without returning an image")
        encoded = str(images[0] or "")
        if "," in encoded and encoded.lstrip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LocalGenerationError("Forge returned invalid base64 image data") from exc
        if not image_bytes:
            raise LocalGenerationError("Forge returned an empty image")
        _atomic_write(output_path, image_bytes)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the ranker's local generation backend")
    parser.add_argument("--config", type=Path, required=True, help="Path to local_generation.json")
    parser.add_argument("--check", action="store_true", help="Check that the configured API is reachable")
    args = parser.parse_args()
    backend = LocalGenerationBackend(args.config.parent, args.config)
    print(f"Backend: {backend.display_name}")
    print(f"Config:  {backend.config_path}")
    if args.check:
        print(backend.healthcheck())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
