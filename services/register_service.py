from __future__ import annotations

import json
import copy
import secrets
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import mail_provider, openai_register
from utils.json_store import ConfigFileError, load_json_config, save_json_config


REGISTER_FILE = DATA_DIR / "register.json"
RUNNING_SAVE_INTERVAL_SECS = 2.0
MAX_CONSECUTIVE_FAILURES = 10
AUTO_RESTART_COOLDOWN_SECS = 60.0
SECRET_MASK = "[REDACTED]"
_SECRET_KEY_NAMES = {
    "access_token",
    "api_key",
    "authorization",
    "bearer_token",
    "cf_api_key",
    "cf_inbox_jwt",
    "client_secret",
    "cookie",
    "cookies",
    "ddg_token",
    "jwt",
    "password",
    "passphrase",
    "private_key",
    "refresh_token",
    "secret",
    "secret_access_key",
    "token",
}


def _secret_key_name(key: object) -> str:
    return "_".join(str(key or "").lower().replace("-", "_").split())


def _is_secret_key(key: object) -> bool:
    normalized = _secret_key_name(key)
    return normalized in _SECRET_KEY_NAMES or normalized.endswith(("_api_key", "_token", "_password", "_secret", "_jwt"))


def _is_blank_secret(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in {"", SECRET_MASK})


def _collect_secret_values(value: object, result: set[str] | None = None) -> set[str]:
    result = result if result is not None else set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_secret_key(key):
                if isinstance(item, (str, int, float)):
                    text = str(item).strip()
                    if len(text) >= 4 and text != SECRET_MASK:
                        result.add(text)
                continue
            _collect_secret_values(item, result)
    elif isinstance(value, list):
        for item in value:
            _collect_secret_values(item, result)
    return result


def _redact_text(value: object, secret_values: set[str]) -> object:
    if not isinstance(value, str) or not value:
        return value
    redacted = value
    for secret in sorted(secret_values, key=len, reverse=True):
        redacted = redacted.replace(secret, SECRET_MASK)
    return redacted


def _redact_value(value: object, secret_values: set[str]) -> object:
    if isinstance(value, dict):
        return {
            key: SECRET_MASK if _is_secret_key(key) and not _is_blank_secret(item) else _redact_value(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secret_values) for item in value]
    return _redact_text(value, secret_values)


def _serialize_outlook_pool(credentials: list[dict]) -> str:
    return "\n".join(
        f'{c["email"]}----{c.get("password", "")}----{c["client_id"]}----{c["refresh_token"]}' for c in credentials
    )


def _merge_outlook_pool(old_text: str, new_text: str) -> str:
    """合并已存邮箱池与新导入文本，按邮箱去重，新导入的同名邮箱覆盖旧凭据。"""
    merged: dict[str, dict] = {}
    for credential in mail_provider.parse_outlook_credentials(old_text or ""):
        merged[credential["email"].strip().lower()] = credential
    for credential in mail_provider.parse_outlook_credentials(new_text or ""):
        merged[credential["email"].strip().lower()] = credential
    return _serialize_outlook_pool(list(merged.values()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {**copy.deepcopy(openai_register.DEFAULT_CONFIG), "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5, "enabled": False, "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": openai_register.DEFAULT_CONFIG["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, "current_quota": 0, "current_available": 0}}


def _normalize(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    cfg = json.loads(json.dumps(_default_config(), ensure_ascii=False))
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    if cfg["threads"] > 32:
        raise ValueError("注册线程数不能超过 32")
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    if not isinstance(cfg.get("mail"), dict):
        raise ValueError("mail 必须是邮箱配置对象")
    cfg["mail"] = {**openai_register.DEFAULT_CONFIG["mail"], **cfg["mail"]}
    if not isinstance(cfg["mail"].get("providers"), list):
        raise ValueError("mail.providers 必须是邮箱提供商列表")
    if isinstance(cfg.get("mail"), dict):
        cfg["mail"].pop("proxy", None)
        providers = cfg["mail"].get("providers")
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                if isinstance(provider.get("enable"), str):
                    provider["enable"] = provider["enable"].strip().lower() in {"true", "1", "yes", "on"}
                if not str(provider.get("id") or "").strip():
                    provider["id"] = f"provider-{secrets.token_hex(8)}"
                if provider.get("type") != "cloudflare_temp_email":
                    continue
                for legacy_key in ("subdomain", "subdomain_levels", "append_random_suffix", "random_subdomain_depth", "rate_limit_cooldown_seconds"):
                    provider.pop(legacy_key, None)
    enabled = cfg.get("enabled")
    cfg["enabled"] = enabled.strip().lower() in {"true", "1", "yes", "on"} if isinstance(enabled, str) else bool(enabled)
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    return cfg


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._load_error = ""
        self._auto_restart_at = 0.0
        self._runner: threading.Thread | None = None
        self._auto_stop_when_target_reached = False
        self._logs: list[dict] = []
        self._last_running_save_at = 0.0
        self._last_snapshot = ""
        self._last_snapshot_payload: dict | None = None
        self._last_snapshot_json = ""
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if not self._load_error:
            openai_register.config.update({key: copy.deepcopy(self._config[key]) for key in ("mail", "proxy", "total", "threads")})
        if self._config["enabled"]:
            try:
                self.start()
            except (ValueError, OSError) as exc:
                self._config["enabled"] = False
                self._config["stats"]["last_error"] = str(exc)
                self._append_log(f"自动恢复注册已取消: {exc}", "red")

    def _load(self) -> dict:
        try:
            raw, recovered = load_json_config(self._store_file, self._store_file.with_name(self._store_file.name + ".bak"))
            cfg = _normalize(raw)
            cfg["stats"].update(running=0, stopping=False)
            if recovered:
                cfg["enabled"] = False
                cfg["stats"]["last_error"] = "注册配置损坏，已从备份恢复；请检查配置后手动启动"
            return cfg
        except (ConfigFileError, ValueError, TypeError) as exc:
            self._load_error = str(exc)
            cfg = _normalize({})
            cfg["stats"]["last_error"] = f"注册配置读取失败，已禁止启动和覆盖保存: {exc}"
            return cfg

    def _save(self) -> None:
        if self._load_error:
            raise ValueError(f"注册配置读取失败，请先恢复 {self._store_file}；原文件未覆盖: {self._load_error}")
        save_json_config(self._store_file, self._config, self._store_file.with_name(self._store_file.name + ".bak"))

    def _save_running_throttled(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_running_save_at < RUNNING_SAVE_INTERVAL_SECS:
            return
        self._last_running_save_at = now
        self._save()

    def get(self) -> dict:
        with self._lock:
            source = {**self._config, "logs": self._logs[-300:]}
            payload = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if payload == self._last_snapshot and self._last_snapshot_payload is not None:
                snapshot = json.loads(json.dumps(self._last_snapshot_payload, ensure_ascii=False))
            else:
                snapshot = json.loads(payload)
                secret_values = _collect_secret_values(source)
                self._redact_outlook_pools(snapshot)
                if isinstance(snapshot.get("logs"), list):
                    snapshot["logs"] = [_redact_value(item, secret_values) for item in snapshot["logs"]]
                snapshot["stats"] = _redact_value(snapshot.get("stats", {}), secret_values)
                self._last_snapshot = payload
                self._last_snapshot_payload = json.loads(json.dumps(snapshot, ensure_ascii=False))
                self._last_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        return snapshot

    def snapshot_json(self) -> str:
        with self._lock:
            source = {**self._config, "logs": self._logs[-300:]}
            payload = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if payload == self._last_snapshot and self._last_snapshot_json:
                return self._last_snapshot_json
        return json.dumps(self.get(), ensure_ascii=False)

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        """把 outlook_token 邮箱池里的密码/refresh_token 从对外输出中抹掉，仅保留脱敏预览与统计。

        mailboxes 改为只写导入框（输出为空），避免把密码与 refresh_token 通过 GET/SSE 反复广播。
        """
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)

    def _merge_provider_secrets(self, updates: dict) -> None:
        """前端回传脱敏快照时保留旧 provider 密钥，避免掩码覆盖真实值。"""
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict):
                continue
            provider_ref = str(provider.get("id") or provider.get("provider_ref") or "").strip()
            old_provider = next(
                (
                    item
                    for item in old_providers
                    if isinstance(item, dict)
                    and provider_ref
                    and str(item.get("id") or item.get("provider_ref") or "").strip() == provider_ref
                ),
                old_providers[index] if index < len(old_providers) else {},
            )
            if not isinstance(old_provider, dict) or old_provider.get("type") != provider.get("type"):
                continue
            for key, old_value in old_provider.items():
                if _is_secret_key(key) and _is_blank_secret(provider.get(key)) and not _is_blank_secret(old_value):
                    provider[key] = old_value

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _merge_outlook_pools(self, updates: dict) -> None:
        """对 outlook_token provider：把前端新导入的 mailboxes 与已存池按邮箱合并去重。

        前端 mailboxes 是只写导入框，留空表示不改动；填入的新行追加/覆盖已存凭据。
        优先按稳定 id/provider_ref 对齐，旧配置再按数组下标兼容。
        """
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            provider_ref = str(provider.get("id") or provider.get("provider_ref") or "").strip()
            old = next(
                (
                    item
                    for item in old_providers
                    if isinstance(item, dict)
                    and provider_ref
                    and str(item.get("id") or item.get("provider_ref") or "").strip() == provider_ref
                ),
                old_providers[index] if index < len(old_providers) and isinstance(old_providers[index], dict) else {},
            )
            old_text = str(old.get("mailboxes") or "") if old.get("type") == "outlook_token" else ""
            new_text = str(provider.get("mailboxes") or "")
            provider["mailboxes"] = _merge_outlook_pool(old_text, new_text) if (old_text or new_text) else ""
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)

    def _prune_unused_outlook_pools(self) -> int:
        mail = self._config.get("mail")
        if not isinstance(mail, dict):
            return 0
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return 0
        total_removed = 0
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)
        return total_removed

    def update(self, updates: dict) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise ValueError("注册任务运行或停止中，请等待结束后修改配置")
            updates = copy.deepcopy(updates)
            expected_revision = updates.pop("config_revision", None)
            if expected_revision is not None and int(expected_revision) != int(self._config.get("config_revision") or 0):
                raise ValueError("注册配置已在其他页面更新，请刷新核对后再保存，避免覆盖新配置")
            if isinstance(updates.get("mail"), dict):
                updates["mail"] = {**self._config["mail"], **updates["mail"]}
            self._merge_provider_secrets(updates)
            self._merge_outlook_pools(updates)
            previous = self._config
            self._config = _normalize({**self._config, **updates})
            self._config["config_revision"] = int(previous.get("config_revision") or 0) + 1
            try:
                self._drop_mail_proxy()
                self._save()
            except Exception:
                self._config = previous
                raise
            openai_register.config.update({k: copy.deepcopy(self._config[k]) for k in ("mail", "proxy", "total", "threads")})
            self._auto_restart_at = 0.0
            return self.get()

    def start(self, auto_stop_when_target_reached: bool = False) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                if self._stop_event.is_set():
                    raise ValueError("上一个注册任务仍在停止，请等待结束后重试")
                return self.get()
            if self._load_error:
                raise ValueError(f"注册配置读取失败，请恢复 {self._store_file} 后重启服务")
            mail_provider.validate_config(self._config.get("mail"))
            self._auto_stop_when_target_reached = bool(auto_stop_when_target_reached)
            self._config["enabled"] = True
            self._drop_mail_proxy()
            self._logs = []
            self._last_snapshot = ""
            self._last_snapshot_payload = None
            self._last_snapshot_json = ""
            self._last_running_save_at = 0.0
            self._stop_event.clear()
            openai_register.reset_cancel()
            mail_provider.set_cancel_checker(openai_register.ensure_not_cancelled)
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: copy.deepcopy(self._config[k]) for k in ("mail", "proxy", "total", "threads")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            try:
                self._save()
            except Exception:
                self._config["enabled"] = False
                self._stop_event.set()
                raise
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}", "yellow")
            return self.get()

    def stop(self) -> dict:
        self._stop_event.set()
        openai_register.cancel_event.set()
        openai_register.request_cancel()
        with self._lock:
            # A concurrent start may have reset cancellation while holding this lock.
            self._stop_event.set()
            openai_register.cancel_event.set()
            self._auto_restart_at = time.monotonic() + AUTO_RESTART_COOLDOWN_SECS
            self._config["enabled"] = False
            self._config["stats"]["stopping"] = bool(self._runner and self._runner.is_alive())
            self._config["stats"]["updated_at"] = _now()
            try:
                self._save()
            except Exception as exc:
                self._append_log(f"停止状态保存失败，但任务已取消: {exc}", "red")
            self._append_log("已请求停止注册任务，正在取消当前远端请求", "yellow")
            return self.get()

    def auto_start_if_quota_low(self, min_quota: int) -> dict:
        threshold = max(1, int(min_quota or 1))
        metrics = self._pool_metrics()
        with self._lock:
            if time.monotonic() < self._auto_restart_at:
                return {"started": False, "reason": "failure_cooldown", **metrics}
            running = bool(self._runner and self._runner.is_alive())
            if running:
                self._auto_stop_when_target_reached = True
                self._bump(save=False, **metrics)
                return {"started": False, "reason": "running", **metrics}
            if metrics["current_quota"] >= threshold:
                # A health/refresh check must not rewrite mailbox configuration.
                self._bump(save=False, **metrics)
                return {"started": False, "reason": "quota_enough", **metrics}
        try:
            self.start(auto_stop_when_target_reached=True)
        except ValueError as exc:
            with self._lock:
                self._auto_restart_at = time.monotonic() + AUTO_RESTART_COOLDOWN_SECS
                self._bump(save=False, last_error=str(exc))
            return {"started": False, "reason": "invalid_config", **metrics}
        self._append_log(
            f"自动启动注册：当前剩余额度={metrics['current_quota']}，最低额度={threshold}",
            "yellow",
        )
        return {"started": True, "reason": "quota_low", **metrics}

    def reset(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise ValueError("注册任务运行或停止中，不能重置")
            self._logs = []
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise ValueError("注册任务运行或停止中，不能重置邮箱池")
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            with self._lock:
                removed = self._prune_unused_outlook_pools()
                openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
                self._save()
                self._append_log(f"已清空 Outlook 邮箱池未使用邮箱，移除 {removed} 个", "yellow")
            return self.get()
        scope = "failed" if str(scope) == "failed" else "all"
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        with self._lock:
            self._append_log(
                f"已重置 Outlook 邮箱池状态（范围={'仅失败/占用' if scope == 'failed' else '全部'}），清除 {cleared} 条记录",
                "yellow",
            )
        return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": self._redact_log_text(str(text)), "level": str(color or "info")})
            self._logs = self._logs[-300:]
            self._last_snapshot = ""
            self._last_snapshot_payload = None
            self._last_snapshot_json = ""

    def _redact_log_text(self, text: str) -> str:
        source = self._config if isinstance(self._config, dict) else {}
        return str(_redact_text(text, _collect_secret_values(source)))

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if account_service._is_image_account_available(item)]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, success: int = 0, *, metrics: dict | None = None, log_result: bool = True) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = metrics or self._pool_metrics()
        self._bump(save=False, **metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            if log_result:
                self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            if log_result:
                self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return success >= int(cfg.get("total") or 1)

    def _can_submit_more(self, cfg: dict, success: int, running: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        if mode != "total":
            return True
        return success + running < int(cfg.get("total") or 1)

    def _bump(self, *, save: bool = True, force_save: bool = False, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._last_snapshot = ""
            self._last_snapshot_payload = None
            self._last_snapshot_json = ""
            if save:
                self._save_running_throttled(force=force_save)

    def _run(self) -> None:
        with self._lock:
            cfg = json.loads(json.dumps(self._config, ensure_ascii=False))
        threads = int(cfg["threads"])
        submitted, done, success, fail = 0, 0, 0, 0
        consecutive_failures = 0
        next_submit_at = 0.0
        next_metrics_at = 0.0
        metrics = {"current_quota": 0, "current_available": 0}
        last_error = ""
        executor = ThreadPoolExecutor(max_workers=threads)
        try:
            futures = set()
            while True:
                with self._lock:
                    cfg = copy.deepcopy(self._config)
                now = time.monotonic()
                refresh_metrics = now >= next_metrics_at
                if refresh_metrics:
                    metrics = self._pool_metrics()
                    next_metrics_at = now + max(1, int(cfg.get("check_interval") or 5))
                target_reached = self._target_reached(cfg, success, metrics=metrics, log_result=refresh_metrics)
                while (
                    cfg["enabled"]
                    and not self._stop_event.is_set()
                    and now >= next_submit_at
                    and not target_reached
                    and len(futures) < threads
                    and self._can_submit_more(cfg, success, len(futures))
                ):
                    submitted += 1
                    futures.add(executor.submit(openai_register.worker, submitted))
                    if self._can_submit_more(cfg, success, len(futures)):
                        continue
                    target_reached = self._target_reached(cfg, success, metrics=metrics, log_result=False)
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                stopping = not cfg["enabled"] or self._stop_event.is_set()
                if stopping:
                    for future in futures:
                        future.cancel()
                if not futures and (stopping or target_reached):
                    if target_reached:
                        self._append_log("注册目标已满足，停止注册任务", "yellow")
                    break
                if not futures:
                    self._stop_event.wait(min(0.5, max(0.05, next_submit_at - time.monotonic())))
                    continue
                finished, futures = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
                if not finished:
                    continue
                for future in finished:
                    if future.cancelled():
                        continue
                    done += 1
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"ok": False, "error": str(exc)}
                    if result.get("ok"):
                        success += 1
                        consecutive_failures = 0
                    else:
                        fail += 1
                        consecutive_failures += 1
                        last_error = str(result.get("error") or "注册失败")
                        delay = min(60.0, 2.0 ** min(consecutive_failures, 6))
                        next_submit_at = max(next_submit_at, time.monotonic() + delay)
                        self._bump(save=False, last_error=last_error, retry_delay_seconds=delay)
                        if result.get("fatal") or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            last_error = f"连续失败已触发保护停止，请检查配置及网络后手动重试: {last_error}"
                            self._append_log(last_error, "red")
                            self._auto_restart_at = time.monotonic() + AUTO_RESTART_COOLDOWN_SECS
                            self._stop_event.set()
                            openai_register.request_cancel()
                        elif not self._stop_event.is_set():
                            self._append_log(f"注册失败，等待 {delay:g} 秒后重试（连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}）", "yellow")
        except Exception as exc:
            last_error = str(exc)
            self._auto_restart_at = time.monotonic() + AUTO_RESTART_COOLDOWN_SECS
            self._stop_event.set()
            openai_register.request_cancel()
            self._append_log(f"注册任务异常，已停止: {exc}", "red")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._config["enabled"] = False
                self._bump(save=False, running=0, done=done, success=success, fail=fail, last_error=last_error, retry_delay_seconds=0, stopping=False, finished_at=_now())
                try:
                    self._save()
                except Exception as exc:
                    self._append_log(f"注册结束状态保存失败，原配置或备份仍保留: {exc}", "red")
            self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
