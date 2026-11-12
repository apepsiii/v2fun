"""
V2Fun.ai Token Manager & Continuous Auto-Refresh Scheduler
Auto-refresh JWT tokens proactively so accounts stay alive continuously.

Strategy:
1. V2Fun.ai does NOT have a refresh token endpoint; JWT expires every ~3 days (72h).
2. A background scheduler runs every 3 hours (configurable via TOKEN_REFRESH_INTERVAL_HOURS)
   and renews any account whose token has < 12h remaining (configurable via
   TOKEN_REFRESH_BEFORE_HOURS), or when triggered on-demand / on-activation.
3. Re-login uses v2fun_google_login.py (--headless --force) with an isolated temp file,
   then syncs the new JWT to session JSON, v2fun_accounts, users, and AccountPool.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple


# Token is considered "expiring soon" if less than this remains
WARNING_THRESHOLD = timedelta(hours=24)
CRITICAL_THRESHOLD = timedelta(hours=6)
DEFAULT_REFRESH_INTERVAL_HOURS = float(os.getenv("TOKEN_REFRESH_INTERVAL_HOURS", "1"))
DEFAULT_REFRESH_BEFORE_HOURS = float(os.getenv("TOKEN_REFRESH_BEFORE_HOURS", "12"))
DEFAULT_REFRESH_FORCE = os.getenv("TOKEN_REFRESH_FORCE", "true").lower() == "true"

BASE_DIR = Path(__file__).resolve().parent.parent
V2FUN_DATA_DIR = BASE_DIR / "v2fun_data"
ACCOUNT_FILE = BASE_DIR / "account.txt"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def decode_jwt(token: str) -> dict:
    """Decode JWT payload without verification"""
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def get_token_expiry(token: str) -> Optional[datetime]:
    """Get expiry datetime from JWT token"""
    payload = decode_jwt(token)
    exp = payload.get('exp')
    if exp:
        return datetime.fromtimestamp(exp)
    return None


def get_time_remaining(token: str) -> timedelta:
    """Get time remaining before token expires"""
    expiry = get_token_expiry(token)
    if not expiry:
        return timedelta(0)

    remaining = expiry - datetime.now()
    if remaining.total_seconds() < 0:
        return timedelta(0)
    return remaining


def is_token_valid(token: str) -> bool:
    """Check if token is still valid"""
    return get_time_remaining(token) > timedelta(minutes=5)


def is_token_expiring_soon(token: str) -> bool:
    """Check if token will expire within 24 hours"""
    return get_time_remaining(token) < WARNING_THRESHOLD


def is_token_critical(token: str) -> bool:
    """Check if token will expire within 6 hours"""
    return get_time_remaining(token) < CRITICAL_THRESHOLD


def should_refresh_token(token: str, min_remaining_hours: float = DEFAULT_REFRESH_BEFORE_HOURS) -> bool:
    """Return True if token is missing, expired, or has less than min_remaining_hours left."""
    if not token:
        return True
    return get_time_remaining(token) < timedelta(hours=min_remaining_hours)


def get_token_status(token: str) -> str:
    """Get token status: valid, warning, critical, expired, missing"""
    if not token:
        return "missing"

    remaining = get_time_remaining(token)
    if remaining <= timedelta(0):
        return "expired"
    if remaining < CRITICAL_THRESHOLD:
        return "critical"
    if remaining < WARNING_THRESHOLD:
        return "warning"
    return "valid"


def refresh_token_sync(email: str, password: str, headless: bool = True) -> Tuple[bool, Optional[str]]:
    """
    Refresh token by running v2fun_google_login.py in isolated temp-file mode.
    Returns (success, new_token).
    """
    if not email or not password:
        return False, None

    from v2fun_scripts.v2fun_client import get_session_file_path, extract_token_from_session

    V2FUN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_account_file = V2FUN_DATA_DIR / f"temp_refresh_{uuid.uuid4().hex}.txt"
    login_script = BASE_DIR / "v2fun_scripts" / "v2fun_google_login.py"
    session_file = get_session_file_path(email, V2FUN_DATA_DIR)

    try:
        temp_account_file.write_text(f"{email}:{password}\n", encoding="utf-8")
        cmd = [sys.executable, str(login_script), "--force", "--account-file", str(temp_account_file)]
        if headless:
            cmd.append("--headless")

        subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR))

        if session_file.exists():
            with open(session_file, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            new_token = extract_token_from_session(session_data)
            if new_token and is_token_valid(new_token):
                return True, new_token
        return False, None
    except Exception as e:
        print(f"[TOKEN] Refresh error for {email}: {e}")
        return False, None
    finally:
        if temp_account_file.exists():
            temp_account_file.unlink()


def check_and_refresh_if_needed(
    token: str,
    email: str,
    password: str = None,
    min_remaining_hours: float = DEFAULT_REFRESH_BEFORE_HOURS,
    force: bool = False,
) -> Tuple[str, Optional[str]]:
    """
    Check token status and refresh if expired or below min_remaining_hours.
    Returns (status, new_token_or_None).
    """
    status = get_token_status(token)
    if force or should_refresh_token(token, min_remaining_hours):
        if password:
            print(f"[TOKEN] {email}: {status} - attempting headless refresh...")
            success, new_token = refresh_token_sync(email, password)
            if success and new_token:
                print(f"[TOKEN] {email}: refreshed successfully")
                return "refreshed", new_token
            print(f"[TOKEN] {email}: refresh failed")
            return "refresh_failed", None
        print(f"[TOKEN] {email}: {status} - no password for refresh")
        return status, None

    return status, None


def _sync_refreshed_token_to_db(email: str, old_token: str, new_token: str):
    """Persist refreshed token to v2fun_accounts and any matching users row."""
    from v2fun_scripts.database import db_session, upsert_v2fun_account_from_session

    expiry = get_token_expiry(new_token)
    expiry_str = expiry.isoformat() if expiry else None
    upsert_v2fun_account_from_session(email, new_token, expiry_str)

    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE v2fun_accounts
            SET jwt_token = ?, token_expiry = ?, status = 'done',
                error_message = NULL, processed_at = CURRENT_TIMESTAMP
            WHERE email = ?
            """,
            (new_token, expiry_str, email),
        )
        if old_token:
            cursor.execute(
                """
                UPDATE users
                SET v2fun_token = ?, updated_at = CURRENT_TIMESTAMP
                WHERE v2fun_token = ? OR google_email = ?
                """,
                (new_token, old_token, email),
            )
        conn.commit()


def refresh_all_expiring_tokens(
    force: bool = False,
    min_remaining_hours: float = DEFAULT_REFRESH_BEFORE_HOURS,
    on_refreshed: Optional[Callable[[], None]] = None,
) -> Dict:
    """
    Scan all known accounts (DB + account.txt) and refresh tokens that are missing,
    expired, or have less than min_remaining_hours remaining.
    """
    from v2fun_scripts.database import db_session
    from v2fun_scripts.v2fun_client import parse_account_lines, get_session_file_path, extract_token_from_session

    candidates: Dict[str, Dict] = {}

    # 1. Load accounts with passwords from DB
    try:
        with db_session() as conn:
            rows = conn.execute(
                "SELECT email, password, jwt_token FROM v2fun_accounts WHERE password IS NOT NULL AND password != ''"
            ).fetchall()
            for r in rows:
                candidates[r["email"]] = {
                    "email": r["email"],
                    "password": r["password"],
                    "token": r["jwt_token"] or "",
                }
    except Exception as e:
        print(f"[TOKEN] DB read error during refresh scan: {e}")

    # 2. Merge passwords from account.txt
    if ACCOUNT_FILE.exists():
        for email, password in parse_account_lines(ACCOUNT_FILE.read_text(encoding="utf-8")):
            if email not in candidates:
                candidates[email] = {"email": email, "password": password, "token": ""}
            elif not candidates[email]["password"]:
                candidates[email]["password"] = password

    checked = 0
    refreshed = 0
    skipped = 0
    failed = 0
    details = []

    for email, info in candidates.items():
        checked += 1
        current_token = info["token"]

        # Prefer latest token from session file if newer/present
        sess_file = get_session_file_path(email, V2FUN_DATA_DIR)
        if sess_file.exists():
            try:
                sess_data = json.loads(sess_file.read_text(encoding="utf-8"))
                file_token = extract_token_from_session(sess_data)
                if file_token and get_time_remaining(file_token) > get_time_remaining(current_token):
                    current_token = file_token
            except Exception:
                pass

        if not force and not should_refresh_token(current_token, min_remaining_hours):
            skipped += 1
            details.append({"email": email, "action": "skipped", "status": get_token_status(current_token)})
            continue

        new_status, new_token = check_and_refresh_if_needed(
            current_token,
            email,
            info["password"],
            min_remaining_hours=min_remaining_hours,
            force=force,
        )
        if new_token:
            _sync_refreshed_token_to_db(email, current_token, new_token)
            refreshed += 1
            details.append({"email": email, "action": "refreshed", "status": "valid"})
        else:
            failed += 1
            details.append({"email": email, "action": "failed", "status": new_status})

    if refreshed > 0 and on_refreshed:
        try:
            on_refreshed()
        except Exception as e:
            print(f"[TOKEN] on_refreshed callback error: {e}")

    return {
        "checked": checked,
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
        "details": details,
        "timestamp": datetime.now().isoformat(),
    }


class TokenRefreshScheduler:
    """Periodic background scheduler (default every 1h) + on-demand trigger."""

    def __init__(
        self,
        interval_hours: float = DEFAULT_REFRESH_INTERVAL_HOURS,
        min_remaining_hours: float = DEFAULT_REFRESH_BEFORE_HOURS,
        force: bool = DEFAULT_REFRESH_FORCE,
        on_refreshed: Optional[Callable[[], None]] = None,
    ):
        self.interval_seconds = max(60.0, float(interval_hours) * 3600.0)
        self.min_remaining_hours = float(min_remaining_hours)
        self.force = bool(force)
        self.on_refreshed = on_refreshed
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.last_run_at: Optional[str] = None
        self.next_run_at: Optional[str] = None
        self.last_summary: Optional[Dict] = None
        self.is_refreshing: bool = False

    def start(self):
        """Start the background refresh loop if not already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TokenRefreshScheduler")
        self._thread.start()
        print(
            f"[TOKEN-SCHEDULER] Started (interval={self.interval_seconds / 3600:.2f}h, "
            f"force={self.force}, refresh_before={self.min_remaining_hours:.1f}h)"
        )

    def stop(self):
        """Stop the background refresh loop."""
        self._stop_event.set()

    def _loop(self):
        # Run an initial check shortly after startup so tokens stay fresh continuously
        if self._stop_event.wait(5):
            return
        while not self._stop_event.is_set():
            self.trigger_now(force=self.force)
            self.next_run_at = (datetime.now() + timedelta(seconds=self.interval_seconds)).isoformat()
            if self._stop_event.wait(self.interval_seconds):
                break

    def trigger_now(self, force: bool = False) -> Dict:
        """Run a refresh cycle synchronously (serialized via lock)."""
        if not self._run_lock.acquire(blocking=False):
            return {"busy": True, "message": "Token refresh cycle already in progress"}
        try:
            self.is_refreshing = True
            summary = refresh_all_expiring_tokens(
                force=force,
                min_remaining_hours=self.min_remaining_hours,
                on_refreshed=self.on_refreshed,
            )
            self.last_run_at = summary["timestamp"]
            self.last_summary = summary
            return summary
        finally:
            self.is_refreshing = False
            self._run_lock.release()

    def trigger_now_async(self, force: bool = False) -> bool:
        """Trigger a refresh cycle in a background daemon thread."""
        if self.is_refreshing:
            return False
        t = threading.Thread(target=self.trigger_now, kwargs={"force": force}, daemon=True)
        t.start()
        return True

    def status(self) -> Dict:
        """Return scheduler status and last cycle summary."""
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "is_refreshing": self.is_refreshing,
            "interval_hours": self.interval_seconds / 3600.0,
            "force": self.force,
            "min_remaining_hours": self.min_remaining_hours,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_summary": self.last_summary,
        }


def get_all_tokens_status() -> list:
    """Get status of all saved V2Fun tokens"""
    v2fun_data = V2FUN_DATA_DIR
    if not v2fun_data.exists():
        return []
    session_files = list(v2fun_data.glob("v2fun_session_*_latest.json"))

    results = []
    for file in session_files:
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        email = data.get("email")
        tokens = data.get("tokens", {})
        token = tokens.get("cookie_token") or tokens.get("localStorage_access_token", "")

        status = get_token_status(token)
        remaining = get_time_remaining(token)
        expiry = get_token_expiry(token)

        results.append({
            "email": email,
            "status": status,
            "remaining": str(remaining),
            "expiry": expiry.isoformat() if expiry else None,
        })

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V2Fun.ai Token Manager & Auto-Refresh")
    parser.add_argument("--refresh-now", action="store_true", help="Refresh expiring/missing tokens immediately")
    parser.add_argument("--force", action="store_true", help="Force refresh all accounts regardless of remaining time")
    parser.add_argument("--daemon", action="store_true", help="Run continuous background refresh loop")
    parser.add_argument("--interval-hours", type=float, default=DEFAULT_REFRESH_INTERVAL_HOURS, help="Check interval in hours (default: 3)")
    parser.add_argument("--min-hours", type=float, default=DEFAULT_REFRESH_BEFORE_HOURS, help="Refresh when remaining hours < this value (default: 12)")
    args = parser.parse_args()

    if args.daemon:
        scheduler = TokenRefreshScheduler(
            interval_hours=args.interval_hours,
            min_remaining_hours=args.min_hours,
        )
        scheduler.start()
        print(f"[TOKEN-SCHEDULER] Running continuously every {args.interval_hours}h. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            scheduler.stop()
            print("\n[TOKEN-SCHEDULER] Stopped.")
    elif args.refresh_now:
        summary = refresh_all_expiring_tokens(force=args.force, min_remaining_hours=args.min_hours)
        print(json.dumps(summary, indent=2))
    else:
        print("=" * 80)
        print("V2Fun.ai Token Status Check")
        print("=" * 80)
        statuses = get_all_tokens_status()
        for s in statuses:
            emoji = {
                "valid": "[OK]",
                "warning": "[!]",
                "critical": "[!!]",
                "expired": "[X]",
                "missing": "[?]",
            }.get(s["status"], "[?]")
            print(f"{emoji} {str(s['email']):40} {s['status']:10} Remaining: {s['remaining']}")
