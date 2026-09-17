from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


HEALTH_STATE_HEALTHY = "healthy"
HEALTH_STATE_RATE_LIMITED = "rate_limited"
HEALTH_STATE_IMAGE_QUOTA_UNKNOWN = "image_quota_unknown"
HEALTH_STATE_INVALID_PENDING = "invalid_pending"
HEALTH_STATE_INVALID_CONFIRMED = "invalid_confirmed"
HEALTH_STATE_NEEDS_RELOGIN = "needs_relogin"
HEALTH_STATE_NEEDS_VERIFICATION = "needs_verification"
HEALTH_STATE_DISABLED = "disabled"
HEALTH_STATE_TRANSIENT_ERROR = "transient_error"
HEALTH_STATE_UNKNOWN_ERROR = "unknown_error"

HEALTH_STATES = frozenset(
    {
        HEALTH_STATE_HEALTHY,
        HEALTH_STATE_RATE_LIMITED,
        HEALTH_STATE_IMAGE_QUOTA_UNKNOWN,
        HEALTH_STATE_INVALID_PENDING,
        HEALTH_STATE_INVALID_CONFIRMED,
        HEALTH_STATE_NEEDS_RELOGIN,
        HEALTH_STATE_NEEDS_VERIFICATION,
        HEALTH_STATE_DISABLED,
        HEALTH_STATE_TRANSIENT_ERROR,
        HEALTH_STATE_UNKNOWN_ERROR,
    }
)

ERROR_INVALID_TOKEN = "invalid_token"
ERROR_DISABLED = "disabled"
ERROR_TRANSIENT = "transient"
ERROR_UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
ERROR_NEEDS_RELOGIN = "needs_relogin"
ERROR_NEEDS_VERIFICATION = "needs_verification"
ERROR_INVALID_CREDENTIALS = "invalid_credentials"
ERROR_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorClassification:
    kind: str
    code: str
    message: str
    status_code: int | None = None
    retry_after_seconds: int | None = None


class TokenRefreshError(RuntimeError):
    """带结构化状态的 refresh_token 刷新异常。"""

    def __init__(
        self,
        status_code: int,
        error_code: str = "",
        detail: str = "",
        retry_after_seconds: int | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.error_code = str(error_code or "").strip()
        self.detail = str(detail or "").strip()
        self.retry_after = retry_after_seconds
        suffix = f": {self.detail}" if self.detail else ""
        super().__init__(f"oauth_refresh_http_{self.status_code}{suffix}")


def _status_code(error: object) -> int | None:
    value = getattr(error, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_code(error: object) -> str:
    return str(
        getattr(error, "error_code", None)
        or getattr(error, "code", None)
        or ""
    ).strip().lower()


def _error_text(error: object) -> str:
    parts = [str(error or "")]
    for field in ("detail", "body"):
        value = getattr(error, field, None)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(repr(value))
        else:
            parts.append(str(value))
    return "\n".join(part for part in parts if part).strip()


def _retry_after(error: object, default: int | None = None) -> int | None:
    value = getattr(error, "retry_after", None)
    if value is None:
        value = getattr(error, "retry_after_seconds", None)
    try:
        return max(1, int(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def classify_account_error(error: object) -> ErrorClassification:
    status_code = _status_code(error)
    error_code = _error_code(error)
    text = _error_text(error)
    lower = text.lower()

    if isinstance(error, TokenRefreshError):
        if (error_code in {
            "invalid_grant",
            "invalid_refresh_token",
            "refresh_token_invalid",
            "refresh_token_invalidated",
            "invalid_token",
            "unauthorized_client",
        } and status_code != 429 and not (status_code and status_code >= 500)) or status_code == 401 or "app_session_terminated" in lower:
            return ErrorClassification(
                ERROR_NEEDS_RELOGIN,
                error_code or "refresh_token_invalid",
                text or "refresh token is invalid",
                status_code,
            )
        if status_code == 429:
            return ErrorClassification(
                ERROR_UPSTREAM_RATE_LIMITED,
                error_code or "http_429",
                text or "refresh token request was rate limited",
                status_code,
                _retry_after(error, 60),
            )
        if status_code in {408, 409, 425, 500, 502, 503, 504}:
            return ErrorClassification(
                ERROR_TRANSIENT,
                error_code or f"http_{status_code}",
                text or "temporary refresh token failure",
                status_code,
                _retry_after(error, 30),
            )

    if error_code in {"account_deactivated", "account_disabled", "user_deactivated"}:
        return ErrorClassification(ERROR_DISABLED, error_code, text or "account is disabled", status_code)
    if error_code in {"need_verification_code", "email_otp_verification", "verification_required", "mfa_required", "challenge_required"}:
        return ErrorClassification(ERROR_NEEDS_VERIFICATION, error_code, text or "account verification is required", status_code)
    if error_code in {"invalid_password", "invalid_credentials", "wrong_password"}:
        return ErrorClassification(ERROR_INVALID_CREDENTIALS, error_code, text or "invalid credentials", status_code)

    disabled_markers = (
        "account_deactivated",
        "account disabled",
        "account is disabled",
        "user_deactivated",
        "deactivated account",
        "账号已停用",
        "账号已禁用",
    )
    if any(marker in lower for marker in disabled_markers):
        return ErrorClassification(ERROR_DISABLED, "account_disabled", text, status_code)

    verification_markers = (
        "need_verification_code",
        "email_otp_verification",
        "verification required",
        "verification code required",
        "requires verification",
        "mfa required",
        "challenge required",
        "需要验证码",
        "需要验证",
        "登录挑战",
    )
    if any(marker in lower for marker in verification_markers):
        return ErrorClassification(ERROR_NEEDS_VERIFICATION, "verification_required", text, status_code)

    if "app_session_terminated" in lower:
        return ErrorClassification(ERROR_NEEDS_RELOGIN, "app_session_terminated", text, status_code)

    credentials_markers = (
        "invalid credentials",
        "wrong password",
        "invalid password",
        "密码错误",
    )
    if any(marker in lower for marker in credentials_markers):
        return ErrorClassification(ERROR_INVALID_CREDENTIALS, "invalid_credentials", text, status_code)

    if status_code == 429 or any(marker in lower for marker in ("rate limit", "rate_limit", "too many requests", "请求过于频繁")):
        return ErrorClassification(
            ERROR_UPSTREAM_RATE_LIMITED,
            "http_429" if status_code == 429 else "rate_limited",
            text or "upstream request was rate limited",
            status_code,
            _retry_after(error, 60),
        )

    invalid_markers = (
        "token invalidated",
        "authentication token has been invalidated",
        "token_invalidated",
        "token_revoked",
        "invalidated oauth token",
        "invalid access token",
        "invalid_grant",
        "status=401",
        "http 401",
        "http_401",
    )
    if status_code == 401 or any(marker in lower for marker in invalid_markers):
        return ErrorClassification(ERROR_INVALID_TOKEN, "invalid_token", text, status_code)

    transient_statuses = {408, 409, 425, 500, 502, 503, 504}
    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "connect error",
        "connection reset",
        "connection aborted",
        "remote disconnected",
        "proxy",
        "tls",
        "ssl",
        "temporarily unavailable",
        "service unavailable",
        "network error",
        "网络错误",
        "连接失败",
        "连接超时",
    )
    if status_code in transient_statuses or any(marker in lower for marker in transient_markers):
        return ErrorClassification(
            ERROR_TRANSIENT,
            f"http_{status_code}" if status_code else "transient_error",
            text or "temporary upstream failure",
            status_code,
            _retry_after(error, 30),
        )

    return ErrorClassification(ERROR_UNKNOWN, error_code or "unknown_error", text or "unknown account error", status_code, _retry_after(error, 30))
