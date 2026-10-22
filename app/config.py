from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    steam_api_key: str = ""
    steam_id: str = ""

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # Leer = Modell wird zur Laufzeit aus dem Tier aufgelöst (siehe app/llm/models.py).
    deepseek_model: str = ""
    deepseek_model_tier: Literal["pro", "flash"] = "pro"

    app_host: str = "127.0.0.1"
    app_port: int = 8765
    db_path: str = "data/app.db"
    timezone: str = "Europe/Berlin"

    request_timeout: float = 30.0
    llm_temperature: float = 0.7
    llm_reasoning_effort: Literal["", "low", "high", "max"] = "low"
    llm_max_tokens: int = 4000
    llm_max_candidates: int = 30
    steam_metadata_limit: int = 80

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        path = Path(self.db_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def has_steam(self) -> bool:
        return bool(self.steam_api_key and self.steam_id)

    @property
    def has_llm(self) -> bool:
        return bool(self.deepseek_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
