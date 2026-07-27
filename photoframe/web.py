"""The remote control: a small Flask app for uploading photos and settings.

Runs in a thread inside the same process as the display loop, so it shares the
library and config objects directly rather than talking over a socket.
"""

from __future__ import annotations

import hmac
import io
import socket
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from PIL import Image

from .config import SECRET_FIELDS, SOURCE_FIELDS, sanitise_sources
from .library import SUPPORTED_EXTENSIONS, LibraryError
from .sources import SourceError, build_source
from .state import COMMANDS

MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# Sent in place of a stored password so it never leaves the Pi; if it comes
# back unchanged on save, we keep what we already had.
SECRET_PLACEHOLDER = "__unchanged__"

# How long a mirror long-poll parks before answering "nothing changed". Long
# enough to be quiet, short enough to survive proxies that cut idle requests.
LONG_POLL_SECONDS = 25.0
FIRST_FRAME_WAIT = 6.0


def redact_config(settings: dict) -> dict:
    """Strip source credentials out of anything we hand to a browser."""
    for source in settings.get("sources", []):
        for field in SECRET_FIELDS:
            if field in source:
                source[field] = SECRET_PLACEHOLDER if source[field] else ""
    return settings


def restore_secrets(patch: dict, current: list[dict]) -> dict:
    """Put real credentials back where the browser echoed the placeholder."""
    if not isinstance(patch.get("sources"), list):
        return patch
    by_id = {source.get("id"): source for source in current}
    for source in patch["sources"]:
        if not isinstance(source, dict):
            continue
        existing = by_id.get(source.get("id"), {})
        for field in SECRET_FIELDS:
            if source.get(field) == SECRET_PLACEHOLDER:
                source[field] = existing.get(field, "")
    return patch


def local_ip() -> str:
    """Best guess at this machine's LAN address (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def create_app(config, library, state, weather, password: str | None = None,
               sync=None, storage=None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["JSON_SORT_KEYS"] = False

    @app.before_request
    def require_password():
        """Optional shared password, off by default for a trusted LAN."""
        if not password:
            return None
        auth = request.authorization
        if auth and hmac.compare_digest(auth.password or "", password):
            return None
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Photo frame"'},
        )

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error=f"File too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"), 413

    # ------------------------------------------------------------------ views

    @app.get("/")
    def index():
        return render_template(
            "admin.html",
            config=redact_config(config.snapshot()),
            photo_count=len(library),
            extensions=sorted(SUPPORTED_EXTENSIONS),
            source_fields=SOURCE_FIELDS,
        )

    @app.get("/mirror")
    def mirror():
        """A live copy of the frame, for testing or for a second screen."""
        return render_template("mirror.html")

    @app.get("/api/status")
    def api_status():
        status = state.status()
        status["photo_count"] = len(library)
        status["weather"] = weather.current
        # Only the cheap figures here — this is polled every few seconds.
        status["storage"] = storage.summary() if storage else None
        return jsonify(status)

    @app.get("/api/storage")
    def api_storage():
        if storage is None:
            return jsonify(error="Storage monitoring is not enabled"), 503
        return jsonify(storage.snapshot(refresh=request.args.get("refresh") == "1"))

    @app.post("/api/storage/trim")
    def api_storage_trim():
        """Drop the render cache. Everything in it rebuilds on demand."""
        if storage is None:
            return jsonify(error="Storage monitoring is not enabled"), 503
        freed = storage.trim_cache()
        return jsonify(ok=True, freed_bytes=freed, storage=storage.snapshot(refresh=True))

    @app.get("/api/photos")
    def api_photos():
        return jsonify(photos=library.list_photos(), count=len(library))

    @app.post("/api/photos")
    def api_upload():
        uploads = request.files.getlist("photos") or request.files.getlist("file")
        if not uploads:
            return jsonify(error="No files in request"), 400

        # Refuse rather than half-write the index onto a full card.
        if storage is not None and not storage.make_room():
            return jsonify(error=storage.describe_shortage(), added=[], errors=[]), 507

        added, errors = [], []
        for upload in uploads:
            try:
                added.append(library.add(upload.read(), upload.filename or "photo.jpg"))
            except LibraryError as exc:
                errors.append({"filename": upload.filename, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - never 500 on one bad file
                errors.append({"filename": upload.filename, "error": f"unexpected: {exc}"})

        if added and storage is not None:
            storage.invalidate()
        status = 200 if added else 400
        return jsonify(added=added, errors=errors, count=len(library)), status

    @app.delete("/api/photos/<photo_id>")
    def api_delete(photo_id: str):
        if not library.delete(photo_id):
            return jsonify(error="No such photo"), 404
        if storage is not None:
            storage.invalidate()
        return jsonify(ok=True, count=len(library))

    @app.get("/thumbs/<photo_id>.jpg")
    def thumb(photo_id: str):
        path = library.thumb_path(photo_id)
        if path is None:
            return jsonify(error="No such photo"), 404
        response = send_from_directory(library.thumbs, path.name)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.get("/api/config")
    def api_config_get():
        return jsonify(redact_config(config.snapshot()))

    @app.route("/api/config", methods=["PUT", "PATCH", "POST"])
    def api_config_set():
        patch = request.get_json(silent=True)
        if not isinstance(patch, dict):
            return jsonify(error="Expected a JSON object"), 400
        previous_weather = config.section("weather")
        patch = restore_secrets(patch, config.section("sources"))
        updated = config.update(patch)
        if updated["weather"] != previous_weather:
            weather.refresh_now()
        if sync is not None and "sources" in patch:
            sync.sync_now()  # picks up added/edited sources, drops removed ones
        return jsonify(redact_config(updated))

    # -------------------------------------------------------------- sources

    @app.get("/api/sources")
    def api_sources():
        if sync is None:
            return jsonify(sources=[], available=sorted(SOURCE_FIELDS))
        return jsonify(sources=sync.status(), available=sorted(SOURCE_FIELDS))

    @app.post("/api/sources/<source_id>/test")
    def api_source_test(source_id: str):
        """Check a source's settings.

        The body may carry the settings to test, which is how the web page
        tests a source you haven't saved yet — otherwise there'd be nothing
        on the server to test and you'd have to save blind. With no body, the
        stored settings are used.
        """
        if sync is None:
            return jsonify(error="Syncing is not enabled"), 503

        payload = request.get_json(silent=True)
        try:
            if isinstance(payload, dict) and payload.get("type"):
                candidate = restore_secrets(
                    {"sources": [dict(payload, id=source_id)]}, config.section("sources")
                )["sources"][0]
                cleaned = sanitise_sources([candidate])
                if not cleaned:
                    return jsonify(ok=False, message="Those settings aren't usable — check the type"), 200
                message = build_source(cleaned[0]).check()
            else:
                message = sync.check(source_id)
            return jsonify(ok=True, message=message)
        except SourceError as exc:
            return jsonify(ok=False, message=str(exc)), 200  # a failed test isn't an HTTP error
        except Exception as exc:  # noqa: BLE001 - report anything the source throws
            return jsonify(ok=False, message=f"Unexpected error: {exc}"), 200

    @app.post("/api/sources/<source_id>/sync")
    def api_source_sync(source_id: str):
        if sync is None:
            return jsonify(error="Syncing is not enabled"), 503
        # Unlike Test, this one writes to the library, so it needs a source
        # that's actually been saved.
        if not sync.sync_now(source_id):
            return jsonify(error="No such source — press Save settings first"), 404
        return jsonify(ok=True)

    @app.post("/api/sync")
    def api_sync_all():
        if sync is None:
            return jsonify(error="Syncing is not enabled"), 503
        sync.sync_now()
        return jsonify(ok=True)

    # --------------------------------------------------------------- mirror

    @app.get("/api/frame.jpg")
    def api_frame():
        """The current screen as a JPEG.

        Pass ?since=<generation> to long-poll: the request parks until the
        picture changes, so a mirror updates the instant the frame does
        without polling in a loop.
        """
        settings = config.section("mirror")
        if not settings["enabled"]:
            return jsonify(error="The web mirror is switched off"), 403

        state.request_mirror()
        try:
            since = int(request.args["since"]) if "since" in request.args else None
        except ValueError:
            since = None

        timeout = LONG_POLL_SECONDS if since is not None else FIRST_FRAME_WAIT
        frame = state.wait_for_frame(timeout=timeout, since=since)
        if frame is None:
            if since is not None:
                return "", 304  # nothing changed while we waited; poll again
            return jsonify(error="No frame available yet — is the display running?"), 503

        generation, data, size = frame
        image = Image.frombytes("RGB", size, data)
        max_width = settings["max_width"]
        if max_width and image.width > max_width:
            height = round(image.height * max_width / image.width)
            image = image.resize((max_width, height), Image.BILINEAR)

        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=settings["quality"])
        response = Response(buffer.getvalue(), mimetype="image/jpeg")
        response.headers["X-Frame-Generation"] = str(generation)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/control")
    def api_control():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        action = payload.get("action") or request.form.get("action", "")
        if not state.send(action):
            return jsonify(error=f"Unknown action; expected one of {sorted(COMMANDS)}"), 400
        return jsonify(ok=True, action=action)

    @app.get("/api/weather")
    def api_weather():
        return jsonify(weather.current or {})

    return app
