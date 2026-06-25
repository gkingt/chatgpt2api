from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.account_service import account_service
from services.proxy_service import proxy_settings
from services.register import mail_provider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None
cancel_event = threading.Event()
active_sessions_lock = threading.Lock()
active_sessions: set[Any] = set()


class RegistrationCancelled(RuntimeError):
    pass

common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def reset_cancel() -> None:
    cancel_event.clear()


def request_cancel() -> None:
    cancel_event.set()
    with active_sessions_lock:
        sessions = list(active_sessions)
    for session in sessions:
        try:
            session.close()
        except Exception:
            pass


def ensure_not_cancelled() -> None:
    if cancel_event.is_set():
        raise RegistrationCancelled("注册任务已停止")


def _track_session(session: Any) -> Any:
    with active_sessions_lock:
        active_sessions.add(session)
    return session


def _untrack_session(session: Any) -> None:
    with active_sessions_lock:
        active_sessions.discard(session)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_error_detail(resp, limit: int = 1200) -> str:
    if resp is None:
        return ""
    status = getattr(resp, "status_code", "unknown")
    content_type = str(resp.headers.get("content-type") or "").split(";", 1)[0]
    data = _response_json(resp)
    if data:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        kind = "json"
    else:
        body = str(getattr(resp, "text", "") or "").replace("\r", " ").replace("\n", " ").strip()
        kind = "text"
    if len(body) > limit:
        body = f"{body[:limit]}...<truncated>"
    parts = [f"status={status}"]
    if content_type:
        parts.append(f"content_type={content_type}")
    if body:
        parts.append(f"{kind}={body}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (403, 429, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    headers = getattr(resp, "headers", {}) or {}
    server = str(headers.get("server") or "").lower()
    content_type = str(headers.get("content-type") or "").lower()
    if "cloudflare" in server and "text/html" in content_type:
        return any(marker in text for marker in ("just a moment", "cf-chl", "challenge-platform", "cf_clearance"))
    return any(marker in text for marker in ("/cdn-cgi/challenge-platform/", "cf-chl", "cf_clearance"))


def _is_oauth_session_conflict(error: object) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "http_409",
            "status=409",
            "invalid_state",
            "state mismatch",
            "code already used",
            "authorization code",
            "oauth_callback_failed",
        )
    )


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox({**config["mail"], "proxy": config.get("proxy") or ""}, username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code({**config["mail"], "proxy": config.get("proxy") or ""}, mailbox)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    generator = SentinelTokenGenerator(device_id, user_agent)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": user_agent,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )
    data = _response_json(resp)
    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


def _is_socks_proxy(proxy: str) -> bool:
    candidate = str(proxy or "").strip().lower()
    return candidate.startswith("socks5://") or candidate.startswith("socks5h://")


def create_session(proxy: str = "") -> Any:
    ensure_not_cancelled()
    kwargs = proxy_settings.build_session_kwargs(proxy=proxy, impersonate="chrome", verify=False, upstream=True)
    if _is_socks_proxy(proxy):
        return _track_session(curl_requests.Session(**kwargs))
    try:
        session = requests.Session(**kwargs)
    except TypeError:
        session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    if hasattr(session, "mount"):
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    if hasattr(session, "verify"):
        session.verify = bool(kwargs.get("verify", False))
    if kwargs.get("proxy"):
        if hasattr(session, "proxies"):
            session.proxies.update({"http": str(kwargs["proxy"]), "https": str(kwargs["proxy"])})
    return _track_session(session)


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    # 修复了 kwargs 和 timeout 参数可能导致的冲突问题
    timeout_val = kwargs.pop("timeout", default_timeout)
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        ensure_not_cancelled()
        try:
            return session.request(method.upper(), url, timeout=timeout_val, **kwargs), ""
        except Exception as error:
            ensure_not_cancelled()
            last_error = str(error)
            if cancel_event.wait(1):
                raise RegistrationCancelled("注册任务已停止")
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> tuple[dict[str, str] | None, str]:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    last_detail = ""
    for _ in range(10):
        response = session.get(current_url, headers=navigate_headers, verify=False, timeout=30, allow_redirects=False)
        location = str(response.headers.get("Location") or "").strip()
        last_detail = f"consent_get url={current_url}, {_response_error_detail(response, 500)}{', location=' + location if location else ''}"
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(location)
        if callback_params:
            return callback_params, ""
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None, f"oauth_callback_not_found, no_auth_session_cookie, {last_detail}"
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception as exc:
        return None, f"auth_session_decode_failed: {exc}, {last_detail}"
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp = session.post(f"{auth_base}/api/accounts/workspace/select", json={"workspace_id": workspace_id}, headers=headers, verify=False, timeout=30, allow_redirects=False)
    ws_location = str(ws_resp.headers.get("Location") or "").strip()
    callback_params = extract_oauth_callback_params_from_url(ws_location)
    if callback_params:
        return callback_params, ""
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None, f"workspace_select_no_orgs, {_response_error_detail(ws_resp, 800)}{', location=' + ws_location if ws_location else ''}"
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None, f"workspace_select_missing_org_id, {_response_error_detail(ws_resp, 800)}"
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = session.post(f"{auth_base}/api/accounts/organization/select", json=body, headers=org_headers, verify=False, timeout=30, allow_redirects=False)
    org_location = str(org_resp.headers.get("Location") or "").strip()
    callback_params = extract_oauth_callback_params_from_url(org_location)
    if callback_params:
        return callback_params, ""
    return None, f"organization_select_no_callback, {_response_error_detail(org_resp, 800)}{', location=' + org_location if org_location else ''}"


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str) -> dict:
    callback_params, callback_error = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    
    # [补丁1] 引入 PR 中的回退方案 (Fallback Mechanism)
    if not callback_params:
        log(f"[exchange_platform_tokens] 主方案失败 ({callback_error})，尝试回退方案", "yellow")
        try:
            r = session.get(consent_url, headers=navigate_headers, allow_redirects=True, verify=False, timeout=30)
            final_url = str(r.url)
            callback_params = extract_oauth_callback_params_from_url(final_url)
            if not callback_params:
                for hist in getattr(r, "history", []) or []:
                    loc = str(hist.headers.get("Location") or "")
                    callback_params = extract_oauth_callback_params_from_url(loc)
                    if callback_params:
                        break
        except Exception as e:
            log(f"[exchange_platform_tokens] 回退方案异常: {e}", "yellow")

    if not callback_params:
        raise RuntimeError(f"oauth_callback_failed (all methods failed): {callback_error}")
        
    code = str(callback_params.get("code") or "").strip()
    if not code:
        raise RuntimeError("oauth_callback_missing_code")
    token_session = create_session(config["proxy"])
    try:
        ensure_not_cancelled()
        resp = token_session.post(
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
            },
            verify=False,
            timeout=60,
        )
    finally:
        try:
            token_session.close()
        finally:
            _untrack_session(token_session)
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
        raise RuntimeError(f"oauth_token_http_{resp.status_code}, {_response_error_detail(resp, 1200)}")
    payload = _decode_jwt_payload(str(data.get("id_token") or "")) or _decode_jwt_payload(str(data.get("access_token") or ""))
    return {
        "email": str(payload.get("email") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = proxy
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())

    def close(self) -> None:
        try:
            self.session.close()
        finally:
            _untrack_session(self.session)

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return proxy_settings.build_headers(headers, target_url=auth_base, proxy=self.proxy, upstream=True)

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _platform_authorize(self, email: str, index: int) -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = proxy_settings.refresh_clearance(target_url=auth_base, proxy=self.proxy, force=True, upstream=True)
            if bundle is not None:
                headers = self._navigate_headers(f"{platform_base}/")
                if bundle.user_agent:
                    headers["user-agent"] = bundle.user_agent
                resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(f"Cloudflare challenge: {_response_error_detail(resp, 1200)}")
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            raise RuntimeError(error or f"platform_authorize_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "platform authorize 完成")
        return code_verifier

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> str:
        step(index, "开始创建账号资料")
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or resp.headers.get("Location") or "").strip()
        step(index, "创建账号资料完成")
        return continue_url
    def _finish_registration_and_exchange_tokens(self, code_verifier: str, continue_url: str, index: int) -> dict:
        step(index, "开始注册会话换 token")
        tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url or f"{auth_base}/sign-in-with-chatgpt/codex/consent")
        step(index, "token 换取完成")
        return tokens


    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int) -> dict:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if attempt:
                    step(index, "登录换 token 遇到会话冲突，重新登录重试")
                return self._login_and_exchange_tokens_once(email, password, mailbox, index)
            except Exception as exc:
                last_error = exc
                if attempt == 0 and _is_oauth_session_conflict(exc):
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("token换取失败")

    def _login_and_exchange_tokens_once(self, email: str, password: str, mailbox: dict, index: int) -> dict:
        step(index, "开始独立登录换 token")
        login_session = create_session(self.proxy)
        login_device_id = str(uuid.uuid4())
        try:
            ensure_not_cancelled()
            login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
            login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
            code_verifier, code_challenge = _generate_pkce()
            params = {
                "issuer": auth_base,
                "client_id": platform_oauth_client_id,
                "audience": platform_oauth_audience,
                "redirect_uri": platform_oauth_redirect_uri,
                "device_id": login_device_id,
                "screen_hint": "login_or_signup",
                "max_age": "0",
                "login_hint": email,
                "scope": "openid profile email offline_access",
                "response_type": "code",
                "response_mode": "query",
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "auth0Client": platform_auth0_client,
            }
            resp, error = request_with_local_retry(login_session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
            if resp is None:
                raise RuntimeError(error or "platform_login_authorize_failed")
            step(index, "登录 authorize 完成")
            
            # [补丁2] 引入 PR 中的分步式流程: 提交流箱
            step(index, "开始提交邮箱")
            def _do_authorize_continue():
                h = dict(common_headers)
                h["referer"] = f"{auth_base}/log-in?usernameKind=email"
                h["oai-device-id"] = login_device_id
                h.update(_make_trace_headers())
                h["openai-sentinel-token"] = build_sentinel_token(login_session, login_device_id, "authorize_continue")
                return request_with_local_retry(
                    login_session, "post",
                    f"{auth_base}/api/accounts/authorize/continue",
                    json={"username": {"kind": "email", "value": email}},
                    headers=h, allow_redirects=False, verify=False
                )

            resp, error = _do_authorize_continue()
            
            # 处理可能的 invalid_state (409) 冲突
            if resp is not None and resp.status_code == 409:
                step(index, "邮箱提交 invalid_state，重新 authorize 后重试")
                login_session.cookies.clear(domain=".auth.openai.com")
                login_session.cookies.clear(domain="auth.openai.com")
                login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
                login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
                resp, error = request_with_local_retry(login_session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
                if resp is None:
                    raise RuntimeError(error or "platform_login_authorize_retry_failed")
                resp, error = _do_authorize_continue()
                
            if resp is None or resp.status_code != 200:
                data = _response_json(resp) if resp is not None else {}
                detail = json.dumps(data, ensure_ascii=False) if data else ""
                raise RuntimeError(error or f"email_submit_http_{getattr(resp, 'status_code', 'unknown')}{f': {detail}' if detail else ''}")
            step(index, "邮箱提交完成")

            # 走正常的校验密码逻辑
            step(index, "开始密码校验")
            headers = dict(common_headers)
            headers["referer"] = f"{auth_base}/log-in/password"
            headers["oai-device-id"] = login_device_id
            headers.update(_make_trace_headers())
            headers["openai-sentinel-token"] = build_sentinel_token(login_session, login_device_id, "password_verify")
            resp, error = request_with_local_retry(login_session, "post", f"{auth_base}/api/accounts/password/verify", json={"password": password}, headers=headers, allow_redirects=False, verify=False)
            if resp is None or resp.status_code != 200:
                detail = _response_error_detail(resp)
                if detail:
                    step(index, f"密码校验失败详情: {detail}", "yellow")
                raise RuntimeError(error or f"password_verify_http_{getattr(resp, 'status_code', 'unknown')}{', ' + detail if detail else ''}")
            step(index, "密码校验完成")
            payload = _response_json(resp)
            continue_url = str(payload.get("continue_url") or "").strip()
            page_type = str(((payload.get("page") or {}).get("type")) or "")
            if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
                step(index, "独立登录需要邮箱验证码")
                code = wait_for_code(mailbox)
                if not code:
                    raise RuntimeError("独立登录等待验证码超时")
                resp, reason = validate_otp(login_session, login_device_id, code)
                if resp is None or resp.status_code != 200:
                    detail = _response_error_detail(resp)
                    if detail:
                        step(index, f"独立登录验证码校验失败详情: {detail}", "yellow")
                    data = _response_json(resp) if resp is not None else {}
                    message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                    raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
                otp_payload = _response_json(resp)
                continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
                step(index, "独立登录验证码校验完成")
            if not continue_url:
                continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
            tokens = exchange_platform_tokens(login_session, login_device_id, code_verifier, continue_url)
            if not tokens:
                raise RuntimeError("token换取失败")
            step(index, "token 换取完成")
            return tokens
        finally:
            try:
                login_session.close()
            finally:
                _untrack_session(login_session)

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        step(index, f"邮箱创建完成: {email}")
        password = _random_password()
        first_name, last_name = _random_name()
        self._platform_authorize(email, index)
        self._register_user(email, password, index)
        self._send_otp(index)
        step(index, "开始等待注册验证码")
        code = wait_for_code(mailbox)
        if not code:
            raise RuntimeError("等待注册验证码超时")
        step(index, f"收到注册验证码: {code}")
        self._validate_otp(code, index)
        self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
        tokens = self._login_and_exchange_tokens(email, password, mailbox, index)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict:
    start = time.time()
    ensure_not_cancelled()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_account_items([{**result, "source_type": "web", "proxy": config["proxy"]}])
        try:
            account_service.fetch_remote_info(access_token, "register", skip_token_refresh=True)
        except Exception as refresh_exc:
            step(index, f"账号已保存，刷新额度暂未成功，稍后可重试: {refresh_exc}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
