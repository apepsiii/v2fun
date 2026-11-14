"""
V2Fun.ai Web UI V2 - Enhanced Flask Backend
Features: User auth, image upload, database integration, generation history

Usage:
    python v2fun_scripts/v2fun_web_v2.py
    Open: http://localhost:5000
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory, Response, send_file
import json
import os
import ipaddress
import requests
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

V2FUN_DATA_DIR = BASE_DIR / "v2fun_data"
RESULTS_DIR = V2FUN_DATA_DIR / "results"
ACCOUNT_FILE = BASE_DIR / "account.txt"

# Add project root to path
sys.path.insert(0, str(BASE_DIR))
from v2fun_scripts.database import (
    init_db, get_db, create_user, verify_user, get_user_by_id,
    update_user_token, create_generation, update_generation_status,
    get_user_generations, save_uploaded_image,
    create_session as create_db_session, verify_session as verify_db_session,
    delete_session, cleanup_expired_sessions,
    import_v2fun_account, get_pending_accounts, get_all_v2fun_accounts,
    update_v2fun_account_status, delete_v2fun_account, get_v2fun_account_by_id,
    sync_v2fun_sessions_to_db, upsert_v2fun_account_from_session,
    update_quota_snapshot, get_all_quota_snapshots, get_quota_snapshot,
    save_integration, get_integration, get_all_integrations, delete_integration, toggle_integration
)
from v2fun_scripts.sse_monitor import SSEMonitor
from v2fun_scripts.token_manager import (
    get_token_status, get_time_remaining, is_token_valid,
    check_and_refresh_if_needed, get_all_tokens_status,
    TokenRefreshScheduler, _sync_refreshed_token_to_db
)
from v2fun_scripts.v2fun_client import (
    V2FunClient, find_account_password, parse_account_lines,
    get_session_file_path, extract_token_from_session
)

# Active monitors: {generation_id: SSEMonitor}
active_monitors: dict = {}
monitors_lock = threading.Lock()
# Pending events queue: {generation_id: [events]}
pending_events: dict = {}


def _get_or_create_secret_key() -> str:
    """Get FLASK_SECRET_KEY from env or persist a generated key in v2fun_data/.flask_secret"""
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key
    secret_file = V2FUN_DATA_DIR / '.flask_secret'
    V2FUN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        stored = secret_file.read_text(encoding='utf-8').strip()
        if stored:
            return stored
    generated = secrets.token_hex(32)
    secret_file.write_text(generated, encoding='utf-8')
    return generated


app = Flask(__name__, 
            template_folder='../v2fun_web_v2/templates',
            static_folder='../v2fun_web_v2/static')
app.secret_key = _get_or_create_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['UPLOAD_FOLDER'] = V2FUN_DATA_DIR / 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Ensure upload and results folders exist
app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


@app.before_request
def _csrf_origin_check():
    """Verify Origin/Referer on state-changing requests to mitigate CSRF."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    check_url = origin or referer
    if check_url:
        parsed = urlparse(check_url)
        if parsed.netloc and parsed.netloc != request.host:
            return jsonify({"success": False, "message": "Cross-origin request rejected"}), 403
    return None


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def get_current_user():
    """Get current logged in user from session"""
    session_token = session.get('session_token')
    if not session_token:
        return None
    
    user_data = verify_db_session(session_token)
    if not user_data:
        session.pop('session_token', None)
        return None
    
    return user_data


class V2FunAPIError(Exception):
    """Custom exception for V2Fun API errors"""
    def __init__(self, message, status_code=None, error_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


# Login rate limiter: {(ip, email): [failure_timestamps]}
_login_failures: dict = defaultdict(list)
_login_lock = threading.Lock()
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300


def _is_login_rate_limited(client_ip: str, email: str) -> bool:
    key = (client_ip or "unknown", (email or "").lower())
    now = time.time()
    with _login_lock:
        recent = [ts for ts in _login_failures[key] if now - ts < LOGIN_WINDOW_SECONDS]
        _login_failures[key] = recent
        return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(client_ip: str, email: str):
    key = (client_ip or "unknown", (email or "").lower())
    with _login_lock:
        _login_failures[key].append(time.time())


def _clear_login_failures(client_ip: str, email: str):
    key = (client_ip or "unknown", (email or "").lower())
    with _login_lock:
        _login_failures.pop(key, None)


@app.route('/')
def index():
    """Main page - redirect to login if not authenticated"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login_page'))
    return render_template('dashboard.html', user=user)


@app.route('/login')
def login_page():
    """Login page"""
    return render_template('login.html')


@app.route('/register')
def register_page():
    """Registration disabled - admin invite only"""
    return redirect(url_for('login_page'))


@app.route('/api/register', methods=['POST'])
def api_register():
    """Registration disabled - admin must create users via CLI"""
    return jsonify({"success": False, "message": "Registration is disabled. Contact admin to create account."})


@app.route('/api/login', methods=['POST'])
def api_login():
    """Login user with brute-force rate limiting"""
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')
    
    if not email or not password:
        return jsonify({"success": False, "message": "Email and password required"})
    
    client_ip = request.remote_addr
    if _is_login_rate_limited(client_ip, email):
        return jsonify({"success": False, "message": "Too many failed login attempts. Try again in 5 minutes."}), 429

    user = verify_user(email, password)
    
    if user:
        _clear_login_failures(client_ip, email)
        cleanup_expired_sessions()
        session_token = create_db_session(user['id'])
        session['session_token'] = session_token
        
        return jsonify({
            "success": True,
            "message": "Login successful!",
            "user": {
                "email": user['email'],
                "credits": user['credits']
            }
        })
    else:
        _record_login_failure(client_ip, email)
        return jsonify({"success": False, "message": "Invalid email or password"}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    """Logout user"""
    session_token = session.get('session_token')
    if session_token:
        delete_session(session_token)
        session.pop('session_token', None)
    
    return jsonify({"success": True, "message": "Logged out successfully"})


@app.route('/api/me')
def api_me():
    """Get current user info with live credits from V2Fun"""
    user = get_current_user()
    
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    credits = user['credits']
    
    # Fetch live credits from V2Fun if token exists
    if user.get('v2fun_token'):
        client = V2FunClient(user['v2fun_token'])
        balance_resp = client.get_balance()
        if balance_resp.get("success"):
            result = balance_resp.get("result", {})
            # Try various possible field names
            if isinstance(result, dict):
                credits = result.get('balance') or result.get('credits') or result.get('points') or credits
    
    return jsonify({
        "success": True,
        "user": {
            "email": user['email'],
            "credits": credits,
            "has_v2fun_token": bool(user.get('v2fun_token')),
            "selected_v2fun_email": session.get('selected_v2fun_email')
        }
    })


@app.route('/api/connect-v2fun', methods=['POST'])
def api_connect_v2fun():
    """Connect V2Fun token to user account"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    v2fun_email = data.get('v2fun_email')
    
    if not v2fun_email:
        return jsonify({"success": False, "message": "V2Fun email required"})
    
    # Load token from saved session
    v2fun_data_dir = V2FUN_DATA_DIR
    email_safe = v2fun_email.replace("@", "_at_").replace(".", "_")
    session_file = v2fun_data_dir / f"v2fun_session_{email_safe}_latest.json"
    
    if not session_file.exists():
        return jsonify({"success": False, "message": "V2Fun session not found. Please login first."})
    
    with open(session_file, "r", encoding="utf-8") as f:
        v2fun_session = json.load(f)
    
    tokens = v2fun_session.get("tokens", {})
    token = tokens.get("cookie_token") or tokens.get("localStorage_access_token")
    
    if not token:
        return jsonify({"success": False, "message": "No token found in session"})
    
    # Update user record with the selected V2Fun token
    uid = user.get('user_id') or user.get('id')
    if not uid:
        return jsonify({"success": False, "message": "User ID missing from session"})
    update_user_token(uid, token)

    # Also keep the v2fun_accounts table in sync so the account shows as
    # 'done' with a valid token (prevents it from disappearing from the list)
    from v2fun_scripts.token_manager import get_token_expiry
    expiry = get_token_expiry(token)
    expiry_str = expiry.isoformat() if expiry else None
    upsert_v2fun_account_from_session(v2fun_email, token, expiry_str, uid)

    # Remember which V2Fun account the user selected so the dropdown stays
    # on this account instead of jumping back to the first "done" account.
    session['selected_v2fun_email'] = v2fun_email

    # Proactively refresh on activation if token is expired or < 12h remaining
    user['v2fun_token'] = token
    auto_refresh_if_needed(user, v2fun_email=v2fun_email)

    return jsonify({"success": True, "message": "V2Fun account connected successfully!"})


@app.route('/api/upload-image', methods=['POST'])
def api_upload_image():
    """Upload reference image to V2Fun OSS"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    if not user.get('v2fun_token'):
        return jsonify({"success": False, "message": "Please select a V2Fun account first (dropdown top-right)"})

    user = auto_refresh_if_needed(user)
    
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"})
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"success": False, "message": "No file selected"})
    
    if not allowed_file(file.filename):
        return jsonify({"success": False, "message": "File type not allowed. Use PNG, JPG, JPEG, GIF, or WEBP"})
    
    # Check file size (Flask MAX_CONTENT_LENGTH handles this, but double-check)
    file.seek(0, 2)  # Seek to end
    file_size_stream = file.tell()
    file.seek(0)  # Reset
    
    if file_size_stream > 16 * 1024 * 1024:
        return jsonify({"success": False, "message": "File too large (max 16MB)"})
    
    if file_size_stream == 0:
        return jsonify({"success": False, "message": "File is empty"})
    
    # Save locally first
    original_filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{original_filename}"
    file_path = app.config['UPLOAD_FOLDER'] / filename
    
    try:
        file.save(str(file_path))
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to save file: {str(e)}"})
    
    file_size = os.path.getsize(file_path)
    
    # Save to local database
    uid = user.get('user_id') or user.get('id')
    image_id = save_uploaded_image(
        uid,
        filename,
        original_filename,
        str(file_path),
        file_size,
        file.content_type
    )
    
    # Upload to V2Fun OSS (with retry mechanism built into client)
    client = V2FunClient(user['v2fun_token'])
    mime = file.content_type or "image/jpeg"
    oss_result = client.upload_to_oss(str(file_path), mime)
    
    if oss_result.get("success"):
        return jsonify({
            "success": True,
            "message": "Image uploaded successfully!",
            "image": {
                "id": image_id,
                "filename": filename,
                "url": f"/uploads/{filename}",
                "oss_path": oss_result["oss_path"]
            }
        })
    else:
        # Clean up local file if OSS upload failed
        try:
            os.remove(str(file_path))
        except:
            pass
        
        error_code = oss_result.get("error_code", "UNKNOWN")
        
        # If token expired, return specific message
        if error_code == "AUTH_EXPIRED":
            return jsonify({"success": False, "message": "V2Fun token expired. Please select account again.", "error_code": "AUTH_EXPIRED"})
        
        return jsonify({
            "success": False,
            "message": oss_result.get("message", "Upload failed"),
            "error_code": error_code
        })


@app.route('/api/generate', methods=['POST'])
def api_generate():
    """Generate image"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    if not user.get('v2fun_token'):
        return jsonify({"success": False, "message": "Please select a V2Fun account first (dropdown top-right)"})

    user = auto_refresh_if_needed(user)
    
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "message": "No data provided"})
    
    prompt = data.get('prompt', '').strip()
    
    if not prompt:
        return jsonify({"success": False, "message": "Prompt is required"})
    
    if len(prompt) > 5000:
        return jsonify({"success": False, "message": "Prompt too long (max 5000 characters)"})
    
    # Validate reference images
    ref_imgs = data.get("reference_images")
    if ref_imgs and not isinstance(ref_imgs, list):
        return jsonify({"success": False, "message": "reference_images must be an array"})
    if ref_imgs and len(ref_imgs) > 3:
        return jsonify({"success": False, "message": "Maximum 3 reference images allowed"})
    
    client = V2FunClient(user['v2fun_token'])
    
    try:
        result = client.generate_image(
            prompt=prompt,
            model=data.get("model", "gpt-image-2.5-sunburst"),
            quality=data.get("quality", "medium"),
            ratio=data.get("ratio", "16:9"),
            num=data.get("num", 1),
            reference_images=ref_imgs
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"Unexpected error: {str(e)}", "error_code": "UNEXPECTED"})
    
    if result.get("success"):
        result_data = result.get("result", {})
        task_uuid = result_data.get("taskuuid")
        
        if not task_uuid:
            return jsonify({"success": False, "message": "Generation submitted but no task UUID returned", "error_code": "NO_TASK_UUID"})
        
        try:
            uid = user.get('user_id') or user.get('id')
            generation_id = create_generation(
                uid,
                prompt,
                model=data.get("model"),
                quality=data.get("quality"),
                ratio=data.get("ratio"),
                num=data.get("num"),
                task_uuid=task_uuid,
                work_area_id=result_data.get("id"),
                task_ids=json.dumps(result_data.get("taskIds", []))
            )
            
            result["generation_id"] = generation_id
            
            # Start SSE monitor in background
            start_monitor(uid, user['v2fun_token'], generation_id, task_uuid)
        except Exception as e:
            return jsonify({"success": False, "message": f"Database error: {str(e)}", "error_code": "DB_ERROR"})
    
    return jsonify(result)


@app.route('/api/generations')
def api_generations():
    """Get user's generation history with local_url resolved from v2fun_data/results"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    from v2fun_scripts.image_downloader import download_image as dl_image, get_local_image_path

    uid = user.get('user_id') or user.get('id')
    generations = get_user_generations(uid, limit=50)

    for g in generations:
        local_path = g.get('local_path')
        resolved_file = None

        if local_path:
            candidate = RESULTS_DIR / Path(local_path).name
            if candidate.is_file():
                resolved_file = candidate

        if not resolved_file:
            found_rel = get_local_image_path(g['id'])
            if found_rel:
                candidate = BASE_DIR / found_rel
                if candidate.is_file():
                    resolved_file = candidate
                    local_path = found_rel
                    update_generation_status(g['id'], g['status'], g.get('result_url'), g.get('thumbnail_url'), local_path=local_path)

        # Backfill download to local folder if generation is done and result_url exists
        if not resolved_file and g.get('status') == 'done' and g.get('result_url'):
            dl_rel = dl_image(g['result_url'], g['id'], g.get('prompt', ''))
            if dl_rel:
                candidate = BASE_DIR / dl_rel
                if candidate.is_file():
                    resolved_file = candidate
                    local_path = dl_rel
                    update_generation_status(g['id'], 'done', g.get('result_url'), g.get('thumbnail_url'), local_path=local_path)

        g['local_path'] = local_path
        g['local_url'] = f"/results/{resolved_file.name}" if resolved_file else None

    return jsonify({
        "success": True,
        "generations": generations
    })


@app.route('/api/sessions')
def api_sessions():
    """Get available V2Fun sessions"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    v2fun_data = V2FUN_DATA_DIR
    session_files = list(v2fun_data.glob("v2fun_session_*_latest.json"))
    
    sessions = []
    for file in session_files:
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        token = extract_token_from_session(data)
        if token:
            sessions.append({
                "email": data.get("email"),
                "timestamp": data.get("timestamp")
            })
    
    return jsonify({"success": True, "sessions": sessions})


@app.route('/api/import-accounts', methods=['POST'])
def api_import_accounts():
    """Import V2Fun accounts to database without processing"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.json or {}
    accounts_text = data.get('accounts', '')
    accounts = parse_account_lines(accounts_text)
    
    if not accounts:
        return jsonify({"success": False, "message": "No valid accounts found"})
    
    imported = 0
    duplicates = 0
    for email, password in accounts:
        if import_v2fun_account(email, password, user.get('user_id')):
            imported += 1
        else:
            duplicates += 1
    
    return jsonify({
        "success": True,
        "message": f"Imported {imported} new account(s). {duplicates} duplicate(s) skipped (already imported — existing tokens are preserved).",
        "imported": imported,
        "duplicates": duplicates,
        "total": len(accounts)
    })


@app.route('/api/v2fun-accounts')
def api_v2fun_accounts():
    """Get all imported V2Fun accounts with status"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    # Sync session files on disk into the database first, so that accounts
    # which were successfully logged in (token on disk) but lost from the DB
    # are recovered automatically. Bind them to the current user if unowned.
    sync_v2fun_sessions_to_db(user.get('user_id'))
    
    accounts = get_all_v2fun_accounts(user.get('user_id'))
    
    # Mask JWT tokens for security (show only first 30 chars)
    for acc in accounts:
        if acc.get('jwt_token'):
            acc['jwt_token'] = acc['jwt_token'][:30] + '...'
    
    return jsonify({"success": True, "accounts": accounts})


@app.route('/api/process-account', methods=['POST'])
def api_process_account():
    """Process a single V2Fun account (login + get JWT)"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    account_id = data.get('account_id')
    
    if not account_id:
        return jsonify({"success": False, "message": "Account ID required"})
    
    account = get_v2fun_account_by_id(account_id)
    if not account:
        return jsonify({"success": False, "message": "Account not found"})
    
    # Check if account already has valid token (skip re-login)
    from v2fun_scripts.token_manager import is_token_valid, get_token_expiry
    email_safe = account['email'].replace("@", "_at_").replace(".", "_")
    session_file = V2FUN_DATA_DIR / f"v2fun_session_{email_safe}_latest.json"
    
    if session_file.exists():
        with open(session_file, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        
        existing_token = session_data.get("tokens", {}).get("cookie_token", "")
        
        if existing_token and is_token_valid(existing_token):
            # Token still valid - skip re-login!
            expiry = get_token_expiry(existing_token)
            update_v2fun_account_status(
                account_id, 'done',
                jwt_token=existing_token,
                token_expiry=expiry.isoformat() if expiry else None
            )
            
            return jsonify({
                "success": True,
                "message": f"Account {account['email']} already has valid token. Skipped re-login.",
                "status": "done",
                "jwt_token": existing_token[:50] + "...",
                "token_expiry": expiry.isoformat() if expiry else None,
                "skipped": True
            })
    
    # No valid token - proceed with login using an isolated temporary account file
    update_v2fun_account_status(account_id, 'processing')
    
    temp_account_file = V2FUN_DATA_DIR / f"temp_account_{uuid.uuid4().hex}.txt"
    login_script = BASE_DIR / "v2fun_scripts" / "v2fun_google_login.py"
    
    try:
        V2FUN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_account_file.write_text(f"{account['email']}:{account['password']}", encoding="utf-8")
        
        result = subprocess.run(
            [sys.executable, str(login_script), "--account-file", str(temp_account_file)],
            capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR)
        )
        
        # Check if token was saved
        if session_file.exists():
            with open(session_file, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            
            token = session_data.get("tokens", {}).get("cookie_token", "")
            
            # Get token expiry
            expiry = get_token_expiry(token)
            
            update_v2fun_account_status(
                account_id, 'done',
                jwt_token=token,
                token_expiry=expiry.isoformat() if expiry else None
            )
            
            return jsonify({
                "success": True,
                "message": f"Account {account['email']} processed successfully!",
                "status": "done",
                "jwt_token": token[:50] + "...",
                "token_expiry": expiry.isoformat() if expiry else None
            })
        else:
            error_msg = "Login automation failed"
            if result.stderr:
                error_msg += f": {result.stderr[:200]}"
            update_v2fun_account_status(account_id, 'failed', error_message=error_msg)
            return jsonify({
                "success": False,
                "message": f"Processing failed for {account['email']}. {error_msg}",
                "status": "failed"
            })
    
    except subprocess.TimeoutExpired:
        update_v2fun_account_status(account_id, 'failed', error_message="Timeout")
        return jsonify({"success": False, "message": "Automation timed out"})
    except Exception as e:
        update_v2fun_account_status(account_id, 'failed', error_message=str(e))
        return jsonify({"success": False, "message": f"Error: {str(e)}"})
    finally:
        if temp_account_file.exists():
            temp_account_file.unlink()


@app.route('/api/process-all-accounts', methods=['POST'])
def api_process_all_accounts():
    """Process all pending V2Fun accounts sequentially"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    pending = get_pending_accounts(user.get('user_id'))
    
    if not pending:
        return jsonify({"success": False, "message": "No pending accounts to process"})
    
    # Return list of pending accounts for frontend to process one by one
    return jsonify({
        "success": True,
        "message": f"Found {len(pending)} pending accounts",
        "pending_ids": [acc['id'] for acc in pending]
    })


@app.route('/api/delete-v2fun-account', methods=['POST'])
def api_delete_v2fun_account():
    """Delete a V2Fun account from database"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    account_id = data.get('account_id')
    
    if not account_id:
        return jsonify({"success": False, "message": "Account ID required"})
    
    uid = user.get('user_id') or user.get('id')
    if not delete_v2fun_account(account_id, user_id=uid):
        return jsonify({"success": False, "message": "Account not found or access denied"}), 403
    
    return jsonify({"success": True, "message": "Account deleted"})


@app.route('/api/bulk-delete-v2fun-accounts', methods=['POST'])
def api_bulk_delete_v2fun_accounts():
    """Bulk delete V2Fun accounts from database (scoped to current user / unowned)"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    account_ids = data.get('account_ids') or []
    if not isinstance(account_ids, list) or not account_ids:
        return jsonify({"success": False, "message": "No account IDs selected"}), 400

    uid = user.get('user_id') or user.get('id')
    deleted = 0
    skipped = 0
    for aid in account_ids:
        try:
            if delete_v2fun_account(int(aid), user_id=uid):
                deleted += 1
            else:
                skipped += 1
        except (TypeError, ValueError):
            skipped += 1

    return jsonify({
        "success": True,
        "deleted": deleted,
        "skipped": skipped,
        "message": f"Deleted {deleted} account(s)" + (f" ({skipped} skipped)" if skipped else ""),
    })


@app.route('/api/quota-status')
def api_quota_status():
    """Get quota status (free generation count) for connected V2Fun account"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    if not user.get('v2fun_token'):
        return jsonify({"success": False, "message": "No V2Fun account connected"})
    
    client = V2FunClient(user['v2fun_token'])
    
    # Get business config to map configId -> model name
    config_resp = client.get_business_config()
    config_map = {}  # configId -> {model, name, type}
    
    if config_resp.get("success"):
        result = config_resp.get("result", {})
        configs = result if isinstance(result, list) else result.get("list", result.get("records", []))
        if isinstance(configs, list):
            for cfg in configs:
                cid = str(cfg.get("id", ""))
                model = cfg.get("model", "")
                name = cfg.get("name", "") or cfg.get("configName", "")
                gen_type = cfg.get("type", "") or cfg.get("generateType", "")
                if cid:
                    config_map[cid] = {"model": model, "name": name, "type": gen_type}
    
    # Get free count
    free_resp = client.get_free_count()
    
    if not free_resp.get("success"):
        return jsonify({"success": False, "message": free_resp.get("message", "Failed to get quota")})
    
    free_counts = free_resp.get("result", {})
    
    # Build quota list with model names
    quotas = []
    for cid, count in free_counts.items():
        cfg_info = config_map.get(cid, {})
        model = cfg_info.get("model", "")
        name = cfg_info.get("name", "")
        gen_type = cfg_info.get("type", "")
        
        # Only include items that have model info or are image-related
        if model or name:
            quotas.append({
                "config_id": cid,
                "model": model,
                "name": name,
                "type": gen_type,
                "free_remaining": count,
                "free_total": 5,
                "free_used": 5 - count
            })
    
    # Sort: most remaining first
    quotas.sort(key=lambda x: x["free_remaining"], reverse=True)
    
    return jsonify({
        "success": True,
        "email": user.get('email'),
        "quotas": quotas,
        "total_free": sum(q["free_remaining"] for q in quotas),
        "total_used": sum(q["free_used"] for q in quotas)
    })


@app.route('/api/dashboard-usage')
def api_dashboard_usage():
    """Get usage dashboard for all v2fun accounts (from cache)"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    # Get cached quota snapshots from database (fast!)
    snapshots = get_all_quota_snapshots()
    
    dashboard_data = []
    total_free_all = 0
    total_used_all = 0
    all_models = {}  # model -> {total_free, total_used}
    
    for snap in snapshots:
        import json
        quotas = []
        
        try:
            if snap.get('quota_json'):
                quotas = json.loads(snap['quota_json'])
        except:
            pass
        
        account_data = {
            "email": snap['email'],
            "status": snap.get('status', 'online'),
            "quotas": quotas,
            "total_free": snap.get('total_free', 0),
            "total_used": snap.get('total_used', 0),
            "error": snap.get('error_message'),
            "updated_at": snap.get('updated_at')
        }
        
        # Aggregate by model
        for q in quotas:
            model = q.get('model', '')
            name = q.get('name', '')
            model_key = model or name
            
            if model_key:
                if model_key not in all_models:
                    all_models[model_key] = {"total_free": 0, "total_used": 0, "name": name}
                all_models[model_key]["total_free"] += q.get('free_remaining', 0)
                all_models[model_key]["total_used"] += q.get('free_used', 0)
        
        total_free_all += account_data["total_free"]
        total_used_all += account_data["total_used"]
        dashboard_data.append(account_data)
    
    # Convert all_models dict to list
    models_list = [
        {
            "model": k,
            "name": v["name"],
            "total_free": v["total_free"],
            "total_used": v["total_used"]
        }
        for k, v in all_models.items()
    ]
    models_list.sort(key=lambda x: x["total_free"], reverse=True)
    
    return jsonify({
        "success": True,
        "accounts": dashboard_data,
        "summary": {
            "total_accounts": len(snapshots),
            "total_free": total_free_all,
            "total_used": total_used_all
        },
        "models": models_list
    })


@app.route('/api/refresh-dashboard-cache', methods=['POST'])
def api_refresh_dashboard_cache():
    """Refresh quota cache by fetching real-time data from V2Fun API"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    import json
    
    # Get all v2fun accounts with tokens
    accounts = get_all_v2fun_accounts(user.get('user_id'))
    
    # Filter only accounts with valid tokens (status=done)
    active_accounts = [a for a in accounts if a.get('status') == 'done' and a.get('jwt_token')]
    
    if len(active_accounts) == 0:
        return jsonify({"success": False, "message": "No active V2Fun accounts found"})
    
    updated_count = 0
    
    for account in active_accounts:
        token = account.get('jwt_token')
        if not token:
            continue
        
        email = account.get('email')
        client = V2FunClient(token)
        
        # Get business config
        config_resp = client.get_business_config()
        config_map = {}
        
        if config_resp.get("success"):
            result = config_resp.get("result", {})
            configs = result if isinstance(result, list) else result.get("list", result.get("records", []))
            if isinstance(configs, list):
                for cfg in configs:
                    cid = str(cfg.get("id", ""))
                    model = cfg.get("model", "")
                    name = cfg.get("name", "") or cfg.get("configName", "")
                    if cid:
                        config_map[cid] = {"model": model, "name": name}
        
        # Get free count
        free_resp = client.get_free_count()
        
        if free_resp.get("success"):
            free_counts = free_resp.get("result", {})
            
            total_free = 0
            total_used = 0
            quotas = []
            
            for cid, count in free_counts.items():
                cfg_info = config_map.get(cid, {})
                model = cfg_info.get("model", "")
                name = cfg_info.get("name", "")
                
                if model or name:
                    free_remaining = count
                    free_total = 5
                    free_used = max(0, free_total - free_remaining)
                    
                    quotas.append({
                        "model": model,
                        "name": name,
                        "free_remaining": free_remaining,
                        "free_total": free_total,
                        "free_used": free_used
                    })
                    
                    total_free += free_remaining
                    total_used += free_used
            
            # Save to database
            update_quota_snapshot(
                email=email,
                total_free=total_free,
                total_used=total_used,
                quota_json=json.dumps(quotas),
                status='online',
                error_message=None
            )
            updated_count += 1
        else:
            # Save error status
            update_quota_snapshot(
                email=email,
                total_free=0,
                total_used=0,
                quota_json='[]',
                status='error',
                error_message=free_resp.get("message", "Failed to fetch quota")
            )
    
    return jsonify({
        "success": True,
        "message": f"Updated {updated_count}/{len(active_accounts)} accounts",
        "updated": updated_count,
        "total": len(active_accounts)
    })


@app.route('/api/export-data')
def api_export_data():
    """Export all data: users, v2fun_accounts, session tokens, account.txt"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    import zipfile
    import io
    import tempfile
    
    # Create in-memory zip
    memory_file = io.BytesIO()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Export database
        db_path = V2FUN_DATA_DIR / "v2fun.db"
        if db_path.exists():
            zf.write(str(db_path), "v2fun.db")
        
        # Export session files (tokens)
        v2fun_data = V2FUN_DATA_DIR
        for session_file in v2fun_data.glob("v2fun_session_*_latest.json"):
            zf.write(str(session_file), f"sessions/{session_file.name}")
        
        # Export account.txt
        account_file = ACCOUNT_FILE
        if account_file.exists():
            zf.write(str(account_file), "account.txt")
        
        # Export v2fun_accounts from DB as JSON
        from v2fun_scripts.database import get_db
        conn = get_db()
        cursor = conn.cursor()
        
        # Export users (without password hashes for security - just emails)
        cursor.execute("SELECT id, email, google_email, credits, created_at FROM users")
        users = [dict(row) for row in cursor.fetchall()]
        
        # Export v2fun_accounts (with tokens - needed for migration)
        cursor.execute("SELECT id, email, password, status, jwt_token, token_expiry, created_at FROM v2fun_accounts")
        v2fun_accounts = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        export_meta = {
            "export_date": datetime.now().isoformat(),
            "exported_by": user.get('email'),
            "users": users,
            "v2fun_accounts": v2fun_accounts
        }
        
        zf.writestr("export_meta.json", json.dumps(export_meta, indent=2, ensure_ascii=False, default=str))
    
    memory_file.seek(0)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'v2fun_backup_{timestamp}.zip'
    )


@app.route('/api/import-data', methods=['POST'])
def api_import_data():
    """Import data from backup zip file"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"})
    
    file = request.files['file']
    
    if not file.filename.endswith('.zip'):
        return jsonify({"success": False, "message": "Please upload a .zip backup file"})
    
    import zipfile
    import tempfile
    import shutil
    
    # Save to temp file
    temp_path = Path(tempfile.gettempdir()) / f"v2fun_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file.save(str(temp_path))
    
    results = {"sessions": 0, "accounts": 0, "db": False, "account_txt": False}
    
    try:
        with zipfile.ZipFile(str(temp_path), 'r') as zf:
            # Read export meta
            meta = None
            if "export_meta.json" in zf.namelist():
                meta = json.loads(zf.read("export_meta.json"))
            
            # Import session files (tokens) with Zip Slip protection
            v2fun_data = V2FUN_DATA_DIR.resolve()
            v2fun_data.mkdir(parents=True, exist_ok=True)
            
            for name in zf.namelist():
                if name.startswith("sessions/") and name.endswith(".json"):
                    rel_name = name[len("sessions/"):]
                    # Reject nested paths or traversal attempts
                    if not rel_name or "/" in rel_name or "\\" in rel_name or ".." in rel_name:
                        continue
                    dest = (v2fun_data / rel_name).resolve()
                    if dest.parent != v2fun_data:
                        continue
                    with open(dest, 'wb') as f:
                        f.write(zf.read(name))
                    results["sessions"] += 1
            
            # Import account.txt
            if "account.txt" in zf.namelist():
                with open(ACCOUNT_FILE, 'wb') as f:
                    f.write(zf.read("account.txt"))
                results["account_txt"] = True
            
            # Import v2fun_accounts into database
            if meta and meta.get("v2fun_accounts"):
                from v2fun_scripts.database import get_db
                conn = get_db()
                cursor = conn.cursor()
                
                for acc in meta["v2fun_accounts"]:
                    try:
                        cursor.execute("""
                            INSERT OR REPLACE INTO v2fun_accounts 
                            (id, email, password, status, jwt_token, token_expiry, created_at, processed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """, (
                            acc.get('id'),
                            acc.get('email'),
                            acc.get('password'),
                            acc.get('status', 'pending'),
                            acc.get('jwt_token'),
                            acc.get('token_expiry'),
                            acc.get('created_at')
                        ))
                        results["accounts"] += 1
                    except Exception as e:
                        print(f"Import account error: {e}")
                
                conn.commit()
                conn.close()
            
            # Import database (overwrite - validate SQLite header first!)
            if "v2fun.db" in zf.namelist() and request.form.get('import_db') == 'true':
                db_bytes = zf.read("v2fun.db")
                if not db_bytes.startswith(b"SQLite format 3\x00"):
                    return jsonify({"success": False, "message": "Invalid SQLite database file in backup"}), 400
                db_path = V2FUN_DATA_DIR / "v2fun.db"
                # Backup current DB first
                if db_path.exists():
                    backup_path = db_path.parent / f"v2fun.db.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    shutil.copy2(str(db_path), str(backup_path))
                
                with open(db_path, 'wb') as f:
                    f.write(db_bytes)
                results["db"] = True
        
        return jsonify({
            "success": True,
            "message": f"Import complete! Sessions: {results['sessions']}, Accounts: {results['accounts']}, DB: {'Yes' if results['db'] else 'No'}, account.txt: {'Yes' if results['account_txt'] else 'No'}",
            "results": results
        })
    
    except zipfile.BadZipFile:
        return jsonify({"success": False, "message": "Invalid or corrupted zip file"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Import failed: {str(e)}"})
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.route('/api/manage-accounts')
def api_manage_accounts():
    """List all V2Fun sessions for management"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    v2fun_data = V2FUN_DATA_DIR
    session_files = list(v2fun_data.glob("v2fun_session_*_latest.json"))
    
    accounts = []
    for file in session_files:
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        token = extract_token_from_session(data)
        accounts.append({
            "email": data.get("email"),
            "timestamp": data.get("timestamp"),
            "has_token": bool(token),
            "filename": file.name
        })
    
    return jsonify({"success": True, "accounts": accounts})


@app.route('/api/delete-account', methods=['POST'])
def api_delete_account():
    """Delete a V2Fun session file"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({"success": False, "message": "Email required"})
    
    v2fun_data = V2FUN_DATA_DIR
    email_safe = email.replace("@", "_at_").replace(".", "_")
    
    # Delete all session files for this email
    deleted = False
    for pattern in [f"v2fun_session_{email_safe}_latest.json", f"v2fun_tokens_{email_safe}_*.json"]:
        for f in v2fun_data.glob(pattern):
            f.unlink()
            deleted = True
    
    if deleted:
        return jsonify({"success": True, "message": f"Account {email} deleted successfully"})
    else:
        return jsonify({"success": False, "message": "Account not found"})


@app.route('/api/bulk-delete-accounts', methods=['POST'])
def api_bulk_delete_accounts():
    """Bulk delete V2Fun session files for selected emails"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    emails = data.get('emails') or []
    if not isinstance(emails, list) or not emails:
        return jsonify({"success": False, "message": "No accounts selected"}), 400

    deleted = 0
    for email in emails:
        if not isinstance(email, str) or not email.strip():
            continue
        email_safe = email.strip().replace("@", "_at_").replace(".", "_")
        removed_any = False
        for pattern in [f"v2fun_session_{email_safe}_latest.json", f"v2fun_tokens_{email_safe}_*.json"]:
            for f in V2FUN_DATA_DIR.glob(pattern):
                f.unlink()
                removed_any = True
        if removed_any:
            deleted += 1

    return jsonify({
        "success": True,
        "deleted": deleted,
        "message": f"Deleted {deleted} session(s) successfully",
    })


@app.route('/api/retry-account', methods=['POST'])
def api_retry_account():
    """Retry login for a specific V2Fun account"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({"success": False, "message": "Email required"})
    
    # Try to find password from v2fun_accounts DB first, then account.txt
    password = None
    
    # Check DB
    from v2fun_scripts.database import get_db
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM v2fun_accounts WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        password = row[0]
    
    # Fallback: check account.txt
    if not password:
        password = find_account_password(email, ACCOUNT_FILE)
    
    if not password:
        return jsonify({"success": False, "message": "Password not found for this email"})
    
    # Run login in isolated temporary account file (never overwrite account.txt)
    temp_account_file = V2FUN_DATA_DIR / f"temp_account_{uuid.uuid4().hex}.txt"
    login_script = BASE_DIR / "v2fun_scripts" / "v2fun_google_login.py"
    
    try:
        V2FUN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_account_file.write_text(f"{email}:{password}", encoding="utf-8")
        
        result = subprocess.run(
            [sys.executable, str(login_script), "--account-file", str(temp_account_file)],
            capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR)
        )
        
        email_safe = email.replace("@", "_at_").replace(".", "_")
        session_file = V2FUN_DATA_DIR / f"v2fun_session_{email_safe}_latest.json"
        
        if session_file.exists():
            return jsonify({"success": True, "message": f"Retry successful for {email}"})
        else:
            error_msg = "Retry failed"
            if result.stderr:
                error_msg += f": {result.stderr[:200]}"
            return jsonify({"success": False, "message": error_msg})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "message": "Automation timed out"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {str(e)}"})
    finally:
        if temp_account_file.exists():
            temp_account_file.unlink()


@app.route('/api/token-status')
def api_token_status():
    """Get token status for all accounts"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    statuses = get_all_tokens_status()
    return jsonify({"success": True, "tokens": statuses})


@app.route('/api/refresh-token', methods=['POST'])
def api_refresh_token():
    """Manually trigger token refresh for an account"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({"success": False, "message": "Email required"})
    
    # Find password from account.txt
    password = find_account_password(email, ACCOUNT_FILE)
    
    if not password:
        return jsonify({"success": False, "message": "Password not found in account.txt"})
    
    # Get current token
    v2fun_data = V2FUN_DATA_DIR
    email_safe = email.replace("@", "_at_").replace(".", "_")
    session_file = v2fun_data / f"v2fun_session_{email_safe}_latest.json"
    
    if not session_file.exists():
        return jsonify({"success": False, "message": "Session file not found"})
    
    with open(session_file, "r", encoding="utf-8") as f:
        sess_data = json.load(f)
    
    old_token = sess_data.get("tokens", {}).get("cookie_token", "")
    status = get_token_status(old_token)
    
    # Refresh token
    new_status, new_token = check_and_refresh_if_needed(old_token, email, password)
    
    if new_token:
        # Update session file
        sess_data["tokens"]["cookie_token"] = new_token
        sess_data["timestamp"] = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(sess_data, f, indent=2, ensure_ascii=False)
        
        # If this is the connected account, update DB
        uid = user.get('user_id') or user.get('id')
        if user.get('v2fun_token') == old_token and uid:
            update_user_token(uid, new_token)
        
        remaining = get_time_remaining(new_token)
        return jsonify({
            "success": True,
            "message": f"Token refreshed! Valid for {remaining}",
            "new_status": new_status,
            "remaining": str(remaining)
        })
    else:
        return jsonify({
            "success": False,
            "message": f"Refresh failed. Current status: {status}",
            "new_status": new_status
        })


# ============================================================================
# INTEGRATIONS API
# ============================================================================

@app.route('/api/integrations', methods=['GET'])
def api_get_integrations():
    """Get all integrations for current user"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    user_id = user.get('user_id') or user.get('id')
    integrations = get_all_integrations(user_id)
    
    # Mask API keys (show only last 4 chars)
    for integration in integrations:
        api_key = integration.get('api_key', '')
        if len(api_key) > 8:
            integration['api_key_masked'] = '*' * (len(api_key) - 4) + api_key[-4:]
        else:
            integration['api_key_masked'] = '****'
    
    return jsonify({"success": True, "integrations": integrations})


@app.route('/api/integrations/<service_name>', methods=['GET'])
def api_get_integration(service_name):
    """Get specific integration for current user"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    user_id = user.get('user_id') or user.get('id')
    integration = get_integration(user_id, service_name)
    
    if not integration:
        return jsonify({"success": False, "message": "Integration not found"}), 404
    
    return jsonify({"success": True, "integration": integration})


@app.route('/api/integrations', methods=['POST'])
def api_save_integration():
    """Save or update integration settings"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    service_name = data.get('service_name', '').strip()
    base_url = data.get('base_url', '').strip()
    api_key = data.get('api_key', '').strip()
    
    if not service_name or not base_url or not api_key:
        return jsonify({"success": False, "message": "Service name, base URL, and API key are required"})
    
    # Validate URL format
    if not base_url.startswith(('http://', 'https://')):
        return jsonify({"success": False, "message": "Base URL must start with http:// or https://"})
    
    user_id = user.get('user_id') or user.get('id')
    save_integration(user_id, service_name, base_url, api_key)
    
    return jsonify({"success": True, "message": f"{service_name} integration saved successfully"})


@app.route('/api/integrations/<service_name>', methods=['DELETE'])
def api_delete_integration(service_name):
    """Delete integration settings"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    user_id = user.get('user_id') or user.get('id')
    delete_integration(user_id, service_name)
    
    return jsonify({"success": True, "message": f"{service_name} integration deleted"})


@app.route('/api/integrations/<service_name>/toggle', methods=['POST'])
def api_toggle_integration(service_name):
    """Enable or disable integration"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    data = request.get_json(silent=True) or {}
    is_active = data.get('is_active', True)
    
    user_id = user.get('user_id') or user.get('id')
    toggle_integration(user_id, service_name, is_active)
    
    status = "enabled" if is_active else "disabled"
    return jsonify({"success": True, "message": f"{service_name} integration {status}"})


def _is_safe_external_url(url: str) -> bool:
    """Reject non-HTTP(S) and internal/private/loopback/link-local targets to mitigate SSRF"""
    if os.environ.get('ALLOW_PRIVATE_INTEGRATIONS', 'false').lower() == 'true':
        return url.startswith(('http://', 'https://'))
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


@app.route('/api/integrations/<service_name>/test', methods=['POST'])
def api_test_integration(service_name):
    """Test integration connection"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    
    user_id = user.get('user_id') or user.get('id')
    integration = get_integration(user_id, service_name)
    
    if not integration:
        return jsonify({"success": False, "message": "Integration not found"}), 404
    
    if not _is_safe_external_url(integration['base_url']):
        return jsonify({"success": False, "message": "Integration URL points to a disallowed or private address"}), 400

    try:
        # Test health endpoint
        response = requests.get(
            f"{integration['base_url'].rstrip('/')}/health",
            headers={'Authorization': f"Bearer {integration['api_key']}"},
            timeout=10,
            allow_redirects=False
        )
        
        if response.status_code == 200:
            return jsonify({
                "success": True, 
                "message": "Connection successful",
                "response": response.json() if response.headers.get('content-type', '').startswith('application/json') else None
            })
        else:
            return jsonify({
                "success": False, 
                "message": f"Connection failed with status {response.status_code}"
            })
    except requests.exceptions.RequestException as e:
        return jsonify({
            "success": False, 
            "message": f"Connection error: {str(e)}"
        })


@app.route('/api/refresh-all-tokens', methods=['POST'])
def api_refresh_all_tokens():
    """Proactively refresh all expiring V2Fun tokens on demand"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    from v2fun_scripts.token_manager import refresh_all_expiring_tokens
    data = request.json or {}
    force = bool(data.get('force', False))
    summary = refresh_all_expiring_tokens(force=force)
    return jsonify({"success": True, "summary": summary})


def auto_refresh_if_needed(user_data: dict, v2fun_email: str = None) -> dict:
    """Auto-check and refresh token before API calls / on activation"""
    token = user_data.get('v2fun_token')
    if not token:
        return user_data

    status = get_token_status(token)
    if status in ("expired", "critical"):
        email = v2fun_email or session.get('selected_v2fun_email') or user_data.get('google_email')
        if not email:
            # Look up matching V2Fun account email in DB by current token
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT email, password FROM v2fun_accounts WHERE jwt_token = ?",
                    (token,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                email = row['email']

        if email:
            password = None
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT password FROM v2fun_accounts WHERE email = ?",
                    (email,),
                ).fetchone()
                if row and row['password']:
                    password = row['password']
            finally:
                conn.close()

            if not password:
                password = find_account_password(email, ACCOUNT_FILE)

            if password:
                _, new_token = check_and_refresh_if_needed(token, email, password)
                if new_token:
                    _sync_refreshed_token_to_db(email, token, new_token)
                    user_data['v2fun_token'] = new_token
                    uid = user_data.get('user_id') or user_data.get('id')
                    if uid:
                        update_user_token(uid, new_token)

    return user_data


def start_monitor(user_id: int, token: str, generation_id: int, task_uuid: str):
    """Start monitoring for generation completion (SSE + polling fallback)"""
    from v2fun_scripts.image_downloader import download_image as dl_image
    
    def push_event(ev):
        if generation_id not in pending_events:
            pending_events[generation_id] = []
        pending_events[generation_id].append(ev)

    def _clear_monitor():
        with monitors_lock:
            mon = active_monitors.pop(generation_id, None)
        if mon:
            mon.stop()

    def on_update(tid, event):
        push_event({
            "type": "progress",
            "progress": event.get("progress", 0),
            "status": event.get("status", ""),
            "work_url": event.get("work_url"),
            "thumb": event.get("thumb")
        })

    def on_done(tid, event):
        status = event.get("status", "")
        work_url = event.get("work_url") or event.get("thumb")
        if work_url and not work_url.startswith("http"):
            work_url = f"https://asset.v2fun.ai/{work_url}"
        db_status = "done" if status in ("C", "COMPLETED", "DONE", "SUCCESS") else "failed"
        
        # Download image to local storage
        local_path = None
        if work_url and db_status == "done":
            # Get prompt from database for filename
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT prompt FROM generations WHERE id = ?", (generation_id,))
            row = cursor.fetchone()
            conn.close()
            
            prompt = row['prompt'] if row else ""
            local_path = dl_image(work_url, generation_id, prompt)
        
        # Update database with result URL and local path
        update_generation_status(generation_id, db_status, work_url, work_url, local_path=local_path)
        local_url = f"/results/{Path(local_path).name}" if local_path else None
        
        push_event({
            "type": "done", 
            "status": db_status, 
            "work_url": work_url, 
            "local_path": local_path,
            "local_url": local_url
        })
        _clear_monitor()

    def on_error(tid, error):
        update_generation_status(generation_id, "failed", error_message=error)
        push_event({"type": "error", "message": error})
        _clear_monitor()

    # Start SSE monitor keyed by generation_id so concurrent tasks don't clobber each other
    monitor = SSEMonitor(token)
    with monitors_lock:
        active_monitors[generation_id] = monitor
    t = threading.Thread(target=lambda: monitor.watch(task_uuid, on_update=on_update, on_done=on_done, on_error=on_error), daemon=True)
    t.start()

    # Start polling fallback (cek status setiap 10 detik)
    def polling_fallback():
        """Poll V2Fun API for generation status (fallback if SSE doesn't work)"""
        import time
        time.sleep(10)  # Wait 10s before starting polling
        client = V2FunClient(token)
        
        # Correct request body for getResourceList (POST)
        payload = {
            "pager": {
                "orderBy": "updateTime",
                "pageSize": 10,
                "pageNo": 1,
                "needQueryCount": True
            },
            "parm": {
                "keyword": "",
                "workType": "",
                "listType": "folder",
                "isUpload": 0,
                "bindState": None,
                "license": "",
                "orderBy": "updateTime",
                "simple": 1
            }
        }
        
        max_attempts = 60  # 10 minutes max (polling every 10s)
        for attempt in range(max_attempts):
            with monitors_lock:
                still_active = generation_id in active_monitors
            if not still_active:
                return  # Monitor already finished via SSE
            
            try:
                resp = requests.post(
                    f"{client.base_url}/work/getResourceList?lan=en",
                    headers=client.headers,
                    json=payload,
                    timeout=10
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        result = data.get("result", {})
                        records = result.get("records") or []
                        
                        if isinstance(records, list):
                            for record in records:
                                # Navigate: record -> child[] -> works[]
                                children = record.get("child", [])
                                for child_item in children:
                                    works = child_item.get("works", [])
                                    for work in works:
                                        task_id = str(work.get("taskId", ""))
                                        gen_status = work.get("generateStatus", "")
                                        work_url = work.get("workUrl") or ""
                                        thumb = work.get("thumb") or ""
                                        progress = work.get("progress", 0)
                                        
                                        # Match by taskId
                                        if task_id == str(task_uuid):
                                            # Status "A" = Active/Done, "I" = In Progress
                                            if gen_status == "A" and (work_url or thumb):
                                                # Generation complete!
                                                full_url = work_url if work_url else thumb
                                                # Build full asset URL if relative
                                                if full_url and not full_url.startswith("http"):
                                                    full_url = f"https://asset.v2fun.ai/{full_url}"
                                                
                                                # Download to local storage
                                                from v2fun_scripts.image_downloader import download_image as dl_image
                                                conn = get_db()
                                                cursor = conn.cursor()
                                                cursor.execute("SELECT prompt FROM generations WHERE id = ?", (generation_id,))
                                                row = cursor.fetchone()
                                                conn.close()
                                                prompt = row['prompt'] if row else ""
                                                local_path = dl_image(full_url, generation_id, prompt)
                                                local_url = f"/results/{Path(local_path).name}" if local_path else None
                                                
                                                update_generation_status(generation_id, "done", full_url, thumb if thumb else full_url, local_path=local_path)
                                                push_event({
                                                    "type": "done",
                                                    "status": "done",
                                                    "work_url": full_url,
                                                    "local_path": local_path,
                                                    "local_url": local_url
                                                })
                                                _clear_monitor()
                                                return
                                            
                                            elif gen_status == "I":
                                                # Still in progress
                                                pct = int(float(progress) * 100) if progress else 50
                                                push_event({
                                                    "type": "progress",
                                                    "progress": pct,
                                                    "status": "processing"
                                                })
                                            
                                            elif gen_status in ("F", "E"):
                                                # Failed
                                                update_generation_status(generation_id, "failed", error_message="Generation failed on server")
                                                push_event({
                                                    "type": "done",
                                                    "status": "failed",
                                                    "work_url": None
                                                })
                                                _clear_monitor()
                                                return
            except Exception as e:
                print(f"Polling error: {e}")
            
            time.sleep(10)
        
        # Timeout - mark as pending (user can check gallery later)
        push_event({
            "type": "done",
            "status": "timeout",
            "work_url": None
        })
        _clear_monitor()

    t2 = threading.Thread(target=polling_fallback, daemon=True)
    t2.start()


@app.route('/api/generation-stream/<int:generation_id>')
def api_generation_stream(generation_id):
    """SSE endpoint - stream generation progress to browser"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    uid = user.get('user_id') or user.get('id')
    conn = get_db()
    try:
        row = conn.execute("SELECT user_id FROM generations WHERE id = ?", (generation_id,)).fetchone()
    finally:
        conn.close()
    if not row or row['user_id'] != uid:
        return jsonify({"success": False, "message": "Generation not found"}), 404

    def event_stream():
        import time
        timeout = 300
        elapsed = 0
        try:
            while elapsed < timeout:
                events = pending_events.pop(generation_id, [])
                for ev in events:
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        return
                time.sleep(1)
                elapsed += 1
            yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
        finally:
            pending_events.pop(generation_id, None)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serve uploaded reference images (authenticated)"""
    if not get_current_user():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/results/<filename>')
def result_file(filename):
    """Serve downloaded result images (authenticated)"""
    if not get_current_user():
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    return send_from_directory(RESULTS_DIR, filename)


if __name__ == '__main__':
    # Initialize database
    init_db()
    
    # Recover V2Fun accounts from session files on disk into the database.
    # This fixes the old bug where duplicate imports wiped successful accounts
    # from the DB even though their tokens were still saved on disk.
    recovered = sync_v2fun_sessions_to_db()
    if recovered > 0:
        print(f"[+] Recovered {recovered} V2Fun account(s) from session files into database.")
    
    print("="*80)
    print("V2Fun.ai Web UI V2 - Enhanced")
    print("="*80)
    print("\nFeatures:")
    print("  [+] User registration & login")
    print("  [+] Image upload for references")
    print("  [+] Database-backed generation history")
    print("  [+] Session management")
    print("\nStarting server...")
    print("Open browser: http://localhost:5000")
    print("\nPress Ctrl+C to stop\n")
    
    if os.environ.get('ENABLE_TOKEN_AUTO_REFRESH', 'true').lower() == 'true':
        scheduler = TokenRefreshScheduler(
            interval_hours=float(os.environ.get('TOKEN_REFRESH_INTERVAL_HOURS', '1')),
            min_remaining_hours=float(os.environ.get('TOKEN_REFRESH_BEFORE_HOURS', '12')),
            force=os.environ.get('TOKEN_REFRESH_FORCE', 'true').lower() == 'true',
        )
        scheduler.start()

    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', '5000'))
    app.run(host=host, port=port, debug=debug_mode)
