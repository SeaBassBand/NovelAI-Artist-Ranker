#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import local_generation_backend as backend_module  # noqa: E402
from local_generation_backend import (  # noqa: E402
    BACKEND_ENV,
    CONFIG_ENV,
    LocalGenerationBackend,
    LocalGenerationError,
    format_anima_artist_tags,
)


IMAGE_BYTES = b"\x89PNG\r\n\x1a\nlocal-backend-test"


class FakeBackendHandler(BaseHTTPRequestHandler):
    queued_payload = None
    forge_payload = None
    delay_forge = False

    def log_message(self, _format, *_args):
        return

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/system_stats":
            self._send_json({"system": {"os": "test"}})
        elif self.path == "/sdapi/v1/options":
            self._send_json({"sd_model_checkpoint": "anima-test.safetensors"})
        elif self.path.startswith("/sdapi/v1/progress"):
            self._send_json({
                "progress": 0.5,
                "eta_relative": 1.0,
                "current_image": base64.b64encode(IMAGE_BYTES).decode("ascii"),
            })
        elif self.path == "/history/prompt-1":
            self._send_json({
                "prompt-1": {
                    "status": {"completed": True, "messages": []},
                    "outputs": {
                        "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
                    },
                }
            })
        elif self.path.startswith("/view?"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(IMAGE_BYTES)))
            self.end_headers()
            self.wfile.write(IMAGE_BYTES)
        else:
            self._send_json({"error": self.path}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/prompt":
            type(self).queued_payload = payload
            self._send_json({"prompt_id": "prompt-1", "number": 1, "node_errors": {}})
        elif self.path == "/sdapi/v1/txt2img":
            type(self).forge_payload = payload
            if type(self).delay_forge:
                time.sleep(0.3)
            self._send_json({"images": [base64.b64encode(IMAGE_BYTES).decode("ascii")], "info": "{}"})
        else:
            self._send_json({"error": self.path}, status=404)


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in (BACKEND_ENV, CONFIG_ENV)}
        os.environ.pop(BACKEND_ENV, None)
        os.environ.pop(CONFIG_ENV, None)
        FakeBackendHandler.queued_payload = None
        FakeBackendHandler.forge_payload = None
        FakeBackendHandler.delay_forge = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackendHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_json(self, root: Path, name: str, value) -> Path:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_anima_artist_syntax(self):
        self.assertEqual(
            format_anima_artist_tags(["John_Doe", "@Already Here", "john_doe", ""]),
            "@john doe, @already here",
        )

    def test_comfyui_workflow_binding_queue_poll_and_download(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            workflow = {
                "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 1, "cfg": 1.0}},
                "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
                "6": {"class_type": "TextEncode", "inputs": {"text": "positive"}},
                "7": {"class_type": "TextEncode", "inputs": {"text": "negative"}},
                "9": {"class_type": "SaveImage", "inputs": {}},
            }
            self._write_json(root, "workflow.json", workflow)
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "comfyui",
                "request_timeout_seconds": 5,
                "poll_interval_seconds": 0.01,
                "comfyui": {
                    "base_url": self.base_url,
                    "workflow_api_file": "workflow.json",
                    "output_node": "9",
                    "cfg": 4.5,
                    "bindings": {
                        "positive_prompt": {"node": "6", "input": "text"},
                        "negative_prompt": {"node": "7", "input": "text"},
                        "seed": {"node": "3", "input": "seed"},
                        "steps": {"node": "3", "input": "steps"},
                        "cfg": {"node": "3", "input": "cfg"},
                        "width": {"node": "5", "input": "width"},
                        "height": {"node": "5", "input": "height"},
                    },
                },
            })
            backend = LocalGenerationBackend(root, config_path)
            self.assertIn("reachable", backend.healthcheck())
            output = root / "out.png"
            backend.generate(
                prompt="masterpiece, @test artist", output_path=output,
                negative_prompt="worst quality", seed=42, width=768, height=1024,
                steps=36, cfg_scale=2.2, sampler="ignored", scheduler="ignored",
            )
            self.assertEqual(output.read_bytes(), IMAGE_BYTES)
            queued = FakeBackendHandler.queued_payload["prompt"]
            self.assertEqual(queued["6"]["inputs"]["text"], "masterpiece, @test artist")
            self.assertEqual(queued["7"]["inputs"]["text"], "worst quality")
            self.assertEqual(queued["3"]["inputs"]["seed"], 42)
            self.assertEqual(queued["3"]["inputs"]["steps"], 36)
            self.assertEqual(queued["3"]["inputs"]["cfg"], 2.2)
            self.assertEqual(queued["5"]["inputs"]["width"], 768)
            self.assertEqual(queued["5"]["inputs"]["height"], 1024)

    def test_comfyui_preview_socket_survives_quiet_model_loading(self):
        class FakeWebSocket:
            def __init__(self):
                self.messages = [
                    TimeoutError("quiet model loading"),
                    struct.pack(">II", 1, 2) + IMAGE_BYTES,
                    json.dumps({
                        "type": "progress",
                        "data": {"prompt_id": "prompt-1", "value": 2, "max": 4},
                    }),
                    json.dumps({
                        "type": "executing",
                        "data": {"prompt_id": "prompt-1", "node": None},
                    }),
                ]

            def recv(self, timeout=None):
                item = self.messages.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self._write_json(root, "workflow.json", {
                "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
                "6": {"class_type": "TextEncode", "inputs": {"text": "positive"}},
                "7": {"class_type": "TextEncode", "inputs": {"text": "negative"}},
                "9": {"class_type": "SaveImage", "inputs": {}},
            })
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "comfyui", "request_timeout_seconds": 5,
                "poll_interval_seconds": 0.01,
                "comfyui": {
                    "base_url": self.base_url, "workflow_api_file": "workflow.json",
                    "output_node": "9", "bindings": {
                        "positive_prompt": {"node": "6", "input": "text"},
                        "negative_prompt": {"node": "7", "input": "text"},
                        "seed": {"node": "3", "input": "seed"},
                    },
                },
            })
            original_connect = backend_module.websocket_connect
            backend_module.websocket_connect = lambda *_args, **_kwargs: FakeWebSocket()
            previews = []
            try:
                LocalGenerationBackend(root, config_path).generate(
                    prompt="@preview artist", output_path=root / "preview.png",
                    negative_prompt="", seed=3, width=512, height=512,
                    steps=4, cfg_scale=1.1, sampler="Euler", scheduler="Automatic",
                    preview_callback=lambda image, metadata: previews.append((image, metadata)),
                )
            finally:
                backend_module.websocket_connect = original_connect
            self.assertTrue(any(image == IMAGE_BYTES for image, _metadata in previews))
            self.assertTrue(any(metadata.get("progress") == 0.5 for _image, metadata in previews))

    def test_forge_payload_and_base64_response(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "forge",
                "request_timeout_seconds": 5,
                "forge": {
                    "base_url": self.base_url,
                    "cfg_scale": 4.0,
                    "steps": 40,
                    "sampler_name": "Euler a",
                    "scheduler": "Automatic",
                    "override_settings": {"sd_model_checkpoint": "anima.safetensors"},
                },
            })
            backend = LocalGenerationBackend(root, config_path)
            self.assertIn("reachable", backend.healthcheck())
            output = root / "forge.png"
            backend.generate(
                prompt="@test artist", output_path=output, negative_prompt="blurry",
                seed=99, width=1024, height=1024, steps=28,
                cfg_scale=3.3, sampler="ignored", scheduler="ignored",
            )
            self.assertEqual(output.read_bytes(), IMAGE_BYTES)
            payload = FakeBackendHandler.forge_payload
            self.assertEqual(payload["prompt"], "@test artist")
            self.assertEqual(payload["seed"], 99)
            self.assertEqual(payload["steps"], 40)
            self.assertEqual(payload["cfg_scale"], 3.3)
            self.assertEqual(payload["sampler_name"], "Euler a")
            self.assertTrue(payload["override_settings_restore_afterwards"])

    def test_non_loopback_is_rejected_by_default(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "forge",
                "forge": {"base_url": "http://192.0.2.10:7860"},
            })
            backend = LocalGenerationBackend(root, config_path)
            with self.assertRaises(LocalGenerationError):
                backend.healthcheck()

    def test_manual_connection_save_reload_and_configured_endpoint_scan(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "comfyui",
                "comfyui": {"base_url": self.base_url, "workflow_api_file": "workflow.json"},
                "forge": {"base_url": self.base_url},
            })
            backend = LocalGenerationBackend(root, config_path)
            saved = backend.save_connection("forge", self.base_url)
            self.assertEqual(saved["backend"], "forge")
            self.assertEqual(saved["connections"]["forge"]["base_url"], self.base_url)
            self.assertIn("reachable", backend.healthcheck())
            found = backend.scan_endpoints()
            self.assertTrue(any(item["backend"] == "forge" and item["base_url"] == self.base_url for item in found))

    def test_forge_native_progress_preview_callback(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = self._write_json(root, "local_generation.json", {
                "backend": "forge",
                "request_timeout_seconds": 5,
                "poll_interval_seconds": 0.1,
                "forge": {"base_url": self.base_url},
            })
            FakeBackendHandler.delay_forge = True
            previews = []
            backend = LocalGenerationBackend(root, config_path)
            backend.generate(
                prompt="@preview artist", output_path=root / "preview.png",
                negative_prompt="", seed=3, width=512, height=512,
                steps=4, cfg_scale=1.4, sampler="Euler", scheduler="Automatic",
                preview_callback=lambda image, metadata: previews.append((image, metadata)),
            )
            self.assertTrue(any(image == IMAGE_BYTES for image, _metadata in previews))
            self.assertTrue(any(metadata.get("progress") == 0.5 for _image, metadata in previews))


if __name__ == "__main__":
    unittest.main()
