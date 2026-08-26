"""Environment configuration for the control plane (SPEC §5.7).

Every knob is a `HARNESS_*` environment variable. Nothing here has a default that
points at a real host, a real key, or a real subscription — see CLAUDE.md rule 5.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All `HARNESS_*` configuration, validated once at import of `get_settings()`."""

    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_file=None,
        extra="ignore",
    )

    # --- auth ------------------------------------------------------------
    # Required in production; empty means "no key configured", which auth.py
    # treats as deny-everything rather than allow-everything.
    api_key: str = ""

    # --- catalog ---------------------------------------------------------
    models_file: Path = Path("/etc/harness/models.yaml")

    # --- backends --------------------------------------------------------
    default_backend: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    vllm_bin: Path = Path("/opt/harness/.venv/bin/vllm")
    vllm_port: int = 8000
    remote_base_url: str = ""
    remote_api_key: str = ""

    # --- supervisor ------------------------------------------------------
    state_dir: Path = Path("/var/lib/harness")
    drain_timeout_s: float = 30.0
    load_timeout_s: float = 900.0
    autoload_last: bool = True
    # Model id to activate at startup. Empty means "activate nothing".
    autoload_model: str = ""
    # How often the supervisor asks the backend whether the model is serving.
    health_poll_interval_s: float = Field(default=2.0, gt=0)

    # --- logging ---------------------------------------------------------
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so env is read once; clear it in tests."""
    return Settings()
