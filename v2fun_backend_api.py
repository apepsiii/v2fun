"""
V2Fun Backend API for Hermes Agent Integration
REST API with round-robin account selection and model priority

Features:
- REST API endpoint for Hermes agent
- Round-robin account rotation
- Model priority system
- Queue management
- Telegram notification
- Status tracking

Usage:
    python v2fun_backend_api.py

API Endpoints:
    POST /api/generate - Generate image
    GET  /api/status/{job_id} - Check generation status
    GET  /api/health - Health check
    GET  /api/accounts - List available accounts
"""

from flask import Flask, request, jsonify, send_file, Response
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict
import uuid

try:
    from flask_cors import CORS
except ImportError:
    CORS = None

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

sys.path.insert(0, str(BASE_DIR))
from v2fun_scripts.database import (
    get_db,
    init_db,
    save_backend_job,
    get_backend_job,
    list_backend_jobs,
    delete_backend_job,
)
from v2fun_scripts.token_manager import (
    is_token_valid,
    get_token_status,
    TokenRefreshScheduler,
)
from v2fun_scripts.image_downloader import download_image
from v2fun_scripts.v2fun_client import (
    V2FunClient,
    MODEL_PRIORITY,
    VALID_RATIOS,
    VALID_QUALITIES,
)

app = Flask(__name__)
_cors_origins = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]
if CORS is not None:
    CORS(
        app,
        resources={
            r"/api/*": {"origins": _cors_origins if _cors_origins != ["*"] else "*"}
        },
    )
else:

    @app.after_request
    def _add_cors_headers(response):
        origin = request.headers.get("Origin")
        if _cors_origins == ["*"]:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin and origin in _cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-API-Key"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return response


BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "").strip()


@app.before_request
def _check_api_auth():
    """Enforce API key on /api/* endpoints (except /api/health) when BACKEND_API_KEY is configured."""
    if not BACKEND_API_KEY:
        return None
    if request.method == "OPTIONS":
        return None
    if not request.path.startswith("/api/") or request.path == "/api/health":
        return None

    auth_header = request.headers.get("Authorization", "")
    bearer_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    api_key = request.headers.get("X-API-Key", "").strip() or bearer_token
    if not api_key or not secrets.compare_digest(api_key, BACKEND_API_KEY):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return None


# Account round-robin state
account_index = 0
account_lock = threading.Lock()

# In-memory active job cache (backed by SQLite backend_jobs table)
jobs = {}  # {job_id: {status, prompt, result, error, ...}}
jobs_lock = threading.Lock()


def _persist_job(job: dict):
    """Persist job state to SQLite without breaking callers if DB write fails."""
    try:
        save_backend_job(job)
    except Exception as e:
        print(f"[DB] Failed to persist job {job.get('id')}: {e}")


def _fetch_job(job_id: str) -> Optional[Dict]:
    """Fetch job from in-memory cache or fall back to SQLite."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            return dict(job)
    return get_backend_job(job_id)


class AccountPool:
    """Manage V2Fun accounts with round-robin selection"""

    def __init__(self):
        self.accounts = []
        self.current_index = 0
        self.lock = threading.Lock()
        self.load_accounts()

    def load_accounts(self):
        """Load all active V2Fun accounts from database"""
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, email, status, jwt_token, token_expiry
            FROM v2fun_accounts
            WHERE status = 'done' AND jwt_token IS NOT NULL
            ORDER BY id ASC
        """)

        rows = cursor.fetchall()
        conn.close()

        self.accounts = []
        for row in rows:
            account = {
                "id": row[0],
                "email": row[1],
                "status": row[2],
                "token": row[3],
                "expiry": row[4],
            }

            # Check if token is still valid
            if is_token_valid(account["token"]):
                self.accounts.append(account)

        print(f"[POOL] Loaded {len(self.accounts)} valid accounts")

    def get_next_account(self) -> Optional[Dict]:
        """Get next account using round-robin"""
        with self.lock:
            if not self.accounts:
                return None

            account = self.accounts[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.accounts)

            # Re-check token validity
            if not is_token_valid(account["token"]):
                print(f"[POOL] Token expired for {account['email']}, reloading pool...")
                self.load_accounts()
                return self.get_next_account()

            return account

    def reload(self):
        """Reload account pool (e.g., after token refresh)"""
        with self.lock:
            old_count = len(self.accounts)
            self.load_accounts()
            new_count = len(self.accounts)
            print(f"[POOL] Reloaded: {old_count} -> {new_count} accounts")


def send_telegram_notification(
    message: str, telegram_token: str = None, chat_id: str = None
):
    """Send notification to Telegram"""
    if not telegram_token or not chat_id:
        # Load from env or config
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not telegram_token or not chat_id:
        print(f"[TELEGRAM] Not configured, skipping: {message}")
        return False

    try:
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")
        return False


def process_generation_job(
    job_id: str, prompt: str, account: Dict, model: str, quality: str, ratio: str
):
    """Background worker to process generation with model fallback"""
    try:
        # Update job status
        with jobs_lock:
            jobs[job_id]["status"] = "processing"
            jobs[job_id]["account"] = account["email"]
            jobs[job_id]["model"] = model
            jobs[job_id]["started_at"] = datetime.now().isoformat()
            _persist_job(jobs[job_id])

        # Create client
        client = V2FunClient(account["token"])

        # Try models in priority order (fallback mechanism)
        models_to_try = [model] + [m for m in MODEL_PRIORITY if m != model]

        for attempt_idx, attempt_model in enumerate(models_to_try):
            print(
                f"[JOB {job_id}] Trying model: {attempt_model} (attempt {attempt_idx + 1}/{len(models_to_try)})"
            )

            # Generate
            result = client.generate_image(prompt, attempt_model, quality, ratio)

            if result.get("success"):
                task_uuid = result.get("result", {}).get("taskuuid")

                # Update job with task_uuid
                with jobs_lock:
                    jobs[job_id]["status"] = "rendering"
                    jobs[job_id]["task_uuid"] = task_uuid
                    jobs[job_id]["model"] = attempt_model  # Update to actual model used
                    jobs[job_id]["result"] = result
                    _persist_job(jobs[job_id])

                print(f"[JOB {job_id}] Task submitted to V2Fun: {task_uuid}")

                # Poll for result (wait until image is ready)
                poll_result = client.poll_result(task_uuid, max_attempts=60, interval=5)

                if poll_result.get("success"):
                    # Image generation completed!
                    work_url = poll_result.get("workUrl")
                    thumb = poll_result.get("thumb")

                    # Download image to local storage
                    print(f"[JOB {job_id}] Downloading image...")
                    local_path = download_image(work_url, job_id, prompt)

                    with jobs_lock:
                        jobs[job_id]["status"] = "completed"
                        jobs[job_id]["work_url"] = work_url
                        jobs[job_id]["thumb"] = thumb
                        jobs[job_id]["local_path"] = local_path
                        jobs[job_id]["poll_attempts"] = poll_result.get("attempts")
                        jobs[job_id]["progress"] = 100
                        jobs[job_id]["completed_at"] = datetime.now().isoformat()
                        _persist_job(jobs[job_id])

                    print(f"[JOB {job_id}] Completed! URL: {work_url}")

                    # Send Telegram notification
                    msg = f"✅ *Generation Completed*\n"
                    msg += f"Job ID: `{job_id}`\n"
                    msg += f"Prompt: {prompt[:50]}...\n"
                    msg += f"Model: {attempt_model}\n"
                    if attempt_idx > 0:
                        msg += f"⚠️ Fallback used (original: {model})\n"
                    msg += f"Account: {account['email']}\n"
                    msg += f"Image URL: {work_url}"
                    send_telegram_notification(msg)
                    return  # Success!
                else:
                    # Polling failed or timeout
                    error_msg = poll_result.get("error", "Polling failed")

                    with jobs_lock:
                        jobs[job_id]["status"] = "timeout"
                        jobs[job_id]["error"] = error_msg
                        jobs[job_id]["task_uuid"] = task_uuid
                        jobs[job_id]["completed_at"] = datetime.now().isoformat()
                        _persist_job(jobs[job_id])

                    print(f"[JOB {job_id}] Polling failed: {error_msg}")

                    # Send Telegram notification
                    msg = f"⚠️ *Generation Timeout*\n"
                    msg += f"Job ID: `{job_id}`\n"
                    msg += f"Prompt: {prompt[:50]}...\n"
                    msg += f"Error: {error_msg}\n"
                    msg += f"Task UUID: {task_uuid} (check manually on V2Fun)"
                    send_telegram_notification(msg)
                    return
            else:
                # Generation API failed
                error_msg = result.get("message") or result.get(
                    "error", "Unknown error"
                )
                error_lower = error_msg.lower()

                # Check if error is quota/limit related
                is_quota_error = any(
                    keyword in error_lower
                    for keyword in [
                        "quota",
                        "limit",
                        "insufficient",
                        "exceeded",
                        "balance",
                    ]
                )

                if is_quota_error and attempt_idx < len(models_to_try) - 1:
                    # Quota error and we have more models to try
                    print(
                        f"[FALLBACK] Model {attempt_model} quota exceeded, trying next model..."
                    )

                    with jobs_lock:
                        jobs[job_id]["fallback_attempts"].append(
                            {
                                "model": attempt_model,
                                "error": error_msg,
                                "timestamp": datetime.now().isoformat(),
                            }
                        )

                    continue  # Try next model
                else:
                    # Non-quota error or last model failed
                    with jobs_lock:
                        jobs[job_id]["status"] = "failed"
                        jobs[job_id]["error"] = error_msg
                        jobs[job_id]["completed_at"] = datetime.now().isoformat()
                        _persist_job(jobs[job_id])

                    # Send Telegram notification
                    msg = f"❌ *Generation Failed*\n"
                    msg += f"Job ID: `{job_id}`\n"
                    msg += f"Prompt: {prompt[:50]}...\n"
                    msg += f"Error: {error_msg}\n"
                    msg += f"Account: {account['email']}"
                    if len(jobs[job_id]["fallback_attempts"]) > 0:
                        msg += f"\n⚠️ Tried {len(jobs[job_id]['fallback_attempts'])} fallback model(s)"
                    send_telegram_notification(msg)
                    return

        # All models failed
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = "All models failed (quota exhausted)"
            jobs[job_id]["completed_at"] = datetime.now().isoformat()
            _persist_job(jobs[job_id])

        msg = f"❌ *All Models Failed*\n"
        msg += f"Job ID: `{job_id}`\n"
        msg += f"Prompt: {prompt[:50]}...\n"
        msg += f"Tried {len(models_to_try)} models, all quota exhausted"
        send_telegram_notification(msg)

    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["completed_at"] = datetime.now().isoformat()
            _persist_job(jobs[job_id])

        # Send Telegram notification
        msg = f"⚠️ *Generation Error*\n"
        msg += f"Job ID: `{job_id}`\n"
        msg += f"Error: {str(e)}"
        send_telegram_notification(msg)


# Ensure database schema exists before loading pool
init_db()

# Initialize account pool & continuous token auto-refresh scheduler
account_pool = AccountPool()
token_scheduler = TokenRefreshScheduler(
    interval_hours=float(os.getenv("TOKEN_REFRESH_INTERVAL_HOURS", "1")),
    min_remaining_hours=float(os.getenv("TOKEN_REFRESH_BEFORE_HOURS", "12")),
    force=os.getenv("TOKEN_REFRESH_FORCE", "true").lower() == "true",
    on_refreshed=account_pool.reload,
)


@app.route("/api/refresh-tokens", methods=["POST"])
def trigger_token_refresh():
    """Trigger immediate token refresh cycle (on-demand / on-activation)."""
    data = request.json or {}
    force = bool(data.get("force", False))
    run_async = bool(data.get("async", True))
    if run_async:
        started = token_scheduler.trigger_now_async(force=force)
        return jsonify({
            "success": True,
            "started": started,
            "message": "Token refresh started in background" if started else "Token refresh already running",
            "status": token_scheduler.status(),
        })
    summary = token_scheduler.trigger_now(force=force)
    return jsonify({"success": True, "summary": summary, "status": token_scheduler.status()})


@app.route("/api/token-refresh-status", methods=["GET"])
def get_token_refresh_status():
    """Get background token refresh scheduler status."""
    return jsonify({"success": True, "scheduler": token_scheduler.status()})


@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify(
        {
            "status": "healthy",
            "version": "1.0.0",
            "accounts_available": len(account_pool.accounts),
            "active_jobs": len(
                [j for j in jobs.values() if j["status"] == "processing"]
            ),
        }
    )


@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    """List available accounts"""
    accounts_info = []
    for acc in account_pool.accounts:
        accounts_info.append(
            {"email": acc["email"], "status": get_token_status(acc["token"])}
        )

    return jsonify(
        {"success": True, "accounts": accounts_info, "total": len(accounts_info)}
    )


@app.route("/api/generate", methods=["POST"])
def generate_image():
    """
    Generate image endpoint for Hermes agent

    Request Body:
    {
        "prompt": "your prompt here",
        "model": "gpt-image-2.5-sunburst" (optional, auto-selected by priority),
        "quality": "medium" (optional: low/medium/high),
        "ratio": "16:9" (optional: 1:1/16:9/9:16),
        "telegram_token": "..." (optional),
        "telegram_chat_id": "..." (optional)
    }

    Response:
    {
        "success": true,
        "job_id": "uuid",
        "status": "queued",
        "account": "email@gmail.com",
        "model": "gpt-image-2.5-sunburst"
    }
    """
    try:
        data = request.json

        if not data or not data.get("prompt"):
            return jsonify({"success": False, "error": "Prompt is required"}), 400

        prompt = str(data.get("prompt", "")).strip()
        if not prompt:
            return jsonify({"success": False, "error": "Prompt is required"}), 400
        if len(prompt) > 5000:
            return (
                jsonify(
                    {"success": False, "error": "Prompt too long (max 5000 characters)"}
                ),
                400,
            )

        model = data.get("model", MODEL_PRIORITY[0])  # Default to best model
        quality = data.get("quality", "high")  # Default to high quality
        ratio = data.get("ratio", "16:9")

        # Validate model, quality, ratio
        if model not in MODEL_PRIORITY:
            model = MODEL_PRIORITY[0]
        if quality not in VALID_QUALITIES:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"Invalid quality. Choose from: {', '.join(sorted(VALID_QUALITIES))}",
                    }
                ),
                400,
            )
        if ratio not in VALID_RATIOS:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"Invalid ratio. Choose from: {', '.join(sorted(VALID_RATIOS))}",
                    }
                ),
                400,
            )

        # Get next account (round-robin)
        account = account_pool.get_next_account()

        if not account:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "No available accounts. Please login some accounts first.",
                    }
                ),
                503,
            )

        # Create job
        job_id = str(uuid.uuid4())

        with jobs_lock:
            # Prune oldest completed/failed jobs if job store exceeds 500 entries
            if len(jobs) >= 500:
                terminal_ids = [
                    jid
                    for jid, j in sorted(
                        jobs.items(), key=lambda kv: kv[1].get("created_at", "")
                    )
                    if j.get("status") in ("completed", "failed", "timeout", "error")
                ]
                for jid in terminal_ids[: len(jobs) - 499]:
                    jobs.pop(jid, None)

            jobs[job_id] = {
                "id": job_id,
                "prompt": prompt,
                "status": "queued",
                "source": "api",  # "api" or "manual"
                "account": account["email"],
                "model": model,
                "quality": quality,
                "ratio": ratio,
                "created_at": datetime.now().isoformat(),
                "started_at": None,
                "completed_at": None,
                "task_uuid": None,
                "work_url": None,
                "thumb": None,
                "local_path": None,  # Downloaded image path
                "progress": 0,  # 0-100%
                "poll_attempts": 0,
                "fallback_attempts": [],  # List of fallback attempts
                "result": None,
                "error": None,
            }
            _persist_job(jobs[job_id])

        # Start background worker
        thread = threading.Thread(
            target=process_generation_job,
            args=(job_id, prompt, account, model, quality, ratio),
            daemon=True,
        )
        thread.start()

        # Send Telegram notification
        msg = f"🚀 *New Generation Started*\n"
        msg += f"Job ID: `{job_id}`\n"
        msg += f"Prompt: {prompt[:50]}...\n"
        msg += f"Model: {model}\n"
        msg += f"Account: {account['email']}"
        send_telegram_notification(
            msg, data.get("telegram_token"), data.get("telegram_chat_id")
        )

        return jsonify(
            {
                "success": True,
                "job_id": job_id,
                "status": "queued",
                "account": account["email"],
                "model": model,
                "quality": quality,
                "ratio": ratio,
            }
        )

    except Exception as e:
        import traceback

        error_trace = traceback.format_exc()
        print(f"[ERROR] Generate endpoint exception: {error_trace}")

        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Internal server error: {str(e)}",
                    "trace": error_trace if app.debug else None,
                }
            ),
            500,
        )


@app.route("/api/status/<job_id>", methods=["GET"])
def get_job_status(job_id):
    """Get generation job status"""
    job = _fetch_job(job_id)

    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    return jsonify({"success": True, "job": job})


@app.route("/api/reload-accounts", methods=["POST"])
def reload_accounts():
    """Reload account pool (admin endpoint)"""
    account_pool.reload()

    return jsonify({"success": True, "accounts_available": len(account_pool.accounts)})


@app.route("/api/image/<job_id>", methods=["GET"])
def serve_job_image(job_id):
    """Serve downloaded image for a job"""
    job = _fetch_job(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if not job.get("local_path"):
        return jsonify({"error": "Image not downloaded yet"}), 404

    try:
        results_root = (BASE_DIR / "v2fun_data" / "results").resolve()
        raw_path = Path(job["local_path"])
        img_path = (
            raw_path if raw_path.is_absolute() else (BASE_DIR / raw_path)
        ).resolve()
        if results_root not in img_path.parents:
            return jsonify({"error": "Invalid image path"}), 403
        mime_type, _ = mimetypes.guess_type(str(img_path))
        return send_file(str(img_path), mimetype=mime_type or "image/jpeg")
    except Exception as e:
        return jsonify({"error": f"Failed to serve image: {str(e)}"}), 500


@app.route("/api/stream/<job_id>")
def stream_job_progress(job_id):
    """SSE endpoint for real-time job updates"""

    def event_stream():
        last_status = None
        last_progress = None
        attempts = 0
        max_attempts = 120  # 10 minutes with 5s interval

        while attempts < max_attempts:
            job = _fetch_job(job_id)

            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            # Send update if status or progress changed
            if job["status"] != last_status or job.get("progress") != last_progress:
                yield f"data: {json.dumps(job)}\n\n"
                last_status = job["status"]
                last_progress = job.get("progress")

            # Stop streaming if job is in terminal state
            if job["status"] in ["completed", "failed", "timeout", "error"]:
                break

            time.sleep(5)  # Poll every 5 seconds
            attempts += 1

        # Final update
        job = _fetch_job(job_id)
        if job:
            yield f"data: {json.dumps(job)}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/gallery", methods=["GET"])
def get_gallery():
    """Get all completed jobs with images"""
    persisted = list_backend_jobs(status="completed", limit=200)
    by_id = {j["id"]: j for j in persisted if j.get("work_url")}
    with jobs_lock:
        for j in jobs.values():
            if j["status"] == "completed" and j.get("work_url"):
                by_id[j["id"]] = dict(j)
    completed = sorted(
        by_id.values(), key=lambda x: x.get("completed_at") or "", reverse=True
    )

    return jsonify({"success": True, "total": len(completed), "images": completed})


@app.route("/api/gallery/<job_id>", methods=["DELETE"])
def delete_from_gallery(job_id):
    """Delete job and its image"""
    job = _fetch_job(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Delete local file if exists (scoped to v2fun_data/results)
    if job.get("local_path"):
        try:
            results_root = (BASE_DIR / "v2fun_data" / "results").resolve()
            raw_path = Path(job["local_path"])
            local_path = (
                raw_path if raw_path.is_absolute() else (BASE_DIR / raw_path)
            ).resolve()
            if results_root in local_path.parents and local_path.exists():
                local_path.unlink()
                print(f"[GALLERY] Deleted local image: {local_path}")
        except Exception as e:
            print(f"[GALLERY] Failed to delete local image: {e}")

    with jobs_lock:
        jobs.pop(job_id, None)
    delete_backend_job(job_id)

    return jsonify({"success": True, "message": "Job and image deleted"})


@app.route("/api/jobs", methods=["GET"])
def list_all_jobs():
    """List all jobs with optional filtering"""
    status_filter = request.args.get("status")  # e.g., ?status=completed
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid limit parameter"}), 400

    persisted = list_backend_jobs(status=status_filter, limit=limit)
    by_id = {j["id"]: j for j in persisted}
    with jobs_lock:
        for j in jobs.values():
            if not status_filter or j["status"] == status_filter:
                by_id[j["id"]] = dict(j)

    job_list = sorted(
        by_id.values(), key=lambda x: x.get("created_at") or "", reverse=True
    )[:limit]

    return jsonify({"success": True, "total": len(job_list), "jobs": job_list})


# ============================================================================
# WEB UI - Simple dashboard for Backend API
# ============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>V2Fun Backend API - Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --color-primary: #7C3AED;
            --color-on-primary: #FFFFFF;
            --color-secondary: #6366F1;
            --color-accent: #EC4899;
            --color-background: #FAF5FF;
            --color-foreground: #0F172A;
            --color-muted: #F7F3FD;
            --color-border: #EFE7FC;
            --color-destructive: #DC2626;
            --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
            --space-6: 24px; --space-8: 32px; --space-10: 40px; --space-12: 48px;
            --radius-sm: 6px; --radius-md: 10px; --radius-lg: 16px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'DM Sans', sans-serif;
            background: var(--color-background);
            color: var(--color-foreground);
            min-height: 100vh;
        }
        .sidebar {
            position: fixed; top: 0; left: 0; bottom: 0; width: 240px;
            background: white; border-right: 1px solid var(--color-border);
            display: flex; flex-direction: column; padding: var(--space-6);
            z-index: 100;
        }
        .sidebar-logo {
            font-family: 'Space Grotesk', sans-serif; font-size: 22px;
            font-weight: 700; color: var(--color-primary);
            margin-bottom: var(--space-8); padding-bottom: var(--space-6);
            border-bottom: 1px solid var(--color-border);
        }
        .sidebar-logo span { color: var(--color-accent); }
        .nav-item {
            display: flex; align-items: center; gap: var(--space-3);
            padding: var(--space-3) var(--space-4); border-radius: var(--radius-md);
            font-size: 14px; font-weight: 500; color: #64748b;
            cursor: pointer; transition: background 150ms ease, color 150ms ease;
            text-decoration: none; margin-bottom: var(--space-1);
        }
        .nav-item:hover { background: var(--color-muted); color: var(--color-primary); }
        .nav-item.active { background: var(--color-muted); color: var(--color-primary); font-weight: 600; }
        .nav-icon { width: 18px; height: 18px; }
        .sidebar-bottom {
            margin-top: auto; padding-top: var(--space-6);
            border-top: 1px solid var(--color-border);
        }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: #10b981; display: inline-block; margin-right: 6px;
        }
        .status-dot.offline { background: #ef4444; }
        .main { margin-left: 240px; padding: var(--space-8); min-height: 100vh; }
        .page { display: none; }
        .page.active { display: block; }
        .page-header { margin-bottom: var(--space-8); }
        .page-header h1 {
            font-family: 'Space Grotesk', sans-serif;
            font-size: 28px; font-weight: 700; margin-bottom: var(--space-2);
        }
        .page-header p { color: #64748b; font-size: 14px; }
        .card {
            background: white; border-radius: var(--radius-lg);
            padding: var(--space-6); border: 1px solid var(--color-border);
            margin-bottom: var(--space-6);
        }
        .card-title {
            font-family: 'Space Grotesk', sans-serif;
            font-size: 16px; font-weight: 600; margin-bottom: var(--space-4);
        }
        .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space-4); margin-bottom: var(--space-6); }
        .stat-card {
            background: white; border-radius: var(--radius-lg); padding: var(--space-6);
            border: 1px solid var(--color-border); text-align: center;
        }
        .stat-value { font-family: 'Space Grotesk', sans-serif; font-size: 32px; font-weight: 700; color: var(--color-primary); }
        .stat-label { font-size: 13px; color: #64748b; margin-top: 4px; }
        .form-group { margin-bottom: var(--space-4); }
        .form-group label { display: block; margin-bottom: var(--space-2); font-size: 13px; font-weight: 500; }
        .form-group input, .form-group select, .form-group textarea {
            width: 100%; padding: 10px 14px; border: 2px solid var(--color-border);
            border-radius: var(--radius-md); font-size: 14px; font-family: inherit;
            transition: border-color 150ms ease; background: white; color: var(--color-foreground);
        }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            outline: none; border-color: var(--color-primary);
        }
        .form-group textarea { resize: vertical; min-height: 120px; }
        .form-row { display: grid; grid-template-columns: 2fr 1fr; gap: var(--space-4); }
        .btn {
            padding: 10px 20px; border-radius: var(--radius-md); font-size: 14px;
            font-weight: 600; font-family: 'Space Grotesk', sans-serif;
            cursor: pointer; transition: opacity 150ms ease, transform 150ms ease;
            border: none; display: inline-flex; align-items: center; gap: var(--space-2);
        }
        .btn:hover { opacity: 0.9; transform: translateY(-1px); }
        .btn:active { transform: translateY(0); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .btn-primary { background: var(--color-primary); color: white; }
        .btn-accent { background: var(--color-accent); color: white; }
        .btn-outline { background: transparent; color: var(--color-primary); border: 2px solid var(--color-primary); }
        .btn-sm { padding: 6px 14px; font-size: 12px; }
        .btn-full { width: 100%; justify-content: center; }
        .alert {
            padding: 12px 16px; border-radius: var(--radius-md);
            margin-bottom: var(--space-4); font-size: 14px; display: none;
        }
        .alert.active { display: block; }
        .alert.success { background: #D1FAE5; color: #065F46; border: 1px solid #A7F3D0; }
        .alert.error { background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA; }
        .alert.info { background: #E0E7FF; color: #3730A3; border: 1px solid #C7D2FE; }
        .badge {
            display: inline-flex; align-items: center; padding: 2px 8px;
            border-radius: 99px; font-size: 11px; font-weight: 600;
        }
        .badge-queued { background: #FEF3C7; color: #92400E; }
        .badge-pending { background: #FEF3C7; color: #92400E; }
        .badge-processing { background: #E0E7FF; color: #3730A3; }
        .badge-rendering { background: #DBEAFE; color: #1E40AF; }
        .badge-completed { background: #D1FAE5; color: #065F46; }
        .badge-done { background: #D1FAE5; color: #065F46; }
        .badge-failed { background: #FEE2E2; color: #991B1B; }
        .badge-timeout { background: #FEE2E2; color: #991B1B; }
        .badge-error { background: #FEE2E2; color: #991B1B; }
        .spinner {
            border: 3px solid var(--color-muted);
            border-top: 3px solid var(--color-primary);
            border-radius: 50%; width: 32px; height: 32px;
            animation: spin 1s linear infinite;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .accounts-table { width: 100%; border-collapse: collapse; font-size: 14px; }
        .accounts-table th {
            text-align: left; padding: var(--space-3) var(--space-4);
            border-bottom: 2px solid var(--color-border);
            font-weight: 600; color: #64748b; font-size: 12px; text-transform: uppercase;
        }
        .accounts-table td {
            padding: var(--space-3) var(--space-4);
            border-bottom: 1px solid var(--color-border);
        }
        .accounts-table tr:last-child td { border-bottom: none; }
        .accounts-table tr:hover td { background: var(--color-muted); }
        .gallery-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: var(--space-4);
        }
        .gallery-item {
            border-radius: var(--radius-md); overflow: hidden;
            border: 1px solid var(--color-border); background: white;
            transition: transform 150ms ease, box-shadow 150ms ease;
        }
        .gallery-item:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(124,58,237,0.1); }
        .gallery-item img { width: 100%; aspect-ratio: 1; object-fit: cover; }
        .gallery-item-info { padding: var(--space-3); }
        .gallery-item-prompt { font-size: 12px; color: var(--color-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: var(--space-1); }
        .gallery-item-meta { font-size: 11px; color: #94a3b8; }
        .job-card {
            background: white; border-radius: var(--radius-lg);
            border: 1px solid var(--color-border); padding: var(--space-6);
            margin-bottom: var(--space-4);
        }
        .job-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--space-3); }
        .job-id { font-family: monospace; font-size: 12px; color: #64748b; }
        .job-prompt { font-size: 14px; margin-bottom: var(--space-3); }
        .job-result-img { max-width: 100%; border-radius: var(--radius-md); border: 2px solid var(--color-border); }
        .progress-bar-bg { background: var(--color-muted); border-radius: 99px; height: 8px; overflow: hidden; margin: var(--space-3) 0; }
        .progress-bar-fill { height: 100%; background: var(--color-primary); border-radius: 99px; transition: width 300ms ease; }
        .code-block {
            background: #1e293b; color: #e2e8f0; padding: var(--space-4);
            border-radius: var(--radius-md); font-family: 'Courier New', monospace;
            font-size: 13px; overflow-x: auto; margin-top: var(--space-3);
        }
        .code-block .key { color: #93c5fd; }
        .code-block .str { color: #86efac; }
        .endpoint-card {
            background: var(--color-muted); border-radius: var(--radius-md);
            padding: var(--space-4); margin-bottom: var(--space-3);
        }
        .method-badge {
            display: inline-block; padding: 2px 8px; border-radius: 6px;
            font-size: 11px; font-weight: 700; font-family: monospace; margin-right: 8px;
        }
        .method-get { background: #DBEAFE; color: #1E40AF; }
        .method-post { background: #D1FAE5; color: #065F46; }
        @media (max-width: 768px) {
            .sidebar { display: none; }
            .main { margin-left: 0; padding: var(--space-4); }
            .form-row { grid-template-columns: 1fr; }
            .stats-row { grid-template-columns: repeat(2, 1fr); }
        }
        @media (prefers-reduced-motion: reduce) {
            * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
        }
    </style>
</head>
<body>

<aside class="sidebar">
    <div class="sidebar-logo">V2Fun<span> API</span></div>
    <nav>
        <a class="nav-item active" onclick="showPage('dashboard', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
            Dashboard
        </a>
        <a class="nav-item" onclick="showPage('generate', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
            Generate
        </a>
        <a class="nav-item" onclick="showPage('jobs', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>
            Jobs
        </a>
        <a class="nav-item" onclick="showPage('gallery', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
            Gallery
        </a>
        <a class="nav-item" onclick="showPage('accounts', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a4 4 0 00-3-3.87M9 20H4v-2a4 4 0 013-3.87m6-2a4 4 0 10-8 0 4 4 0 008 0zm6 0a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
            Accounts
        </a>
        <a class="nav-item" onclick="showPage('docs', event)" href="#">
            <svg class="nav-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
            API Docs
        </a>
    </nav>
    <div class="sidebar-bottom">
        <div style="display:flex;align-items:center;gap:8px;padding:8px">
            <span class="status-dot" id="statusDot"></span>
            <span style="font-size:13px;font-weight:500" id="statusText">Checking...</span>
        </div>
        <div style="font-size:11px;color:#94a3b8;padding:0 8px 8px" id="versionText">v1.0.0</div>
        <button class="btn btn-outline btn-sm btn-full" onclick="configureApiKey()">API Key</button>
    </div>
</aside>

<main class="main">

    <!-- Dashboard Page -->
    <div id="page-dashboard" class="page active">
        <div class="page-header">
            <h1>Dashboard</h1>
            <p>Monitor V2Fun Backend API status and job queue</p>
        </div>
        <div id="alert-dash" class="alert"></div>
        <div class="stats-row">
            <div class="stat-card">
                <div class="stat-value" id="statAccounts">—</div>
                <div class="stat-label">Available Accounts</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statJobs">—</div>
                <div class="stat-label">Active Jobs</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statTotal">—</div>
                <div class="stat-label">Total Jobs</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statStatus">—</div>
                <div class="stat-label">API Status</div>
            </div>
        </div>
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-4)">
                <div class="card-title" style="margin:0">Model Priority</div>
                <button class="btn btn-primary btn-sm" onclick="loadDashboard()">
                    <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                    Refresh
                </button>
            </div>
            <div id="modelList"><p style="color:#94a3b8;text-align:center;padding:20px">Loading...</p></div>
        </div>
        <div class="card">
            <div class="card-title">Recent Jobs</div>
            <div id="recentJobs"><p style="color:#94a3b8;text-align:center;padding:20px">Loading...</p></div>
        </div>
    </div>

    <!-- Generate Page -->
    <div id="page-generate" class="page">
        <div class="page-header">
            <h1>Generate Image</h1>
            <p>Submit a prompt to generate an image via V2Fun API</p>
        </div>
        <div id="alert-gen" class="alert"></div>
        <div class="card">
            <div class="card-title">Prompt</div>
            <div class="form-group">
                <textarea id="prompt" placeholder="Describe the image you want to generate..." maxlength="5000" oninput="updateCharCount()"></textarea>
                <div style="text-align:right;font-size:12px;color:#94a3b8;margin-top:4px" id="charCount">0 / 5000</div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Model</label>
                    <select id="model">
                        <option value="gpt-image-2.5-sunburst" selected>gpt-image-2.5-sunburst (Best)</option>
                        <option value="gpt-image-2.5-flare">gpt-image-2.5-flare</option>
                        <option value="gpt-image-2">gpt-image-2</option>
                        <option value="nano-banana-pro">nano-banana-pro</option>
                        <option value="nano-banana-2">nano-banana-2</option>
                        <option value="nano-banana-2-lite">nano-banana-2-lite (Fast)</option>
                        <option value="qwen-image">qwen-image (High volume)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Quality</label>
                    <select id="quality">
                        <option value="high" selected>High (Recommended)</option>
                        <option value="medium">Medium</option>
                        <option value="low">Low (Fast)</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label>Aspect Ratio</label>
                <select id="aspect">
                    <option value="1:1">1:1 (Square)</option>
                    <option value="16:9" selected>16:9 (Wide)</option>
                    <option value="9:16">9:16 (Portrait)</option>
                    <option value="4:3">4:3</option>
                    <option value="3:4">3:4</option>
                </select>
            </div>
            <button class="btn btn-primary btn-full" id="generateBtn" onclick="generateImage()">
                <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                Generate Image
            </button>
        </div>
        <div class="card" id="genResult" style="display:none">
            <div class="card-title">Result</div>
            <div id="genResultContent"></div>
        </div>
    </div>

    <!-- Jobs Page -->
    <div id="page-jobs" class="page">
        <div class="page-header">
            <h1>Jobs</h1>
            <p>Track all image generation jobs</p>
        </div>
        <div id="alert-jobs" class="alert"></div>
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-4)">
                <div class="card-title" style="margin:0">All Jobs</div>
                <button class="btn btn-primary btn-sm" onclick="loadJobs()">Refresh</button>
            </div>
            <div id="jobsList"><p style="color:#94a3b8;text-align:center;padding:20px">Loading...</p></div>
        </div>
    </div>

    <!-- Gallery Page -->
    <div id="page-gallery" class="page">
        <div class="page-header">
            <h1>Gallery</h1>
            <p>View and manage completed generations</p>
        </div>
        <div id="alert-gallery" class="alert"></div>
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-4)">
                <div class="card-title" style="margin:0">Completed Images</div>
                <button class="btn btn-primary btn-sm" onclick="loadGallery()">Refresh</button>
            </div>
            <div id="galleryGrid" class="gallery-grid"><p style="color:#94a3b8;text-align:center;padding:20px">Loading...</p></div>
        </div>
    </div>

    <!-- Accounts Page -->
    <div id="page-accounts" class="page">
        <div class="page-header">
            <h1>Accounts</h1>
            <p>V2Fun account pool with round-robin selection</p>
        </div>
        <div id="alert-accounts" class="alert"></div>
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-4)">
                <div>
                    <div class="card-title" style="margin:0">Account Pool</div>
                    <div id="schedulerStatus" style="font-size:12px;color:#64748b;margin-top:4px">Auto-refresh: checking...</div>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-accent btn-sm" onclick="refreshTokensNow()">Refresh Expiring Tokens</button>
                    <button class="btn btn-primary btn-sm" onclick="reloadAccounts()">Reload Pool</button>
                </div>
            </div>
            <div id="accountsList"><p style="color:#94a3b8;text-align:center;padding:20px">Loading...</p></div>
        </div>
    </div>

    <!-- API Docs Page -->
    <div id="page-docs" class="page">
        <div class="page-header">
            <h1>API Documentation</h1>
            <p>REST API endpoints for V2Fun Backend</p>
        </div>
        <div class="card">
            <div class="card-title">Endpoints</div>
            <div class="endpoint-card">
                <span class="method-badge method-get">GET</span>
                <code>/api/health</code>
                <p style="font-size:13px;color:#64748b;margin-top:8px">Check API health, account count, and active jobs</p>
            </div>
            <div class="endpoint-card">
                <span class="method-badge method-get">GET</span>
                <code>/api/accounts</code>
                <p style="font-size:13px;color:#64748b;margin-top:8px">List all available V2Fun accounts</p>
            </div>
            <div class="endpoint-card">
                <span class="method-badge method-post">POST</span>
                <code>/api/generate</code>
                <p style="font-size:13px;color:#64748b;margin-top:8px">Submit image generation job</p>
                <div class="code-block">{ "prompt": "string", "model": "gpt-image-2.5-sunburst", "aspect": "1:1" }</div>
            </div>
            <div class="endpoint-card">
                <span class="method-badge method-get">GET</span>
                <code>/api/status/&lt;job_id&gt;</code>
                <p style="font-size:13px;color:#64748b;margin-top:8px">Check status of a specific job</p>
            </div>
            <div class="endpoint-card">
                <span class="method-badge method-post">POST</span>
                <code>/api/reload-accounts</code>
                <p style="font-size:13px;color:#64748b;margin-top:8px">Reload the account pool (admin)</p>
            </div>
        </div>
        <div class="card">
            <div class="card-title">Example: Generate Image</div>
            <div class="code-block">curl -X POST https://image-gen-v2.gxa.my.id/api/generate \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "a cat on the moon", "model": "gpt-image-2.5-sunburst"}'</div>
        </div>
        <div class="card">
            <div class="card-title">Example: Check Status</div>
            <div class="code-block">curl https://image-gen-v2.gxa.my.id/api/status/&lt;job_id&gt;</div>
        </div>
    </div>

</main>

<script>
const API = window.location.origin;
let currentPage = 'dashboard';
let pollInterval = null;
let activeJobIds = [];

function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function getApiHeaders(extra = {}) {
    const headers = { ...extra };
    const storedKey = localStorage.getItem('v2fun_backend_api_key');
    if (storedKey) headers['X-API-Key'] = storedKey;
    return headers;
}

function configureApiKey() {
    const current = localStorage.getItem('v2fun_backend_api_key') || '';
    const entered = prompt('Enter Backend API Key (leave empty to clear):', current);
    if (entered === null) return;
    if (entered.trim()) {
        localStorage.setItem('v2fun_backend_api_key', entered.trim());
    } else {
        localStorage.removeItem('v2fun_backend_api_key');
    }
    loadDashboard();
}

function showPage(name, event) {
    currentPage = name;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('page-' + name).classList.add('active');
    if (event && event.currentTarget) { event.currentTarget.classList.add('active'); }
    if (name === 'dashboard') loadDashboard();
    if (name === 'jobs') loadJobs();
    if (name === 'gallery') loadGallery();
    if (name === 'accounts') loadAccounts();
}

async function apiGet(path) {
    const r = await fetch(API + path, { headers: getApiHeaders() });
    return r.json();
}

async function apiPost(path, body) {
    const r = await fetch(API + path, {
        method: 'POST',
        headers: getApiHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body)
    });
    return r.json();
}

async function loadDashboard() {
    try {
        const h = await apiGet('/api/health');
        document.getElementById('statAccounts').textContent = h.accounts_available;
        document.getElementById('statJobs').textContent = h.active_jobs;
        document.getElementById('statTotal').textContent = h.total_jobs || h.active_jobs;
        document.getElementById('statStatus').textContent = h.status === 'healthy' ? '✓ Healthy' : '✗ Error';
        document.getElementById('statStatus').style.color = h.status === 'healthy' ? '#10b981' : '#ef4444';
        document.getElementById('versionText').textContent = 'v' + h.version;
        document.getElementById('statusText').textContent = h.status === 'healthy' ? 'Online' : 'Offline';
        document.getElementById('statusDot').className = h.status === 'healthy' ? 'status-dot' : 'status-dot offline';

        // Model priority list
        const models = ['gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2', 'nano-banana-pro', 'nano-banana-2', 'nano-banana-2-lite', 'qwen-image'];
        const labels = ['Best quality', 'High quality', 'Good alternative', 'Standard quality', 'Standard', 'Fast', 'High volume (50+)'];
        document.getElementById('modelList').innerHTML = models.map((m, i) => 
            `<div style="display:flex;align-items:center;gap:12px;padding:10px 0;${i < models.length-1 ? 'border-bottom:1px solid var(--color-border)' : ''}">
                <span style="background:var(--color-muted);color:var(--color-primary);font-weight:700;font-size:12px;width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center">${i+1}</span>
                <span style="font-weight:600;font-size:14px">${m}</span>
                <span style="color:#94a3b8;font-size:13px;margin-left:auto">${labels[i]}</span>
            </div>`
        ).join('');

        // Recent jobs
        if (activeJobIds.length > 0) {
            document.getElementById('recentJobs').innerHTML = activeJobIds.slice(0, 5).map(id =>
                `<div style="padding:8px 0;border-bottom:1px solid var(--color-border)">
                    <span class="job-id">${id}</span>
                    <span id="dash-job-${id}" style="margin-left:12px"><span class="badge badge-processing">Checking...</span></span>
                </div>`
            ).join('');
            for (const id of activeJobIds.slice(0, 5)) { pollJob(id, 'dash-job-' + id); }
        } else {
            document.getElementById('recentJobs').innerHTML = '<p style="color:#94a3b8;text-align:center;padding:20px">No active jobs</p>';
        }
    } catch (err) {
        showAlert('alert-dash', 'Error: ' + err.message, 'error');
    }
}

async function loadAccounts() {
    try {
        const [data, st] = await Promise.all([apiGet('/api/accounts'), apiGet('/api/token-refresh-status')]);
        const schedEl = document.getElementById('schedulerStatus');
        if (schedEl && st && st.scheduler) {
            const sc = st.scheduler;
            schedEl.textContent = `Auto-refresh every ${sc.interval_hours}h (before <${sc.min_remaining_hours}h) • ${sc.running ? 'Active' : 'Stopped'}${sc.last_run_at ? ' • Last: ' + new Date(sc.last_run_at).toLocaleString() : ''}`;
        }
        const accounts = data.accounts || [];
        if (accounts.length === 0) {
            document.getElementById('accountsList').innerHTML = '<p style="color:#94a3b8;text-align:center;padding:20px">No accounts available</p>';
            return;
        }
        document.getElementById('accountsList').innerHTML = `
            <table class="accounts-table">
                <thead><tr><th>#</th><th>Email</th><th>Status</th></tr></thead>
                <tbody>
                ${accounts.map((a, i) => `
                    <tr>
                        <td>${i+1}</td>
                        <td style="font-size:13px">${escapeHtml(a.email)}</td>
                        <td><span class="badge badge-done">${escapeHtml(a.status)}</span></td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;
    } catch (err) {
        document.getElementById('accountsList').innerHTML = `<p style="color:#ef4444;text-align:center;padding:20px">Error: ${err.message}</p>`;
    }
}

async function reloadAccounts() {
    try {
        const data = await apiPost('/api/reload-accounts', {});
        showAlert('alert-accounts', `Reloaded! ${data.accounts_available} accounts available`, data.success ? 'success' : 'error');
        loadAccounts();
    } catch (err) {
        showAlert('alert-accounts', 'Error: ' + err.message, 'error');
    }
}

async function refreshTokensNow() {
    try {
        showAlert('alert-accounts', 'Triggering token auto-refresh for all accounts...', 'info');
        const data = await apiPost('/api/refresh-tokens', { async: true, force: true });
        showAlert('alert-accounts', data.message || 'Token refresh started', data.success ? 'success' : 'error');
        loadAccounts();
    } catch (err) {
        showAlert('alert-accounts', 'Error: ' + err.message, 'error');
    }
}

async function loadJobs() {
    try {
        const data = await apiGet('/api/jobs?limit=50');
        const jobs = data.jobs || [];
        
        if (jobs.length === 0) {
            document.getElementById('jobsList').innerHTML = '<p style="color:#94a3b8;text-align:center;padding:20px">No jobs yet. Generate an image first!</p>';
            return;
        }
        
        document.getElementById('jobsList').innerHTML = jobs.map(job => {
            const safeId = escapeHtml(job.id);
            const safeStatus = escapeHtml(job.status);
            const statusBadge = `badge-${safeStatus}`;
            const hasImage = job.status === 'completed' && job.local_path;
            const hasFallback = job.fallback_attempts && job.fallback_attempts.length > 0;
            
            return `
                <div class="job-card" id="job-card-${safeId}">
                    <div class="job-card-header">
                        <span class="job-id">${safeId}</span>
                        <span class="badge ${statusBadge}">${safeStatus}</span>
                    </div>
                    <div class="job-prompt" style="margin:var(--space-3) 0;font-weight:500">${escapeHtml(job.prompt)}</div>
                    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:var(--space-2);font-size:12px;color:#64748b;margin-bottom:var(--space-3)">
                        <div>Model: <strong>${escapeHtml(job.model)}</strong></div>
                        <div>Quality: <strong>${escapeHtml(job.quality)}</strong></div>
                        <div>Account: <strong>${escapeHtml(job.account)}</strong></div>
                        <div>Source: <strong>${escapeHtml(job.source || 'api')}</strong></div>
                    </div>
                    ${hasFallback ? `<div style="font-size:12px;color:#f59e0b;margin-bottom:var(--space-3)">⚠️ Fallback used: ${job.fallback_attempts.length} attempt(s)</div>` : ''}
                    ${job.status === 'processing' || job.status === 'rendering' ? `
                        <div class="progress-bar-bg">
                            <div class="progress-bar-fill" style="width:${Number(job.progress) || 50}%"></div>
                        </div>
                        <p style="font-size:12px;color:#94a3b8;text-align:center">${job.status === 'rendering' ? 'Rendering...' : 'Processing...'}</p>
                    ` : ''}
                    ${hasImage ? `
                        <img class="job-result-img" src="/api/image/${encodeURIComponent(job.id)}" alt="Result" style="max-height:400px;margin:var(--space-3) 0">
                        <div style="display:flex;gap:var(--space-2)">
                            <a href="${escapeHtml(job.work_url)}" target="_blank" class="btn btn-primary btn-sm">Open Full Size</a>
                            <a href="${escapeHtml(job.work_url)}" download class="btn btn-outline btn-sm">Download</a>
                        </div>
                    ` : ''}
                    ${job.status === 'failed' || job.status === 'error' || job.status === 'timeout' ? `
                        <div style="background:#FEE2E2;color:#991B1B;padding:var(--space-3);border-radius:var(--radius-md);font-size:13px">
                            <strong>Error:</strong> ${escapeHtml(job.error || 'Unknown error')}
                        </div>
                    ` : ''}
                </div>
            `;
        }).join('');
    } catch (err) {
        document.getElementById('jobsList').innerHTML = `<p style="color:#ef4444;text-align:center;padding:20px">Error: ${err.message}</p>`;
    }
}

let galleryImagesMap = {};

async function loadGallery() {
    try {
        const data = await apiGet('/api/gallery');
        const images = data.images || [];
        images.forEach(img => { galleryImagesMap[img.id] = img; });
        
        if (images.length === 0) {
            document.getElementById('galleryGrid').innerHTML = '<p style="color:#94a3b8;text-align:center;padding:20px;grid-column:1/-1">No completed images yet</p>';
            return;
        }
        
        document.getElementById('galleryGrid').innerHTML = images.map(img => `
            <div class="gallery-item">
                <img src="/api/image/${encodeURIComponent(img.id)}" alt="${escapeHtml(img.prompt)}" loading="lazy" onclick="viewImage('${escapeHtml(img.id)}', '${escapeHtml(img.work_url)}')">
                <div class="gallery-item-info">
                    <div class="gallery-item-prompt" title="${escapeHtml(img.prompt)}">${escapeHtml(img.prompt)}</div>
                    <div class="gallery-item-meta">${escapeHtml(img.model)} • ${new Date(img.completed_at).toLocaleDateString()}</div>
                    <div style="display:flex;gap:4px;margin-top:var(--space-2)">
                        <a href="${escapeHtml(img.work_url)}" target="_blank" class="btn btn-primary btn-sm" style="flex:1;font-size:11px;padding:4px 8px">View</a>
                        <button onclick="deleteImage('${escapeHtml(img.id)}')" class="btn btn-outline btn-sm" style="font-size:11px;padding:4px 8px;color:#ef4444;border-color:#ef4444">Delete</button>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (err) {
        document.getElementById('galleryGrid').innerHTML = `<p style="color:#ef4444;text-align:center;padding:20px;grid-column:1/-1">Error: ${err.message}</p>`;
    }
}

function viewImage(jobId, url) {
    window.open(url, '_blank');
}

async function deleteImage(jobId) {
    const img = galleryImagesMap[jobId] || {};
    const promptText = img.prompt || '';
    
    let isConfirmed = false;
    if (typeof Swal !== 'undefined') {
        const result = await Swal.fire({
            title: 'Hapus Gambar?',
            html: `Apakah Anda yakin ingin menghapus gambar ini beserta promptnya?<br><br>
                   <div style="text-align:left;font-size:12px;font-weight:600;color:#64748b;margin-bottom:4px">Prompt:</div>
                   <div style="text-align:left;max-height:100px;overflow-y:auto;background:var(--color-muted, #f8fafc);padding:10px;border-radius:8px;border:1px solid var(--color-border, #e2e8f0);font-size:13px;color:var(--color-foreground, #1e293b);word-break:break-word;white-space:pre-wrap">${escapeHtml(promptText) || '<em>Tidak ada prompt</em>'}</div>`,
            icon: 'warning',
            showCancelButton: true,
            confirmButtonColor: '#dc2626',
            cancelButtonColor: '#64748b',
            confirmButtonText: 'Ya, Hapus',
            cancelButtonText: 'Tidak',
            reverseButtons: true
        });
        isConfirmed = result.isConfirmed;
    } else {
        isConfirmed = confirm(`Apakah Anda yakin ingin menghapus gambar ini beserta promptnya?\n\nPrompt:\n${promptText}`);
    }

    if (!isConfirmed) return;
    
    try {
        const response = await fetch(API + '/api/gallery/' + encodeURIComponent(jobId), {
            method: 'DELETE',
            headers: getApiHeaders()
        });
        const data = await response.json();
        
        if (data.success) {
            delete galleryImagesMap[jobId];
            if (typeof Swal !== 'undefined') {
                Swal.fire({
                    title: 'Berhasil Dihapus!',
                    text: 'Gambar beserta prompt berhasil dihapus.',
                    icon: 'success',
                    timer: 1500,
                    showConfirmButton: false
                });
            }
            showAlert('alert-gallery', 'Image deleted successfully', 'success');
            loadGallery();
        } else {
            if (typeof Swal !== 'undefined') {
                Swal.fire('Gagal!', data.error || 'Gagal menghapus gambar.', 'error');
            } else {
                showAlert('alert-gallery', 'Failed to delete: ' + (data.error || 'Unknown error'), 'error');
            }
        }
    } catch (err) {
        if (typeof Swal !== 'undefined') {
            Swal.fire('Error!', err.message || 'Terjadi kesalahan.', 'error');
        } else {
            showAlert('alert-gallery', 'Error: ' + err.message, 'error');
        }
    }
}

async function pollJob(jobId, statusElId, bodyElId) {
    try {
        const data = await apiGet('/api/status/' + jobId);
        if (!data.success || !data.job) return;
        
        const job = data.job;
        const statusEl = document.getElementById(statusElId);
        if (!statusEl) return;
        
        const badge = `badge-${job.status}`;
        statusEl.innerHTML = `<span class="badge ${badge}">${job.status}</span>`;

        if (bodyElId) {
            const bodyEl = document.getElementById(bodyElId);
            if (bodyEl) {
                if (job.status === 'processing' || job.status === 'rendering' || job.status === 'queued') {
                    bodyEl.innerHTML = `
                        <div class="job-prompt">${escapeHtml(job.prompt || '')}</div>
                        <div class="progress-bar-bg"><div class="progress-bar-fill" style="width:${Number(job.progress) || 50}%"></div></div>
                        <p style="font-size:12px;color:#94a3b8">${job.status === 'rendering' ? 'Rendering...' : job.status === 'queued' ? 'Queued...' : 'Processing...'}</p>`;
                } else if (job.status === 'completed' && job.local_path) {
                    bodyEl.innerHTML = `
                        <div class="job-prompt">${escapeHtml(job.prompt || '')}</div>
                        <img class="job-result-img" src="/api/image/${encodeURIComponent(job.id)}" alt="Result" style="max-height:300px">
                        <div style="margin-top:10px;display:flex;gap:8px">
                            <a href="${escapeHtml(job.work_url)}" target="_blank" class="btn btn-primary btn-sm">Open</a>
                            <a href="${escapeHtml(job.work_url)}" download class="btn btn-outline btn-sm">Download</a>
                        </div>`;
                } else if (job.status === 'failed' || job.status === 'error' || job.status === 'timeout') {
                    bodyEl.innerHTML = `<div class="job-prompt">${escapeHtml(job.prompt || '')}</div><p style="color:#ef4444">${escapeHtml(job.error || 'Generation failed')}</p>`;
                } else {
                    bodyEl.innerHTML = `<div class="job-prompt">${escapeHtml(job.prompt || '')}</div><pre style="font-size:12px;color:#64748b">${escapeHtml(JSON.stringify(job, null, 2))}</pre>`;
                }
            }
        }
    } catch (err) {
        const el = document.getElementById(statusElId);
        if (el) el.innerHTML = `<span class="badge badge-failed">Error</span>`;
    }
}

async function generateImage() {
    const prompt = document.getElementById('prompt').value.trim();
    if (!prompt) { showAlert('alert-gen', 'Please enter a prompt', 'error'); return; }
    const model = document.getElementById('model').value;
    const quality = document.getElementById('quality').value;
    const aspect = document.getElementById('aspect').value;
    const btn = document.getElementById('generateBtn');
    btn.disabled = true; btn.innerHTML = '<div class="spinner" style="width:18px;height:18px;border-width:2px"></div> Generating...';

    try {
        const data = await apiPost('/api/generate', { prompt, model, ratio: aspect, quality });
        if (data.success || data.job_id) {
            const jobId = data.job_id;
            activeJobIds.unshift(jobId);
            showAlert('alert-gen', `Job created: ${jobId}`, 'success');
            document.getElementById('genResult').style.display = 'block';
            document.getElementById('genResultContent').innerHTML = `
                <p style="font-size:14px;margin-bottom:12px"><strong>Job ID:</strong> <code>${escapeHtml(jobId)}</code></p>
                <div id="gen-job-status"><span class="badge badge-processing">Processing...</span></div>
                <div id="gen-job-body"></div>`;
            startPolling(jobId);
        } else {
            showAlert('alert-gen', data.error || data.message || 'Generation failed', 'error');
        }
    } catch (err) {
        showAlert('alert-gen', 'Error: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg> Generate Image';
    }
}

function startPolling(jobId) {
    const poll = () => {
        pollJob(jobId, 'gen-job-status', 'gen-job-body');
        if (currentPage === 'dashboard' || currentPage === 'jobs') {
            pollJob(jobId, 'dash-job-' + jobId);
            pollJob(jobId, 'job-status-' + jobId, 'job-body-' + jobId);
        }
    };
    poll();
    const interval = setInterval(poll, 3000);
    setTimeout(() => clearInterval(interval), 300000);
}

function updateCharCount() {
    const t = document.getElementById('prompt');
    const c = document.getElementById('charCount');
    c.textContent = `${t.value.length} / 5000`;
    c.style.color = t.value.length > 4900 ? '#ef4444' : t.value.length > 4000 ? '#f59e0b' : '#94a3b8';
}

function showAlert(id, message, type) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'alert ' + type + ' active';
    el.textContent = message;
    setTimeout(() => el.classList.remove('active'), 5000);
}

// Auto-refresh dashboard every 10s
setInterval(() => { if (currentPage === 'dashboard') loadDashboard(); }, 10000);
loadDashboard();
</script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def dashboard_ui():
    """Serve the web UI dashboard"""
    return DASHBOARD_HTML


@app.route("/ui", methods=["GET"])
def dashboard_ui_alias():
    """Alias for /ui path"""
    return DASHBOARD_HTML


if __name__ == "__main__":
    # Initialize database
    init_db()

    print("=" * 80)
    print("V2Fun Backend API for Hermes Agent")
    print("=" * 80)
    print(f"\nAccounts available: {len(account_pool.accounts)}")
    print(f"Model priority: {' > '.join(MODEL_PRIORITY)}")
    print(f"\nAPI Endpoints:")
    print(f"  POST http://localhost:5001/api/generate")
    print(f"  GET  http://localhost:5001/api/status/<job_id>")
    print(f"  GET  http://localhost:5001/api/health")
    print(f"  GET  http://localhost:5001/api/accounts")
    print(
        f"\nTelegram notifications: {'Enabled' if os.getenv('TELEGRAM_BOT_TOKEN') else 'Disabled'}"
    )
    print(f"\nStarting server...")
    print()

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "5001"))
    debug = os.getenv("API_DEBUG", "false").lower() == "true"
    if os.getenv("ENABLE_TOKEN_AUTO_REFRESH", "true").lower() == "true":
        token_scheduler.start()
    if not BACKEND_API_KEY:
        print("[WARN] BACKEND_API_KEY is not set; API endpoints are unauthenticated.")
    app.run(host=host, port=port, debug=debug, threaded=True)
