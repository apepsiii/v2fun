"""
Shared V2Fun.ai API Client and Account/Session Helpers.
Centralizes V2Fun HTTP endpoints, OSS upload, polling, and account parsing.
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
V2FUN_DATA_DIR = BASE_DIR / "v2fun_data"
ACCOUNT_FILE = BASE_DIR / "account.txt"

MODEL_PRIORITY = [
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2",
    "nano-banana-pro",
    "nano-banana-2",
    "nano-banana-2-lite",
    "qwen-image",
]

VALID_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
VALID_QUALITIES = {"low", "medium", "high"}

V2FUN_CONFIG_IDS = [
    "2048727132348956673", "2027223916544393217", "1999059275770777602",
    "1999058822540242946", "2072232369544179713", "2048727897058656258",
    "2027224098833039361", "1999364117728792578", "1999345578888335362",
    "2072232439534530561", "2005566311005237249", "2037420722827362306",
    "2005578171616145409", "2005584046959439873", "2005584317174652929",
    "2005586377985830913", "2005888354655268866", "2037418377902362625",
    "2005887778982850561",
]


def get_session_file_path(email: str, data_dir: Path = V2FUN_DATA_DIR) -> Path:
    """Return the standardized session JSON path for a V2Fun email."""
    email_safe = email.replace("@", "_at_").replace(".", "_")
    return Path(data_dir) / f"v2fun_session_{email_safe}_latest.json"


def extract_token_from_session(session_data: dict) -> str:
    """Extract JWT token from a loaded session dictionary."""
    tokens = session_data.get("tokens", {})
    return tokens.get("cookie_token") or tokens.get("localStorage_access_token") or ""


def parse_account_lines(text: str) -> List[Tuple[str, str]]:
    """Parse email|password or email:password lines, ignoring comments and blanks."""
    accounts: List[Tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            parts = line.split("|", 1)
        elif ":" in line:
            parts = line.split(":", 1)
        else:
            continue
        email, password = parts[0].strip(), parts[1].strip()
        if email and password:
            accounts.append((email, password))
    return accounts


def find_account_password(email: str, account_file: Path = ACCOUNT_FILE) -> Optional[str]:
    """Find password for a given email from account.txt."""
    path = Path(account_file)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    for acc_email, password in parse_account_lines(text):
        if acc_email == email:
            return password
    return None


class V2FunClient:
    """Centralized V2Fun API client."""

    def __init__(self, token: str):
        self.base_url = "https://api.prod.v2fun.ai"
        self.token = token
        self.headers = {
            "Authorization": token,
            "X-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://v2fun.ai/",
        }

    def _handle_response(self, response: requests.Response, action: str = "API call") -> Dict:
        """Centralized response handler with structured error codes."""
        if response.status_code == 401:
            return {"success": False, "message": "Token expired or invalid. Please reconnect V2Fun account.", "error_code": "AUTH_EXPIRED"}
        if response.status_code == 403:
            return {"success": False, "message": "Access forbidden. Account may be banned or restricted.", "error_code": "FORBIDDEN"}
        if response.status_code == 429:
            return {"success": False, "message": "Rate limited. Too many requests. Please wait.", "error_code": "RATE_LIMITED"}
        if response.status_code >= 500:
            return {"success": False, "message": f"V2Fun server error ({response.status_code}). Try again later.", "error_code": "SERVER_ERROR"}
        if response.status_code != 200:
            try:
                error_data = response.json()
                msg = error_data.get("message") or error_data.get("error") or str(error_data)
                return {"success": False, "message": f"{action} failed: {msg}", "error_code": "API_ERROR"}
            except ValueError:
                return {"success": False, "message": f"{action} failed with status {response.status_code}", "error_code": "HTTP_ERROR"}

        try:
            return response.json()
        except ValueError:
            return {"success": False, "message": "Invalid JSON response from server", "error_code": "PARSE_ERROR"}

    def generate_image(self, prompt: str, **kwargs) -> Dict:
        """Submit an image generation request to V2Fun API."""
        if not prompt or not prompt.strip():
            return {"success": False, "message": "Prompt cannot be empty", "error_code": "VALIDATION"}
        if len(prompt) > 5000:
            return {"success": False, "message": "Prompt too long (max 5000 characters)", "error_code": "VALIDATION"}

        payload = {
            "prompt": prompt.strip(),
            "model": kwargs.get("model", "gpt-image-2.5-sunburst"),
            "ratio": kwargs.get("ratio", "16:9"),
            "num": kwargs.get("num", 1),
            "quality": kwargs.get("quality", "medium"),
        }

        ref_imgs = kwargs.get("reference_images")
        if ref_imgs:
            if not isinstance(ref_imgs, list):
                return {"success": False, "message": "reference_images must be an array", "error_code": "VALIDATION"}
            if len(ref_imgs) > 3:
                return {"success": False, "message": "Maximum 3 reference images allowed", "error_code": "VALIDATION"}
            payload["referenceImages"] = ref_imgs

        try:
            response = requests.post(
                f"{self.base_url}/work/external/generate/image-generate?lan=en",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            return self._handle_response(response, "Image generation")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Generation request timed out. V2Fun may be slow, try again.", "error_code": "TIMEOUT"}
        except requests.exceptions.ConnectionError:
            return {"success": False, "message": "Cannot connect to V2Fun server. Check internet connection.", "error_code": "CONNECTION"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def get_balance(self) -> Dict:
        """Get user credit balance."""
        try:
            response = requests.get(
                f"{self.base_url}/sys/user/get-balance?lan=en",
                headers=self.headers,
                timeout=10,
            )
            return self._handle_response(response, "Get balance")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Balance check timed out", "error_code": "TIMEOUT"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def get_user_info(self) -> Dict:
        """Get user login info."""
        try:
            response = requests.post(
                f"{self.base_url}/sys/user/getLoginInfo?lan=en",
                headers=self.headers,
                timeout=10,
            )
            return self._handle_response(response, "Get user info")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "User info request timed out", "error_code": "TIMEOUT"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def get_free_count(self, config_ids: Optional[List[str]] = None) -> Dict:
        """Get free generation count per model from V2Fun."""
        try:
            response = requests.post(
                f"{self.base_url}/work/get-free-cnt?lan=en",
                headers=self.headers,
                json={"configIds": V2FUN_CONFIG_IDS if config_ids is None else config_ids},
                timeout=10,
            )
            return self._handle_response(response, "Get free count")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Free count request timed out", "error_code": "TIMEOUT"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def get_business_config(self) -> Dict:
        """Get business config to map configIds to model names."""
        try:
            response = requests.get(
                f"{self.base_url}/work/config/business-config/list?lan=en",
                headers=self.headers,
                timeout=10,
            )
            return self._handle_response(response, "Get business config")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Config request timed out", "error_code": "TIMEOUT"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def get_upload_credentials(self) -> Dict:
        """Get Alibaba Cloud OSS STS credentials from V2Fun."""
        try:
            response = requests.post(
                f"{self.base_url}/sys/oss/nologin/getAliSTS?lan=en",
                headers=self.headers,
                json={},
                timeout=10,
            )
            return self._handle_response(response, "Get upload credentials")
        except requests.exceptions.Timeout:
            return {"success": False, "message": "Upload credentials request timed out", "error_code": "TIMEOUT"}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Network error: {str(e)}", "error_code": "NETWORK"}

    def upload_to_oss(self, file_path: str, mime_type: str = "image/jpeg", max_retries: int = 2) -> Dict:
        """Upload file to Alibaba Cloud OSS with retry mechanism."""
        if not os.path.exists(file_path):
            return {"success": False, "message": "File not found", "error_code": "FILE_NOT_FOUND"}

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return {"success": False, "message": "File is empty", "error_code": "FILE_EMPTY"}
        if file_size > 16 * 1024 * 1024:
            return {"success": False, "message": "File too large (max 16MB)", "error_code": "FILE_TOO_LARGE"}

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                sts_resp = self.get_upload_credentials()
                if not sts_resp.get("success"):
                    last_error = sts_resp.get("message", "Failed to get credentials")
                    if attempt < max_retries:
                        time.sleep(2)
                        continue
                    return {"success": False, "message": f"Upload credentials failed: {last_error}", "error_code": "STS_FAILED"}

                sts = sts_resp.get("result", {})
                oss_host = sts.get("ossHost")
                oss_path = sts.get("path")
                if not oss_host or not oss_path:
                    return {"success": False, "message": "Invalid OSS credentials received", "error_code": "STS_INVALID"}

                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f, mime_type)}
                    form_data = {
                        "OSSAccessKeyId": sts.get("accessKeyId"),
                        "policy": sts.get("policy"),
                        "Signature": sts.get("signature"),
                        "key": oss_path,
                        "x-oss-security-token": sts.get("securityToken"),
                        "success_action_status": "200",
                    }
                    upload_resp = requests.post(oss_host, data=form_data, files=files, timeout=30)

                if upload_resp.status_code == 200:
                    return {"success": True, "oss_path": oss_path}

                error_xml = upload_resp.text
                if "InvalidAccessKeyId" in error_xml:
                    last_error = "OSS credentials expired"
                elif "AccessDenied" in error_xml:
                    last_error = "OSS access denied"
                else:
                    last_error = f"OSS error: {upload_resp.status_code}"

                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"success": False, "message": last_error, "error_code": "OSS_UPLOAD_FAILED"}
            except requests.exceptions.Timeout:
                last_error = "OSS upload timed out"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"success": False, "message": last_error, "error_code": "TIMEOUT"}
            except requests.exceptions.ConnectionError:
                last_error = "Cannot connect to OSS server"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"success": False, "message": last_error, "error_code": "CONNECTION"}
            except IOError as e:
                return {"success": False, "message": f"File read error: {str(e)}", "error_code": "FILE_READ_ERROR"}
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"success": False, "message": f"Upload error: {last_error}", "error_code": "UNKNOWN"}

        return {"success": False, "message": f"Upload failed after {max_retries + 1} attempts: {last_error}", "error_code": "RETRY_EXHAUSTED"}

    def get_resource_list(self, page_size: int = 20, page_no: int = 1) -> Dict:
        """Fetch recent resource works from V2Fun."""
        payload = {
            "pager": {
                "orderBy": "updateTime",
                "pageSize": page_size,
                "pageNo": page_no,
                "needQueryCount": True,
            },
            "parm": {
                "keyword": "",
                "workType": "",
                "listType": "folder",
                "isUpload": 0,
                "bindState": None,
                "license": "",
                "orderBy": "updateTime",
                "simple": 1,
            },
        }
        response = requests.post(
            f"{self.base_url}/work/getResourceList?lan=en",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        return self._handle_response(response, "Get resource list")

    @staticmethod
    def find_work_by_task_id(records: list, task_uuid: str) -> Optional[Dict]:
        """Locate a work item matching task_uuid inside getResourceList records."""
        target = str(task_uuid)
        for record in records or []:
            for child_item in record.get("child", []):
                for work in child_item.get("works", []):
                    if str(work.get("taskId", "")) == target:
                        return work
        return None

    def poll_result(self, task_uuid: str, max_attempts: int = 60, interval: int = 5) -> Dict:
        """Poll V2Fun API until image generation completes, fails, or times out."""
        for attempt in range(max_attempts):
            try:
                data = self.get_resource_list(page_size=20, page_no=1)
                if data.get("success"):
                    records = data.get("result", {}).get("records") or []
                    matched_work = self.find_work_by_task_id(records, task_uuid)
                    if matched_work:
                        gen_status = matched_work.get("generateStatus", "")
                        work_url = matched_work.get("workUrl") or ""
                        thumb = matched_work.get("thumb") or ""
                        progress = matched_work.get("progress", 0)

                        if gen_status == "A" and (work_url or thumb):
                            full_url = work_url if work_url else thumb
                            if full_url and not full_url.startswith("http"):
                                full_url = f"https://asset.v2fun.ai/{full_url}"
                            return {
                                "success": True,
                                "status": "completed",
                                "workUrl": full_url,
                                "thumb": thumb if thumb else full_url,
                                "progress": 100,
                                "attempts": attempt + 1,
                            }
                        if gen_status == "I":
                            pct = int(float(progress) * 100) if progress else 50
                            print(f"[POLL] Attempt {attempt + 1}/{max_attempts}: {pct}% (status: {gen_status})")
                        elif gen_status in ("E", "F"):
                            return {
                                "success": False,
                                "status": "failed",
                                "error": "Generation failed on V2Fun side",
                            }
            except Exception as e:
                print(f"[POLL] Error on attempt {attempt + 1}: {e}")

            if attempt < max_attempts - 1:
                time.sleep(interval)

        return {
            "success": False,
            "status": "timeout",
            "error": f"Polling timeout after {max_attempts * interval}s",
        }
