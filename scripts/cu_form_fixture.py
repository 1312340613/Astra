"""Serve only the local CU test page and its independent read-only result oracle."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/cu-form-reliability.html"
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path not in {"/form", "/state"}:
                self.send_error(404)
                return
            data = fixture.read_bytes() if path == "/form" else json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if path == "/form" else "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/report":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size < 8192:
                self.send_error(400)
                return
            value = json.loads(self.rfile.read(size))
            state.clear()
            state.update(value)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    print(f"CU fixture: http://127.0.0.1:{args.port}/form", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
