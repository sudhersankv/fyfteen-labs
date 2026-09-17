"""FYFTEN LLM settings. Keys stay in the environment."""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class FyftenSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FYFTEN_", extra="ignore")

    llm_api_key: str = ""
    llm_base_url: str = "https://api.fireworks.ai/inference/v1"
    llm_model: str = "accounts/fireworks/models/glm-5p3-flash"


class AliasKeys(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fireworks_api_key: str = ""
    openai_api_key: str = ""


def llm_config() -> dict[str, str]:
    settings = FyftenSettings()
    aliases = AliasKeys()
    key = (settings.llm_api_key
           or aliases.fireworks_api_key
           or os.environ.get("FIREWORKS_API_KEY")
           or "")
    if "openai.com" in settings.llm_base_url:
        key = key or aliases.openai_api_key or os.environ.get("OPENAI_API_KEY") or ""
    return {
        "key": key,
        "base": settings.llm_base_url.rstrip("/"),
        "model": settings.llm_model,
    }
