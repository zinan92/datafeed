"""Configuration via environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    db_path: str = "data/kline.db"

    # Server
    port: int = 8100

    # TuShare Pro (required for A-shares)
    tushare_token: str = ""
    # Optional JSON receipt proving minute/history/persistence rights. A token
    # without this operator-controlled receipt remains blocked_for_entitlement.
    tushare_entitlement_path: str = ""

    # Provider timeouts (seconds)
    request_timeout: int = 30

    # Optional third-party adapter configuration. The file is JSON and may use
    # ${ENV_VAR} placeholders so credentials never need to be committed.
    adapter_config_path: str = ""
    load_entrypoint_adapters: bool = True

    model_config = {"env_prefix": "KLINE_", "env_file": ".env", "extra": "ignore"}


def get_settings() -> Settings:
    return Settings()


# Ensure data directory exists
def ensure_data_dir(settings: Settings) -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
