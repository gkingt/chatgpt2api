from __future__ import annotations

import hashlib
from html import unescape
import imaplib
import json
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from email import message_from_bytes, message_from_string, policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from threading import Lock
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from curl_cffi import requests


from services.config import DATA_DIR

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()

OUTLOOK_TOKEN_USED_FILE = DATA_DIR / "outlook_token_used.json"
_outlook_token_state_lock = Lock()
# in_use 超过该秒数视为陈旧（注册进程崩溃残留），可被重新领用
OUTLOOK_IN_USE_STALE_SECONDS = 3600
OUTLOOK_RECORDED_STATES = {"used", "in_use", "token_invalid", "failed"}
OUTLOOK_UNAVAILABLE_STATES = {"used", "token_invalid", "failed"}
cancel_checker: Callable[[], None] | None = None


def set_cancel_checker(checker: Callable[[], None] | None) -> None:
    global cancel_checker
    cancel_checker = checker


def _check_cancelled() -> None:
    if cancel_checker is not None:
        cancel_checker()


def _load_ddg_aliases() -> set[str]:
    try:
        if DDG_ALIASES_FILE.exists():
            data = json.loads(DDG_ALIASES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(item).strip().lower() for item in data if str(item).strip()}
    except Exception:
        pass
    return set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    DDG_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DDG_ALIASES_FILE.write_text(json.dumps(sorted(aliases), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


def _load_outlook_token_state() -> dict[str, dict[str, Any]]:
    """读取邮箱池状态文件，返回 {email_lower: {state, reason, updated_at}}。

    兼容旧格式：纯字符串列表（历史的“已用邮箱”）会被解释为 used。
    """
    try:
        if not OUTLOOK_TOKEN_USED_FILE.exists():
            return {}
        data = json.loads(OUTLOOK_TOKEN_USED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    state: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            key = str(item).strip().lower()
            if key:
                state[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            email = str(key).strip().lower()
            if not email:
                continue
            if isinstance(value, dict):
                state[email] = {
                    "state": str(value.get("state") or "used").strip() or "used",
                    "reason": str(value.get("reason") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                }
            else:
                state[email] = {"state": str(value or "used").strip() or "used", "reason": "", "updated_at": ""}
    return state


def _save_outlook_token_state(state: dict[str, dict[str, Any]]) -> None:
    OUTLOOK_TOKEN_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: state[key] for key in sorted(state)}
    OUTLOOK_TOKEN_USED_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _outlook_entry_available(entry: dict[str, Any] | None) -> bool:
    """该邮箱当前是否可领用：未记录、或 in_use 已陈旧、或非终态时可用。"""
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if current == "in_use":
        updated_at = str(entry.get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds()
            return age >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def _set_outlook_token_state(address: str, state: str, reason: str = "") -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        store[target] = {"state": str(state), "reason": str(reason or ""), "updated_at": datetime.now(timezone.utc).isoformat()}
        _save_outlook_token_state(store)


def _release_outlook_token_state(address: str) -> None:
    """把 in_use 释放回未使用（仅当当前确实是 in_use 时）。"""
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        entry = store.get(target)
        if isinstance(entry, dict) and str(entry.get("state") or "") == "in_use":
            store.pop(target, None)
            _save_outlook_token_state(store)


def reset_outlook_token_pool_state(scope: str = "all") -> int:
    """重置邮箱池状态文件。

    scope=all 清空所有记录；scope=failed 仅清除 failed/token_invalid/in_use（保留 used）。
    返回被清除的条目数。
    """
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        if not store:
            return 0
        if str(scope) == "failed":
            remove = {key for key, value in store.items() if str(value.get("state") or "") in {"failed", "token_invalid", "in_use"}}
            for key in remove:
                store.pop(key, None)
            _save_outlook_token_state(store)
            return len(remove)
        count = len(store)
        _save_outlook_token_state({})
        return count


def prune_outlook_unused_credentials(credentials: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Return credentials with recorded state, plus the number pruned as unused."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
    kept: list[dict[str, str]] = []
    removed = 0
    for credential in credentials:
        key = str(credential.get("email") or "").strip().lower()
        entry = store.get(key) if key else None
        state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
        if state in OUTLOOK_RECORDED_STATES:
            kept.append(credential)
        else:
            removed += 1
    return kept, removed


def outlook_token_pool_stats(pool: list[dict[str, str]] | None = None) -> dict[str, int]:
    """统计邮箱池各状态数量。pool 为该 provider 当前导入的邮箱列表（用于算 unused）。"""
    store = _load_outlook_token_state()
    counts = {"unused": 0, "in_use": 0, "used": 0, "token_invalid": 0, "failed": 0}
    if pool:
        for credential in pool:
            entry = store.get(str(credential.get("email") or "").strip().lower())
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
            else:
                counts["unused"] += 1
    else:
        for entry in store.values():
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
    return counts


ResultT = TypeVar("ResultT")
domain_lock = Lock()
provider_lock = Lock()
domain_index = 0
provider_index = 0
cloudmail_token_lock = Lock()
cloudmail_token_cache: dict[str, tuple[str, float]] = {}
disabled_domain_lock = Lock()
disabled_domains: set[str] = set()


def set_disabled_domains(domains: list[str] | set[str] | tuple[str, ...]) -> None:
    with disabled_domain_lock:
        disabled_domains.clear()
        disabled_domains.update(str(item).strip().lower() for item in domains if str(item).strip())


def _config(mail_config: dict) -> dict:
    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 120),
        "wait_interval": float(mail_config.get("wait_interval") or 3),
        "user_agent": str(mail_config.get("user_agent") or "Mozilla/5.0"),
        "proxy": str(mail_config.get("proxy") or "").strip(),
    }


def _random_mailbox_name() -> str:
    return f"{''.join(random.choices(string.ascii_lowercase, k=5))}{''.join(random.choices(string.digits, k=random.randint(1, 3)))}{''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))}"


def _random_subdomain_label() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 10)))


def _random_subdomain_suffix() -> str:
    chars = [random.choice(string.ascii_lowercase), random.choice(string.digits)]
    chars.extend(random.choices(string.ascii_lowercase + string.digits, k=3))
    random.shuffle(chars)
    return "".join(chars)


def _next_domain(domains: list[str]) -> str:
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    return random.choice(domains)


def _random_domain(domains: list[str]) -> str:
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    return random.choice(domains)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_dns_name(value: Any, field: str) -> str:
    text = str(value or "").strip().strip(".").lower()
    if not text:
        raise RuntimeError(f"{field} 不能为空")
    labels = text.split(".")
    if any(not label for label in labels):
        raise RuntimeError(f"{field} 格式不正确")
    for label in labels:
        if len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise RuntimeError(f"{field} 包含非法标签: {label}")
    return text


def _create_session(conf: dict):
    proxy = str(conf.get("proxy") or "").strip()
    kwargs = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return requests.Session(**kwargs)


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            # Some APIs (notably testmail.app) return Unix milliseconds.
            if abs(timestamp) >= 100_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(data: dict[str, Any]) -> tuple[str, str]:
    text_content = _value_as_text(data.get("text_content") or data.get("text") or data.get("body") or data.get("content"))
    html_content = _value_as_text(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html"))
    if text_content or html_content:
        return text_content, html_content
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            html.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                out.extend(_extract_text_candidates(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_text_candidates(item))
        return out
    return []


def _message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in ("to", "toAddr", "toAddrOrig", "rcptto", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    if not target or not candidates:
        return True
    for item in candidates:
        value = str(item).strip().lower()
        if not value:
            continue
        if value == target:
            return True
        try:
            if any(address.lower() == target for _, address in getaddresses([value]) if address):
                return True
        except Exception:
            pass
    return False


def _response_json(resp: Any, provider: str, action: str, expected: tuple[int, ...] = (200, 201)) -> Any:
    try:
        data = resp.json()
    except Exception:
        data = str(getattr(resp, "text", "") or "")
    if getattr(resp, "status_code", 0) not in expected:
        detail = data.get("message") if isinstance(data, dict) else ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            detail = data["error"].get("message") or detail
        raise RuntimeError(f"{provider} {action}失败: HTTP {getattr(resp, 'status_code', 0)}, {detail or str(getattr(resp, 'text', ''))[:300]}")
    return data


def _response_text(resp: Any, provider: str, action: str, expected: tuple[int, ...] = (200,)) -> str:
    status_code = getattr(resp, "status_code", 0)
    if status_code not in expected:
        raise RuntimeError(f"{provider} {action}失败: HTTP {status_code}, {str(getattr(resp, 'text', ''))[:300]}")
    return str(getattr(resp, "text", "") or "")


def _unwrap_data(data: Any) -> Any:
    current = data
    for _ in range(3):
        if isinstance(current, dict) and isinstance(current.get("data"), (dict, list)):
            current = current["data"]
            continue
        break
    return current


def _payload_items(data: Any, keys: tuple[str, ...] = ("messages", "emails", "items", "results", "data")) -> list[dict[str, Any]]:
    current = _unwrap_data(data)
    if isinstance(current, list):
        return [item for item in current if isinstance(item, dict)]
    if not isinstance(current, dict):
        return []
    for key in keys:
        value = current.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _payload_value(data: Any, *keys: str) -> Any:
    current = _unwrap_data(data)
    if isinstance(current, dict):
        for key in keys:
            value = current.get(key)
            if value not in (None, ""):
                return value
        for nested_key in ("inbox", "mailbox", "account", "email"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                value = _payload_value(nested, *keys)
                if value not in (None, ""):
                    return value
    return None


def _sender_value(value: Any) -> str:
    if isinstance(value, dict):
        address = value.get("address") or value.get("email") or value.get("name") or value.get("value") or ""
        return str(address)
    if isinstance(value, list):
        return ", ".join(_sender_value(item) for item in value if _sender_value(item))
    return str(value or "")


def _value_as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_value_as_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        for key in ("content", "text", "body", "value", "data"):
            if value.get(key) not in (None, ""):
                return _value_as_text(value[key])
    return str(value or "")


def _extract_message_content(item: dict[str, Any]) -> tuple[str, str]:
    body = item.get("body")
    if isinstance(body, dict):
        text_content = _value_as_text(body.get("text") or body.get("plain") or body.get("content"))
        html_content = _value_as_text(body.get("html") or body.get("html_content"))
    else:
        text_content, html_content = _extract_content(item)
    if not text_content:
        for key in ("text", "text_content", "mail_text", "plain", "data", "content", "body"):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("text") or value.get("plain") or value.get("content")
            if value not in (None, ""):
                text_content = _value_as_text(value)
                break
    if not html_content:
        for key in ("html", "html_content", "html_body", "mail_html", "body_html"):
            value = item.get(key)
            if value not in (None, ""):
                html_content = _value_as_text(value)
                break
    return str(text_content or ""), str(html_content or "")


def _message_sort_key(item: dict[str, Any]) -> tuple[float, str]:
    received = _parse_received_at(
        item.get("receivedAt")
        or item.get("received_at")
        or item.get("receivedDateTime")
        or item.get("received_date")
        or item.get("createdAt")
        or item.get("created_at")
        or item.get("mail_timestamp")
        or item.get("timestamp")
        or item.get("date")
    )
    return (received.timestamp() if received else 0.0, str(item.get("id") or item.get("_id") or item.get("message_id") or ""))


def _graphql_data(data: Any, provider: str, action: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError(f"{provider} {action}返回结构不是对象")
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        details = "; ".join(str(item.get("message") or item) if isinstance(item, dict) else str(item) for item in errors)
        raise RuntimeError(f"{provider} {action}失败: {details[:300]}")
    result = data.get("data")
    if not isinstance(result, dict):
        raise RuntimeError(f"{provider} {action}响应缺少 data")
    return result


def _local_part(value: str | None) -> str:
    text = str(value or "").strip()
    return text.rsplit("@", 1)[0] if "@" in text else text


def _configured_domains(entry: dict, default: str = "") -> list[str]:
    raw = entry.get("domain") or entry.get("domains") or []
    values = _normalize_string_list(raw)
    return values or ([default] if default else [])


def _extract_code(message: dict[str, Any]) -> str | None:
    content = f"{message.get('subject', '')}\n{message.get('text_content', '')}\n{message.get('html_content', '')}".strip()
    if not content:
        return None
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


def _message_tracking_ref(message: dict[str, Any]) -> str:
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"
    received_at = message.get("received_at")
    received_value = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    content = "\n".join(str(message.get(key) or "") for key in ("subject", "sender", "text_content", "html_content"))
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


class BaseMailProvider:
    name = "unknown"

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref

    def wait_for(self, mailbox: dict[str, Any], on_message: Callable[[dict[str, Any]], ResultT | None]) -> ResultT | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            _check_cancelled()
            message = self.fetch_latest_message(mailbox)
            if message:
                result = on_message(message)
                if result is not None:
                    return result
            sleep_for = max(0.2, self.conf["wait_interval"])
            until = min(deadline, time.monotonic() + sleep_for)
            while time.monotonic() < until:
                _check_cancelled()
                time.sleep(min(0.2, until - time.monotonic()))
        return None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            _check_cancelled()
            fetch_recent = getattr(self, "fetch_recent_messages", None)
            messages = fetch_recent(mailbox) if callable(fetch_recent) else None
            if messages is None:
                latest = self.fetch_latest_message(mailbox)
                messages = [latest] if latest else []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                code = _extract_code(message)
                seen_refs.add(ref)
                if code:
                    seen_value.append(ref)
                    return code
            sleep_for = max(0.2, self.conf["wait_interval"])
            until = min(deadline, time.monotonic() + sleep_for)
            while time.monotonic() < until:
                _check_cancelled()
                time.sleep(min(0.2, until - time.monotonic()))
        return None

    def close(self) -> None:
        pass


class MailNestProvider(BaseMailProvider):
    name = "mailnest"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://mailnest.top").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.project_code = str(entry.get("project_code") or "ChatGPT0001").strip()
        self.sale_mode = str(entry.get("sale_mode") or "temporary").strip().lower() or "temporary"
        if self.sale_mode not in {"temporary", "exclusive"}:
            self.sale_mode = "temporary"
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("MailNest api_key 不能为空")
        return {"Authorization": f"Bearer {self.api_key}", "User-Agent": self.conf["user_agent"]}

    @staticmethod
    def _data(resp, action: str) -> Any:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise RuntimeError(f"MailNest {action}失败: HTTP {resp.status_code}, {detail or resp.text[:300]}")
        if not isinstance(data, dict) or data.get("code") != "00000":
            msg = data.get("msg") if isinstance(data, dict) else ""
            code = data.get("code") if isinstance(data, dict) else ""
            raise RuntimeError(f"MailNest {action}失败: {code or 'unknown'} {msg or resp.text[:300]}")
        return data.get("data")

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"count": 1}
        path = "/api/v1/email/exclusive/buy"
        if self.sale_mode == "temporary":
            if not self.project_code:
                raise RuntimeError("MailNest project_code 不能为空")
            payload["project_code"] = self.project_code
            path = "/api/v1/email/temporary/buy"
        resp = self.session.post(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        items = self._data(resp, "购买邮箱")
        if not isinstance(items, list) or not items:
            raise RuntimeError("MailNest 购买邮箱响应缺少 data[]")
        item = items[0] if isinstance(items[0], dict) else {}
        address = str(item.get("email") or "").strip()
        if not address:
            raise RuntimeError("MailNest 购买邮箱响应缺少 email")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "order_id": str(item.get("id") or ""),
            "sale_mode": str(item.get("sale_mode") or self.sale_mode),
            "project_code": str(item.get("project_code") or self.project_code),
        }

    def _normalize_message(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body_type = str(item.get("body_type") or "").lower()
        body = str(item.get("body") or "")
        preview = str(item.get("body_preview") or "")
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from_email") or item.get("from_name") or ""),
            "text_content": "\n".join(part for part in (preview, item.get("code_match") or "", body if body_type != "html" else "") if str(part or "").strip()),
            "html_content": body if body_type == "html" else "",
            "received_at": _parse_received_at(item.get("received_at")),
            "raw": item,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        path = "/api/v1/email/user-mailbox/receive" if mailbox.get("sale_mode") == "user-mailbox" else "/api/v1/email/receive"
        resp = self.session.post(
            f"{self.api_base}{path}",
            headers=self._headers(),
            json={"email": mailbox["address"]},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        items = self._data(resp, "收件")
        if not isinstance(items, list):
            return []
        return [self._normalize_message(mailbox, item) for item in items if isinstance(item, dict)]

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None


class CloudflareTempMailProvider(BaseMailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_password = str(entry["admin_password"]).strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.subdomain_levels = _normalize_string_list(entry.get("subdomain_levels"))
        suffix_value = entry.get("append_random_suffix", True)
        if isinstance(suffix_value, bool):
            self.append_random_suffix = suffix_value
        else:
            self.append_random_suffix = str(suffix_value).strip().lower() not in {"0", "false", "no", "off"}
        try:
            depth = int(entry.get("random_subdomain_depth") or 1)
        except (TypeError, ValueError):
            depth = 1
        self.random_subdomain_depth = max(1, min(5, depth))
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"Content-Type": "application/json", "User-Agent": self.conf["user_agent"], **(headers or {})}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudflareTempMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _resolve_domain(self) -> str:
        base_domain = _normalize_dns_name(_next_domain(self.domain), "CloudflareTempMail 根域名")
        if self.subdomain_levels:
            levels = [
                _normalize_dns_name(value, f"CloudflareTempMail 第 {index} 级域名")
                for index, value in enumerate(self.subdomain_levels, start=1)
            ]
            if any("." in level for level in levels):
                raise RuntimeError("CloudflareTempMail 手动域名每一级只能填写一个标签，不能包含点号")
            if self.append_random_suffix:
                levels = [
                    _normalize_dns_name(
                        f"{level}{_random_subdomain_suffix()}",
                        f"CloudflareTempMail 第 {index} 级域名（含随机后缀）",
                    )
                    for index, level in enumerate(levels, start=1)
                ]
            return f"{'.'.join(reversed(levels))}.{base_domain}"
        if self.subdomain:
            custom = _normalize_dns_name(random.choice(self.subdomain), "CloudflareTempMail N 级域名")
            if custom == base_domain or custom.endswith(f".{base_domain}"):
                return custom
            return f"{custom}.{base_domain}"
        prefix = ".".join(_random_subdomain_label() for _ in range(self.random_subdomain_depth))
        return f"{prefix}.{base_domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/admin/new_address",
            headers={"x-admin-auth": self.admin_password},
            payload={"enablePrefix": True, "name": username or _random_mailbox_name(), "domain": self._resolve_domain()},
        )
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError("CloudflareTempMail 缺少 address 或 jwt")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def get_existing_mailbox(self, email: str) -> dict[str, Any]:
        """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
        data = self._request("POST", "/admin/get_address", headers={"x-admin-auth": self.admin_password}, payload={"address": email})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError(f"CloudflareTempMail 无法获取已有邮箱 {email} 的 JWT")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/mails", headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 10, "offset": 0})
        raw = list(data.get("results") or []) if isinstance(data, dict) else data if isinstance(data, list) else []
        messages = [item for item in raw if isinstance(item, dict) and _message_matches_email(item, str(mailbox.get("address") or ""))]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("_id") or ""), "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DDGMailProvider(BaseMailProvider):
    name = "ddg_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.ddg_token = str(entry["ddg_token"]).strip()
        self.cf_api_base = str(entry.get("api_base") or entry.get("cf_api_base") or "").rstrip("/")
        self.cf_inbox_jwt = str(entry.get("cf_inbox_jwt") or "").strip()
        self.cf_admin_password = str(entry.get("admin_password") or "").strip()
        self.cf_api_key = str(entry.get("cf_api_key") or "").strip()
        self.cf_auth_mode = str(entry.get("cf_auth_mode") or "none").strip().lower()
        self.cf_domain = entry.get("cf_domain") or []
        self.cf_create_path = str(entry.get("cf_create_path") or "/api/new_address").strip()
        self.cf_messages_path = str(entry.get("cf_messages_path") or "/api/mails").strip()
        self.session = _create_session(conf)

    def _cf_build_headers(self, content_type: bool = False) -> dict:
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.cf_api_key:
            if self.cf_auth_mode == "x-api-key":
                headers["X-API-Key"] = self.cf_api_key
            elif self.cf_auth_mode != "none":
                headers["Authorization"] = f"Bearer {self.cf_api_key}"
        return headers

    def _cf_request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict:
        merged_headers = {**self._cf_build_headers(True), **(headers or {}), "User-Agent": self.conf["user_agent"]}
        if self.cf_admin_password and method.upper() in ("POST",):
            merged_headers["x-admin-auth"] = self.cf_admin_password
        if self.cf_api_key and self.cf_auth_mode == "query-key":
            params = {**(params or {}), "key": self.cf_api_key}
        resp = self.session.request(method.upper(), f"{self.cf_api_base}{path}", headers=merged_headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DDGMail CF请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _ddg_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = self.session.request(method.upper(), f"https://quack.duckduckgo.com{path}", headers={"Authorization": f"Bearer {self.ddg_token}", "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"DDG API请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return resp.json()

    def _cf_list_payload(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "hydra:member", "data", "messages"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    return value["messages"]
        return []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        ddg_data = self._ddg_request("POST", "/api/email/addresses", payload={})
        ddg_address_part = str(ddg_data.get("address") or "").strip()
        if not ddg_address_part:
            raise RuntimeError("DDG API 返回无 address 字段")
        ddg_address = f"{ddg_address_part}@duck.com"

        if _is_ddg_alias_duplicate(ddg_address):
            raise RuntimeError(f"[{self.label}] DDG日上限已达，别名 {ddg_address} 已存在，自动切换邮箱提供商")

        _record_ddg_alias(ddg_address)

        if not self.cf_inbox_jwt:
            raise RuntimeError("DDGMail 需要 cf_inbox_jwt（DDG 转发目标的固定收件箱 JWT），请在邮箱配置中填写 CF Inbox JWT")

        return {"provider": self.name, "provider_ref": self.provider_ref, "address": ddg_address, "token": self.cf_inbox_jwt, "label": self.label}

    def _parse_raw_recipient(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        match = re.search(r"^To:\s*(.+?)$", raw_text, re.MULTILINE | re.IGNORECASE)
        if match:
            addr = match.group(1).strip()
            addr = re.sub(r"\s*<[^>]*>", "", addr)
            return addr.strip().lower()
        try:
            parsed = message_from_string(raw_text, policy=policy.default)
            return str(parsed.get("To") or "").strip().lower()
        except Exception:
            return ""

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        target_address = str(mailbox.get("address") or "").strip().lower()
        data = self._cf_request("GET", self.cf_messages_path, headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0})
        raw_list = self._cf_list_payload(data)
        messages = [item for item in raw_list if isinstance(item, dict)]
        if not messages:
            return None

        for item in messages:
            message_id = str(item.get("id") or item.get("msgid") or item.get("_id") or "")
            raw_text = str(item.get("raw") or "")
            raw_recipient = self._parse_raw_recipient(raw_text)
            if target_address and raw_recipient and target_address not in raw_recipient:
                continue
            text_content, html_content = _extract_content(item)
            subject = str(item.get("subject") or "")
            sender = item.get("from") or item.get("sender") or item.get("source") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
            if raw_text and (not subject or not sender or subject == sender == ""):
                try:
                    parsed = message_from_string(raw_text, policy=policy.default)
                    if not subject:
                        subject = str(parsed.get("Subject") or "")
                    if not sender:
                        sender = str(parsed.get("From") or "")
                except Exception:
                    pass
            return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": subject, "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

        return None

    def close(self) -> None:
        self.session.close()


class CloudMailGenProvider(BaseMailProvider):
    name = "cloudmail_gen"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_email = str(entry.get("admin_email") or "").strip()
        self.admin_password = str(entry.get("admin_password") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.session = _create_session(conf)

    def _request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.conf["user_agent"],
                **(headers or {}),
            },
            params=params,
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudMailGen 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _cache_key(self) -> str:
        return f"{self.api_base}|{self.admin_email}"

    def _get_token(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMailGen 缺少 admin_email 或 admin_password")
        cache_key = self._cache_key()
        now = time.time()
        with cloudmail_token_lock:
            cached = cloudmail_token_cache.get(cache_key)
            if cached and now < cached[1] - 300:
                return cached[0]
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
        )
        token = ""
        if isinstance(data, dict) and data.get("code") == 200:
            token = str((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError(f"CloudMailGen genToken 返回异常: {data}")
        with cloudmail_token_lock:
            cloudmail_token_cache[cache_key] = (token, now + 24 * 3600)
        return token

    def _resolve_address(self, username: str | None = None) -> str:
        domain = _next_domain(self.domain)
        if self.subdomain:
            domain = f"{random.choice(self.subdomain)}.{domain}"
        if username:
            local_part = username
        elif self.email_prefix:
            local_part = f"{self.email_prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
        else:
            local_part = _random_mailbox_name()
        return f"{local_part}@{domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.domain:
            raise RuntimeError("CloudMailGen 需要至少配置一个 domain")
        address = self._resolve_address(username)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("CloudMailGen 缺少 address")
        token = self._get_token()
        data = self._request(
            "POST",
            "/api/public/emailList",
            headers={"Authorization": token},
            payload={"toEmail": address, "size": 20, "timeSort": "desc"},
        )
        items = (data.get("data") or []) if isinstance(data, dict) and data.get("code") == 200 else []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("sender") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")
            ),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(BaseMailProvider):
    name = "tempmail_lol"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _resolve_domain(domain: str) -> tuple[str, bool]:
        text = str(domain or "").strip().lower()
        if text.startswith("*.") and len(text) > 2:
            return f"{_random_subdomain_label()}.{text[2:]}", True
        return text, False

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"https://api.tempmail.lol/v2{path}", params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"TempMail.lol 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.domain:
            domain, force_random_prefix = self._resolve_domain(_random_domain(self.domain))
            payload["domain"] = domain
            if force_random_prefix:
                payload["prefix"] = _random_mailbox_name()
        if username and "prefix" not in payload:
            payload["prefix"] = username
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/inbox", params={"token": mailbox["token"]})
        items = data.get("emails") or data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("created_at") or value.get("createdAt") or value.get("date") or value.get("received_at") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or value.get("token") or "")))
        text_content, html_content = _extract_content(item)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("token") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from") or item.get("from_address") or ""), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("created_at") or item.get("createdAt") or item.get("date") or item.get("received_at") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DuckMailProvider(BaseMailProvider):
    name = "duckmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", use_api_key: bool = False, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.api_key if use_api_key else token}"} if use_api_key or token else {}
        resp = self.session.request(method.upper(), f"https://api.duckmail.sbs{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"DuckMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("hydra:member") or data.get("member") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username or _random_mailbox_name()}@{self.default_domain}"
        payload = {"address": address, "password": password}
        account = self._request("POST", "/accounts", use_api_key=True, payload=payload)
        token_data = self._request("POST", "/token", use_api_key=True, payload=payload)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": str(token_data.get("token") or ""), "password": password, "account_id": str(account.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"page": 1})
        items = self._items(data)
        if not items:
            return None
        item = items[0]
        message_id = str(item.get("id") or item.get("@id") or "").replace("/messages/", "")
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""))
        sender = item.get("from") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("name") or ""
        html_content = item.get("html") or ""
        if isinstance(html_content, list):
            html_content = "".join(str(value) for value in html_content)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": str(item.get("text") or item.get("text_content") or ""), "html_content": str(html_content), "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")), "raw": item}

    def close(self) -> None:
        self.session.close()


class GptMailProvider(BaseMailProvider):
    name = "gptmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "").strip()
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key})

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        query = dict(params or {})
        resp = self.session.request(method.upper(), f"https://mail.chatgpt.org.uk{path}", params=query, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code != 200:
            raise RuntimeError(f"GPTMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in {"prefix": username, "domain": self.default_domain}.items() if value}
        data = self._request("POST" if payload else "GET", "/api/generate-email", payload=payload or None)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": str(data["email"])}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/emails", params={"email": mailbox["address"]})
        emails = data if isinstance(data, list) else data.get("emails") or []
        if not emails:
            return None
        item = max(emails, key=lambda value: (float(value.get("timestamp") or 0), str(value.get("id") or "")))
        if item.get("id"):
            item = self._request("GET", f"/api/email/{item['id']}")
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from_address") or ""), "text_content": str(item.get("content") or ""), "html_content": str(item.get("html_content") or ""), "received_at": _parse_received_at(item.get("timestamp") or item.get("created_at")), "raw": item}

    def close(self) -> None:
        self.session.close()


class MoEmailProvider(BaseMailProvider):
    name = "moemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.expiry_time = int(entry.get("expiry_time") or 0)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"MoEmail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"MoEmail {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/api/emails/generate", payload={"name": username or _random_mailbox_name(), "expiryTime": self.expiry_time, "domain": _next_domain(self.domain)}, expected=(200, 201))
        address = str(data.get("email") or "").strip()
        email_id = str(data.get("id") or data.get("email_id") or "").strip()
        if not address or not email_id:
            raise RuntimeError("MoEmail 缺少 email 或 id")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "email_id": email_id}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        email_id = str(mailbox.get("email_id") or "").strip()
        if not email_id:
            raise RuntimeError("MoEmail 缺少 email_id")
        data = self._request("GET", f"/api/emails/{email_id}")
        items = data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        _, item = max(enumerate(messages), key=lambda pair: (((_parse_received_at(pair[1].get("createdAt") or pair[1].get("created_at") or pair[1].get("receivedAt") or pair[1].get("date") or pair[1].get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()), pair[0]))
        message_id = str(item.get("id") or item.get("message_id") or item.get("_id") or "").strip()
        detail = self._request("GET", f"/api/emails/{email_id}/{message_id}") if message_id else {"message": item}
        message = detail.get("message") if isinstance(detail.get("message"), dict) else detail
        text_content, html_content = _extract_content(message)
        sender = message.get("from") or message.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(message.get("subject") or item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(message.get("createdAt") or message.get("created_at") or message.get("receivedAt") or message.get("date") or message.get("timestamp") or item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": detail}

    def close(self) -> None:
        self.session.close()


class InbucketMailProvider(BaseMailProvider):
    name = "inbucket"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.random_subdomain = bool(entry.get("random_subdomain", True))
        self.session = _create_session(conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"Inbucket 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        content_type = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def _resolve_domain(self) -> str:
        if self.domain:
            return _next_domain(self.domain)
        raise RuntimeError("Inbucket 需要至少配置一个 domain")

    def _mailbox_name(self, address: str) -> str:
        local_part, _, _ = str(address or "").partition("@")
        return local_part.strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = username or _random_mailbox_name()
        base_domain = self._resolve_domain()
        domain = f"{_random_subdomain_label()}.{base_domain}" if self.random_subdomain else base_domain
        address = f"{local_part}@{domain}"
        mailbox_name = self._mailbox_name(address)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "base_domain": base_domain,
            "mailbox_name": mailbox_name,
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        mailbox_name = str(mailbox.get("mailbox_name") or self._mailbox_name(str(mailbox.get("address") or ""))).strip()
        if not mailbox_name:
            raise RuntimeError("Inbucket 缺少 mailbox_name")
        data = self._request("GET", f"/api/v1/mailbox/{mailbox_name}")
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if not items:
            return None
        items.sort(
            key=lambda value: (
                (_parse_received_at(value.get("date")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
                str(value.get("id") or ""),
            ),
            reverse=True,
        )
        address = str(mailbox.get("address") or "").strip()
        for item in items:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            detail = self._request("GET", f"/api/v1/mailbox/{mailbox_name}/{message_id}")
            if not isinstance(detail, dict):
                continue
            header = detail.get("header") if isinstance(detail.get("header"), dict) else {}
            body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
            normalized = {
                "provider": self.name,
                "mailbox": mailbox_name,
                "message_id": message_id,
                "subject": str(detail.get("subject") or item.get("subject") or ""),
                "sender": str(detail.get("from") or item.get("from") or ""),
                "text_content": str(body.get("text") or ""),
                "html_content": str(body.get("html") or ""),
                "received_at": _parse_received_at(detail.get("date") or item.get("date")),
                "to": header.get("To") if isinstance(header, dict) else None,
                "raw": detail,
            }
            if _message_matches_email(normalized, address):
                return normalized
        return None

    def close(self) -> None:
        self.session.close()


class YydsMailProvider(BaseMailProvider):
    name = "yyds_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.subdomain = str(entry.get("subdomain") or "").strip()
        self.wildcard = bool(entry.get("wildcard"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=False)
        if resp.status_code not in expected:
            raise RuntimeError(f"YYDSMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"YYDSMail 请求失败: {data.get('errorCode') or data.get('error')}")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("items") or data.get("messages") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {"localPart": username or _random_mailbox_name()}
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        if self.subdomain:
            payload["subdomain"] = self.subdomain
        data = self._request("POST", "/accounts/wildcard" if self.wildcard else "/accounts", payload=payload)
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or data.get("temp_token") or data.get("tempToken") or data.get("access_token") or "").strip()
        if not address or not token:
            raise RuntimeError("YYDSMail 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token, "account_id": str(data.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("createdAt") or value.get("created_at") or value.get("receivedAt") or value.get("date") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or "")))
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"


class OutlookTokenError(RuntimeError):
    """refresh_token 换取 access_token 失败（凭据失效/权限不对），与“读邮件失败”区分。"""


def _clean_outlook_value(value: str) -> str:
    return str(value or "").replace("﻿", "").replace(" ", " ").strip()


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    """解析邮箱池文本，每行格式：email----password----client_id----refresh_token。"""
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = _clean_outlook_value(raw_line)
        if not line or "----" not in line:
            continue
        parts = [_clean_outlook_value(part) for part in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    return credentials


def _normalize_outlook_pool(value: Any) -> list[dict[str, str]]:
    """邮箱池既支持纯文本（每行一条），也支持已解析的对象列表。"""
    if isinstance(value, str):
        return parse_outlook_credentials(value)
    if isinstance(value, list):
        items: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                items.extend(parse_outlook_credentials(item))
            elif isinstance(item, dict):
                email = _clean_outlook_value(item.get("email") or item.get("address") or "")
                client_id = _clean_outlook_value(item.get("client_id") or "")
                refresh_token = _clean_outlook_value(item.get("refresh_token") or "")
                if "@" in email and client_id and refresh_token:
                    items.append({"email": email, "password": _clean_outlook_value(item.get("password") or ""), "client_id": client_id, "refresh_token": refresh_token})
        return items
    return []


class OutlookTokenProvider(BaseMailProvider):
    """使用 refresh_token 读取 Outlook/Hotmail 邮箱验证码。

    邮箱池在应用配置里维护（mailboxes 字段，每行 email----password----client_id----refresh_token），
    create_mailbox() 从池中取下一个未使用的邮箱，wait_for_code() 用 refresh_token 换取 access_token
    后通过 Graph/IMAP 读取最新邮件。
    """

    name = "outlook_token"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = _normalize_outlook_pool(entry.get("mailboxes") or entry.get("pool"))
        self.mode = str(entry.get("mode") or "graph").strip().lower() or "graph"
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "graph"
        self.imap_host = str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip() or OUTLOOK_DEFAULT_IMAP_HOST
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _exchange_refresh_token(self, client_id: str, refresh_token: str, scope: str) -> str:
        resp = self.session.post(
            OUTLOOK_TOKEN_URL,
            data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scope},
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.conf["user_agent"]},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error_description") or data.get("error") or resp.text[:300]
            raise OutlookTokenError(f"OutlookToken 刷新失败: HTTP {resp.status_code}, {detail}")
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise OutlookTokenError("OutlookToken 刷新响应缺少 access_token")
        return access_token

    def _access_token(self, mailbox: dict[str, Any], client_id: str, refresh_token: str, scope: str) -> str:
        """缓存 access_token 复用：避免 wait_for_code 轮询时每次都换 token 触发限流。"""
        cache = mailbox.get("_outlook_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_outlook_token_cache"] = cache
        cached = cache.get(scope)
        if isinstance(cached, tuple) and len(cached) == 2 and time.monotonic() < cached[1]:
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, refresh_token, scope)
        cache[scope] = (token, time.monotonic() + 600)
        return token

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError("OutlookToken 邮箱池为空，请在邮箱配置中导入 email----password----client_id----refresh_token")
        with _outlook_token_state_lock:
            store = _load_outlook_token_state()
            credential = next((item for item in self.pool if _outlook_entry_available(store.get(item["email"].strip().lower()))), None)
            if credential is None:
                raise RuntimeError(f"[{self.label}] OutlookToken 邮箱池暂无可用邮箱（共 {len(self.pool)} 个，已用尽或全部占用/失效），请导入新邮箱或重置池状态")
            store[credential["email"].strip().lower()] = {"state": "in_use", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()}
            _save_outlook_token_state(store)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": credential["email"],
            "label": self.label,
            "client_id": credential["client_id"],
            "refresh_token": credential["refresh_token"],
        }

    def _read_graph(self, access_token: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            OUTLOOK_GRAPH_MESSAGES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            params={"$top": self.message_limit, "$orderby": "receivedDateTime desc", "$select": "subject,receivedDateTime,from,body,bodyPreview"},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else resp.text[:300]
            raise RuntimeError(f"OutlookToken Graph 失败: HTTP {resp.status_code}, {detail}")
        items = data.get("value") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _graph_sender(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        if isinstance(sender, dict):
            address = sender.get("emailAddress") or {}
            if isinstance(address, dict):
                return str(address.get("address") or address.get("name") or "")
        return ""

    def _normalize_graph_item(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        content_type = str(body.get("contentType") or "").lower()
        content = str(body.get("content") or "")
        text_content = content if content_type != "html" else str(item.get("bodyPreview") or "")
        html_content = content if content_type == "html" else ""
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": self._graph_sender(item),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedDateTime")),
            "raw": item,
        }

    def _graph_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件（Graph 已按 receivedDateTime desc 排序，最新在前）。"""
        return [self._normalize_graph_item(mailbox, item) for item in self._read_graph(access_token)]

    def _imap_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件，最新在前。"""
        auth_string = f"user={mailbox['address']}\x01auth=Bearer {access_token}\x01\x01"
        imap = imaplib.IMAP4_SSL(self.imap_host)
        try:
            imap.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("OutlookToken IMAP select INBOX 失败")
            status, data = imap.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-self.message_limit :]
            messages: list[dict[str, Any]] = []
            for uid in reversed(uids):  # 最新在前
                status, fetched = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw_payload = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
                if raw_payload:
                    messages.append(self._parse_imap_message(mailbox, raw_payload))
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _parse_imap_message(self, mailbox: dict[str, Any], raw: bytes) -> dict[str, Any]:
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = _parse_received_at(parsedate_to_datetime(str(message.get("Date") or "")))
        except Exception:
            received = None
        plain: list[str] = []
        html: list[str] = []
        for part in (message.walk() if message.is_multipart() else [message]):
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if not payload:
                continue
            if part.get_content_type() == "text/html":
                html.append(str(payload))
            else:
                plain.append(str(payload))

        def _decode(value: str | None) -> str:
            if not value:
                return ""
            try:
                return str(make_header(decode_header(value)))
            except Exception:
                return value

        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": _decode(str(message.get("Message-ID") or "")),
            "subject": _decode(str(message.get("Subject") or "")),
            "sender": _decode(str(message.get("From") or "")),
            "text_content": "\n".join(plain).strip(),
            "html_content": "\n".join(html).strip(),
            "received_at": received,
            "raw": None,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        """拉取最近 N 封邮件（最新在前），供 wait_for_code 逐封扫描验证码。"""
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("OutlookToken mailbox 缺少 client_id 或 refresh_token")
        errors: list[str] = []
        if self.mode in {"graph", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_GRAPH_SCOPE)
                return self._graph_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "graph":
                    raise
                errors.append(f"graph: {error}")
        if self.mode in {"imap", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_IMAP_SCOPE)
                return self._imap_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "imap":
                    raise
                errors.append(f"imap: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        """轮询时遍历最近 N 封邮件，逐封提取验证码，避免最新一封是广告/安全提醒时错过验证码。"""
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            _check_cancelled()
            for message in self.fetch_recent_messages(mailbox):
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                code = _extract_code(message)
                if code:
                    seen_value.append(ref)
                    return code
                seen_refs.add(ref)
            sleep_for = max(0.2, self.conf["wait_interval"])
            until = min(deadline, time.monotonic() + sleep_for)
            while time.monotonic() < until:
                _check_cancelled()
                time.sleep(min(0.2, until - time.monotonic()))
        return None


class _ApiPlatformMailProvider(BaseMailProvider):
    """Mail.tm/Mail.gw 兼容的 API-Platform 临时邮箱实现。"""

    default_api_base = ""

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or self.default_api_base).rstrip("/")
        self.domain = _configured_domains(entry)
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        path: str,
        token: str = "",
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200, 201),
    ) -> Any:
        headers = {"Accept": "application/json", "User-Agent": self.conf["user_agent"]}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            headers=headers,
            params=params,
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        return _response_json(resp, self.name, path, expected)

    def _resolve_domain(self) -> str:
        if self.domain:
            return _next_domain(self.domain).lstrip("@").strip()
        data = self._request("GET", "/domains", params={"page": 1})
        items = _payload_items(data, ("hydra:member", "domains", "items", "data"))
        domains = [
            str(item.get("domain") or item.get("name") or "").strip().lstrip("@")
            for item in items
            if isinstance(item, dict) and item.get("isActive", True) is not False
        ]
        domains = [item for item in domains if item]
        if not domains:
            raise RuntimeError(f"{self.name} /domains 未返回可用域名")
        return _next_domain(domains)

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = re.sub(r"[^a-zA-Z0-9._-]", "", _local_part(username)) or _random_mailbox_name()
        address = f"{local_part}@{self._resolve_domain()}"
        password = secrets.token_urlsafe(18)
        account = self._request("POST", "/accounts", payload={"address": address, "password": password})
        account_id = str(_payload_value(account, "id") or "").strip()
        created_address = str(_payload_value(account, "address") or address).strip()
        token_data = self._request("POST", "/token", payload={"address": created_address, "password": password})
        token = str(_payload_value(token_data, "token") or "").strip()
        if not token:
            raise RuntimeError(f"{self.name} 创建 token 响应缺少 token")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": created_address,
            "password": password,
            "token": token,
            "account_id": account_id,
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        token = str(mailbox.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"{self.name} mailbox 缺少 token")
        data = self._request("GET", "/messages", token=token, params={"page": 1})
        items = _payload_items(data, ("hydra:member", "messages", "items", "data"))
        if not items:
            return None
        item = max(items, key=_message_sort_key)
        message_id = str(item.get("id") or item.get("message_id") or item.get("@id") or "").strip()
        if message_id.startswith("/messages/"):
            message_id = message_id.rsplit("/", 1)[-1]
        if message_id:
            item = self._request("GET", f"/messages/{quote(message_id, safe='')}", token=token)
        text_content, html_content = _extract_message_content(item)
        html_value = item.get("html")
        if isinstance(html_value, list) and not html_content:
            html_content = "".join(str(value) for value in html_value)
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": message_id,
            "subject": str(item.get("subject") or ""),
            "sender": _sender_value(item.get("from") or item.get("sender")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")),
            "raw": item,
        }


class MailTmProvider(_ApiPlatformMailProvider):
    name = "mail_tm"
    default_api_base = "https://api.mail.tm"


class MailGwProvider(_ApiPlatformMailProvider):
    name = "mail_gw"
    default_api_base = "https://api.mail.gw"


class DropMailProvider(BaseMailProvider):
    name = "dropmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://dropmail.me/api/graphql").rstrip("/")
        self.api_token = str(entry.get("token") or entry.get("api_key") or "").strip()
        self.domain = _configured_domains(entry)
        self.permanent_domain_only = entry.get("permanent_domain_only", False) is True
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_token:
            raise RuntimeError("DropMail 需要 api_key/token（请填写免费的 af_... API token）")
        resp = self.session.post(
            f"{self.api_base}/{self.api_token}",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            json={"query": query, "variables": variables or {}},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        return _graphql_data(_response_json(resp, self.name, "GraphQL", (200,)), self.name, "GraphQL")

    def _domain_id(self) -> str:
        if not self.domain:
            return ""
        data = self._request("query { domains { id name availableVia expiresAt } }")
        domains = data.get("domains") if isinstance(data.get("domains"), list) else []
        wanted = {str(value).strip().lstrip("@").lower() for value in self.domain}
        for item in domains:
            if isinstance(item, dict) and str(item.get("name") or "").strip().lower() in wanted:
                return str(item.get("id") or "").strip()
        raise RuntimeError(f"DropMail 未找到配置的 domain: {', '.join(self.domain)}")

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        # DropMail 的地址 local-part 由服务端分配，username 仅作为兼容接口保留。
        domain_id = self._domain_id()
        if domain_id:
            input_value = f'withAddress: true, domainId: "{domain_id}"'
        else:
            input_value = "withAddress: true"
        if self.permanent_domain_only:
            input_value += ", permanentDomainOnly: true"
        query = f"mutation {{ introduceSession(input: {{{input_value}}}) {{ id expiresAt addresses {{ id address restoreKey }} }} }}"
        data = self._request(query)
        session_data = data.get("introduceSession") if isinstance(data.get("introduceSession"), dict) else {}
        addresses = session_data.get("addresses") if isinstance(session_data.get("addresses"), list) else []
        address_data = addresses[0] if addresses and isinstance(addresses[0], dict) else {}
        address = str(address_data.get("address") or "").strip()
        session_id = str(session_data.get("id") or "").strip()
        if not address or not session_id:
            raise RuntimeError("DropMail 创建响应缺少 session id 或 address")
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "session_id": session_id,
            "restore_key": str(address_data.get("restoreKey") or ""),
            "expires_at": str(session_data.get("expiresAt") or ""),
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(mailbox.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("DropMail mailbox 缺少 session_id")
        query = "query ($id: ID!) { session(id: $id) { mails { id raw fromAddr toAddr receivedAt text html headerFrom headerSubject } } }"
        data = self._request(query, {"id": session_id})
        session_data = data.get("session") if isinstance(data.get("session"), dict) else {}
        items = session_data.get("mails") if isinstance(session_data.get("mails"), list) else []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, str(mailbox.get("address") or ""))]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        text_content, html_content = _extract_message_content(item)
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("headerSubject") or item.get("subject") or ""),
            "sender": str(item.get("fromAddr") or item.get("headerFrom") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedAt") or item.get("date")),
            "raw": item,
        }


class GuerrillaMailProvider(BaseMailProvider):
    name = "guerrilla_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://api.guerrillamail.com/ajax.php").strip()
        self.client_ip = str(entry.get("client_ip") or "127.0.0.1").strip()
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _restore_cookies(self, mailbox: dict[str, Any]) -> None:
        cookies = mailbox.get("cookies")
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                if value:
                    try:
                        self.session.cookies.set(str(name), str(value))
                    except Exception:
                        pass
        for name in ("PHPSESSID", "SUBSCR"):
            value = str(mailbox.get(name.lower()) or "").strip()
            if value:
                try:
                    self.session.cookies.set(name, value)
                except Exception:
                    pass

    def _remember_cookies(self, mailbox: dict[str, Any]) -> None:
        cookies: dict[str, str] = {}
        for name in ("PHPSESSID", "SUBSCR"):
            try:
                value = self.session.cookies.get(name)
            except Exception:
                value = ""
            if value:
                cookies[name] = str(value)
                mailbox[name.lower()] = str(value)
        if cookies:
            mailbox["cookies"] = cookies

    def _request(self, function: str, mailbox: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
        if mailbox:
            self._restore_cookies(mailbox)
            subscriber = str(mailbox.get("subscr") or mailbox.get("SUBSCR") or "").strip()
            if subscriber:
                params.setdefault("SUBSCR", subscriber)
        query = {"f": function, "ip": self.client_ip, "agent": self.conf["user_agent"], **params}
        resp = self.session.get(self.api_base, params=query, timeout=self.conf["request_timeout"], verify=False)
        data = _response_json(resp, self.name, function, (200,))
        if not isinstance(data, dict):
            raise RuntimeError(f"GuerrillaMail {function} 返回结构不是对象")
        if mailbox:
            self._remember_cookies(mailbox)
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("get_email_address", lang="en")
        address = str(data.get("email_addr") or "").strip()
        if username:
            user = _local_part(username)
            if user:
                data = self._request("set_email_user", email_user=user, lang="en")
                address = str(data.get("email_addr") or address).strip()
        if not address:
            raise RuntimeError("GuerrillaMail 创建响应缺少 email_addr")
        mailbox: dict[str, Any] = {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "email_timestamp": data.get("email_timestamp") or data.get("ts"),
        }
        self._remember_cookies(mailbox)
        return mailbox

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("check_email", mailbox=mailbox, seq=0)
        items = data.get("list") if isinstance(data.get("list"), list) else []
        messages = [item for item in items if isinstance(item, dict)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        message_id = str(item.get("mail_id") or item.get("id") or "").strip()
        if not message_id:
            return None
        detail = self._request("fetch_email", mailbox=mailbox, email_id=message_id)
        text_content = _value_as_text(detail.get("email_body_plain") or detail.get("mail_body_plain") or detail.get("body_plain") or detail.get("text") or detail.get("mail_excerpt"))
        html_content = _value_as_text(detail.get("email_body") or detail.get("body") or detail.get("html"))
        if not text_content and html_content:
            text_content = unescape(re.sub(r"<[^>]+>", " ", html_content))
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": message_id,
            "subject": unescape(str(detail.get("mail_subject") or item.get("mail_subject") or "")),
            "sender": str(detail.get("mail_from") or item.get("mail_from") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(detail.get("mail_timestamp") or item.get("mail_timestamp") or detail.get("mail_date") or item.get("mail_date")),
            "raw": detail,
        }


class MaildropProvider(BaseMailProvider):
    name = "maildrop"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://api.maildrop.cc/graphql").rstrip("/")
        self.default_domain = "maildrop.cc"
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.post(
            self.api_base,
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            json={"query": query, "variables": variables or {}},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        return _graphql_data(_response_json(resp, self.name, "GraphQL", (200,)), self.name, "GraphQL")

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = re.sub(r"[^a-zA-Z0-9._-]", "", _local_part(username)) or _random_mailbox_name()
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": f"{local_part}@{self.default_domain}"}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        mailbox_name = _local_part(address) or address
        data = self._request(
            "query Inbox($mailbox: String!) { inbox(mailbox: $mailbox) { id headerfrom mailfrom rcptto subject date } }",
            {"mailbox": mailbox_name},
        )
        items = data.get("inbox") if isinstance(data.get("inbox"), list) else []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        message_id = str(item.get("id") or "").strip()
        detail = self._request(
            "query Message($mailbox: String!, $id: String!) { message(mailbox: $mailbox, id: $id) { id headerfrom mailfrom rcptto subject date data html } }",
            {"mailbox": mailbox_name, "id": message_id},
        )
        message = detail.get("message") if isinstance(detail.get("message"), dict) else item
        text_content = _value_as_text(message.get("data") or message.get("text"))
        html_content = _value_as_text(message.get("html"))
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": message_id,
            "subject": str(message.get("subject") or ""),
            "sender": str(message.get("headerfrom") or message.get("mailfrom") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(message.get("date")),
            "raw": message,
        }


class CatchmailProvider(BaseMailProvider):
    name = "catchmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://api.catchmail.io").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = _configured_domains(entry, "catchmail.io")
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": self.conf["user_agent"]}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, timeout=self.conf["request_timeout"], verify=False)
        return _response_json(resp, self.name, path, (200, 204))

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        address = f"{re.sub(r'[^a-zA-Z0-9._-]', '', _local_part(username)) or _random_mailbox_name()}@{_next_domain(self.domain)}"
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        data = self._request("GET", "/api/v1/mailbox", {"address": address, "page": 1, "page_size": 50})
        items = _payload_items(data, ("messages", "items", "data"))
        messages = [item for item in items if _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if not message_id:
            return None
        detail = self._request("GET", f"/api/v1/message/{quote(message_id, safe='')}", {"mailbox": address})
        message = detail if isinstance(detail, dict) else item
        text_content, html_content = _extract_message_content(message)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": message_id,
            "subject": str(message.get("subject") or item.get("subject") or ""),
            "sender": _sender_value(message.get("from") or item.get("from")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(message.get("date") or item.get("date")),
            "raw": message,
        }


class DustMailProvider(BaseMailProvider):
    name = "dustmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://dustmail.net/api/v1").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("DustMail 需要 API Key")
        headers = {
            "Accept": "application/json",
            "User-Agent": self.conf["user_agent"],
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Key": self.api_key,
        }
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, json=payload, timeout=self.conf["request_timeout"], verify=False)
        return _response_json(resp, self.name, path, (200, 201, 204))

    @staticmethod
    def _inbox_data(data: Any) -> dict[str, Any]:
        current = _unwrap_data(data)
        if isinstance(current, list):
            return {"messages": current}
        if isinstance(current, dict):
            for key in ("inbox", "data"):
                nested = current.get(key)
                if isinstance(nested, list):
                    return {"messages": nested}
                if isinstance(nested, dict):
                    return nested
            return current
        return {}

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        # username/domain are premium-only on the free plan. Always use the
        # random free inbox unless the service itself accepts the optional field.
        data = self._request("POST", "/inbox", payload=None)
        inbox = self._inbox_data(data)
        inbox_id = str(inbox.get("id") or inbox.get("_id") or inbox.get("inbox_id") or inbox.get("inboxId") or "").strip()
        address = str(inbox.get("address") or inbox.get("email") or inbox.get("email_address") or "").strip()
        if (not inbox_id or not address) and inbox_id:
            try:
                detail = self._inbox_data(self._request("GET", f"/inbox/{quote(inbox_id, safe='')}"))
                address = address or str(detail.get("address") or detail.get("email") or detail.get("email_address") or "").strip()
            except RuntimeError:
                pass
        if not inbox_id or not address:
            raise RuntimeError("DustMail 创建响应缺少 inbox id 或 address")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "inbox_id": inbox_id}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        inbox_id = str(mailbox.get("inbox_id") or "").strip()
        if not inbox_id:
            raise RuntimeError("DustMail mailbox 缺少 inbox_id")
        data = self._inbox_data(self._request("GET", f"/inbox/{quote(inbox_id, safe='')}"))
        items = _payload_items(data, ("messages", "emails", "items", "data"))
        if not items:
            return None
        address = str(mailbox.get("address") or "")
        messages = [item for item in items if _message_matches_email(item, address)]
        item = max(messages or items, key=_message_sort_key)
        text_content, html_content = _extract_message_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("message_id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": _sender_value(item.get("from") or item.get("sender")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedAt") or item.get("received_at") or item.get("createdAt") or item.get("date")),
            "raw": item,
        }


class CleanTempMailProvider(BaseMailProvider):
    name = "cleantempmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://cleantempmail.com/api").rstrip("/")
        self.api_key = str(entry.get("api_key") or "ct-test").strip() or "ct-test"
        self.domain = _configured_domains(entry)
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None) -> Any:
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            headers={"Accept": "application/json", "User-Agent": self.conf["user_agent"], "X-API-Key": self.api_key},
            params=params,
            json=payload,
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        return _response_json(resp, self.name, path, (200, 201, 204))

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if username:
            payload["prefix"] = _local_part(username)
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        data = self._request("POST" if payload else "GET", "/generate-email", payload=payload or None)
        address = str(_payload_value(data, "email", "address", "email_address") or "").strip()
        if not address and isinstance(data, str):
            address = data.strip()
        if not address:
            raise RuntimeError("CleanTempMail 创建响应缺少 email/address")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        data = self._request("GET", "/emails", params={"email": address})
        items = _payload_items(data, ("emails", "messages", "items", "data"))
        messages = [item for item in items if _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        message_id = str(item.get("id") or item.get("_id") or item.get("message_id") or "").strip()
        detail = self._request("GET", f"/email/{quote(message_id, safe='')}") if message_id else item
        message = detail if isinstance(detail, dict) else item
        text_content, html_content = _extract_message_content(message)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": message_id,
            "subject": str(message.get("subject") or item.get("subject") or ""),
            "sender": _sender_value(message.get("from") or message.get("sender") or item.get("from")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(message.get("received_at") or message.get("receivedAt") or message.get("created_at") or message.get("date") or item.get("date")),
            "raw": message,
        }


class TestmailAppProvider(BaseMailProvider):
    name = "testmail_app"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://api.testmail.app/api/json").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.namespace = str(entry.get("namespace") or "").strip()
        self.default_domain = "inbox.testmail.app"
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.api_key or not self.namespace:
            raise RuntimeError("testmail.app 需要 api_key 和 namespace（从控制台获取）")
        tag = re.sub(r"[^a-zA-Z0-9._-]", "", _local_part(username)) or "user"
        # Keep every registration isolated from old messages and parallel runs.
        tag = f"{tag}.{secrets.token_hex(4)}"
        address = f"{self.namespace}.{tag}@{self.default_domain}"
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "namespace": self.namespace,
            "tag": tag,
            "timestamp_from": int(time.time() * 1000),
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        params = {
            "apikey": self.api_key,
            "namespace": str(mailbox.get("namespace") or self.namespace),
            "tag": str(mailbox.get("tag") or ""),
            "timestamp_from": int(mailbox.get("timestamp_from") or 0),
            "limit": 20,
        }
        resp = self.session.get(self.api_base, params=params, headers={"Accept": "application/json", "User-Agent": self.conf["user_agent"]}, timeout=self.conf["request_timeout"], verify=False)
        data = _response_json(resp, self.name, "inbox", (200,))
        if isinstance(data, dict) and str(data.get("result") or "success") == "fail":
            raise RuntimeError(f"testmail.app 收件失败: {data.get('message') or 'unknown error'}")
        items = _payload_items(data, ("emails", "messages", "items", "data"))
        address = str(mailbox.get("address") or "")
        messages = [item for item in items if _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        text_content, html_content = _extract_message_content(item)
        sender = item.get("from") or item.get("from_parsed") or item.get("sender")
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("message_id") or item.get("messageId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": _sender_value(sender),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("timestamp") or item.get("received_at") or item.get("date")),
            "raw": item,
        }


class MailiskProvider(BaseMailProvider):
    name = "mailisk"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://api.mailisk.com/api").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.namespace = str(entry.get("namespace") or "").strip()
        self.default_domain = "mailisk.net"
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, path: str, params: dict | None = None) -> Any:
        if not self.api_key or not self.namespace:
            raise RuntimeError("Mailisk 需要 api_key 和 namespace（从 dashboard 获取）")
        resp = self.session.get(
            f"{self.api_base}{path}",
            params=params,
            headers={"Accept": "application/json", "User-Agent": self.conf["user_agent"], "X-Api-Key": self.api_key},
            timeout=self.conf["request_timeout"],
            verify=False,
        )
        return _response_json(resp, self.name, path, (200,))

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.api_key or not self.namespace:
            raise RuntimeError("Mailisk 需要 api_key 和 namespace（从 dashboard 获取）")
        local_part = re.sub(r"[^a-zA-Z0-9._-]", "", _local_part(username)) or _random_mailbox_name()
        address = f"{local_part}@{self.namespace}.{self.default_domain}"
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "namespace": self.namespace}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "")
        local_part = _local_part(address)
        params = {"limit": 20, "offset": 0}
        if local_part:
            params["to_addr_prefix"] = f"{local_part}@"
        data = self._request(f"/emails/{quote(str(mailbox.get('namespace') or self.namespace), safe='')}/inbox", params)
        items = _payload_items(data, ("data", "emails", "messages", "items"))
        messages = [item for item in items if _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        text_content, html_content = _extract_message_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("message_id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": _sender_value(item.get("from") or item.get("sender")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("received_date") or item.get("received_timestamp") or item.get("received_at") or item.get("date")),
            "raw": item,
        }


class MailsacProvider(BaseMailProvider):
    name = "mailsac"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://mailsac.com").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = _configured_domains(entry, "mailsac.com")
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _request(self, path: str, params: dict | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": self.conf["user_agent"]}
        if self.api_key:
            headers["Mailsac-Key"] = self.api_key
        resp = self.session.get(f"{self.api_base}{path}", params=params, headers=headers, timeout=self.conf["request_timeout"], verify=False)
        return _response_json(resp, self.name, path, (200,))

    def _request_text(self, path: str) -> str:
        headers = {"Accept": "text/plain, text/html", "User-Agent": self.conf["user_agent"]}
        if self.api_key:
            headers["Mailsac-Key"] = self.api_key
        resp = self.session.get(f"{self.api_base}{path}", headers=headers, timeout=self.conf["request_timeout"], verify=False)
        return _response_text(resp, self.name, path)

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = re.sub(r"[^a-zA-Z0-9._-]", "", _local_part(username)) or _random_mailbox_name()
        address = f"{local_part}@{_next_domain(self.domain)}"
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        encoded_address = quote(address, safe="@")
        data = self._request(f"/api/addresses/{encoded_address}/messages")
        items = _payload_items(data, ("messages", "items", "data"))
        messages = [item for item in items if _message_matches_email(item, address)]
        if not messages:
            return None
        item = max(messages, key=_message_sort_key)
        message_id = str(item.get("_id") or item.get("id") or item.get("message_id") or "").strip()
        if not message_id:
            return None
        text_content = ""
        html_content = ""
        try:
            text_content = self._request_text(f"/api/text/{encoded_address}/{quote(message_id, safe='')}")
        except RuntimeError:
            pass
        try:
            html_content = self._request_text(f"/api/body/{encoded_address}/{quote(message_id, safe='')}")
        except RuntimeError:
            pass
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": message_id,
            "subject": str(item.get("subject") or ""),
            "sender": _sender_value(item.get("from") or item.get("sender")),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("received") or item.get("received_at") or item.get("date")),
            "raw": {"metadata": item, "text": text_content, "html": html_content},
        }


def _entries(mail_config: dict) -> list[dict]:
    result: list[dict] = []
    counters: dict[str, int] = {}
    for item in mail_config["providers"]:
        idx = len(result) + 1
        t = item.get("type", "")
        cnt = counters.get(t, 0) + 1
        counters[t] = cnt
        label = f"DDG-{cnt}" if t == "ddg_mail" else f"{t}#{idx}"
        result.append({**item, "provider_ref": f"{item['type']}#{idx}", "label": label})
    return result


def _enabled_entries(mail_config: dict) -> list[dict]:
    items = [item for item in _entries(mail_config) if item.get("enable")]
    if not items:
        raise RuntimeError("mail.providers 没有启用的 provider")
    return items


def _next_entry(mail_config: dict) -> dict:
    global provider_index
    items = _enabled_entries(mail_config)
    if len(items) == 1:
        return dict(items[0])
    with provider_lock:
        value = dict(items[provider_index % len(items)])
        provider_index = (provider_index + 1) % len(items)
        return value


def _create_provider(mail_config: dict, provider: str = "", provider_ref: str = "") -> BaseMailProvider:
    entry = next((dict(item) for item in _entries(mail_config) if provider_ref and item["provider_ref"] == provider_ref), None)
    entry = entry or next((dict(item) for item in _enabled_entries(mail_config) if provider and item["type"] == provider), None) or _next_entry(mail_config)
    conf = _config(mail_config)
    if entry["type"] == "cloudmail_gen":
        return CloudMailGenProvider(entry, conf)
    if entry["type"] == "cloudflare_temp_email":
        return CloudflareTempMailProvider(entry, conf)
    if entry["type"] == "ddg_mail":
        return DDGMailProvider(entry, conf)
    if entry["type"] == "tempmail_lol":
        return TempMailLolProvider(entry, conf)
    if entry["type"] == "duckmail":
        return DuckMailProvider(entry, conf)
    if entry["type"] == "gptmail":
        return GptMailProvider(entry, conf)
    if entry["type"] == "moemail":
        return MoEmailProvider(entry, conf)
    if entry["type"] == "inbucket":
        return InbucketMailProvider(entry, conf)
    if entry["type"] == "yyds_mail":
        return YydsMailProvider(entry, conf)
    if entry["type"] == "outlook_token":
        return OutlookTokenProvider(entry, conf)
    if entry["type"] == "mailnest":
        return MailNestProvider(entry, conf)
    if entry["type"] == "mail_tm":
        return MailTmProvider(entry, conf)
    if entry["type"] == "mail_gw":
        return MailGwProvider(entry, conf)
    if entry["type"] == "dropmail":
        return DropMailProvider(entry, conf)
    if entry["type"] == "guerrilla_mail":
        return GuerrillaMailProvider(entry, conf)
    if entry["type"] == "maildrop":
        return MaildropProvider(entry, conf)
    if entry["type"] == "catchmail":
        return CatchmailProvider(entry, conf)
    if entry["type"] == "dustmail":
        return DustMailProvider(entry, conf)
    if entry["type"] == "cleantempmail":
        return CleanTempMailProvider(entry, conf)
    if entry["type"] == "testmail_app":
        return TestmailAppProvider(entry, conf)
    if entry["type"] == "mailisk":
        return MailiskProvider(entry, conf)
    if entry["type"] == "mailsac":
        return MailsacProvider(entry, conf)
    raise RuntimeError(f"不支持的 mail.provider: {entry['type']}")


def create_mailbox(mail_config: dict, username: str | None = None) -> dict:
    _check_cancelled()
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            _check_cancelled()
            if provider_key in tried:
                continue
            tried.add(provider_key)
            mailbox = provider.create_mailbox(username)
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法创建邮箱")


def wait_for_code(mail_config: dict, mailbox: dict) -> str | None:
    _check_cancelled()
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        _check_cancelled()
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def mark_mailbox_result(mailbox: dict, *, success: bool, error: Exception | str | None = None) -> None:
    """注册流程结束后更新邮箱池状态。

    仅对 outlook_token 邮箱生效：成功标记 used；失败时若是 token 失效标记 token_invalid，
    其余失败标记 failed（保留邮箱占用以便排查，可通过重置释放）。
    """
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return
    if success:
        _set_outlook_token_state(address, "used")
        return
    reason = str(error or "").strip()
    if isinstance(error, OutlookTokenError) or "OutlookToken 刷新失败" in reason or "access_token" in reason:
        _set_outlook_token_state(address, "token_invalid", reason[:300])
    else:
        _set_outlook_token_state(address, "failed", reason[:300])


def release_mailbox(mailbox: dict) -> None:
    """把 outlook_token 邮箱从 in_use 释放回未使用（用于流程主动放弃且未消费验证码时）。"""
    if str(mailbox.get("provider") or "") != OutlookTokenProvider.name:
        return
    _release_outlook_token_state(str(mailbox.get("address") or ""))


def get_existing_mailbox(mail_config: dict, email: str) -> dict:
    """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            if hasattr(provider, "get_existing_mailbox"):
                mailbox = provider.get_existing_mailbox(email)
                return mailbox
            else:
                raise RuntimeError(f"邮箱提供商 {provider.name} 不支持查询已有邮箱")
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法查询已有邮箱")
