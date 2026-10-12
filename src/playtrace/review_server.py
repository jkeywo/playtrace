"""Dependency-free local server for claim-linked design reviews."""

from __future__ import annotations

import ipaddress
import json
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from playtrace.demo import build_demo_review
from playtrace.local_service import (
    BoundedJobManager,
    LocalReviewProjector,
    ProjectReviewJobRequest,
)


REVIEW_ROOT = Path(__file__).with_name("review")


class ReviewHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args: object,
        review_payload: dict[str, object] | None = None,
        projector: LocalReviewProjector | None = None,
        project_id: str | None = None,
        jobs: BoundedJobManager | None = None,
        readiness: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self.review_payload = review_payload
        self.projector = projector
        self.project_id = project_id
        self.jobs = jobs
        self.readiness = readiness or {"ready": True}
        super().__init__(*args, directory=str(REVIEW_ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "local_only": True, **self.readiness})
            return
        if path == "/api/review":
            payload = (
                self.review_payload
                or (self.projector.project(self.project_id) if self.projector else None)
                or build_demo_review()
            )
            self._json(payload)
            return
        if path.startswith("/api/jobs/") and self.jobs:
            job_id = path.removeprefix("/api/jobs/").split("/", 1)[0]
            job = self.jobs.get(job_id)
            if job is None:
                self._json({"error": "job not found"}, 404)
            elif path.endswith("/events"):
                body = "".join(
                    f"id: {event.sequence}\ndata: {event.model_dump_json()}\n\n"
                    for event in job.events
                )
                self._bytes(body.encode("utf-8"), "text/event-stream; charset=utf-8")
            else:
                self._json(job.model_dump(mode="json"))
            return
        if path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        if urlparse(self.path).path != "/api/jobs" or not self.jobs:
            self._json({"error": "not found"}, 404)
            return
        raw_length = self.headers.get("Content-Length")
        try:
            if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
                raise ValueError("Content-Length is required")
            length = int(raw_length, 10)
        except (TypeError, ValueError):
            self._json({"error": "invalid Content-Length"}, 400)
            return
        if length > 64_000:
            self._json({"error": "request body too large"}, 413)
            return
        if self.projector is None:
            self._json({"error": "background projection requires --db"}, 503)
            return
        try:
            request = ProjectReviewJobRequest.model_validate_json(
                self.rfile.read(length) or b"{}"
            )
            if (
                self.project_id is not None
                and request.project_id is not None
                and request.project_id != self.project_id
            ):
                self._json({"error": "project_id does not match the served project"}, 403)
                return
            project_id = request.project_id or self.project_id
            job_id = request.id or f"job:{uuid.uuid4().hex}"

            def project(progress, cancel):
                progress("Loading local review state", 0.25)
                if cancel.is_set():
                    return {}
                result = self.projector.project(project_id) if self.projector else {}
                progress("Projection complete", 1.0)
                return result

            job = self.jobs.submit(job_id, "project-review", project)
            self._json(job.model_dump(mode="json"), 202)
        except (ValueError, RuntimeError, json.JSONDecodeError, ValidationError) as error:
            self._json({"error": str(error)}, 400)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib hook name
        path = urlparse(self.path).path
        if not path.startswith("/api/jobs/") or not self.jobs:
            self._json({"error": "not found"}, 404)
            return
        try:
            job = self.jobs.cancel(path.removeprefix("/api/jobs/"))
            self._json(job.model_dump(mode="json"))
        except KeyError:
            self._json({"error": "job not found"}, 404)

    def _json(self, payload: object, status: int = 200) -> None:
        self._bytes(
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))

        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self) -> None:
        """Apply the local-service security policy to API and shell assets alike."""

        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'",
        )
        super().end_headers()


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    review_file: Path | None = None,
    *,
    db_path: Path | None = None,
    project_id: str | None = None,
) -> None:
    try:
        address = ipaddress.ip_address(host)
        if address.version != 4 or not address.is_loopback:
            raise ValueError("the local review service may bind only to a loopback address")
    except ValueError as error:
        if "loopback" in str(error):
            raise
        raise ValueError("review host must be an explicit loopback IP address") from error
    payload = (
        json.loads(review_file.read_text(encoding="utf-8")) if review_file else None
    )
    projector = LocalReviewProjector(db_path) if db_path else None
    readiness = projector.preflight() if projector else {"ready": True, "database": None}
    jobs = BoundedJobManager()

    def handler(*args: object, **kwargs: object) -> ReviewHandler:
        return ReviewHandler(
            *args,
            review_payload=payload,
            projector=projector,
            project_id=project_id,
            jobs=jobs,
            readiness=readiness,
            **kwargs,
        )

    server = ThreadingHTTPServer((host, port), handler)
    print(f"Play Trace review shell: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        jobs.close()
