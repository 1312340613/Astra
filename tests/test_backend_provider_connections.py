"""Real backend IPC: credentials, provider discovery, switching and responsiveness."""
import json
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend


class ModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.slow:
            self.server.started.set()
            self.server.release.wait(5)
        self.server.requests.append((self.path, self.headers.get("Authorization", "")))
        payload = json.dumps({"data": [{"id": "fresh-model", "context_length": 65536}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.parametrize("unconfigured", [False, True])
def test_provider_flow_over_real_ipc(tmp_path, monkeypatch, unconfigured):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    server.slow = False
    server.started, server.release = threading.Event(), threading.Event()
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    overrides = {}
    if unconfigured:
        config = tmp_path / "unconfigured.yaml"
        config.write_text("models:\n  Qwen3.6-35B-A3B:\n    base_url: https://unconfigured.example/v1\n    api_key_env: ABSENT_TEST_PROVIDER_KEY\n    context_limit: 32768\n")
        monkeypatch.delenv("ABSENT_TEST_PROVIDER_KEY", raising=False)
        overrides["AGENT_MODELS_FILE"] = str(config)
    proc, _, seen, wait = _start_question_protocol_backend(tmp_path, server.server_port, "provider-flow", env_overrides=overrides)
    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
    try:
        initial = wait(lambda e: e.get("type") == "model_info")
        original_key = initial["model_key"]
        assert initial["connection_routes"] and initial["providers"]
        send({"type": "connect_provider", "request_id": "connect-1", "route_id": "custom",
              "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "PRIVATE-CONNECT-KEY", "api_key_env": ""})
        connected = wait(lambda e: e.get("type") == "connection_result")
        assert not connected["error"]
        assert all(e.get("model_key", original_key) == original_key for e in seen)
        provider = connected["provider_id"]
        selector = f"{provider}::fresh-model"
        send({"type": "command", "cmd": f"/model {selector}"})
        selected = wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
        assert selected["recent_models"][0] == selector
        assert json.loads((tmp_path / "settings.json").read_text())["selected_model"] == selector
        # A slow GET must not block live controls or a second provider's UI.
        server.slow = True
        send({"type": "refresh_models", "provider_id": provider, "force": True})
        assert server.started.wait(2)
        send({"type": "command", "cmd": "/yolo status"})
        wait(lambda e: e.get("type") == "yolo_status", timeout=2)
        server.release.set()
        wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
        assert "PRIVATE-CONNECT-KEY" not in json.dumps(seen)
        assert any(auth == "Bearer PRIVATE-CONNECT-KEY" for _, auth in server.requests)
    finally:
        server.release.set()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
    for path in (tmp_path / "sessions").glob("*.json*"):
        assert "PRIVATE-CONNECT-KEY" not in path.read_text()
