from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Lock, Thread
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from services.account_health import (
    ERROR_DISABLED,
    ERROR_INVALID_CREDENTIALS,
    ERROR_INVALID_TOKEN,
    ERROR_NEEDS_RELOGIN,
    ERROR_NEEDS_VERIFICATION,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
    ERROR_UPSTREAM_RATE_LIMITED,
    HEALTH_STATE_DISABLED,
    HEALTH_STATE_HEALTHY,
    HEALTH_STATE_IMAGE_QUOTA_UNKNOWN,
    HEALTH_STATE_INVALID_CONFIRMED,
    HEALTH_STATE_INVALID_PENDING,
    HEALTH_STATE_NEEDS_RELOGIN,
    HEALTH_STATE_NEEDS_VERIFICATION,
    HEALTH_STATE_RATE_LIMITED,
    HEALTH_STATE_TRANSIENT_ERROR,
    HEALTH_STATE_UNKNOWN_ERROR,
    HEALTH_STATES,
    TokenRefreshError,
    classify_account_error,
)
from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


INVALID_TOKEN_ERROR_MARKERS = (
    "token invalidated",
    "token_invalidated",
    "token_revoked",
    "invalidated oauth token",
    "invalid access token",
    "invalid_grant",
    "oauth_refresh_http_400",
    "oauth_refresh_http_401",
    "unauthorized",
)

DISABLED_ACCOUNT_ERROR_MARKERS = (
    "account_deactivated",
    "account disabled",
    "account is disabled",
    "user_deactivated",
    "deactivated account",
)


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _NEW_ACCOUNT_INVALID_GRACE_SECONDS = 10 * 60
    _INVALID_CONFIRM_SECONDS = 30
    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_SECONDS = 3 * 24 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS = 6 * 60 * 60
    _REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE = 3
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_REDIRECT_URI = "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()
    # 重新登录进度追踪
    _relogin_progress: dict[str, dict] = {}
    _relogin_progress_lock = Lock()

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._token_refresh_lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}
        self._token_aliases: dict[str, str] = {}
        self._relogin_inflight: set[str] = set()
        self._health_check_inflight: set[str] = set()
        self._cumulative_total = self._load_cumulative_total()

    def _get_cumulative_file(self) -> Path:
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        try:
            payload = str(token or "").split(".")[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            import base64
            import json
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    @staticmethod
    def _health_state_for_status(status: object, image_quota_unknown: bool = False) -> str:
        normalized = str(status or "正常").strip()
        if normalized == "禁用":
            return HEALTH_STATE_DISABLED
        if normalized == "限流":
            return HEALTH_STATE_RATE_LIMITED
        if normalized == "异常":
            return HEALTH_STATE_INVALID_CONFIRMED
        if image_quota_unknown:
            return HEALTH_STATE_IMAGE_QUOTA_UNKNOWN
        return HEALTH_STATE_HEALTHY

    @staticmethod
    def _health_state_blocks_requests(account: dict) -> bool:
        if not isinstance(account, dict):
            return True
        return str(account.get("health_state") or "").strip() in {
            HEALTH_STATE_INVALID_PENDING,
            HEALTH_STATE_INVALID_CONFIRMED,
            HEALTH_STATE_NEEDS_RELOGIN,
            HEALTH_STATE_NEEDS_VERIFICATION,
            HEALTH_STATE_DISABLED,
        }

    @classmethod
    def _health_cooldown_active(cls, account: dict, now: datetime | None = None) -> bool:
        if account.get("health_state") == HEALTH_STATE_IMAGE_QUOTA_UNKNOWN:
            return False
        retry_at = cls._parse_time(account.get("health_retry_at"))
        return retry_at is not None and retry_at > (now or datetime.now(timezone.utc))

    @classmethod
    def _is_image_account_available(cls, account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if cls._health_state_blocks_requests(account) or cls._health_cooldown_active(account):
            return False
        if int(account.get("invalid_count") or 0) > 0:
            return False
        if account.get("image_quota_unknown"):
            return False
        return int(account.get("quota") or 0) > 0

    @staticmethod
    def _is_invalid_token_error(error: object) -> bool:
        return classify_account_error(error).kind == ERROR_INVALID_TOKEN

    @staticmethod
    def _is_disabled_account_error(error: object) -> bool:
        return classify_account_error(error).kind == ERROR_DISABLED

    @classmethod
    def _is_account_eligible_for_text(cls, account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "异常"}:
            return False
        if cls._health_state_blocks_requests(account) or cls._health_cooldown_active(account):
            return False
        if int(account.get("invalid_count") or 0) > 0:
            return False
        return bool(account.get("access_token"))

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        return str(value or "web").strip().lower() or "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key) or raw

    def _search_account_type(self, payload: object) -> str | None:
        if isinstance(payload, dict):
            for key in ("plan_type", "account_plan", "account_type", "subscription_type", "type"):
                plan = self._normalize_account_type(payload.get(key))
                if plan:
                    return plan
            for value in payload.values():
                plan = self._search_account_type(value)
                if plan:
                    return plan
        elif isinstance(payload, list):
            for value in payload:
                plan = self._search_account_type(value)
                if plan:
                    return plan
        return None

    @staticmethod
    def _coerce_bool(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return default

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        normalized["type"] = normalized.get("type") or "free"
        status = str(normalized.get("status") or "正常").strip()
        status_aliases = {
            "姝ｅ父": "正常",
            "闄愭祦": "限流",
            "寮傚父": "异常",
            "绂佺敤": "禁用",
        }
        normalized["status"] = status_aliases.get(status, status if status in {"正常", "禁用", "限流", "异常"} else "正常")
        try:
            normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        except (TypeError, ValueError, OverflowError):
            normalized["quota"] = 0
        normalized["image_quota_unknown"] = self._coerce_bool(normalized.get("image_quota_unknown"), False)
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        try:
            normalized["success"] = int(normalized.get("success") or 0)
        except (TypeError, ValueError, OverflowError):
            normalized["success"] = 0
        try:
            normalized["fail"] = int(normalized.get("fail") or 0)
        except (TypeError, ValueError, OverflowError):
            normalized["fail"] = 0
        try:
            normalized["invalid_count"] = max(0, int(normalized.get("invalid_count") or 0))
        except (TypeError, ValueError, OverflowError):
            normalized["invalid_count"] = 0
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["last_check_at"] = normalized.get("last_check_at") or None
        normalized["last_successful_check_at"] = normalized.get("last_successful_check_at") or None
        normalized["image_quota_error"] = self._short_error(normalized.get("image_quota_error"), 500) or None
        normalized["health_reason"] = str(normalized.get("health_reason") or "").strip() or None
        normalized["health_source"] = str(normalized.get("health_source") or "").strip() or None
        normalized["health_error_kind"] = str(normalized.get("health_error_kind") or "").strip() or None
        normalized["health_error_code"] = str(normalized.get("health_error_code") or "").strip() or None
        normalized["health_updated_at"] = normalized.get("health_updated_at") or None
        normalized["health_retry_at"] = normalized.get("health_retry_at") or None
        try:
            normalized["health_failure_count"] = max(0, int(normalized.get("health_failure_count") or 0))
        except (TypeError, ValueError, OverflowError):
            normalized["health_failure_count"] = 0
        health_state = str(normalized.get("health_state") or "").strip()
        if health_state not in HEALTH_STATES:
            if normalized["invalid_count"] > 0 and normalized["status"] == "正常":
                health_state = HEALTH_STATE_INVALID_PENDING
            else:
                health_state = self._health_state_for_status(normalized["status"], normalized["image_quota_unknown"])
        normalized["health_state"] = health_state
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        return normalized

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountService._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str) -> int | None:
        exp = cls._jwt_exp(access_token)
        if exp <= 0:
            return None
        return exp - int(time.time())

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        try:
            iat = int(cls._decode_jwt_payload(access_token).get("iat") or 0)
        except (TypeError, ValueError):
            return None
        if iat <= 0:
            return None
        return datetime.fromtimestamp(iat, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    def _record_token_refresh_error(self, access_token: str, event: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_token_refresh_error"] = str(error or "refresh token failed")
            next_item["last_token_refresh_error_at"] = now
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[resolved] = account
                self._save_accounts()
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
        )

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _recent_refresh_token_keepalive_error(self, account: dict, now: datetime) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (now - last_error_at).total_seconds() < self._REFRESH_TOKEN_KEEPALIVE_ERROR_BACKOFF_SECONDS

    @staticmethod
    def _short_error(error: object, limit: int = 500) -> str:
        return str(error or "").strip()[:limit]

    def _record_account_health_failure(
        self,
        access_token: str,
        event: str,
        error: object,
        classification=None,
    ) -> dict | None:
        classification = classification or classify_account_error(error)
        now = datetime.now(timezone.utc)
        reason = self._short_error(classification.message or error)
        retry_at = None
        if classification.retry_after_seconds:
            retry_at = (now + timedelta(seconds=classification.retry_after_seconds)).isoformat()

        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_check_at"] = now.isoformat()
            next_item["last_refresh_error"] = reason or "account check failed"
            next_item["last_refresh_error_at"] = now.isoformat()
            next_item["health_error_kind"] = classification.kind
            next_item["health_error_code"] = classification.code
            next_item["health_reason"] = reason or classification.code
            next_item["health_source"] = event
            next_item["health_updated_at"] = now.isoformat()
            next_item["health_retry_at"] = retry_at
            next_item["health_failure_count"] = int(next_item.get("health_failure_count") or 0) + 1
            if classification.kind == ERROR_DISABLED:
                next_item["status"] = "禁用"
                next_item["health_state"] = HEALTH_STATE_DISABLED
                next_item["quota"] = 0
                next_item["image_quota_unknown"] = False
                next_item["health_retry_at"] = None
            elif classification.kind == ERROR_NEEDS_RELOGIN:
                next_item["status"] = "异常"
                next_item["health_state"] = HEALTH_STATE_NEEDS_RELOGIN
            elif classification.kind == ERROR_NEEDS_VERIFICATION:
                next_item["status"] = "异常"
                next_item["health_state"] = HEALTH_STATE_NEEDS_VERIFICATION
            elif classification.kind == ERROR_UPSTREAM_RATE_LIMITED:
                next_item["health_state"] = HEALTH_STATE_RATE_LIMITED
            elif classification.kind == ERROR_TRANSIENT:
                next_item["health_state"] = HEALTH_STATE_TRANSIENT_ERROR
            elif classification.kind == ERROR_INVALID_CREDENTIALS:
                next_item["status"] = "异常"
                next_item["health_state"] = HEALTH_STATE_NEEDS_RELOGIN
            else:
                next_item["health_state"] = HEALTH_STATE_UNKNOWN_ERROR
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[resolved] = account
                self._save_accounts()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "账号健康检查失败",
            {
                "source": event,
                "token": anonymize_token(access_token),
                "kind": classification.kind,
                "code": classification.code,
                "status_code": classification.status_code,
                "retry_after_seconds": classification.retry_after_seconds,
                "error": reason,
            },
        )
        return dict(account) if account is not None else None

    def _record_account_health_success(
        self,
        access_token: str,
        event: str,
        result: dict[str, Any] | None,
    ) -> dict | None:
        """记录一次成功的远程检查，并把图片额度未知与认证成功分开保存。

        远程检查成功只代表 token/账号接口可用；如果图片额度接口没有返回
        ``image_gen``，不能把旧额度覆盖成 0，也不能把账号误判为限流。
        """
        now = datetime.now(timezone.utc).isoformat()
        payload = dict(result) if isinstance(result, dict) else {}

        remove_limited = False
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return None

            next_item = {**current, **payload}
            next_item["health_source"] = event
            image_quota_unknown = self._coerce_bool(payload.get("image_quota_unknown"), False)
            incoming_status = str(payload.get("status") or "").strip()
            is_deactivated = self._coerce_bool(payload.get("is_deactivated"), False)

            if is_deactivated or incoming_status == "禁用":
                next_item["status"] = "禁用"
                next_item["quota"] = 0
                next_item["image_quota_unknown"] = False
                next_item["health_state"] = HEALTH_STATE_DISABLED
                next_item["health_error_kind"] = ERROR_DISABLED
                next_item["health_error_code"] = "account_deactivated"
                next_item["health_reason"] = "account is disabled"
                next_item["health_retry_at"] = None
            elif image_quota_unknown:
                # 认证已成功，但图片额度当前不可确认。保留上一次已知额度，
                # 并将主状态恢复为正常，避免把未知当成“额度为 0”。
                next_item["status"] = "正常"
                next_item["quota"] = current.get("quota", 0)
                next_item["image_quota_unknown"] = True
                next_item["health_state"] = HEALTH_STATE_IMAGE_QUOTA_UNKNOWN
                next_item["health_error_kind"] = ERROR_TRANSIENT if payload.get("image_quota_error") else None
                next_item["health_error_code"] = "image_quota_unknown" if payload.get("image_quota_error") else None
                next_item["health_reason"] = self._short_error(payload.get("image_quota_error"), 500) or "image quota is unavailable"
                next_item["health_source"] = f"{event}:image_quota"
                next_item["health_retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                next_item["image_quota_error"] = self._short_error(payload.get("image_quota_error"), 500) or None
            else:
                if incoming_status not in {"正常", "限流", "异常", "禁用"}:
                    if "quota" in payload:
                        incoming_status = "限流" if int(payload.get("quota") or 0) == 0 else "正常"
                    else:
                        incoming_status = str(current.get("status") or "正常")
                next_item["status"] = incoming_status
                if "quota" in payload:
                    next_item["quota"] = payload.get("quota")
                next_item["image_quota_unknown"] = False
                next_item["image_quota_error"] = None
                next_item["health_state"] = (
                    HEALTH_STATE_RATE_LIMITED if incoming_status == "限流" else HEALTH_STATE_HEALTHY
                )
                next_item["health_error_kind"] = None
                next_item["health_error_code"] = None
                next_item["health_reason"] = None
                next_item["health_retry_at"] = None

            next_item["last_check_at"] = now
            next_item["last_successful_check_at"] = now
            next_item["health_updated_at"] = now
            next_item["health_failure_count"] = 0
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = (
                next_item.get("image_quota_error") if image_quota_unknown else None
            )
            next_item["last_refresh_error_at"] = now if image_quota_unknown and next_item.get("image_quota_error") else None

            account = self._normalize_account(next_item)
            if account is None:
                return None
            self._accounts[resolved] = account
            self._save_accounts()
            remove_limited = account.get("status") == "限流" and config.auto_remove_rate_limited_accounts

        if remove_limited:
            self.delete_accounts([access_token])
            return None

        return dict(account)

    @staticmethod
    def _classify_password_login_result(result: dict[str, Any]) -> tuple[Any, str]:
        """把密码登录返回的错误码/响应体转换为统一异常分类。"""
        detail = result.get("detail")
        error_code = str(result.get("error") or "").strip()
        status_code = None
        message = error_code
        if isinstance(detail, dict):
            raw_error = detail.get("error")
            if isinstance(raw_error, dict):
                error_code = str(raw_error.get("code") or error_code).strip()
                message = str(raw_error.get("message") or message).strip()
            status_value = detail.get("status_code") or detail.get("status")
            try:
                status_code = int(status_value) if status_value is not None else None
            except (TypeError, ValueError):
                status_code = None
        error = SimpleNamespace(
            error_code=error_code,
            status_code=status_code,
            detail=message or str(detail or ""),
        )
        return classify_account_error(error), message or error_code

    def _refresh_token_keepalive_anchor(self, account: dict) -> datetime | None:
        return (
            self._parse_time(account.get("last_token_refresh_at"))
            or self._token_issued_at(str(account.get("access_token") or ""))
            or self._parse_time(account.get("created_at"))
        )

    def _refresh_token_keepalive_due_at(self, account: dict, now: datetime) -> datetime | None:
        if not str(account.get("refresh_token") or "").strip():
            return None
        if account.get("status") == "禁用":
            return None
        if self._recent_refresh_token_keepalive_error(account, now):
            return None
        anchor = self._refresh_token_keepalive_anchor(account)
        if anchor is None:
            return now
        due_at = anchor + timedelta(seconds=self._REFRESH_TOKEN_KEEPALIVE_SECONDS)
        return due_at if due_at <= now else None

    def _request_access_token_refresh(self, refresh_token: str, account: dict | None = None) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        session = requests.Session(**proxy_settings.build_session_kwargs(account=account, impersonate="chrome110", verify=True))
        try:
            response = session.post(
                self._OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": self._OAUTH_USER_AGENT,
                },
                json={
                    "client_id": self._OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "redirect_uri": self._OAUTH_REDIRECT_URI,
                    "refresh_token": refresh_token,
                },
                timeout=60,
            )
            try:
                data = response.json() if response.text else {}
            except (ValueError, TypeError):
                data = {}
            if response.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
                detail = ""
                if isinstance(data, dict):
                    detail = str(data.get("error_description") or data.get("error") or data.get("message") or "")
                detail = detail or self._safe_response_text(response)
                raise TokenRefreshError(
                    response.status_code,
                    str(data.get("error") or data.get("code") or "") if isinstance(data, dict) else "",
                    detail,
                    getattr(response, "headers", {}).get("Retry-After"),
                )
            return {
                "access_token": str(data.get("access_token") or "").strip(),
                "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
                "id_token": str(data.get("id_token") or "").strip(),
            }
        finally:
            session.close()

    def _apply_refreshed_tokens(self, old_access_token: str, token_data: dict, event: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                return old_token
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token

            next_item = dict(current)
            next_item["access_token"] = new_token
            if token_data.get("refresh_token"):
                next_item["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            if token_data.get("id_token"):
                next_item["id_token"] = str(token_data.get("id_token") or "").strip()
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            next_item["health_failure_count"] = 0
            next_item["health_error_kind"] = None
            next_item["health_error_code"] = None
            next_item["health_reason"] = None
            next_item["health_source"] = event
            next_item["health_updated_at"] = now
            next_item["health_retry_at"] = None
            if next_item.get("status") in {"异常", "禁用"}:
                next_item["status"] = "正常"
            next_item["health_state"] = (
                HEALTH_STATE_IMAGE_QUOTA_UNKNOWN
                if next_item.get("image_quota_unknown")
                else (HEALTH_STATE_RATE_LIMITED if next_item.get("status") == "限流" else HEALTH_STATE_HEALTHY)
            )

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            if rotated:
                self._accounts.pop(old_token, None)
                self._token_aliases[old_token] = new_token
                if old_token in self._health_check_inflight:
                    self._health_check_inflight.add(new_token)
                old_inflight = int(self._image_inflight.pop(old_token, 0))
                if old_inflight:
                    self._image_inflight[new_token] = int(self._image_inflight.get(new_token, 0)) + old_inflight
            self._accounts[new_token] = account
            self._save_accounts()
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def refresh_access_token(self, access_token: str, *, force: bool = False, event: str = "refresh_access_token") -> str:
        if not access_token:
            return ""
        with self._token_refresh_lock:
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                return access_token
            active_token = str(account.get("access_token") or resolved_token or access_token)
            if not self._token_needs_refresh(active_token, force=force):
                return active_token
            refresh_token = str(account.get("refresh_token") or "").strip()
            if not refresh_token:
                return active_token
            if not force and self._recent_token_refresh_error(account):
                return active_token
            try:
                token_data = self._request_access_token_refresh(refresh_token, account)
            except Exception as exc:
                error_str = str(exc or "")
                classification = classify_account_error(exc)
                self._record_token_refresh_error(active_token, event, error_str)
                if classification.kind == ERROR_DISABLED:
                    self._record_account_health_failure(active_token, event, exc, classification)
                    return ""
                if classification.kind == ERROR_NEEDS_RELOGIN or classification.kind == ERROR_INVALID_CREDENTIALS:
                    self._record_account_health_failure(active_token, event, exc, classification)
                    if config.auto_relogin_after_refresh:
                        self._start_password_relogin_if_possible(active_token, account, event)
                    return ""
                if classification.kind == ERROR_INVALID_TOKEN:
                    should_remove = self._record_invalid_token_seen(
                        active_token,
                        event,
                        error_str,
                        defer_invalid_removal=True,
                    )
                    if should_remove:
                        self._remove_confirmed_invalid_token(active_token, event, quiet=True)
                    return ""
                self._record_account_health_failure(active_token, event, exc, classification)
                return active_token
            return self._apply_refreshed_tokens(active_token, token_data, event)

    def _start_password_relogin_if_possible(self, access_token: str, account: dict | None, event: str, progress_id: str | None = None) -> bool:
        if not isinstance(account, dict):
            return False
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "").strip()
        if not email or not password:
            return False
        resolved = self.resolve_access_token(access_token)
        with self._lock:
            if resolved in self._relogin_inflight:
                return False
            self._relogin_inflight.add(resolved)
        Thread(
            target=self._password_re_login_worker,
            args=(resolved, email, password, event, progress_id),
            daemon=True,
            name="account-password-relogin",
        ).start()
        return True

    def _password_re_login_worker(self, access_token: str, email: str, password: str, event: str, progress_id: str | None = None) -> None:
        try:
            self._password_re_login_thread(access_token, email, password, event, progress_id)
        finally:
            with self._lock:
                self._relogin_inflight.discard(self._resolve_access_token_locked(access_token))
                self._relogin_inflight.discard(access_token)

    def _password_re_login_thread(self, access_token: str, email: str, password: str, event: str, progress_id: str | None = None) -> None:
        """密码重新登录线程入口"""
        try:
            result = self._login_with_password(email, password)
            if result.get("ok"):
                # 登录成功，更新账号
                new_access_token = result.get("access_token", "")
                new_refresh_token = result.get("refresh_token", "")
                new_id_token = result.get("id_token", "")
                new_expires_at = result.get("expires_at")

                # 构建 token_data 供 _apply_refreshed_tokens 使用
                token_data = {
                    "access_token": new_access_token,
                    "refresh_token": new_refresh_token,
                    "id_token": new_id_token,
                }

                # 使用 _apply_refreshed_tokens 更新账号（处理 token 别名）
                new_token = self._apply_refreshed_tokens(access_token, token_data, f"{event}:password_relogin")

                # 额外更新 source_type 和 status（静默，避免重复日志）
                self.update_account(new_token, {
                    "source_type": result.get("source_type", "password"),
                    "status": "正常",
                    "health_state": HEALTH_STATE_HEALTHY,
                    "health_reason": None,
                    "health_source": f"{event}:password_relogin",
                    "health_error_kind": None,
                    "health_error_code": None,
                    "health_retry_at": None,
                    "health_failure_count": 0,
                    "last_successful_check_at": datetime.now(timezone.utc).isoformat(),
                }, quiet=True)

                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "更新账号",
                    {
                        "source": event,
                        "old_token": anonymize_token(access_token),
                        "new_token": anonymize_token(new_access_token),
                        "email": email,
                        "status": "成功",
                    },
                )
                if progress_id:
                    self.update_relogin_progress(progress_id, access_token, "成功")
            else:
                # 登录失败
                error_type = result.get("error", "")
                classification, message = self._classify_password_login_result(result)
                login_error = SimpleNamespace(
                    error_code=classification.code,
                    status_code=classification.status_code,
                    detail=message or error_type,
                )
                self._record_account_health_failure(
                    access_token,
                    f"{event}:password_relogin_failed",
                    login_error,
                    classification,
                )
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "密码重新登录失败",
                    {
                        "source": event,
                        "token": anonymize_token(access_token),
                        "email": email,
                        "error": error_type,
                        "kind": classification.kind,
                        "code": classification.code,
                    },
                )
                if progress_id:
                    progress_status = {
                        ERROR_DISABLED: "禁用",
                        ERROR_NEEDS_VERIFICATION: "需验证码",
                        ERROR_NEEDS_RELOGIN: "需重新登录",
                        ERROR_INVALID_CREDENTIALS: "凭据错误",
                        ERROR_TRANSIENT: "临时错误",
                    }.get(classification.kind, "异常")
                    self.update_relogin_progress(progress_id, access_token, progress_status, message or error_type)
        except Exception as exc:
            classification = classify_account_error(exc)
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "更新账号",
                {
                    "source": event,
                    "token": anonymize_token(access_token),
                    "email": email,
                    "status": "失败",
                    "kind": classification.kind,
                    "code": classification.code,
                    "error": self._short_error(exc),
                },
            )
            self._record_account_health_failure(
                access_token,
                f"{event}:password_relogin_exception",
                exc,
                classification,
            )
            if progress_id:
                progress_status = "临时错误" if classification.kind == ERROR_TRANSIENT else "异常"
                self.update_relogin_progress(progress_id, access_token, progress_status, self._short_error(exc))

    def _login_with_password(self, email: str, password: str) -> dict:
        """通过邮箱+密码登录，返回 {access_token, refresh_token, id_token, ...}"""
        from curl_cffi import requests

        # 常量
        auth_base = "https://auth.openai.com"
        platform_oauth_audience = "https://api.openai.com/v1"
        platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
        platform_oauth_client_id = self._OAUTH_CLIENT_ID
        platform_oauth_redirect_uri = "https://platform.openai.com/auth/callback"
        user_agent = self._OAUTH_USER_AGENT

        # 创建 session
        session_kwargs = {"impersonate": "chrome110", "verify": False}
        proxy = config.get_proxy_settings()
        if proxy:
            session_kwargs["proxy"] = proxy
        session = requests.Session(**session_kwargs)

        try:
            device_id = str(uuid.uuid4())

            # ─── 方式2: OAuth authorize 流程 ──────────────────────────
            # 使用 Platform Client + PKCE（与注册流程相同）

            from utils.pkce import generate_pkce
            code_verifier, code_challenge = generate_pkce()

            # ② 发起 OAuth authorize 请求 (使用 Platform Client + PKCE)
            session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
            session.cookies.set("oai-did", device_id, domain="auth.openai.com")
            params = {
                "issuer": auth_base,
                "client_id": platform_oauth_client_id,
                "audience": platform_oauth_audience,
                "redirect_uri": platform_oauth_redirect_uri,
                "device_id": device_id,
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
            authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
            resp = session.get(
                authorize_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "user-agent": user_agent,
                    "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                    "referer": "https://platform.openai.com/",
                },
                allow_redirects=True,
                timeout=30,
            )

            if resp.status_code not in (200, 302):
                return {"ok": False, "error": f"authorize_failed_{resp.status_code}", "detail": {"url": resp.url, "text": resp.text[:500]}}

            # 检测最终 URL 是否指向错误页面
            final_url = str(resp.url)
            if "/error" in final_url and "payload=" in final_url:
                from urllib.parse import parse_qs, urlparse
                try:
                    parsed_query = parse_qs(urlparse(final_url).query)
                    error_payload_b64 = parsed_query.get("payload", [""])[0]
                    error_payload_b64 += "=" * ((4 - len(error_payload_b64) % 4) % 4)
                    error_payload = json.loads(base64.b64decode(error_payload_b64))
                    error_code = error_payload.get("errorCode", "")
                    if error_code == "rate_limit_exceeded":
                        return {"ok": False, "error": "rate_limit_exceeded", "detail": error_payload}
                    else:
                        return {"ok": False, "error": f"authorize_error_{error_code}", "detail": error_payload}
                except Exception as e:
                    return {"ok": False, "error": "authorize_redirect_error", "detail": {"url": final_url, "parse_error": str(e)}}

            # ③ 提交密码验证
            login_headers = {
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9",
                "content-type": "application/json",
                "origin": auth_base,
                "priority": "u=1, i",
                "user-agent": user_agent,
                "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer": f"{auth_base}/email-verification",
                "oai-device-id": device_id,
            }

            # 添加 sentinel token
            try:
                from utils.sentinel import build_sentinel_token
                sentinel_val, oai_sc_val = build_sentinel_token(session, device_id, "password_verify")
                login_headers["openai-sentinel-token"] = sentinel_val
                if oai_sc_val:
                    session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
            except Exception:
                pass

            login_resp = session.post(
                f"{auth_base}/api/accounts/password/verify",
                headers=login_headers,
                json={"password": password},
                timeout=30,
            )

            login_data = {}
            try:
                login_data = login_resp.json() if login_resp.text else {}
            except Exception:
                pass

            if login_resp.status_code != 200:
                error_code = login_data.get("error", {}).get("code", "")
                error_msg = login_data.get("error", {}).get("message", "")
                if error_code == "unsupported_country_region_territory":
                    return {"ok": False, "error": "unsupported_country_region_territory", "detail": login_data}
                elif error_code == "invalid_state":
                    return {"ok": False, "error": "invalid_state", "detail": login_data}
                elif "Invalid credentials" in error_msg or "wrong password" in error_msg.lower():
                    return {"ok": False, "error": "invalid_password", "detail": login_data}
                return {"ok": False, "error": f"password_verify_failed_{login_resp.status_code}", "detail": login_data}

            # 获取 authorization code
            continue_url = str(login_data.get("continue_url") or "").strip()
            auth_code = ""
            if continue_url:
                from urllib.parse import parse_qs, urlparse
                parsed_params = parse_qs(urlparse(continue_url).query)
                auth_code = str((parsed_params.get("code") or [""])[0]).strip()

            # ─── 处理邮箱 OTP 验证 ──────────────────────────
            if not auth_code:
                page_type = ""
                page_info = login_data.get("page")
                if isinstance(page_info, dict):
                    page_type = str(page_info.get("type") or "")

                if page_type == "email_otp_verification":
                    # 需要验证码才能登录，直接标记为账号异常
                    return {"ok": False, "error": "need_verification_code", "detail": login_data}
                else:
                    return {"ok": False, "error": "no_auth_code", "detail": login_data}

            # ④ 用 code 换 token (使用 Platform Client + code_verifier，与注册流程相同)
            platform_base = "https://platform.openai.com"
            token_resp = session.post(
                f"{auth_base}/api/accounts/oauth/token",
                headers={
                    "accept": "*/*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "auth0-client": platform_auth0_client,
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": platform_base,
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": f"{platform_base}/",
                    "sec-ch-ua": '"Chromium";v="145", "Google Chrome";v="145", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site",
                    "user-agent": user_agent,
                },
                json={
                    "client_id": platform_oauth_client_id,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": platform_oauth_redirect_uri,
                },
                verify=False,
                timeout=60,
            )

            token_data = {}
            try:
                token_data = token_resp.json() if token_resp.text else {}
            except Exception:
                pass

            if token_resp.status_code != 200 or not token_data.get("access_token"):
                return {"ok": False, "error": "token_exchange_failed", "detail": token_data}

            access_token = str(token_data.get("access_token") or "").strip()
            refresh_token = str(token_data.get("refresh_token") or "").strip()
            id_token = str(token_data.get("id_token") or "").strip()

            # ⑤ 用 access_token 获取用户信息
            user_info = {}
            try:
                me_resp = session.get(
                    "https://chatgpt.com/backend-api/me",
                    headers={
                        "accept": "application/json",
                        "authorization": f"Bearer {access_token}",
                        "user-agent": user_agent,
                    },
                    timeout=30,
                )
                if me_resp.status_code == 200:
                    user_info = me_resp.json() if me_resp.text else {}
            except Exception:
                pass

            # 解析 JWT payload
            jwt_payload = self._decode_jwt_payload(access_token)

            email_from_jwt = str(jwt_payload.get("https://api.openai.com/profile", {}).get("email") or "").strip()
            account_id_from_jwt = str(
                jwt_payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or ""
            ).strip()

            account_info = user_info.get("account") if isinstance(user_info.get("account"), dict) else {}
            result = {
                "ok": True,
                "email": email_from_jwt or email,
                "account_id": account_id_from_jwt or account_info.get("account_id", ""),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expires_at": jwt_payload.get("exp"),
                "source_type": "password",
            }

            return result

        finally:
            session.close()

    def list_expiring_access_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for account in self._accounts.values()
                if str(account.get("refresh_token") or "").strip()
                and (token := str(account.get("access_token") or "").strip())
                and self._token_needs_refresh(token)
            ]

    def list_refresh_token_keepalive_tokens(self) -> list[str]:
        now = datetime.now(timezone.utc)
        due_items: list[tuple[datetime, str]] = []
        with self._lock:
            for account in self._accounts.values():
                due_at = self._refresh_token_keepalive_due_at(account, now)
                token = str(account.get("access_token") or "").strip()
                if due_at is not None and token:
                    due_items.append((due_at, token))
        due_items.sort(key=lambda item: item[0])
        return [token for _, token in due_items[: self._REFRESH_TOKEN_KEEPALIVE_BATCH_SIZE]]

    def keepalive_refresh_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        for access_token in access_tokens:
            before = self.resolve_access_token(access_token)
            after = self.refresh_access_token(before, force=True, event="refresh_token_keepalive")
            account = self.get_account(after)
            if account and str(account.get("last_token_refresh_error") or "").strip():
                errors.append({
                    "token": anonymize_token(before),
                    "error": str(account.get("last_token_refresh_error") or "refresh token failed"),
                })
                continue
            if account:
                refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
            "relogined": 0,
        }

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types):
                    ready_count = len(self._list_ready_candidate_tokens(set(), plan_type, source_type, plan_types))
                    available_count = len(self._list_available_candidate_tokens(set(), plan_type, source_type, plan_types))
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "图片账号取号失败",
                        {
                            "plan_type": plan_type,
                            "source_type": source_type,
                            "plan_types": list(plan_types or []),
                            "excluded": len(excluded_tokens or set()),
                            "ready": ready_count,
                            "available": available_count,
                        },
                    )
                    self._schedule_auto_start_register_if_needed("no_ready_image_quota")
                    raise RuntimeError(
                        f"no available {plan_type or source_type or ''} image quota".replace("  ", " ").strip()
                        if plan_type or source_type else "no available image quota"
                    )
                tokens = self._list_available_candidate_tokens(excluded_tokens, plan_type, source_type, plan_types)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def _schedule_auto_start_register_if_needed(self, reason: str = "") -> None:
        if not config.auto_start_register_enabled:
            return

        def worker() -> None:
            try:
                from services.register_service import register_service

                result = register_service.auto_start_if_quota_low(config.auto_start_register_min_quota)
                if result.get("started"):
                    log_service.add(
                        LOG_TYPE_ACCOUNT,
                        "auto_start_register",
                        {"reason": reason, **result},
                    )
            except Exception as exc:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "auto_start_register_failed",
                    {"reason": reason, "error": str(exc)},
                )

        Thread(target=worker, name="auto-start-register", daemon=True).start()

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        """
        max_attempts = 20  # 防止无限循环
        attempted_tokens: set[str] = set()
        for _attempt in range(max_attempts):
            access_token = self._acquire_next_candidate_token(
                excluded_tokens=attempted_tokens,
                plan_type=plan_type,
                source_type=source_type,
                plan_types=plan_types,
            )
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception as exc:
                self.release_image_slot(access_token)
                # fetch_remote_info already records the structured failure once.
                continue
            # fetch_remote_info 内部可能因 token rotation 导致 access_token 变化，
            # 把新 token 也加入排除列表，防止重复尝试
            resolved = str((account or {}).get("access_token") or "")
            if resolved and resolved != access_token:
                attempted_tokens.add(resolved)
            if (
                    self._is_image_account_available(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                return str((account or {}).get("access_token") or access_token)
            self.release_image_slot(access_token)
        with self._lock:
            ready_count = len(self._list_ready_candidate_tokens(set(), plan_type, source_type, plan_types))
            available_count = len(self._list_available_candidate_tokens(set(), plan_type, source_type, plan_types))
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "图片账号取号失败",
            {
                "plan_type": plan_type,
                "source_type": source_type,
                "plan_types": list(plan_types or []),
                "attempted": len(attempted_tokens),
                "ready": ready_count,
                "available": available_count,
            },
        )
        self._schedule_auto_start_register_if_needed("no_available_image_quota")
        raise RuntimeError(
            f"no available {plan_type or source_type or ''} image quota (tried {len(attempted_tokens)} tokens)".replace("  ", " ").strip()
            if plan_type or source_type else f"no available image quota (tried {len(attempted_tokens)} tokens)"
        )

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                     if self._is_account_eligible_for_text(account)
                         and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
        refreshed_token = self.refresh_access_token(access_token, event="get_text_access_token")
        if refreshed_token:
            return refreshed_token
        account = self.get_account(access_token)
        if account and self._health_state_blocks_requests(account):
            return ""
        return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def _remove_confirmed_invalid_token(self, access_token: str, event: str, quiet: bool = False) -> bool:
        """仅移除已经经过复核确认的失效 token。"""
        if not config.auto_remove_invalid_accounts:
            return False
        # Recheck and delete under one lock so a concurrent successful refresh
        # cannot recover this account between the check and deletion.
        with self._image_slot_condition:
            token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(token)
            if not account or account.get("health_state") != HEALTH_STATE_INVALID_CONFIRMED:
                return False
            self._accounts.pop(token)
            self._image_inflight.pop(token, None)
            self._token_aliases = {old: new for old, new in self._token_aliases.items()
                                   if old != token and new != token}
            self._index = self._index % len(self._accounts) if self._accounts else 0
            self._save_accounts()
            self._image_slot_condition.notify_all()
            removed = True
        if removed:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "自动移除已确认失效账号",
                {"source": event, "token": anonymize_token(access_token)},
            )
        return removed

    def remove_invalid_token(
        self,
        access_token: str,
        event: str,
        quiet: bool = False,
        defer_confirmation: bool = True,
        error: str = "invalid access token",
    ) -> bool:
        """记录 token 失效并按统一复核策略决定是否移除。

        文字请求、图片请求、刷新任务和手动检查都走同一条路径；首次 401
        只会进入 ``invalid_pending``，不会因为一次网络抖动或上游误报直接删号。
        """
        current = self.get_account(access_token)
        if current and current.get("health_state") in {
            HEALTH_STATE_NEEDS_RELOGIN,
            HEALTH_STATE_NEEDS_VERIFICATION,
            HEALTH_STATE_DISABLED,
        }:
            return False
        should_remove = self._record_invalid_token_seen(
            access_token,
            event,
            error,
            defer_invalid_removal=defer_confirmation,
        )
        return self._remove_confirmed_invalid_token(access_token, event, quiet=quiet) if should_remove else False

    def mark_image_quota_exhausted_token(self, access_token: str, event: str, error: str = "") -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        account = self.update_account(
            access_token,
            {
                "status": "限流",
                "quota": 0,
                "image_quota_unknown": False,
                "last_refresh_error": str(error or "image quota exhausted")[:500],
                "last_refresh_error_at": datetime.now(timezone.utc).isoformat(),
            },
            quiet=True,
        )
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "图片额度不足-标记限流",
            {"source": event, "token": anonymize_token(access_token), "error": str(error or "")[:500]},
        )
        return account

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result

    def list_due_health_tokens(self) -> list[str]:
        """Retry pending checks independently of the normal pool refresh interval."""
        now = datetime.now(timezone.utc)
        retryable = {HEALTH_STATE_INVALID_PENDING, HEALTH_STATE_TRANSIENT_ERROR,
                     HEALTH_STATE_UNKNOWN_ERROR, HEALTH_STATE_RATE_LIMITED,
                     HEALTH_STATE_IMAGE_QUOTA_UNKNOWN}
        with self._lock:
            due = []
            for token, account in self._accounts.items():
                if account.get("health_state") not in retryable:
                    continue
                retry_at = self._parse_time(account.get("health_retry_at"))
                if retry_at is None and account.get("health_state") != HEALTH_STATE_INVALID_PENDING:
                    continue
                if retry_at is None or retry_at <= now:
                    due.append((retry_at or datetime.min.replace(tzinfo=timezone.utc), token))
            return [token for _, token in sorted(due)[:10]]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    def list_normal_tokens(self) -> list[str]:
        now = datetime.now(timezone.utc)
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and int(item.get("invalid_count") or 0) == 0
                   and not self._health_state_blocks_requests(item)
                   and not self._health_cooldown_active(item, now)
                   and (token := item.get("access_token") or "")
            ]

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _prepare_account_payload(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def add_account_items(self, items: list[dict]) -> dict:
        payloads = [
            payload
            for item in items
            if (payload := self._prepare_account_payload(item)) is not None
        ]
        return self._add_account_payloads(payloads)

    def add_accounts(self, tokens: list[str], source_type: str = "web") -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ])

    def _add_account_payloads(self, payloads: list[dict]) -> dict:
        deduped: dict[str, dict] = {}
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token, payload in deduped.items():
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    self._cumulative_total += 1
                    self._save_cumulative_total()
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                account = self._normalize_account(
                    {
                        **current,
                        **incoming,
                        "access_token": access_token,
                        "type": str(incoming.get("type") or current.get("type") or "free"),
                    }
                )
                if account is not None:
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def delete_accounts(self, tokens: list[str]) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts()}
        with self._lock:
            target_set = {self._resolve_access_token_locked(token) for token in target_set if token}
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
            self._token_aliases = {
                old: new
                for old, new in self._token_aliases.items()
                if old not in target_set and new not in target_set
            }
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict, quiet: bool = False) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            account = self._normalize_account({**current, **updates, "access_token": access_token})
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def _record_refresh_success(self, access_token: str) -> None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account

    def _should_defer_invalid_token(self, account: dict | None, now: datetime) -> bool:
        if not isinstance(account, dict):
            return False
        invalid_count = int(account.get("invalid_count") or 0)
        if invalid_count <= 0:
            return True
        last_invalid_at = self._parse_time(account.get("last_invalid_at"))
        if last_invalid_at is not None and (now - last_invalid_at).total_seconds() < self._INVALID_CONFIRM_SECONDS:
            return True
        created_at = self._parse_time(account.get("created_at"))
        if created_at is not None:
            return (now - created_at).total_seconds() < self._NEW_ACCOUNT_INVALID_GRACE_SECONDS
        return False

    def _record_invalid_token_seen(
        self,
        access_token: str,
        event: str,
        error: str,
        defer_invalid_removal: bool = True,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return True
            # Concurrent/rapid reports are not independent confirmations and must
            # not move the deadline indefinitely into the future.
            last_invalid = self._parse_time(current.get("last_invalid_at"))
            if (defer_invalid_removal and int(current.get("invalid_count") or 0) > 0
                    and last_invalid is not None
                    and (now - last_invalid).total_seconds() < self._INVALID_CONFIRM_SECONDS):
                return False
            should_defer = defer_invalid_removal and self._should_defer_invalid_token(current, now)
            next_item = dict(current)
            next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
            next_item["last_invalid_at"] = now.isoformat()
            next_item["last_refresh_error"] = str(error or "invalid access token")
            next_item["last_refresh_error_at"] = now.isoformat()
            next_item["last_check_at"] = now.isoformat()
            next_item["health_failure_count"] = int(next_item.get("health_failure_count") or 0) + 1
            next_item["health_error_kind"] = ERROR_INVALID_TOKEN
            next_item["health_error_code"] = "invalid_token"
            next_item["health_reason"] = str(error or "invalid access token")[:500]
            next_item["health_source"] = event
            next_item["health_updated_at"] = now.isoformat()
            if should_defer:
                next_item["health_state"] = HEALTH_STATE_INVALID_PENDING
                retry_at = now + timedelta(seconds=self._INVALID_CONFIRM_SECONDS)
                created_at = self._parse_time(current.get("created_at"))
                if created_at is not None:
                    retry_at = max(retry_at, created_at + timedelta(seconds=self._NEW_ACCOUNT_INVALID_GRACE_SECONDS))
                next_item["health_retry_at"] = retry_at.isoformat()
            else:
                next_item["status"] = "异常"
                next_item["quota"] = 0
                next_item["image_quota_unknown"] = False
                next_item["health_state"] = HEALTH_STATE_INVALID_CONFIRMED
                next_item["health_retry_at"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                self._save_accounts()
            if should_defer:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "暂缓标记异常账号",
                    {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
                )
                return False
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "确认账号 token 已失效",
                {"source": event, "token": anonymize_token(access_token), "error": str(error or "")},
            )
        return True

    def _record_image_preflight_error(self, access_token: str, event: str, error: str) -> dict | None:
        if not access_token:
            return None
        classification = classify_account_error(error)
        if classification.kind == ERROR_INVALID_TOKEN:
            should_remove = self._record_invalid_token_seen(
                access_token,
                event,
                str(error or "invalid access token"),
                defer_invalid_removal=True,
            )
            if should_remove:
                self._remove_confirmed_invalid_token(access_token, event, quiet=True)
            return self.get_account(access_token)
        account = self._record_account_health_failure(
            access_token,
            event,
            error,
            classification,
        )
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "图片账号预检失败",
            {"source": event, "token": anonymize_token(access_token), "error": str(error or "")[:500]},
        )
        return account

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                    self._schedule_auto_start_register_if_needed("image_quota_decrement")
                if not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    def fetch_remote_info(
        self, access_token: str, event: str = "fetch_remote_info",
        defer_invalid_removal: bool = True, skip_token_refresh: bool = False,
    ) -> dict[str, Any] | None:
        # Background and manual checks may overlap. A duplicate must not create
        # a second confirmation or overwrite the result of an in-flight check.
        with self._lock:
            token = self._resolve_access_token_locked(access_token)
            if token in self._health_check_inflight:
                return None
            self._health_check_inflight.add(token)
        try:
            return self._fetch_remote_info(token, event, defer_invalid_removal, skip_token_refresh)
        finally:
            with self._lock:
                self._health_check_inflight.discard(token)
                self._health_check_inflight.discard(self._resolve_access_token_locked(token))

    def _fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        defer_invalid_removal: bool = True,
        skip_token_refresh: bool = False,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        active_token = access_token
        if not skip_token_refresh:
            refreshed_token = self.refresh_access_token(access_token, event=f"{event}:preflight")
            if refreshed_token:
                active_token = refreshed_token
            else:
                account_after_refresh = self.get_account(access_token)
                if account_after_refresh and account_after_refresh.get("health_state") in {
                    HEALTH_STATE_NEEDS_RELOGIN,
                    HEALTH_STATE_NEEDS_VERIFICATION,
                    HEALTH_STATE_DISABLED,
                }:
                    reason = str(account_after_refresh.get("health_reason") or "account health check requires attention")
                    raise RuntimeError(reason)
        from services import openai_backend_api

        try:
            backend = openai_backend_api.OpenAIBackendAPI(active_token)
            try:
                result = backend.get_user_info()
            finally:
                backend.close()
        except openai_backend_api.InvalidAccessTokenError as exc:
            refreshed_token = self.refresh_access_token(active_token, force=True, event=f"{event}:invalid_access_token")
            if refreshed_token and refreshed_token != active_token:
                try:
                    backend = openai_backend_api.OpenAIBackendAPI(refreshed_token)
                    try:
                        result = backend.get_user_info()
                    finally:
                        backend.close()
                except openai_backend_api.InvalidAccessTokenError as retry_exc:
                    if self._record_invalid_token_seen(
                        refreshed_token,
                        event,
                        str(retry_exc),
                        defer_invalid_removal=defer_invalid_removal,
                    ):
                        self._remove_confirmed_invalid_token(refreshed_token, event)
                    raise
                except Exception as retry_exc:
                    classification = classify_account_error(retry_exc)
                    if classification.kind == ERROR_INVALID_TOKEN:
                        should_remove = self._record_invalid_token_seen(
                            refreshed_token,
                            event,
                            str(retry_exc),
                            defer_invalid_removal=defer_invalid_removal,
                        )
                        if should_remove:
                            self._remove_confirmed_invalid_token(refreshed_token, event)
                    else:
                        self._record_account_health_failure(
                            refreshed_token,
                            event,
                            retry_exc,
                            classification,
                        )
                    raise
                active_token = refreshed_token
            else:
                current = self.get_account(active_token)
                if current and current.get("health_state") in {
                    HEALTH_STATE_NEEDS_RELOGIN,
                    HEALTH_STATE_NEEDS_VERIFICATION,
                    HEALTH_STATE_DISABLED,
                }:
                    raise
                if self._record_invalid_token_seen(
                    active_token,
                    event,
                    str(exc),
                    defer_invalid_removal=defer_invalid_removal,
                ):
                    self._remove_confirmed_invalid_token(active_token, event)
                raise
        except Exception as exc:
            classification = classify_account_error(exc)
            if classification.kind == ERROR_INVALID_TOKEN:
                should_remove = self._record_invalid_token_seen(
                    active_token,
                    event,
                    str(exc),
                    defer_invalid_removal=defer_invalid_removal,
                )
                if should_remove:
                    self._remove_confirmed_invalid_token(active_token, event)
            else:
                self._record_account_health_failure(active_token, event, exc, classification)
            raise

        self._record_refresh_success(active_token)
        return self._record_account_health_success(active_token, event, result)

    # ---- 刷新进度追踪 ----

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
            }

    def update_refresh_progress(self, progress_id: str, token: str) -> None:
        """刷新单个账号后，更新进度计数。"""
        account = self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "正常"
        quota = (
            max(0, int(account.get("quota") or 0))
            if account and not account.get("image_quota_unknown")
            else 0
        )

        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota

    def finish_refresh_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记刷新完成。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)

    # ---- 重新登录进度追踪 ----

    def init_relogin_progress(self, progress_id: str, total: int) -> None:
        """初始化重新登录进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "results": [],
            }

    def update_relogin_progress(self, progress_id: str, token: str, status: str, error: str | None = None) -> None:
        """更新单个重新登录进度。当所有账号处理完毕时自动标记完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["results"].append({
                "token": anonymize_token(token),
                "status": status,
                "error": error,
            })
            if progress["processed"] >= progress["total"]:
                progress["done"] = True

    def finish_relogin_progress(self, progress_id: str, result: dict | None = None, error: str | None = None) -> None:
        """标记重新登录完成。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            if error:
                progress["error"] = error

    def get_relogin_progress(self, progress_id: str) -> dict | None:
        """查询重新登录进度。"""
        with self._relogin_progress_lock:
            progress = self._relogin_progress.get(progress_id)
            return dict(progress) if progress else None

    def clean_relogin_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._relogin_progress_lock:
            self._relogin_progress.pop(progress_id, None)

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        defer_invalid_removal: bool = True,
    ) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            items = self.list_accounts()
            result = {"refreshed": 0, "errors": [], "items": items, "relogined": 0}
            if progress_id:
                self.finish_refresh_progress(progress_id, result)
            return result

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        if progress_id:
            self.init_refresh_progress(progress_id, len(access_tokens))

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts", defer_invalid_removal): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    error_str = str(exc)
                    classification = classify_account_error(exc)
                    errors.append(
                        {
                            "token": anonymize_token(token),
                            "error": error_str,
                            "kind": classification.kind,
                            "code": classification.code,
                            "retry_after_seconds": classification.retry_after_seconds,
                        }
                    )
                else:
                    if account is not None:
                        refreshed += 1

                if progress_id:
                    self.update_refresh_progress(progress_id, token)
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        # 自动重新登录异常账号（仅当配置开启时）
        relogined = 0
        if config.auto_relogin_after_refresh:
            for token in access_tokens:
                account = self.get_account(token)
                if not account:
                    continue
                if account.get("health_state") != HEALTH_STATE_NEEDS_RELOGIN:
                    continue
                email = str(account.get("email") or "").strip()
                password = str(account.get("password") or "").strip()
                if not email or not password:
                    continue
                if self._start_password_relogin_if_possible(
                    token,
                    account,
                    "auto_relogin_after_refresh",
                ):
                    relogined += 1
        result = {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
            "relogined": relogined,
        }
        if config.auto_start_register_enabled:
            try:
                from services.register_service import register_service

                result["auto_register"] = register_service.auto_start_if_quota_low(
                    config.auto_start_register_min_quota
                )
            except Exception as exc:
                result["auto_register"] = {"started": False, "reason": "error", "error": str(exc)}

        if progress_id:
            self.finish_refresh_progress(progress_id, result)

        return result

    def re_login_accounts(self, access_tokens: list[str], progress_id: str | None = None) -> dict[str, Any]:
        """对选中账号执行密码重新登录流程。

        仅对包含 email + password 的账号有效。
        登录成功后自动将状态设为"正常"。
        """
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            result = {"relogined": 0, "skipped": 0, "errors": [], "items": self.list_accounts()}
            if progress_id:
                self.finish_relogin_progress(progress_id, result)
            return result

        if progress_id:
            self.init_relogin_progress(progress_id, len(access_tokens))

        relogined = 0
        skipped = 0
        errors = []

        for token in access_tokens:
            account = self.get_account(token)
            if not account:
                errors.append({"token": anonymize_token(token), "error": "账号不存在"})
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "账号不存在")
                continue

            email = str(account.get("email") or "").strip()
            password = str(account.get("password") or "").strip()
            if not email or not password:
                skipped += 1
                if progress_id:
                    self.update_relogin_progress(progress_id, token, "跳过", "无邮箱密码")
                continue

            # 在新线程中执行密码重新登录
            t = Thread(
                target=self._password_re_login_thread,
                args=(token, email, password, "manual_relogin", progress_id),
                daemon=True,
            )
            t.start()
            relogined += 1

        result = {
            "relogined": relogined,
            "skipped": skipped,
            "errors": errors,
            "items": self.list_accounts(),
        }
        if progress_id:
            # 如果所有账号都已同步处理完毕（没有启动线程），直接标记完成
            if relogined == 0:
                self.finish_relogin_progress(progress_id, result)
            else:
                # 有线程在运行，等线程结束后再完成
                pass
        return result

    def build_export_items(self, access_tokens: list[str] | None = None) -> list[dict[str, str]]:
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        items: list[dict[str, str]] = []
        for account in accounts:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token or not refresh_token or not id_token:
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)
            auth_claim = access_payload.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = access_payload.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

            email = (
                str(account.get("email") or "").strip()
                or str(profile_claim.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
            )
            account_id = (
                str(account.get("account_id") or "").strip()
                or str(auth_claim.get("chatgpt_account_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            )
            item = {
                "type": str(account.get("export_type") or "codex"),
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": self._timestamp_to_iso(access_payload.get("exp")),
                "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
            }
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items

    def get_stats(self) -> dict:
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        total_quota = sum(
            max(0, int(a.get("quota") or 0))
            for a in items
            if a.get("status") == "正常" and not a.get("image_quota_unknown")
        )
        unlimited = sum(1 for a in items if self._coerce_bool(a.get("unlimited_quota"), False))
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        image_quota_unknown_count = sum(1 for a in items if a.get("image_quota_unknown"))
        pending_invalid_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_INVALID_PENDING)
        needs_relogin_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_NEEDS_RELOGIN)
        needs_verification_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_NEEDS_VERIFICATION)
        transient_error_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_TRANSIENT_ERROR)
        unknown_error_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_UNKNOWN_ERROR)
        rate_limited_health_count = sum(1 for a in items if a.get("health_state") == HEALTH_STATE_RATE_LIMITED)
        available_text_count = sum(1 for a in items if self._is_account_eligible_for_text(a))
        by_type = {}
        for a in items:
            t = a.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "unlimited_quota_count": unlimited,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
            "available_text_count": available_text_count,
            "image_quota_unknown_count": image_quota_unknown_count,
            "pending_invalid_count": pending_invalid_count,
            "needs_relogin_count": needs_relogin_count,
            "needs_verification_count": needs_verification_count,
            "transient_error_count": transient_error_count,
            "unknown_error_count": unknown_error_count,
            "rate_limited_health_count": rate_limited_health_count,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        result = {
            "healthy": stats["available_text_count"] > 0 or stats["unlimited_quota_count"] > 0,
            "status": "ok" if stats["available_text_count"] > 0 else "degraded",
            **stats,
        }
        if config.auto_start_register_enabled:
            try:
                from services.register_service import register_service

                result["auto_register"] = register_service.auto_start_if_quota_low(
                    config.auto_start_register_min_quota
                )
            except Exception as exc:
                result["auto_register"] = {"started": False, "reason": "error", "error": str(exc)}

        return result


account_service = AccountService(config.get_storage_backend())
