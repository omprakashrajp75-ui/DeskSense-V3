"""
Password + session auth for the dashboard.

Design:
  - The password itself is never stored. Only a PBKDF2-HMAC-SHA256 digest of
    it, over a per-install random salt, is written to disk.
  - Everything lives in one JSON file in the app's data folder, next to the
    SQLite DB - so it survives restarts, stays out of the source tree, and
    never goes near config.py. The path is derived from config.DB_PATH's
    directory rather than adding a new config value.
  - Sessions are {token: expiry_epoch}, held in memory *and* in that same
    file, so an app restart or a board reboot doesn't log you out.
  - Every public method takes the lock. The WebUI Brick dispatches each HTTP
    handler and socket callback on its own thread (asyncio.to_thread), so
    concurrent calls are the normal case here, not an edge case.
  - Login is rate-limited globally rather than per-IP: this is a single-user
    appliance on a home LAN, and a global counter can't be sidestepped by
    rotating source addresses.
"""
import os
import json
import time
import hmac
import hashlib
import logging
import secrets
import threading

logger = logging.getLogger("workspace_ai.auth")

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
TOKEN_BYTES = 32
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

MIN_PASSWORD_LENGTH = 6
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


class AuthStore:
    """Password hash + live session tokens, persisted to a single JSON file."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._salt = None        # bytes
        self._digest = None      # bytes
        self._iterations = PBKDF2_ITERATIONS
        self._sessions = {}      # token -> expiry epoch
        self._failed = 0
        self._locked_until = 0.0
        self._load()

    # ---------- persistence ----------

    def _load(self):
        if not os.path.exists(self._path):
            logger.info("No auth file yet at %s - first run will ask to set a password.", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            salt = blob.get("salt")
            digest = blob.get("hash")
            if salt and digest:
                self._salt = bytes.fromhex(salt)
                self._digest = bytes.fromhex(digest)
                self._iterations = int(blob.get("iterations") or PBKDF2_ITERATIONS)
            self._sessions = {
                str(tok): float(exp)
                for tok, exp in (blob.get("sessions") or {}).items()
            }
            self._prune_locked()
            logger.info("Auth loaded (password set=%s, %d live session(s)).",
                        self._digest is not None, len(self._sessions))
        except Exception:  # noqa: BLE001 - a corrupt file must not brick the app
            logger.exception("Could not read %s - treating as unset. Delete the file to reset.",
                             self._path)

    def _save_locked(self):
        """Caller must hold the lock. Writes via a temp file + replace so a
        crash mid-write can't leave a half-written auth file behind."""
        blob = {
            "version": 1,
            "salt": self._salt.hex() if self._salt else None,
            "hash": self._digest.hex() if self._digest else None,
            "iterations": self._iterations,
            "sessions": self._sessions,
        }
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except Exception:  # noqa: BLE001
            logger.exception("Could not write auth file %s", self._path)

    # ---------- password ----------

    @staticmethod
    def _derive(password: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def _matches_locked(self, password: str) -> bool:
        if not self._digest or not self._salt:
            return False
        candidate = self._derive(password, self._salt, self._iterations)
        return hmac.compare_digest(candidate, self._digest)

    def is_password_set(self) -> bool:
        with self._lock:
            return self._digest is not None

    def set_initial_password(self, password: str):
        """First-run setup. Returns (token, error). Refuses once a password
        exists, so this can never be used to take over a configured board."""
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            return None, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        with self._lock:
            if self._digest is not None:
                return None, "A password is already set."
            self._salt = secrets.token_bytes(SALT_BYTES)
            self._iterations = PBKDF2_ITERATIONS
            self._digest = self._derive(password, self._salt, self._iterations)
            token = self._new_session_locked()
            self._save_locked()
        logger.info("Initial dashboard password set.")
        return token, None

    def change_password(self, old_password: str, new_password: str):
        """Returns (ok, error). Existing sessions are deliberately left alone,
        so changing the password doesn't sign you out of the device you're
        changing it on (or any other). Use logout to end a session."""
        if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
            return False, f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
        with self._lock:
            if self._digest is None:
                return False, "No password is set yet."
            if not self._matches_locked(old_password):
                return False, "Current password is incorrect."
            self._salt = secrets.token_bytes(SALT_BYTES)
            self._iterations = PBKDF2_ITERATIONS
            self._digest = self._derive(new_password, self._salt, self._iterations)
            self._save_locked()
        logger.info("Dashboard password changed.")
        return True, None

    # ---------- login / lockout ----------

    def login(self, password: str):
        """Returns (token, error, retry_after_seconds). After
        MAX_FAILED_ATTEMPTS wrong guesses the next attempt is refused for
        LOCKOUT_SECONDS, whatever password it carries."""
        now = time.time()
        with self._lock:
            if now < self._locked_until:
                wait = int(self._locked_until - now) + 1
                return None, f"Too many failed attempts. Try again in {wait}s.", wait
            if self._digest is None:
                return None, "No password is set yet.", 0
            if not self._matches_locked(password or ""):
                self._failed += 1
                remaining = MAX_FAILED_ATTEMPTS - self._failed
                if self._failed >= MAX_FAILED_ATTEMPTS:
                    self._locked_until = now + LOCKOUT_SECONDS
                    self._failed = 0
                    logger.warning("Login locked out for %ds after %d failed attempts.",
                                   LOCKOUT_SECONDS, MAX_FAILED_ATTEMPTS)
                    return None, f"Too many failed attempts. Try again in {LOCKOUT_SECONDS}s.", LOCKOUT_SECONDS
                logger.warning("Failed dashboard login (%d attempt(s) left).", remaining)
                return None, "Incorrect password.", 0

            self._failed = 0
            token = self._new_session_locked()
            self._save_locked()
        logger.info("Dashboard login succeeded.")
        return token, None, 0

    # ---------- sessions ----------

    def _new_session_locked(self) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._sessions[token] = time.time() + SESSION_TTL_SECONDS
        self._prune_locked()
        return token

    def _prune_locked(self):
        now = time.time()
        for tok in [t for t, exp in self._sessions.items() if exp <= now]:
            del self._sessions[tok]

    def validate_token(self, token) -> bool:
        if not token or not isinstance(token, str):
            return False
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                del self._sessions[token]
                self._save_locked()
                return False
            return True

    def revoke_token(self, token) -> bool:
        if not token:
            return False
        with self._lock:
            existed = self._sessions.pop(token, None) is not None
            if existed:
                self._save_locked()
        return existed


def default_auth_path(db_path: str) -> str:
    """Auth file sits in the same data folder as the SQLite DB, so it inherits
    the app's existing data location without introducing a config value."""
    return os.path.join(os.path.dirname(db_path), "auth.json")
