"""Loopback-only viewer server with an explicit local artifact preset."""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .errors import JgError
from .safety import read_json


_KINDS = ("inventory", "candidates", "relations", "review")


def bootstrap(artifacts: dict[str, Path]) -> bytes:
    """Serialize an explicit local artifact set without exposing its paths."""
    payload: dict[str, dict[str, Any]] = {}
    for kind, path in artifacts.items():
        if kind not in _KINDS:
            raise JgError(f"unsupported viewer artifact: {kind}")
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise JgError(f"viewer artifact is not a file: {kind}")
        payload[kind] = {"name": resolved.name, "document": read_json(resolved)}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"window.JevGraphDefaultArtifacts={encoded};\n".encode("utf-8")


def serve(artifacts: dict[str, Path], port: int = 8877) -> None:
    if not 1 <= port <= 65535:
        raise JgError("viewer port must be between 1 and 65535")
    document_root = Path(__file__).resolve().parents[2] / "docs"
    document = bootstrap(artifacts)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(document_root), **kwargs)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/run-data.js":
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)
                return
            super().do_GET()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise JgError(f"viewer could not bind 127.0.0.1:{port}") from exc
    with server:
        server.serve_forever()
