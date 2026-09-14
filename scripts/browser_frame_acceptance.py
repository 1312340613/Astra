"""Serve the local, explicit browser-frame test fixture; never exposes the checkout."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor-dir', type=Path, required=True, help='Installed tinymce package directory')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output', type=Path, required=True, help='Local JSON report path')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    vendor = args.vendor_dir.resolve()
    allowed = {
        '/tests/fixtures/browser-frame-fill-acceptance.html': root / 'tests/fixtures/browser-frame-fill-acceptance.html',
        '/tests/fixtures/browser-observation-acceptance.html': root / 'tests/fixtures/browser-observation-acceptance.html',
        '/browser-control-extension/page.js': root / 'browser-control-extension/page.js',
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = unquote(urlsplit(self.path).path)
            path = allowed.get(url)
            if url.startswith('/vendor/tinymce/'):
                candidate = (vendor / url.removeprefix('/vendor/tinymce/')).resolve()
                if candidate.is_relative_to(vendor):
                    path = candidate
            if path is None or not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(path.read_bytes())

        def do_POST(self):
            if self.path not in {'/report', '/progress'}:
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 100000:
                    raise ValueError('Invalid report length')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or (self.path == '/report' and type(data.get('pass')) is not bool):
                    raise ValueError('Invalid report')
            except (ValueError, OSError):
                self.send_error(400)
                return
            output = args.output if self.path == '/report' else args.output.with_suffix('.progress.json')
            output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
            if self.path == '/report':
                print(f"Report: pass={data['pass']}, verifiedFills={data.get('verifiedFills')}", flush=True)

        def log_message(self, *args):
            pass

    print(f'Open http://127.0.0.1:{args.port}/tests/fixtures/browser-frame-fill-acceptance.html', flush=True)
    print(f'Observation fixture: http://127.0.0.1:{args.port}/tests/fixtures/browser-observation-acceptance.html', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
