from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from threading import Condition, Lock
from typing import Any
from datetime import datetime

from curl_cffi import requests

from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.proxy_service import proxy_settings
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}

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
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized["access_token"] = access_token
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        return normalized

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        try:
            payload = token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    def _exchange_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        candidate = str(refresh_token or "").strip()
        if not candidate:
            raise RuntimeError("refresh_token is empty")
        session = requests.Session(**proxy_settings.build_session_kwargs(
            impersonate="edge101",
            verify=True,
        ))
        try:
            response = session.post(
                "https://auth.openai.com/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": candidate,
                    "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                    "redirect_uri": "https://platform.openai.com/auth/callback",
                },
                timeout=60,
            )
        finally:
            session.close()

        body: dict[str, Any]
        try:
            body = response.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        if response.status_code != 200:
            raw_text = str(getattr(response, "text", "") or "")
            if len(raw_text) > 1200:
                raw_text = f"{raw_text[:1200]}...<truncated>"
            raise RuntimeError(f"oauth_refresh_http_{response.status_code}, body={raw_text}")

        new_access_token = str(body.get("access_token") or "").strip()
        if not new_access_token:
            raise RuntimeError("oauth_refresh_missing_access_token")
        payload = self._decode_jwt_payload(str(body.get("id_token") or "")) or self._decode_jwt_payload(new_access_token)
        return {
            "email": str(payload.get("email") or "").strip(),
            "access_token": new_access_token,
            "refresh_token": str(body.get("refresh_token") or "").strip(),
            "id_token": str(body.get("id_token") or "").strip(),
        }

    def _apply_refreshed_tokens_locked(self, old_access_token: str, refreshed: dict[str, Any]) -> dict | None:
        current = self._accounts.get(old_access_token)
        if current is None:
            return None
        new_access_token = str(refreshed.get("access_token") or "").strip()
        if not new_access_token:
            return None

        target_existing = self._accounts.get(new_access_token) if new_access_token != old_access_token else None
        merged = {
            **(target_existing or {}),
            **current,
            "access_token": new_access_token,
            "email": str(refreshed.get("email") or current.get("email") or "").strip() or current.get("email"),
            "refresh_token": str(refreshed.get("refresh_token") or current.get("refresh_token") or "").strip(),
            "id_token": str(refreshed.get("id_token") or current.get("id_token") or "").strip(),
            "status": "正常" if str(current.get("status") or "") == "异常" else current.get("status"),
            "last_used_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        account = self._normalize_account(merged)
        if account is None:
            return None

        if new_access_token != old_access_token:
            self._accounts.pop(old_access_token, None)
        self._accounts[new_access_token] = account

        inflight = int(self._image_inflight.pop(old_access_token, 0))
        if inflight:
            self._image_inflight[new_access_token] = int(self._image_inflight.get(new_access_token, 0)) + inflight

        self._save_accounts()
        return dict(account)

    def try_refresh_access_token(self, access_token: str, event: str) -> str:
        old_access_token = str(access_token or "").strip()
        if not old_access_token:
            return ""

        with self._lock:
            current = self._accounts.get(old_access_token)
            refresh_token = str((current or {}).get("refresh_token") or "").strip()
        if not refresh_token:
            return ""

        try:
            refreshed = self._exchange_refresh_token(refresh_token)
        except Exception as exc:
            log_service.add(LOG_TYPE_ACCOUNT, "refresh_token 刷新 access_token 失败", {
                "source": event,
                "token": anonymize_token(old_access_token),
                "error": str(exc),
            })
            return ""

        with self._lock:
            updated = self._apply_refreshed_tokens_locked(old_access_token, refreshed)
        if not updated:
            return ""

        new_access_token = str(updated.get("access_token") or "").strip()
        if not new_access_token or new_access_token == old_access_token:
            log_service.add(LOG_TYPE_ACCOUNT, "refresh_token 刷新未更换 access_token", {
                "source": event,
                "token": anonymize_token(old_access_token),
            })
            return ""
        log_service.add(LOG_TYPE_ACCOUNT, "refresh_token 自动刷新 access_token 成功", {
            "source": event,
            "old_token": anonymize_token(old_access_token),
            "new_token": anonymize_token(new_access_token),
        })
        return new_access_token

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def list_refreshable_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for token, account in self._accounts.items()
                if str((account or {}).get("refresh_token") or "").strip()
            ]

    def _list_ready_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(self, excluded_tokens: set[str] | None = None) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens):
                    raise RuntimeError("no available image quota")
                tokens = self._list_available_candidate_tokens(excluded_tokens)
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
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(self) -> str:
        attempted_tokens: set[str] = set()
        while True:
            access_token = self._acquire_next_candidate_token(excluded_tokens=attempted_tokens)
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception:
                self.release_image_slot(access_token)
                continue
            if self._is_image_account_available(account or {}):
                return access_token
            self.release_image_slot(access_token)

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
            return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
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

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0})
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0})
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    def add_accounts(self, tokens: list[str]) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token in tokens:
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    current = {}
                else:
                    skipped += 1
                account = self._normalize_account(
                    {
                        **current,
                        "access_token": access_token,
                        "type": str(current.get("type") or "free"),
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
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        with self._lock:
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
            log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                            {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
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

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            refreshed_access_token = self.try_refresh_access_token(access_token, event)
            if refreshed_access_token:
                result = OpenAIBackendAPI(refreshed_access_token).get_user_info()
                return self.update_account(refreshed_access_token, result)
            self.remove_invalid_token(access_token, event)
            raise
        return self.update_account(access_token, result)

    def refresh_accounts(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts"): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                try:
                    account = future.result()
                except Exception as exc:
                    errors.append({"token": anonymize_token(futures[future]), "error": str(exc)})
                    continue
                if account is not None:
                    refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


account_service = AccountService(config.get_storage_backend())
