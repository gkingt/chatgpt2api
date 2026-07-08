"""按邮箱 provider/domain 统计注册成功率。

用于后续优化：某些临时邮箱域名会被最终风控拒绝（registration_disallowed），
通过按 domain 统计成功率，可以手动或自动停用低成功率域名。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from services.config import DATA_DIR

DOMAIN_STATS_FILE = DATA_DIR / "domain_stats.json"
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    """加载统计文件。

    结构:
    {
        "domains": {
            "example.com": {"success": 10, "fail": 2, "last_updated": "..."},
            ...
        },
        "providers": {
            "ddg_mail": {"success": 5, "fail": 1, "last_updated": "..."},
            ...
        },
        "updated_at": "..."
    }
    """
    try:
        if DOMAIN_STATS_FILE.exists():
            data = json.loads(DOMAIN_STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("domains", {})
                data.setdefault("providers", {})
                return data
    except Exception:
        pass
    return {"domains": {}, "providers": {}, "updated_at": _now()}


def _save(data: dict) -> None:
    try:
        DOMAIN_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now()
        DOMAIN_STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def record(email: str, provider: str, *, success: bool) -> None:
    """记录一次注册结果到 domain/provider 统计。

    Args:
        email: 注册使用的邮箱地址
        provider: 邮箱服务商标识 (如 ddg_mail, cloudmail_gen 等)
        success: 是否注册成功
    """
    domain = str(email or "").split("@")[-1].strip().lower()
    provider = str(provider or "").strip()
    if not domain:
        return

    with _lock:
        data = _load()
        now = _now()

        # domain 统计
        dom_entry = data["domains"].setdefault(domain, {"success": 0, "fail": 0, "last_updated": now})
        dom_entry["success"] = int(dom_entry.get("success", 0)) + (1 if success else 0)
        dom_entry["fail"] = int(dom_entry.get("fail", 0)) + (0 if success else 1)
        dom_entry["last_updated"] = now

        # provider 统计
        if provider:
            prov_entry = data["providers"].setdefault(provider, {"success": 0, "fail": 0, "last_updated": now})
            prov_entry["success"] = int(prov_entry.get("success", 0)) + (1 if success else 0)
            prov_entry["fail"] = int(prov_entry.get("fail", 0)) + (0 if success else 1)
            prov_entry["last_updated"] = now

        _save(data)


def get_stats() -> dict:
    """返回当前 domain/provider 统计快照，含成功率计算。"""
    with _lock:
        data = _load()

    result = {
        "domains": [],
        "providers": [],
        "updated_at": data.get("updated_at", ""),
    }

    for domain, entry in sorted(data["domains"].items(), key=lambda x: x[0]):
        s = int(entry.get("success", 0))
        f = int(entry.get("fail", 0))
        total = s + f
        result["domains"].append({
            "domain": domain,
            "success": s,
            "fail": f,
            "total": total,
            "success_rate": round(s * 100 / total, 1) if total else 0,
            "last_updated": entry.get("last_updated", ""),
        })

    for provider, entry in sorted(data["providers"].items(), key=lambda x: x[0]):
        s = int(entry.get("success", 0))
        f = int(entry.get("fail", 0))
        total = s + f
        result["providers"].append({
            "provider": provider,
            "success": s,
            "fail": f,
            "total": total,
            "success_rate": round(s * 100 / total, 1) if total else 0,
            "last_updated": entry.get("last_updated", ""),
        })

    return result


def reset_stats() -> dict:
    """清空所有 domain/provider 统计。"""
    with _lock:
        _save({"domains": {}, "providers": {}, "updated_at": _now()})
    return get_stats()
