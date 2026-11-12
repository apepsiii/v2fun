import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import v2fun_backend_api as bapi
from v2fun_scripts import database as db
from v2fun_scripts import token_manager as tm
from v2fun_scripts import v2fun_client as vc
from v2fun_scripts import v2fun_web_v2 as web


class IsolatedSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.orig_db_data = db.V2FUN_DATA_DIR
        self.orig_db_path = db.DB_PATH
        self.orig_web_data = web.V2FUN_DATA_DIR
        self.orig_web_results = web.RESULTS_DIR
        self.orig_upload_folder = web.app.config["UPLOAD_FOLDER"]

        db.V2FUN_DATA_DIR = self.tmp / "v2fun_data"
        db.DB_PATH = db.V2FUN_DATA_DIR / "v2fun.db"
        web.V2FUN_DATA_DIR = db.V2FUN_DATA_DIR
        web.RESULTS_DIR = db.V2FUN_DATA_DIR / "results"
        web.app.config["UPLOAD_FOLDER"] = db.V2FUN_DATA_DIR / "uploads"
        web.app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)
        web.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        db.init_db()

    def tearDown(self):
        db.V2FUN_DATA_DIR = self.orig_db_data
        db.DB_PATH = self.orig_db_path
        web.V2FUN_DATA_DIR = self.orig_web_data
        web.RESULTS_DIR = self.orig_web_results
        web.app.config["UPLOAD_FOLDER"] = self.orig_upload_folder
        self.tmpdir.cleanup()

    def test_password_hashing_and_legacy_upgrade(self):
        conn = db.get_db()
        legacy_hash = hashlib.sha256(b"SecretPass123").hexdigest()
        conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("legacy@example.com", legacy_hash),
        )
        conn.commit()
        conn.close()

        user = db.verify_user("legacy@example.com", "SecretPass123")
        self.assertIsNotNone(user)
        self.assertNotIn("password_hash", user)

        conn = db.get_db()
        upgraded = conn.execute(
            "SELECT password_hash FROM users WHERE email = ?",
            ("legacy@example.com",),
        ).fetchone()[0]
        conn.close()
        self.assertNotEqual(upgraded, legacy_hash)
        self.assertTrue(upgraded.startswith(("scrypt:", "pbkdf2:")))
        self.assertIsNotNone(db.verify_user("legacy@example.com", "SecretPass123"))
        self.assertIsNone(db.verify_user("legacy@example.com", "WrongPass"))

    def test_session_utc_expiry_and_foreign_keys(self):
        uid = db.create_user("sess@example.com", "Pass1234")
        token = db.create_session(uid, expires_in_days=1)
        verified = db.verify_session(token)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user_id"], uid)

        # Deleting user cascades to sessions because PRAGMA foreign_keys = ON
        conn = db.get_db()
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_ownership_delete_v2fun_account(self):
        uid1 = db.create_user("u1@example.com", "Pass1234")
        uid2 = db.create_user("u2@example.com", "Pass1234")
        db.import_v2fun_account("acc1@gmail.com", "pw1", user_id=uid1)

        conn = db.get_db()
        acc_id = conn.execute("SELECT id FROM v2fun_accounts WHERE email = 'acc1@gmail.com'").fetchone()[0]
        conn.close()

        self.assertFalse(db.delete_v2fun_account(acc_id, user_id=uid2))
        self.assertTrue(db.delete_v2fun_account(acc_id, user_id=uid1))

    def test_bulk_delete_endpoints_and_ownership(self):
        uid1 = db.create_user("bulk1@example.com", "Pass1234")
        uid2 = db.create_user("bulk2@example.com", "Pass1234")
        db.import_v2fun_account("b1@gmail.com", "pw1", user_id=uid1)
        db.import_v2fun_account("b2@gmail.com", "pw2", user_id=uid1)
        db.import_v2fun_account("other@gmail.com", "pw3", user_id=uid2)

        with db.db_session() as conn:
            id1 = conn.execute("SELECT id FROM v2fun_accounts WHERE email='b1@gmail.com'").fetchone()[0]
            id2 = conn.execute("SELECT id FROM v2fun_accounts WHERE email='b2@gmail.com'").fetchone()[0]
            id_other = conn.execute("SELECT id FROM v2fun_accounts WHERE email='other@gmail.com'").fetchone()[0]

        client = web.app.test_client()
        self.assertEqual(client.post("/api/bulk-delete-v2fun-accounts", json={"account_ids": [id1]}).status_code, 401)
        self.assertEqual(client.post("/api/bulk-delete-accounts", json={"emails": ["b1@gmail.com"]}).status_code, 401)

        client.post("/api/login", json={"email": "bulk1@example.com", "password": "Pass1234"})
        resp = client.post("/api/bulk-delete-v2fun-accounts", json={"account_ids": [id1, id2, id_other]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["deleted"], 2)
        self.assertEqual(data["skipped"], 1)

        # Create dummy session files and bulk delete them
        s1 = web.V2FUN_DATA_DIR / "v2fun_session_b1_at_gmail_com_latest.json"
        s2 = web.V2FUN_DATA_DIR / "v2fun_session_b2_at_gmail_com_latest.json"
        s1.write_text("{}", encoding="utf-8")
        s2.write_text("{}", encoding="utf-8")
        resp2 = client.post("/api/bulk-delete-accounts", json={"emails": ["b1@gmail.com", "b2@gmail.com"]})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()["deleted"], 2)
        self.assertFalse(s1.exists())
        self.assertFalse(s2.exists())

    def test_gallery_serves_local_results_first(self):
        uid = db.create_user("gal@example.com", "Pass1234")
        gen_id = db.create_generation(uid, "a sunset landscape", task_uuid="task-gal-1")
        local_file = web.RESULTS_DIR / f"gen_{gen_id}_20261112_sunset.png"
        local_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        db.update_generation_status(
            gen_id,
            "done",
            result_url="https://expired-cdn.example.com/sunset.png",
            local_path=f"v2fun_data/results/{local_file.name}",
        )

        client = web.app.test_client()
        client.post("/api/login", json={"email": "gal@example.com", "password": "Pass1234"})
        resp = client.get("/api/generations")
        self.assertEqual(resp.status_code, 200)
        gens = resp.get_json()["generations"]
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0]["local_url"], f"/results/{local_file.name}")

        with client.get(gens[0]["local_url"]) as img_resp:
            self.assertEqual(img_resp.status_code, 200)
            self.assertTrue(img_resp.data.startswith(b"\x89PNG"))

    def test_web_v2_auth_csrf_zipslip_ssrf(self):
        client = web.app.test_client()

        for path in ["/api/sessions", "/api/manage-accounts", "/uploads/a.png", "/results/a.png", "/api/generation-stream/1"]:
            resp = client.get(path)
            self.assertEqual(resp.status_code, 401, f"Expected 401 on {path}")

        # Cross-origin POST blocked
        csrf_resp = client.post(
            "/api/login",
            json={"email": "u1@example.com", "password": "Pass1234"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(csrf_resp.status_code, 403)

        db.create_user("u1@example.com", "Pass1234")
        login_resp = client.post("/api/login", json={"email": "u1@example.com", "password": "Pass1234"})
        self.assertTrue(login_resp.get_json()["success"])

        # Zip Slip + invalid SQLite header blocked
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("sessions/../../evil.json", "{}")
            zf.writestr("v2fun.db", b"NOT_SQLITE")
        buf.seek(0)
        imp_resp = client.post(
            "/api/import-data",
            data={"file": (buf, "bad.zip"), "import_db": "true"},
            content_type="multipart/form-data",
        )
        self.assertEqual(imp_resp.status_code, 400)
        self.assertFalse((self.tmp / "evil.json").exists())

        # SSRF blocked
        client.post(
            "/api/integrations",
            json={"service_name": "local", "base_url": "http://127.0.0.1:5000", "api_key": "k"},
        )
        ssrf_resp = client.post("/api/integrations/local/test")
        self.assertEqual(ssrf_resp.status_code, 400)

    def test_backend_api_auth_and_poll_result(self):
        orig_key = bapi.BACKEND_API_KEY
        try:
            bapi.BACKEND_API_KEY = "unit-test-key"
            client = bapi.app.test_client()
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/api/accounts").status_code, 401)
            self.assertEqual(client.get("/api/accounts", headers={"X-API-Key": "unit-test-key"}).status_code, 200)
            self.assertEqual(client.get("/api/accounts", headers={"Authorization": "Bearer unit-test-key"}).status_code, 200)
        finally:
            bapi.BACKEND_API_KEY = orig_key

        # poll_result handles 'F' failure status and sleeps when task not yet listed
        api_client = vc.V2FunClient("dummy")
        mock_resp_empty = MagicMock(status_code=200)
        mock_resp_empty.json.return_value = {"success": True, "result": {"records": []}}
        mock_resp_fail = MagicMock(status_code=200)
        mock_resp_fail.json.return_value = {
            "success": True,
            "result": {"records": [{"child": [{"works": [{"taskId": "t-1", "generateStatus": "F"}]}]}]},
        }
        with patch("v2fun_scripts.v2fun_client.requests.post", side_effect=[mock_resp_empty, mock_resp_fail]), \
             patch("v2fun_scripts.v2fun_client.time.sleep") as mock_sleep:
            res = api_client.poll_result("t-1", max_attempts=3, interval=2)
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "failed")
        mock_sleep.assert_called_once_with(2)

    def test_backend_job_persistence_and_validation(self):
        orig_key = bapi.BACKEND_API_KEY
        try:
            bapi.BACKEND_API_KEY = ""
            client = bapi.app.test_client()

            # Invalid limit returns 400 instead of 500
            self.assertEqual(client.get("/api/jobs?limit=notanint").status_code, 400)

            # Invalid quality/ratio return 400
            self.assertEqual(client.post("/api/generate", json={"prompt": "cat", "quality": "ultra"}).status_code, 400)
            self.assertEqual(client.post("/api/generate", json={"prompt": "cat", "ratio": "21:9"}).status_code, 400)

            # Persist job in DB, clear in-memory cache (simulating restart), verify still accessible
            job = {
                "id": "job-persist-1",
                "prompt": "a red fox",
                "status": "completed",
                "source": "api",
                "account": "acc@gmail.com",
                "model": "nano-banana-pro",
                "quality": "high",
                "ratio": "16:9",
                "created_at": "2026-11-12T12:00:00",
                "completed_at": "2026-11-12T12:00:30",
                "work_url": "https://example.com/fox.png",
                "progress": 100,
                "fallback_attempts": [],
            }
            db.save_backend_job(job)
            bapi.jobs.clear()

            status_resp = client.get("/api/status/job-persist-1")
            self.assertEqual(status_resp.status_code, 200)
            self.assertEqual(status_resp.get_json()["job"]["prompt"], "a red fox")

            gallery_resp = client.get("/api/gallery")
            self.assertEqual(gallery_resp.status_code, 200)
            self.assertEqual(gallery_resp.get_json()["total"], 1)

            del_resp = client.delete("/api/gallery/job-persist-1")
            self.assertEqual(del_resp.status_code, 200)
            self.assertEqual(client.get("/api/status/job-persist-1").status_code, 404)
        finally:
            bapi.BACKEND_API_KEY = orig_key

    def test_login_rate_limiting_and_account_parser(self):
        parsed = vc.parse_account_lines("# comment\n\na@b.com|pw1\nc@d.com:pw2\nbadline\n")
        self.assertEqual(parsed, [("a@b.com", "pw1"), ("c@d.com", "pw2")])

        db.create_user("rl@example.com", "RightPass1")
        client = web.app.test_client()
        web._login_failures.clear()
        for _ in range(web.LOGIN_MAX_ATTEMPTS):
            r = client.post("/api/login", json={"email": "rl@example.com", "password": "wrong"})
            self.assertEqual(r.status_code, 401)
        blocked = client.post("/api/login", json={"email": "rl@example.com", "password": "RightPass1"})
        self.assertEqual(blocked.status_code, 429)
        web._login_failures.clear()

    @staticmethod
    def _make_jwt(exp_ts: int) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_ts, "userid": "1"}).encode()).decode().rstrip("=")
        return f"{header}.{payload}.sig"

    def test_proactive_token_refresh_and_scheduler_endpoints(self):
        orig_tm_data = tm.V2FUN_DATA_DIR
        orig_tm_acc = tm.ACCOUNT_FILE
        orig_key = bapi.BACKEND_API_KEY
        try:
            tm.V2FUN_DATA_DIR = self.tmp / "v2fun_data"
            tm.ACCOUNT_FILE = self.tmp / "account.txt"
            bapi.BACKEND_API_KEY = ""

            expiring_tok = self._make_jwt(int(time.time()) + 2 * 3600)   # 2h left (< 12h)
            healthy_tok = self._make_jwt(int(time.time()) + 48 * 3600)   # 48h left (> 12h)
            fresh_tok = self._make_jwt(int(time.time()) + 72 * 3600)     # 72h left

            self.assertTrue(tm.should_refresh_token("", min_remaining_hours=12))
            self.assertTrue(tm.should_refresh_token(expiring_tok, min_remaining_hours=12))
            self.assertFalse(tm.should_refresh_token(healthy_tok, min_remaining_hours=12))

            uid = db.create_user("owner@example.com", "Pass1234")
            db.update_user_token(uid, expiring_tok)
            db.import_v2fun_account("exp@gmail.com", "pw_exp", user_id=uid)
            db.import_v2fun_account("ok@gmail.com", "pw_ok", user_id=uid)
            with db.db_session() as conn:
                conn.execute("UPDATE v2fun_accounts SET status='done', jwt_token=? WHERE email='exp@gmail.com'", (expiring_tok,))
                conn.execute("UPDATE v2fun_accounts SET status='done', jwt_token=? WHERE email='ok@gmail.com'", (healthy_tok,))
                conn.commit()

            reloaded = []
            with patch("v2fun_scripts.token_manager.refresh_token_sync", return_value=(True, fresh_tok)) as mock_ref:
                summary = tm.refresh_all_expiring_tokens(
                    force=False,
                    min_remaining_hours=12,
                    on_refreshed=lambda: reloaded.append(True),
                )

            self.assertEqual(summary["checked"], 2)
            self.assertEqual(summary["refreshed"], 1)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(len(reloaded), 1)
            mock_ref.assert_called_once_with("exp@gmail.com", "pw_exp")

            # Verify DB users & v2fun_accounts updated with fresh token
            updated_user = db.get_user_by_id(uid)
            self.assertEqual(updated_user["v2fun_token"], fresh_tok)

            # Verify scheduler status & manual trigger endpoints (1h interval + force=True by default)
            bclient = bapi.app.test_client()
            st = bclient.get("/api/token-refresh-status")
            self.assertEqual(st.status_code, 200)
            self.assertEqual(st.get_json()["scheduler"]["interval_hours"], 1.0)
            self.assertTrue(st.get_json()["scheduler"]["force"])

            # When force=True, even healthy token (48h left) is refreshed; failed refresh preserves existing token
            with patch("v2fun_scripts.token_manager.refresh_token_sync", return_value=(False, None)):
                fail_summary = tm.refresh_all_expiring_tokens(force=True)
                self.assertEqual(fail_summary["failed"], 2)
            self.assertEqual(db.get_user_by_id(uid)["v2fun_token"], fresh_tok)

            with patch.object(bapi.token_scheduler, "trigger_now", return_value=summary):
                trig = bclient.post("/api/refresh-tokens", json={"async": False, "force": False})
                self.assertEqual(trig.status_code, 200)
                self.assertEqual(trig.get_json()["summary"]["refreshed"], 1)

            # Web V2 endpoint requires auth
            self.assertEqual(web.app.test_client().post("/api/refresh-all-tokens", json={}).status_code, 401)
        finally:
            tm.V2FUN_DATA_DIR = orig_tm_data
            tm.ACCOUNT_FILE = orig_tm_acc
            bapi.BACKEND_API_KEY = orig_key

    def test_inline_javascript_syntax(self):
        if not shutil.which("node"):
            self.skipTest("node not available")
        for label, html_src in [
            ("backend_api DASHBOARD_HTML", bapi.DASHBOARD_HTML),
            ("web_v2 dashboard.html", (PROJECT_ROOT / "v2fun_web_v2/templates/dashboard.html").read_text(encoding="utf-8")),
        ]:
            scripts = re.findall(r"<script>(.*?)</script>", html_src, flags=re.DOTALL)
            self.assertTrue(scripts, f"No script found in {label}")
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tf.write("\n".join(scripts))
                js_path = tf.name
            try:
                res = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"JS syntax error in {label}: {res.stderr}")
            finally:
                os.unlink(js_path)


if __name__ == "__main__":
    unittest.main()
