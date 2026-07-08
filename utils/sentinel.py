"""OpenAI Sentinel token helpers."""
from __future__ import annotations

import base64
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from curl_cffi.requests import Session


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500_000
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
            "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
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


DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"'
SENTINEL_SDK_BOOTSTRAP_URL = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
SENTINEL_OBSERVER_WAIT_MS = 5000
_SDK_SCRIPT_RE = re.compile(r"script\.src\s*=\s*['\"]([^'\"]+/sentinel/([^/'\"]+)/sdk\.js)['\"]")
_SDK_CACHE_TTL = 600
_sdk_cache_lock = threading.Lock()
_sdk_cache: dict[str, str | float] = {}


@dataclass(frozen=True)
class SentinelTokenBundle:
    sentinel_token: str
    oai_sc: str = ""
    so_token: str = ""
    sdk_version: str = ""
    sdk_url: str = ""
    requirements_token_length: int = 0
    sentinel_req_so_required: bool = False


def _json_compact(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _sdk_headers(user_agent: str, sec_ch_ua: str, referer: str = "") -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _load_current_sdk(session: "Session", user_agent: str, sec_ch_ua: str) -> tuple[str, str, str]:
    now = time.time()
    with _sdk_cache_lock:
        if (
            _sdk_cache.get("source")
            and _sdk_cache.get("url")
            and _sdk_cache.get("version")
            and now - float(_sdk_cache.get("loaded_at") or 0) < _SDK_CACHE_TTL
        ):
            return str(_sdk_cache["source"]), str(_sdk_cache["url"]), str(_sdk_cache["version"])

    bootstrap_resp = session.get(
        SENTINEL_SDK_BOOTSTRAP_URL,
        headers=_sdk_headers(user_agent, sec_ch_ua, "https://sentinel.openai.com/backend-api/sentinel/frame.html"),
        timeout=20,
        verify=False,
    )
    if bootstrap_resp.status_code != 200:
        raise RuntimeError(f"sentinel_sdk_bootstrap_http_{bootstrap_resp.status_code}")
    match = _SDK_SCRIPT_RE.search(str(bootstrap_resp.text or ""))
    if not match:
        raise RuntimeError("sentinel_sdk_url_not_found")
    sdk_url, sdk_version = match.group(1), match.group(2)
    sdk_resp = session.get(
        sdk_url,
        headers=_sdk_headers(user_agent, sec_ch_ua, SENTINEL_SDK_BOOTSTRAP_URL),
        timeout=20,
        verify=False,
    )
    if sdk_resp.status_code != 200 or not str(sdk_resp.text or "").strip():
        raise RuntimeError(f"sentinel_sdk_http_{sdk_resp.status_code}")
    source = str(sdk_resp.text)
    with _sdk_cache_lock:
        _sdk_cache.update({"source": source, "url": sdk_url, "version": sdk_version, "loaded_at": now})
    return source, sdk_url, sdk_version


def _sentinel_base_from_sdk_url(sdk_url: str) -> str:
    parsed = urlparse(sdk_url)
    if not parsed.scheme or not parsed.netloc:
        return "https://sentinel.openai.com/backend-api/sentinel/"
    return f"{parsed.scheme}://{parsed.netloc}/backend-api/sentinel/"


def _extract_oai_sc(sentinel_token: str) -> str:
    try:
        token = str(json.loads(sentinel_token).get("c") or "").strip()
    except Exception:
        token = ""
    return f"0{token}" if token else ""


def _post_sentinel_req(
    session: "Session",
    *,
    sdk_url: str,
    sdk_version: str,
    flow: str,
    p_value: str,
    device_id: str,
    user_agent: str,
    sec_ch_ua: str,
) -> dict:
    base = _sentinel_base_from_sdk_url(sdk_url)
    origin = base.split("/backend-api/", 1)[0]
    resp = session.post(
        f"{base}req",
        data=_json_compact({"p": p_value, "id": device_id, "flow": flow}),
        headers={
            **_sdk_headers(user_agent, sec_ch_ua, f"{base}frame.html?sv={sdk_version}"),
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": origin,
        },
        timeout=30,
        verify=False,
    )
    try:
        data = resp.json() if resp.text else {}
    except Exception:
        data = {}
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("token"):
        detail = _json_compact(data)[:500] if data else str(getattr(resp, "text", "") or "")[:500]
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}: {detail}")
    return data


def _run_official_sdk(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str,
    sec_ch_ua: str,
    include_so: bool,
    observer_wait_ms: int,
) -> SentinelTokenBundle:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node_not_available_for_sentinel_sdk")
    sdk_source, sdk_url, sdk_version = _load_current_sdk(session, user_agent, sec_ch_ua)
    runner = Path(__file__).with_name("sentinel_sdk_runner.js")
    if not runner.exists():
        raise RuntimeError("sentinel_sdk_runner_missing")

    proc = subprocess.Popen(
        [node, str(runner)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "NODE_NO_WARNINGS": "1"},
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdout_lines: queue.Queue[str | None] = queue.Queue()

    def _read_stdout() -> None:
        try:
            for output_line in proc.stdout:
                stdout_lines.put(output_line)
        finally:
            stdout_lines.put(None)

    stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
    stdout_thread.start()

    proc.stdin.write(
        _json_compact(
            {
                "type": "start",
                "flow": flow,
                "deviceId": device_id,
                "userAgent": user_agent,
                "sdkSource": sdk_source,
                "sdkUrl": sdk_url,
                "sdkVersion": sdk_version,
                "includeSo": include_so,
                "observerWaitMs": max(0, int(observer_wait_ms or 0)),
                "pageUrl": "https://auth.openai.com/about-you",
            }
        )
        + "\n"
    )
    proc.stdin.flush()

    first_p_len = 0
    so_required = False
    deadline = time.time() + max(45, observer_wait_ms / 1000 + 45)
    try:
        while time.time() < deadline:
            try:
                line = stdout_lines.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                if proc.poll() is not None:
                    break
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            msg_type = str(message.get("type") or "")
            if msg_type == "sentinel_req":
                if first_p_len:
                    continue
                p_value = str(message.get("p") or "")
                first_p_len = first_p_len or len(p_value)
                try:
                    req_data = _post_sentinel_req(
                        session,
                        sdk_url=sdk_url,
                        sdk_version=sdk_version,
                        flow=str(message.get("flow") or flow),
                        p_value=p_value,
                        device_id=device_id,
                        user_agent=user_agent,
                        sec_ch_ua=sec_ch_ua,
                    )
                    so_required = so_required or bool((req_data.get("so") or {}).get("required"))
                    response = {
                        "type": "sentinel_req_result",
                        "requestId": message.get("requestId"),
                        "result": {"cachedChatReq": req_data, "cachedProof": p_value},
                    }
                except Exception as error:
                    response = {
                        "type": "sentinel_req_result",
                        "requestId": message.get("requestId"),
                        "error": str(error),
                    }
                proc.stdin.write(_json_compact(response) + "\n")
                proc.stdin.flush()
                continue
            if msg_type == "result":
                sentinel_token = str(message.get("token") or "")
                if not sentinel_token:
                    raise RuntimeError("sentinel_sdk_empty_token")
                return SentinelTokenBundle(
                    sentinel_token=sentinel_token,
                    oai_sc=_extract_oai_sc(sentinel_token),
                    so_token=str(message.get("soToken") or ""),
                    sdk_version=sdk_version,
                    sdk_url=sdk_url,
                    requirements_token_length=first_p_len,
                    sentinel_req_so_required=so_required,
                )
            if msg_type == "error":
                raise RuntimeError(str(message.get("message") or "sentinel_sdk_error"))
        raise RuntimeError("sentinel_sdk_timeout")
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        try:
            proc.stderr.read()
        except Exception:
            pass


def _build_legacy_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    generator = SentinelTokenGenerator(device_id, ua)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=_json_compact({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = _json_compact({"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow})
        return fallback, ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    sentinel_value = _json_compact({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow})
    return sentinel_value, "0" + token


def build_sentinel_tokens(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    include_so: bool = False,
    observer_wait_ms: int = SENTINEL_OBSERVER_WAIT_MS,
) -> SentinelTokenBundle:
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    try:
        bundle = _run_official_sdk(
            session,
            device_id,
            flow,
            user_agent=ua,
            sec_ch_ua=ch_ua,
            include_so=include_so,
            observer_wait_ms=observer_wait_ms,
        )
        if include_so and not bundle.so_token:
            raise RuntimeError("sentinel_sdk_so_token_missing")
        return bundle
    except Exception:
        if include_so:
            raise
        sentinel_token, oai_sc = _build_legacy_sentinel_token(
            session,
            device_id,
            flow,
            user_agent=ua,
            sec_ch_ua=ch_ua,
        )
        return SentinelTokenBundle(sentinel_token=sentinel_token, oai_sc=oai_sc)


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
) -> tuple[str, str]:
    bundle = build_sentinel_tokens(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
    )
    return bundle.sentinel_token, bundle.oai_sc
