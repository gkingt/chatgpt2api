from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODELS, IMAGE_26_MODELS, is_codex_image_model


def list_models() -> dict[str, Any]:
    backend = OpenAIBackendAPI()
    try:
        result = backend.list_models()
    finally:
        backend.close()
    data = result.get("data")
    if not isinstance(data, list):
        return result
    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) != "codex"
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if accounts:
        dynamic_models.add("gpt-image-2")
    if web_image_accounts:
        dynamic_models.update(IMAGE_26_MODELS)
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.update(CODEX_IMAGE_MODELS)
    for plan in ("Plus", "Team", "Pro"):
        if plan in codex_types:
            dynamic_models.update(f"{plan.lower()}-{model}" for model in CODEX_IMAGE_MODELS)

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append({
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model,
                "parent": None,
                **({"experimental": True, "upstream": "codex" if is_codex_image_model(model) else "chatgpt_web", "availability": "requires_upstream_account_support"} if "2.6" in model else {}),
            })
    return result
