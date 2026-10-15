"""
Dashboard backend, now built on the confirmed WebUI-HTML Brick API:

    from arduino.app_bricks.web_ui import WebUI
    ui = WebUI()
    ui.expose_api("GET", "/path", handler)          # REST, for pull-on-demand data
    ui.on_connect(lambda sid: ...)                    # push initial state on load
    ui.send_message("channel", payload)               # push live updates

Design:
  - /api/history and /api/today stay as plain REST (`expose_api`) - the
    dashboard already polls these every ~20s and there's no benefit to
    pushing them over the socket.
  - The once-a-second live sample (scores + state) is pushed over the
    WebSocket via send_message("live_sample", ...) from the capture loop in
    main.py, instead of the dashboard polling /api/live. A REST /api/live
    still exists as a fallback for the very first paint, before any socket
    message has arrived.
  - The live camera frame is pushed as a base64 JPEG inside the same
    "live_sample" message (at 1 fps this is cheap) rather than using
    ui.expose_camera(), which expects a BaseCamera-typed object we haven't
    confirmed the interface for yet. Worth revisiting once that's confirmed -
    expose_camera would be the more "proper", lower-overhead way to stream.

Auth (see web/auth.py for the store itself):
  - expose_api() hands the handler straight to FastAPI's add_api_route, so
    each handler declares an `authorization` Header parameter and calls
    _require_auth(). Every data route is gated; only /api/auth/status,
    /api/auth/setup and /api/auth/login are reachable without a token.
  - The WebSocket cannot be refused at connect time: the Brick's on_connect
    callback receives only the sid and has no way to reject. So the socket
    authenticates *after* connecting, via an "auth" message, and the server
    tracks which sids cleared it. Nothing is pushed to a socket that hasn't.
  - Pushes therefore target per-sid rooms, never a shared named room: the
    Brick registers "enter_room" as an open socket event, so any client could
    join a room by name and start receiving frames. A sid is private to its
    own connection, which makes it the only safe room to send to.
"""
import base64
import logging
import threading
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

import config
from web.auth import AuthStore, default_auth_path
from web.qr import get_dashboard_url, generate_qr_svg
from web.llm_summary import generate_summary

logger = logging.getLogger("workspace_ai.web")

# Set by register_web_ui(). Module-level so push_live_sample() can see it
# without changing its signature (and therefore without touching main.py).
_auth: Optional[AuthStore] = None

# Sids that have presented a valid token over the socket, mapped to the token
# they used - so logging out on one device only silences that device's sockets
# and leaves another browser's still-valid session running. Guarded because the
# Brick runs each socket callback on its own thread, while push_live_sample()
# is called from the video-pump thread.
_authed_sids = {}  # sid -> token
_sids_lock = threading.Lock()


class _PasswordBody(BaseModel):
    password: str


class _PomodoroSettingsBody(BaseModel):
    """Every field optional: the Focus tab's settings card can send just the
    one control the user touched. Pomodoro.update_settings() clamps each."""
    focus_minutes: Optional[int] = None
    short_break_minutes: Optional[int] = None
    long_break_minutes: Optional[int] = None
    sessions_before_long_break: Optional[int] = None
    auto_start_next: Optional[bool] = None


class _ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


def _token_from_header(authorization: Optional[str]) -> Optional[str]:
    """Pulls the bearer token out of an Authorization header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def _require_auth(authorization: Optional[str]) -> str:
    """Raises 401 unless the header carries a live session token. Called at
    the top of every data route."""
    token = _token_from_header(authorization)
    if _auth is None or not _auth.validate_token(token):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return token


def register_web_ui(ui, shared_state, storage, web_port: int, scoring_engine, pomodoro=None):
    """Wires the WebUI Brick instance up to our data sources. Call once at startup."""
    global _auth
    _auth = AuthStore(default_auth_path(config.DB_PATH))

    # ---------- data routes (all gated) ----------

    def get_live(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _sample_payload(shared_state.get_sample(), shared_state)

    def get_history(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return storage.get_recent(limit=100)

    def get_today(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        # Completed periods come from the DB; the current, still-accumulating
        # period isn't in there yet, so append it as one more row. The
        # dashboard already sums across all rows in this list, so this is
        # enough to make the stat cards reflect the in-progress period too -
        # including the case where today has no completed periods at all.
        return storage.get_today() + [scoring_engine.current_totals()]

    def get_trends(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        # Daily-grouped average posture/engagement for the Trends tab -
        # separate from /api/history (raw rollup periods), which the
        # dashboard also still shows but doesn't group by calendar day.
        return storage.get_daily_summary(days=7)

    def get_qr(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        url = get_dashboard_url(web_port)
        try:
            svg = generate_qr_svg(url)
        except Exception as exc:  # noqa: BLE001 - dashboard should still work without this
            logger.warning("QR generation failed (%s)", exc)
            return {"url": url, "svg": None}
        return {"url": url, "svg": svg}

    def post_generate_summary(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        # Deliberately slow (up to ~90s on-device) - the frontend shows a
        # clear "this will take a moment" state and never calls this
        # automatically, only from an explicit button click.
        return generate_summary(storage.get_today())

    ui.expose_api("GET", "/api/live", get_live)
    ui.expose_api("GET", "/api/history", get_history)
    ui.expose_api("GET", "/api/today", get_today)
    ui.expose_api("GET", "/api/trends", get_trends)
    ui.expose_api("GET", "/api/qr", get_qr)
    ui.expose_api("POST", "/api/generate_summary", post_generate_summary)

    # ---------- auth routes ----------

    def get_auth_status():
        """The only unauthenticated read. Says nothing but whether setup is
        still needed, so the client knows which screen to show."""
        return {"password_set": _auth.is_password_set()}

    def post_auth_setup(body: _PasswordBody):
        token, error = _auth.set_initial_password(body.password)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return {"token": token}

    def post_auth_login(body: _PasswordBody):
        token, error, retry_after = _auth.login(body.password)
        if error:
            # 429 for the lockout, 401 for a plain wrong password, so the
            # client can tell "wait" from "try again".
            raise HTTPException(status_code=429 if retry_after else 401, detail=error)
        return {"token": token}

    def post_auth_change_password(body: _ChangePasswordBody,
                                  authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        ok, error = _auth.change_password(body.old_password, body.new_password)
        if not ok:
            raise HTTPException(status_code=400, detail=error)
        return {"ok": True}

    def post_auth_logout(authorization: Optional[str] = Header(default=None)):
        token = _require_auth(authorization)
        _auth.revoke_token(token)
        # Drop only the sockets riding this token; other devices' sessions
        # stay live.
        with _sids_lock:
            for sid in [s for s, t in _authed_sids.items() if t == token]:
                del _authed_sids[sid]
        return {"ok": True}

    # ---------- pomodoro (gated like every other data route) ----------

    def _pomo_or_503():
        if pomodoro is None:
            raise HTTPException(status_code=503, detail="Pomodoro is not available.")
        return pomodoro

    def get_pomodoro(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().snapshot()

    def post_pomodoro_start(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().start()

    def post_pomodoro_pause(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().pause()

    def post_pomodoro_resume(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().resume()

    def post_pomodoro_skip(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().skip()

    def post_pomodoro_reset(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        return _pomo_or_503().reset()

    def post_pomodoro_settings(body: _PomodoroSettingsBody,
                               authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        # model_dump on pydantic v2, dict() on v1 - don't assume which the
        # brick's FastAPI pulled in.
        payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        _, error = _pomo_or_503().update_settings(payload)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return _pomo_or_503().snapshot()

    ui.expose_api("GET", "/api/pomodoro", get_pomodoro)
    ui.expose_api("POST", "/api/pomodoro/start", post_pomodoro_start)
    ui.expose_api("POST", "/api/pomodoro/pause", post_pomodoro_pause)
    ui.expose_api("POST", "/api/pomodoro/resume", post_pomodoro_resume)
    ui.expose_api("POST", "/api/pomodoro/skip", post_pomodoro_skip)
    ui.expose_api("POST", "/api/pomodoro/reset", post_pomodoro_reset)
    ui.expose_api("POST", "/api/pomodoro/settings", post_pomodoro_settings)

    ui.expose_api("GET", "/api/auth/status", get_auth_status)
    ui.expose_api("POST", "/api/auth/setup", post_auth_setup)
    ui.expose_api("POST", "/api/auth/login", post_auth_login)
    ui.expose_api("POST", "/api/auth/change_password", post_auth_change_password)
    ui.expose_api("POST", "/api/auth/logout", post_auth_logout)

    # ---------- socket ----------

    def on_socket_connect(sid):
        # Deliberately pushes nothing: an unauthenticated socket must not see
        # stats or frames. (This used to send get_live() straight away.)
        with _sids_lock:
            _authed_sids.pop(sid, None)

    def on_socket_auth(sid, data):
        """Socket handshake. The return value is emitted back to this sid
        alone as "auth_response" by the Brick."""
        token = data.get("token") if isinstance(data, dict) else None
        if _auth is not None and _auth.validate_token(token):
            with _sids_lock:
                _authed_sids[sid] = token
            # First paint for this client only, now that it's allowed one.
            ui.send_message("live_sample",
                            _sample_payload(shared_state.get_sample(), shared_state),
                            room=sid)
            if pomodoro is not None:
                ui.send_message("pomodoro", pomodoro.snapshot(), room=sid)
            return {"ok": True}
        with _sids_lock:
            _authed_sids.pop(sid, None)
        logger.warning("Socket %s failed auth - it will receive no pushes.", sid)
        return {"ok": False}

    def on_socket_disconnect(sid):
        with _sids_lock:
            _authed_sids.pop(sid, None)

    ui.on_connect(on_socket_connect)
    ui.on_message("auth", on_socket_auth)
    ui.on_disconnect(on_socket_disconnect)


def _authed_targets():
    with _sids_lock:
        return list(_authed_sids.keys())


def push_pomodoro_state(ui, pomodoro):
    """Call once a second from the pomodoro tick thread. Same per-sid rule as
    the live sample: authenticated sockets only, never a broadcast, and it
    returns before doing any work when nobody is signed in."""
    targets = _authed_targets()
    if not targets or pomodoro is None:
        return
    payload = pomodoro.snapshot()
    for sid in targets:
        try:
            ui.send_message("pomodoro", payload, room=sid)
        except Exception as exc:  # noqa: BLE001 - a dashboard hiccup shouldn't stop the timer
            logger.warning("Failed to push pomodoro state to %s (%s)", sid, exc)


def push_live_sample(ui, sample, shared_state, jpeg_bytes: Optional[bytes]):
    """Call from the video-pump thread each frame push. `sample` may be None
    before the first scoring pass completes - in that case we still push the
    frame (for a live preview) with a 'starting_up' status.

    Only authenticated sids get anything. With nobody logged in this returns
    before the base64 encode, so an idle dashboard costs nothing."""
    targets = _authed_targets()
    if not targets:
        return

    payload = _sample_payload(sample, shared_state)
    if jpeg_bytes is not None:
        payload["frame_b64"] = base64.b64encode(jpeg_bytes).decode("ascii")
    for sid in targets:
        try:
            ui.send_message("live_sample", payload, room=sid)
        except Exception as exc:  # noqa: BLE001 - a dashboard hiccup shouldn't kill scoring
            logger.warning("Failed to push live sample to %s over WebSocket (%s)", sid, exc)


def _sample_payload(sample, shared_state) -> dict:
    physical = shared_state.get_physical()
    if sample is None:
        return {
            "status": "starting_up",
            "camera_ok": shared_state.is_camera_ok(),
            "physical": physical,
        }
    return {
        "status": "ok",
        "camera_ok": shared_state.is_camera_ok(),
        "physical": physical,
        "ts": sample.ts,
        "person_present": sample.person_present,
        "state": sample.state,
        "posture_score": sample.posture_score,
        "posture_label": sample.posture_label,
        "posture_issues": sample.posture_issues,
        "engagement_score": sample.engagement_score,
        "looking_at_screen": sample.looking_at_screen,
        "face_present": sample.face_present,
        "phone_detected": sample.phone_detected,
        "person_confidence": sample.person_confidence,
        "face_confidence": getattr(sample, "face_confidence", 0.0),
        "posture_debug": getattr(sample, "posture_debug", {}),
    }
