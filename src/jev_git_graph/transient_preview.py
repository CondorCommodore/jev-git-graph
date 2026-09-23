"""Loopback-only, in-memory rendering for explicitly approved presence payloads."""
from __future__ import annotations

import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping


class _PreviewServer(HTTPServer):
    daemon_threads = True


def serve_presence_preview(preview: Mapping[str, Any]) -> None:
    """Serve one exact preview in memory until interrupted; never write payload bytes."""
    token = secrets.token_urlsafe(24)
    body = html.escape(json.dumps(preview, indent=2, sort_keys=True))
    page = ("<!doctype html><meta charset=utf-8><title>Transient Jev presence preview</title>"
            "<style>body{font:14px ui-monospace,monospace;margin:2rem}pre{white-space:pre-wrap;"
            "overflow-wrap:anywhere}</style><h1>Transient Jev presence preview</h1>"
            "<p>This exact request is held in memory and is not stored by this server.</p>"
            f"<pre>{body}</pre>").encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != f"/{token}":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = _PreviewServer(("127.0.0.1", 0), Handler)
    print(f"Transient no-store preview: http://127.0.0.1:{server.server_port}/{token}")
    print("Press Ctrl+C to close; request text remains in memory only.")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
