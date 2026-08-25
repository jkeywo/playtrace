"""Dependency-free local server for claim-linked design reviews."""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playtrace.demo import build_demo_review


REVIEW_ROOT = Path(__file__).with_name("review")


class ReviewHandler(SimpleHTTPRequestHandler):
    def __init__(
        self, *args: object, review_payload: dict[str, object] | None = None, **kwargs: object
    ) -> None:
        self.review_payload = review_payload
        super().__init__(*args, directory=str(REVIEW_ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        if urlparse(self.path).path == "/api/review":
            payload = json.dumps(
                self.review_payload or build_demo_review(), default=str
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()


def serve(
    host: str = "127.0.0.1", port: int = 8765, review_file: Path | None = None
) -> None:
    payload = (
        json.loads(review_file.read_text(encoding="utf-8")) if review_file else None
    )

    def handler(*args: object, **kwargs: object) -> ReviewHandler:
        return ReviewHandler(*args, review_payload=payload, **kwargs)

    server = ThreadingHTTPServer((host, port), handler)
    print(f"Play Trace review shell: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
