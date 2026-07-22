from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import string
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.account_service import account_service
from services.proxy_service import proxy_settings
from services.register import mail_provider
from utils.sentinel import (
    SENTINEL_OBSERVER_WAIT_MS,
    build_sentinel_token as _build_sentinel_token_tuple,
    build_sentinel_tokens as _build_sentinel_tokens,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 120,
        "wait_interval": 3,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
domain_stats_file = base_dir.parents[1] / "data" / "domain_stats.json"
register_auth_sessions_file = base_dir.parents[1] / "data" / "register_auth_sessions.json"
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
chatgpt_auth_session_url = "https://chatgpt.com/api/auth/session"
chatgpt_csrf_url = "https://chatgpt.com/api/auth/csrf"
chatgpt_signin_openai_url = "https://chatgpt.com/api/auth/signin/openai"

# 固定的最后回退指纹（仅当未通过 BrowserProfile 注入时使用）
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'

# 常见真实 Chrome/Edge 大版本池（当前主流覆盖范围）
_CHROME_MAJOR_VERSIONS = [130, 131, 132, 133, 134, 135]
# 合理的 Windows 平台 + 版本号
_WINDOWS_PROFILES = [
    ("Windows", "10.0.0", "Windows NT 10.0"),
    ("Windows", "11.0.0", "Windows NT 10.0"),  # Win11 仍报告 Windows NT 10.0
    ("Windows", "15.0.0", "Windows NT 10.0"),
]
# 常见桌面屏幕分辨率（横向 x 纵向）
_SCREEN_RESOLUTIONS = [
    (1920, 1080),
    (2560, 1440),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (1680, 1050),
    (1600, 900),
    (1280, 720),
]
_NOT_A_BRAND_POOL = [
    ('"Not?A_Brand"', '"8"'),
    ('"Not/A)Brand"', '"99"'),
    ('"Not.A/Brand"', '"8"'),
    ('"Not A(Brand"', '"8"'),
    ('"Not?A(Brand"', '"24"'),
    ('"Not)A_Brand"', '"8"'),
    ('"Not:B-Brand"', '"99"'),
]


@dataclass(frozen=True)
class BrowserProfile:
    """按账号随机生成的浏览器表面，保持在单次注册流程中一致。"""
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_full_version_list: str
    sec_ch_ua_platform: str
    sec_ch_ua_platform_version: str
    sec_ch_ua_arch: str
    sec_ch_ua_bitness: str
    screen_width: int
    screen_height: int
    hardware_concurrency: int
    accept_language: str


def _random_browser_profile() -> BrowserProfile:
    """按真实使用分布随机选取一组一致的浏览器表面特征。"""
    chrome_major = random.choice(_CHROME_MAJOR_VERSIONS)
    chrome_full = f"{chrome_major}.0.0.0"
    platform_sparse, platform_version, platform_nt = random.choice(_WINDOWS_PROFILES)
    not_brand_name, not_brand_ver = random.choice(_NOT_A_BRAND_POOL)
    # Client Hints 三元：Chromium / Google Chrome / Not*A Brand，顺序会随机打乱
    parts = [
        f'"Google Chrome";v="{chrome_major}"',
        f'"Chromium";v="{chrome_major}"',
        f'{not_brand_name};v={not_brand_ver}',
    ]
    random.shuffle(parts)
    sec_ch_ua_value = ", ".join(parts)
    full_parts = [
        (f'"Chromium";v="{chrome_full}"'),
        (f'"Google Chrome";v="{chrome_full}"'),
        (f'{not_brand_name};v="{random.choice(["99", chrome_full, "0"])}"') if not_brand_name else f'"Not:A-Brand";v="99.0.0.0"',
    ]
    random.shuffle(full_parts)
    sec_ch_ua_full_value = ", ".join(full_parts)
    width, height = random.choice(_SCREEN_RESOLUTIONS)
    ua = (
        f"Mozilla/5.0 ({platform_nt}; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )
    return BrowserProfile(
        user_agent=ua,
        sec_ch_ua=sec_ch_ua_value.replace(not_brand_name, not_brand_name),
        sec_ch_ua_full_version_list=sec_ch_ua_full_value,
        sec_ch_ua_platform=f'"{platform_sparse}"',
        sec_ch_ua_platform_version=f'"{platform_version}"',
        sec_ch_ua_arch='"x86_64"',
        sec_ch_ua_bitness='"64"',
        screen_width=width,
        screen_height=height,
        hardware_concurrency=random.choice([4, 6, 8, 12, 16]),
        accept_language=random.choice(["en-US,en;q=0.9", "en-US,en;q=0.8", "en-GB,en-US;q=0.9,en;q=0.8"]),
    )


_default_timeout = 30
default_timeout = _default_timeout
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None
cancel_event = threading.Event()
active_sessions_lock = threading.Lock()
active_sessions: set[Any] = set()
sentinel_sdk_url = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"
sentinel_so_observer_timeout_ms = 5000
sentinel_sdk_lock = threading.Lock()
sentinel_sdk_metadata: dict[str, str] | None = None
domain_stats_lock = threading.Lock()
min_domain_attempts_before_disable = 5
min_domain_success_rate = 0.2


@dataclass(frozen=True)
class SentinelTokens:
    token: str
    so_token: str = ""
    sdk_version: str = "unknown"
    req_host: str = ""
    req_keys: str = ""
    so_shape: str = "missing"
    oai_sc: str = ""


class RegistrationCancelled(RuntimeError):
    pass


def _default_profile() -> BrowserProfile:
    """兼容历史接口：返回一个稳定的默认 BrowserProfile。"""
    return BrowserProfile(
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"10.0.0"',
        sec_ch_ua_arch='"x86_64"',
        sec_ch_ua_bitness='"64"',
        screen_width=1920,
        screen_height=1080,
        hardware_concurrency=16,
        accept_language="en-US,en;q=0.9",
    )


def _build_common_headers(profile: BrowserProfile) -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": profile.accept_language,
        "content-type": "application/json",
        "origin": auth_base,
        "priority": "u=1, i",
        "user-agent": profile.user_agent,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": profile.sec_ch_ua_arch,
        "sec-ch-ua-bitness": profile.sec_ch_ua_bitness,
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": profile.sec_ch_ua_platform_version,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _build_navigate_headers(profile: BrowserProfile) -> dict[str, str]:
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": profile.accept_language,
        "user-agent": profile.user_agent,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": profile.sec_ch_ua_arch,
        "sec-ch-ua-bitness": profile.sec_ch_ua_bitness,
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": profile.sec_ch_ua_platform_version,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }


# 缓存的默认 Headers，用于无关历史的独立辅助函数（例如 validate_otp）。
_default_profile_cache = _default_profile()
common_headers = _build_common_headers(_default_profile_cache)
navigate_headers = _build_navigate_headers(_default_profile_cache)


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


def _openai_error_code(data: dict) -> str:
    error = data.get("error") if isinstance(data, dict) else {}
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or "").strip()


def _openai_error_message(data: dict) -> str:
    error = data.get("error") if isinstance(data, dict) else {}
    if not isinstance(error, dict):
        return str(data.get("message") or "").strip() if isinstance(data, dict) else ""
    return str(error.get("message") or data.get("message") or "").strip()


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


register_auth_sessions_lock = threading.Lock()


def _session_cookie_header(session: requests.Session, domain_hint: str = "chatgpt.com") -> str:
    pieces: list[str] = []
    try:
        for cookie in session.cookies:
            domain = str(getattr(cookie, "domain", "") or "")
            if domain_hint and domain_hint not in domain:
                continue
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            if name and value:
                pieces.append(f"{name}={value}")
    except Exception:
        return ""
    return "; ".join(pieces)


def _cookie_value(session: requests.Session, name: str) -> str:
    try:
        value = session.cookies.get(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    try:
        for cookie in session.cookies:
            cookie_name = str(getattr(cookie, "name", "") or "")
            if cookie_name == name:
                value = str(getattr(cookie, "value", "") or "")
                if value:
                    return value
    except Exception:
        pass
    return ""


def _load_register_auth_sessions() -> list[dict]:
    try:
        data = json.loads(register_auth_sessions_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _append_register_auth_session(entry: dict) -> None:
    register_auth_sessions_file.parent.mkdir(parents=True, exist_ok=True)
    with register_auth_sessions_lock:
        items = _load_register_auth_sessions()
        items.append(entry)
        register_auth_sessions_file.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_and_save_chatgpt_auth_session(session: requests.Session, device_id: str, callback_params: dict[str, str]) -> dict:
    headers = dict(navigate_headers)
    headers["accept"] = "application/json"
    headers["referer"] = "https://chatgpt.com/"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    response = session.get(chatgpt_auth_session_url, headers=headers, verify=False, timeout=30)
    data = _response_json(response)
    session_token = _cookie_value(session, "__Secure-next-auth.session-token") or str(data.get("sessionToken") or data.get("session_token") or "").strip()
    entry = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "status_code": getattr(response, "status_code", None),
        "device_id": device_id,
        "oauth_state": str(callback_params.get("state") or "").strip(),
        "oauth_scope": str(callback_params.get("scope") or "").strip(),
        "session_token": session_token,
        "cookie_header": _session_cookie_header(session),
        "response": data,
    }
    _append_register_auth_session(entry)
    return entry


def _chatgpt_headers(profile: BrowserProfile, referer: str = "https://chatgpt.com/") -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": profile.accept_language,
        "origin": "https://chatgpt.com",
        "referer": referer,
        "user-agent": profile.user_agent,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": profile.sec_ch_ua_arch,
        "sec-ch-ua-bitness": profile.sec_ch_ua_bitness,
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": profile.sec_ch_ua_platform_version,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        **_make_trace_headers(),
    }


def _normalize_location(location: str, current_url: str) -> str:
    if not location:
        return ""
    return urljoin(current_url, location)


def _consume_chatgpt_callback(session: requests.Session, callback_url: str, profile: BrowserProfile) -> None:
    current_url = callback_url
    for _ in range(8):
        headers = dict(_build_navigate_headers(profile))
        headers["referer"] = "https://auth.openai.com/"
        response = session.get(current_url, headers=headers, verify=False, timeout=30, allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return
        location = _normalize_location(str(response.headers.get("Location") or ""), current_url)
        if not location:
            return
        current_url = location
        parsed = urlparse(current_url)
        if "chatgpt.com" in parsed.netloc and "/api/auth/callback" not in parsed.path:
            session.get(current_url, headers=_build_navigate_headers(profile), verify=False, timeout=30, allow_redirects=True)
            return


def _choose_account_select(session: requests.Session, html_text: str, current_url: str, profile: BrowserProfile, device_id: str) -> str:
    match = re.search(r"us_[A-Za-z0-9]{16,}", html_text or "")
    if not match:
        return ""
    session_id = match.group(0)
    headers = dict(_build_common_headers(profile))
    headers["referer"] = "https://auth.openai.com/choose-an-account"
    headers["origin"] = auth_base
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    candidates = [
        (f"{auth_base}/api/accounts/session/select", {"session_id": session_id}, "json"),
        (f"{auth_base}/choose-an-account", {"intent": "select", "session_id": session_id}, "form"),
    ]
    for url, body, kind in candidates:
        try:
            request_headers = dict(headers)
            if kind == "json":
                request_headers["content-type"] = "application/json"
                response = session.post(url, headers=request_headers, json=body, verify=False, timeout=30, allow_redirects=False)
            else:
                request_headers["content-type"] = "application/x-www-form-urlencoded"
                response = session.post(url, headers=request_headers, data=urlencode(body), verify=False, timeout=30, allow_redirects=False)
            if response.status_code not in (200, 201, 302, 303):
                continue
            location = str(response.headers.get("Location") or response.headers.get("location") or "").strip()
            data = _response_json(response)
            next_url = str(data.get("continue_url") or location or "").strip()
            if next_url:
                return _normalize_location(next_url, current_url)
            if response.status_code == 200:
                return current_url
        except Exception:
            continue
    return ""


def create_chatgpt_web_session(auth_session: requests.Session, auth_device_id: str, profile: BrowserProfile) -> dict:
    chatgpt_session = create_session(config["proxy"])
    chatgpt_session.cookies.set("oai-did", auth_device_id, domain=".chatgpt.com")
    try:
        csrf_resp = chatgpt_session.get(chatgpt_csrf_url, headers=_chatgpt_headers(profile, "https://chatgpt.com/auth/login"), verify=False, timeout=30)
        csrf_data = _response_json(csrf_resp)
        csrf_token = str(csrf_data.get("csrfToken") or "").strip()
        if not csrf_token:
            raise RuntimeError(f"chatgpt_csrf_missing, {_response_error_detail(csrf_resp, 800)}")
        signin_headers = _chatgpt_headers(profile, "https://chatgpt.com/auth/login")
        signin_headers["content-type"] = "application/x-www-form-urlencoded"
        signin_resp = chatgpt_session.post(
            chatgpt_signin_openai_url,
            headers=signin_headers,
            data={"csrfToken": csrf_token, "callbackUrl": "https://chatgpt.com/", "json": "true"},
            verify=False,
            timeout=30,
        )
        signin_data = _response_json(signin_resp)
        auth_url = str(signin_data.get("url") or "").strip()
        if not auth_url:
            raise RuntimeError(f"chatgpt_signin_url_missing, {_response_error_detail(signin_resp, 800)}")

        callback_url = ""
        current_url = auth_url
        for _ in range(12):
            auth_headers = dict(_build_navigate_headers(profile))
            auth_headers["referer"] = "https://chatgpt.com/auth/login"
            response = auth_session.get(current_url, headers=auth_headers, verify=False, timeout=30, allow_redirects=False)
            location = _normalize_location(str(response.headers.get("Location") or ""), current_url)
            candidate = str(getattr(response, "url", "") or current_url)
            if "/api/auth/callback/openai" in candidate and "code=" in candidate:
                callback_url = candidate
                break
            if "/api/auth/callback/openai" in location and "code=" in location:
                callback_url = location
                break
            if "/choose-an-account" in urlparse(current_url).path and response.status_code == 200:
                selected_url = _choose_account_select(auth_session, getattr(response, "text", "") or "", current_url, profile, auth_device_id)
                if selected_url:
                    current_url = selected_url
                    continue
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                break
            current_url = location
        if not callback_url:
            fallback_params, fallback_error = extract_oauth_callback_params_from_consent_session(auth_session, current_url, auth_device_id)
            if fallback_params:
                callback_url = "https://chatgpt.com/api/auth/callback/openai?" + urlencode(
                    {key: value for key, value in fallback_params.items() if value}
                )
            else:
                raise RuntimeError(f"chatgpt_callback_not_found, {fallback_error}")

        _consume_chatgpt_callback(chatgpt_session, callback_url, profile)
        return fetch_and_save_chatgpt_auth_session(chatgpt_session, auth_device_id, extract_oauth_callback_params_from_url(callback_url) or {})
    finally:
        try:
            chatgpt_session.close()
        finally:
            _untrack_session(chatgpt_session)


def _email_domain(email: str) -> str:
    _, sep, domain = str(email or "").strip().lower().rpartition("@")
    return domain if sep else ""


def _load_domain_stats() -> dict:
    try:
        data = json.loads(domain_stats_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_domain_stats(data: dict) -> None:
    domain_stats_file.parent.mkdir(parents=True, exist_ok=True)
    domain_stats_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_register_domain_result(mailbox: dict, success: bool) -> None:
    email = str(mailbox.get("address") or "").strip()
    domain = str(mailbox.get("base_domain") or _email_domain(email)).strip().lower()
    provider = str(mailbox.get("provider") or "unknown").strip() or "unknown"
    provider_ref = str(mailbox.get("provider_ref") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    if not domain and not provider:
        return
    with domain_stats_lock:
        data = _load_domain_stats()
        domains = data.setdefault("domains", {})
        providers = data.setdefault("providers", {})
        if domain:
            item = domains.setdefault(domain, {"success": 0, "fail": 0})
            item["success" if success else "fail"] = int(item.get("success" if success else "fail") or 0) + 1
            total = int(item.get("success") or 0) + int(item.get("fail") or 0)
            item["success_rate"] = round(int(item.get("success") or 0) * 100 / max(1, total), 1)
            item["last_updated"] = now
        provider_key = f"{provider}:{provider_ref}" if provider_ref else provider
        item = providers.setdefault(provider_key, {"success": 0, "fail": 0, "provider": provider, "provider_ref": provider_ref})
        item["success" if success else "fail"] = int(item.get("success" if success else "fail") or 0) + 1
        total = int(item.get("success") or 0) + int(item.get("fail") or 0)
        item["success_rate"] = round(int(item.get("success") or 0) * 100 / max(1, total), 1)
        item["last_updated"] = now
        disabled = []
        for domain_name, domain_item in domains.items():
            total = int(domain_item.get("success") or 0) + int(domain_item.get("fail") or 0)
            rate = int(domain_item.get("success") or 0) / max(1, total)
            if total >= min_domain_attempts_before_disable and rate < min_domain_success_rate:
                disabled.append(str(domain_name).lower())
        data["disabled_domains"] = sorted(set(disabled))
        data["updated_at"] = now
        _save_domain_stats(data)
        mail_provider.set_disabled_domains(data.get("disabled_domains") or [])


def _sync_disabled_domains_from_stats() -> None:
    data = _load_domain_stats()
    domains = data.get("disabled_domains") if isinstance(data, dict) else []
    if isinstance(domains, list):
        mail_provider.set_disabled_domains(domains)


_sync_disabled_domains_from_stats()


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str, screen_width: int = 1920, screen_height: int = 1080, hardware_concurrency: int = 16):
        self.device_id = device_id
        self.user_agent = ua
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.hardware_concurrency = hardware_concurrency
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
            f"{self.screen_width}x{self.screen_height}",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            sentinel_sdk_url,
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
            self.hardware_concurrency,
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


def _sentinel_sdk_version() -> str:
    marker = "/sentinel/"
    try:
        path = urlparse(sentinel_sdk_url).path
        if marker in path:
            return path.split(marker, 1)[1].split("/", 1)[0] or "unknown"
    except Exception:
        pass
    return "unknown"


def _load_sentinel_sdk_metadata(session: requests.Session) -> dict[str, str]:
    global sentinel_sdk_metadata
    with sentinel_sdk_lock:
        if sentinel_sdk_metadata is not None:
            return dict(sentinel_sdk_metadata)
    version = _sentinel_sdk_version()
    metadata = {"version": version, "observer_timeout_ms": str(sentinel_so_observer_timeout_ms)}
    try:
        resp = session.get(
            sentinel_sdk_url,
            headers={"User-Agent": user_agent, "Accept": "application/javascript,*/*;q=0.8"},
            timeout=20,
            verify=False,
        )
        body = str(getattr(resp, "text", "") or "")
        if getattr(resp, "status_code", 0) == 200 and body:
            if "5e3" in body or "5000" in body:
                metadata["observer_timeout_ms"] = "5000"
    except Exception:
        pass
    with sentinel_sdk_lock:
        sentinel_sdk_metadata = dict(metadata)
    return metadata


def _sentinel_req_endpoints(flow: str) -> list[tuple[str, str, str]]:
    auth_endpoint = (
        "https://auth.openai.com/backend-api/sentinel/req",
        "https://auth.openai.com/sentinel/20260124ceb8/frame.html",
        "https://auth.openai.com",
    )
    sentinel_endpoint = (
        "https://sentinel.openai.com/backend-api/sentinel/req",
        "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "https://sentinel.openai.com",
    )
    if flow == "oauth_create_account":
        return [auth_endpoint, sentinel_endpoint]
    return [sentinel_endpoint, auth_endpoint]


def _so_shape(value: object) -> str:
    if isinstance(value, dict):
        return "dict:" + ",".join(sorted(str(key) for key in value.keys()))
    if isinstance(value, str):
        return "str" if value else "empty_str"
    if value is None:
        return "missing"
    return type(value).__name__


def build_sentinel_tokens(
    session: requests.Session,
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    include_so: bool = False,
    screen_width: int = 1920,
    screen_height: int = 1080,
    hardware_concurrency: int = 16,
) -> SentinelTokens:
    bundle = _build_sentinel_tokens(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        include_so=include_so or (flow == "oauth_create_account"),
        observer_wait_ms=SENTINEL_OBSERVER_WAIT_MS,
        screen_width=screen_width,
        screen_height=screen_height,
        hardware_concurrency=hardware_concurrency,
    )
    return SentinelTokens(
        token=bundle.sentinel_token,
        so_token=bundle.so_token,
        sdk_version=bundle.sdk_version or "legacy",
        req_host=urlparse(bundle.sdk_url).netloc,
        req_keys=f"p_len={bundle.requirements_token_length}",
        so_shape="required" if bundle.sentinel_req_so_required else "unknown",
        oai_sc=bundle.oai_sc,
    )


def build_sentinel_token(
    session: requests.Session,
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    screen_width: int = 1920,
    screen_height: int = 1080,
    hardware_concurrency: int = 16,
) -> str:
    sentinel_value, _oai_sc = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        screen_width=screen_width,
        screen_height=screen_height,
        hardware_concurrency=hardware_concurrency,
    )
    return sentinel_value


def _apply_sentinel_headers(headers: dict[str, str], tokens: SentinelTokens, *, require_so_header: bool = False) -> None:
    headers["OpenAI-Sentinel-Token"] = tokens.token
    if tokens.so_token or require_so_header:
        headers["OpenAI-Sentinel-SO-Token"] = tokens.so_token


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


def validate_otp(
    session: requests.Session,
    device_id: str,
    code: str,
    *,
    base_headers: dict[str, str] | None = None,
    user_agent: str = "",
    sec_ch_ua: str = "",
    screen_width: int = 1920,
    screen_height: int = 1080,
    hardware_concurrency: int = 16,
):
    headers = dict(base_headers or common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    _apply_sentinel_headers(
        headers,
        build_sentinel_tokens(
            session,
            device_id,
            "authorize_continue",
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            screen_width=screen_width,
            screen_height=screen_height,
            hardware_concurrency=hardware_concurrency,
        ),
    )
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


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str, profile: BrowserProfile | None = None) -> dict:
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
    auth_session: dict = {}
    if profile is not None:
        try:
            auth_session = create_chatgpt_web_session(session, device_id, profile)
        except Exception as exc:
            log(f"ChatGPT Web session 保存失败: {exc}", "yellow")
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
        "chatgpt_auth_session": auth_session,
    }


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = proxy
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())
        self.profile: BrowserProfile = _random_browser_profile()
        self.common_headers = _build_common_headers(self.profile)
        self.navigate_headers = _build_navigate_headers(self.profile)
        self.code_verifier = ""
        self.passwordless_signup = False
        self.last_otp_continue_url = ""

    def close(self) -> None:
        try:
            self.session.close()
        finally:
            _untrack_session(self.session)

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(self.navigate_headers)
        if referer:
            headers["referer"] = referer
        return proxy_settings.build_headers(headers, target_url=auth_base, proxy=self.proxy, upstream=True)

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(self.common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _build_sentinel_tokens(self, flow: str, *, include_so: bool = False) -> "SentinelTokens":
        return build_sentinel_tokens(
            self.session,
            self.device_id,
            flow,
            user_agent=self.profile.user_agent,
            sec_ch_ua=self.profile.sec_ch_ua,
            include_so=include_so,
            screen_width=self.profile.screen_width,
            screen_height=self.profile.screen_height,
            hardware_concurrency=self.profile.hardware_concurrency,
        )

    def _build_sentinel_token(self, flow: str) -> str:
        sentinel_value, _oai_sc = _build_sentinel_token_tuple(
            self.session,
            self.device_id,
            flow,
            user_agent=self.profile.user_agent,
            sec_ch_ua=self.profile.sec_ch_ua,
            screen_width=self.profile.screen_width,
            screen_height=self.profile.screen_height,
            hardware_concurrency=self.profile.hardware_concurrency,
        )
        return sentinel_value

    def _platform_authorize(self, email: str, index: int) -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
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
        final_url = str(getattr(resp, "url", "") or "")
        self.passwordless_signup = "/email-verification" in final_url.lower()
        mode = "passwordless" if self.passwordless_signup else "password"
        step(index, f"platform authorize 完成 mode={mode} url={final_url[:160]}")
        return self.code_verifier

    def _start_passwordless_signup(self, index: int) -> None:
        step(index, "开始切换 passwordless signup 并发送验证码")

        def _do_send_otp(ua_override: str = "") -> tuple[Any, str]:
            headers = self._json_headers(f"{auth_base}/create-account/password")
            if ua_override:
                headers["user-agent"] = ua_override
            # 与 authorize_continue / user_register 一致注入 Sentinel Token，
            # 否则 OpenAI 风控极易直接判机器流量并在 200 下静默丢弃邮件。
            _apply_sentinel_headers(headers, self._build_sentinel_tokens("authorize_continue"))
            return request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/passwordless/send-otp",
                headers=headers,
                verify=False,
            )

        resp, error = _do_send_otp()
        # 命中 Cloudflare challenge 时刷新 clearance 并重试一次
        if _is_cloudflare_challenge(resp):
            step(index, "passwordless send-otp 命中 Cloudflare，刷新 clearance 重试", "yellow")
            bundle = proxy_settings.refresh_clearance(target_url=auth_base, proxy=self.proxy, force=True, upstream=True)
            if bundle is not None:
                resp, error = _do_send_otp(ua_override=str(bundle.user_agent or ""))
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(f"passwordless_send_otp_cloudflare_challenge: {_response_error_detail(resp, 1200)}")
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        self.passwordless_signup = True
        step(index, "passwordless signup 验证码发送完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        _apply_sentinel_headers(headers, self._build_sentinel_tokens("username_password_create"))
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            error_code = _openai_error_code(data)
            error_message = _openai_error_message(data)
            if error_code == "invalid_auth_step":
                step(index, "注册失败提示: OpenAI 返回 invalid_auth_step，当前会话步骤不匹配", "yellow")
            elif error_code == "account_creation_failed" or error_message == "Failed to create account. Please try again.":
                step(index, "注册失败提示: OpenAI 拒绝创建账号，通常是邮箱域名、IP 或会话风控触发，请更换邮箱域名/出口IP后重试", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _submit_email_continue(self, email: str, index: int, referer: str = "") -> None:
        step(index, "开始提交注册邮箱")
        headers = self._json_headers(referer or f"{auth_base}/create-account")
        _apply_sentinel_headers(headers, self._build_sentinel_tokens("authorize_continue"))
        resp, error = request_with_local_retry(
            self.session,
            "post",
            f"{auth_base}/api/accounts/authorize/continue",
            json={"username": {"kind": "email", "value": email}},
            headers=headers,
            allow_redirects=False,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            detail = _response_error_detail(resp)
            raise RuntimeError(error or f"register_email_continue_http_{getattr(resp, 'status_code', 'unknown')}{', ' + detail if detail else ''}")
        step(index, "注册邮箱提交完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> str:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(
            self.session,
            self.device_id,
            code,
            base_headers=self.common_headers,
            user_agent=self.profile.user_agent,
            sec_ch_ua=self.profile.sec_ch_ua,
            screen_width=self.profile.screen_width,
            screen_height=self.profile.screen_height,
            hardware_concurrency=self.profile.hardware_concurrency,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or resp.headers.get("Location") or "").strip()
        self.last_otp_continue_url = continue_url
        if continue_url:
            self._authorize_continue(continue_url, index)
        step(index, "验证码校验完成")
        return continue_url

    def _authorize_continue(self, continue_url: str, index: int) -> None:
        url = str(continue_url or "").strip()
        if not url:
            return
        if not url.lower().startswith("http"):
            url = urljoin(f"{auth_base}/", url.lstrip("/"))
        step(index, "开始执行 authorize/continue")
        resp, error = request_with_local_retry(
            self.session,
            "get",
            url,
            headers=self._navigate_headers(f"{auth_base}/email-verification"),
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, f"authorize/continue 完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _create_account(self, name: str, birthdate: str, index: int, referer: str = "") -> str:
        step(index, "开始创建账号资料")
        headers = self._json_headers(referer or f"{auth_base}/about-you")
        sentinel_tokens = self._build_sentinel_tokens("oauth_create_account", include_so=True)
        if not sentinel_tokens.so_token:
            raise RuntimeError("OpenAI-Sentinel-SO-Token 生成失败")
        _apply_sentinel_headers(headers, sentinel_tokens, require_so_header=True)
        if sentinel_tokens.oai_sc:
            self.session.cookies.set("oai-sc", sentinel_tokens.oai_sc, domain=".openai.com")
            self.session.cookies.set("oai-sc", sentinel_tokens.oai_sc, domain=".auth.openai.com")
        step(index, f"Sentinel create_account: token_len={len(sentinel_tokens.token)}, so_token={'yes' if sentinel_tokens.so_token else 'no'}, sdk={sentinel_tokens.sdk_version}, req_host={sentinel_tokens.req_host}, req_keys={sentinel_tokens.req_keys}, so={sentinel_tokens.so_shape}")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            error_code = _openai_error_code(data)
            error_message = _openai_error_message(data)
            if error_code == "invalid_auth_step":
                step(index, "创建账号失败提示: OpenAI 返回 invalid_auth_step，当前会话步骤不匹配", "yellow")
            elif error_code == "account_creation_failed" or error_message == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: OpenAI 拒绝创建账号，通常是邮箱域名、IP 或会话风控触发，请更换邮箱域名/出口IP后重试", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or resp.headers.get("Location") or "").strip()
        step(index, "创建账号资料完成")
        return continue_url
    def _finish_registration_and_exchange_tokens(self, code_verifier: str, continue_url: str, index: int) -> dict:
        step(index, "开始注册会话换 token")
        tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url or f"{auth_base}/sign-in-with-chatgpt/codex/consent", self.profile)
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
                h = dict(self.common_headers)
                h["referer"] = f"{auth_base}/log-in?usernameKind=email"
                h["oai-device-id"] = login_device_id
                h.update(_make_trace_headers())
                _apply_sentinel_headers(
                    h,
                    build_sentinel_tokens(
                        login_session,
                        login_device_id,
                        "authorize_continue",
                        user_agent=self.profile.user_agent,
                        sec_ch_ua=self.profile.sec_ch_ua,
                        screen_width=self.profile.screen_width,
                        screen_height=self.profile.screen_height,
                        hardware_concurrency=self.profile.hardware_concurrency,
                    ),
                )
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
            headers = dict(self.common_headers)
            headers["referer"] = f"{auth_base}/log-in/password"
            headers["oai-device-id"] = login_device_id
            headers.update(_make_trace_headers())
            _apply_sentinel_headers(
                headers,
                build_sentinel_tokens(
                    login_session,
                    login_device_id,
                    "password_verify",
                    user_agent=self.profile.user_agent,
                    sec_ch_ua=self.profile.sec_ch_ua,
                    screen_width=self.profile.screen_width,
                    screen_height=self.profile.screen_height,
                    hardware_concurrency=self.profile.hardware_concurrency,
                ),
            )
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
                resp, reason = validate_otp(
                    login_session,
                    login_device_id,
                    code,
                    base_headers=self.common_headers,
                    user_agent=self.profile.user_agent,
                    sec_ch_ua=self.profile.sec_ch_ua,
                    screen_width=self.profile.screen_width,
                    screen_height=self.profile.screen_height,
                    hardware_concurrency=self.profile.hardware_concurrency,
                )
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
            tokens = exchange_platform_tokens(login_session, login_device_id, code_verifier, continue_url, self.profile)
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
        try:
            password = ""
            first_name, last_name = _random_name()
            code_verifier = self._platform_authorize(email, index)
            if not self.passwordless_signup:
                self._start_passwordless_signup(index)
            step(index, "已进入 passwordless signup，不创建本地不可用的随机密码")
            step(index, "开始等待注册验证码")
            code = wait_for_code(mailbox)
            if not code:
                raise RuntimeError("等待注册验证码超时")
            step(index, f"收到注册验证码: {code}")
            continue_url = self._validate_otp(code, index)
            account_continue_url = self._create_account(f"{first_name} {last_name}", _random_birthdate(), index, continue_url or f"{auth_base}/about-you")
            tokens = self._finish_registration_and_exchange_tokens(code_verifier, account_continue_url, index)
            try:
                _record_register_domain_result(mailbox, True)
            except Exception as exc:
                step(index, f"注册域名统计写入失败: {exc}", "yellow")
            return {
                "email": email,
                "password": password,
                "access_token": str(tokens.get("access_token") or "").strip(),
                "refresh_token": str(tokens.get("refresh_token") or "").strip(),
                "id_token": str(tokens.get("id_token") or "").strip(),
                "chatgpt_auth_session": tokens.get("chatgpt_auth_session"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            try:
                _record_register_domain_result(mailbox, False)
            except Exception as exc:
                step(index, f"注册域名统计写入失败: {exc}", "yellow")
            raise


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
        refresh_result = account_service.refresh_accounts([access_token])
        if refresh_result.get("errors"):
            step(index, f"账号已保存，刷新额度暂未成功，稍后可重试: {refresh_result['errors']}", "yellow")
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
