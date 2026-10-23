from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GW_", env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    # Includes event-loop lag, so too tight a value fails healthy calls under load.
    redis_timeout_ms: int = Field(default=100, gt=0)
    redis_max_connections: int = Field(default=512, gt=0)
    key_prefix: str = "rl"

    upstream_url: str = "http://localhost:8001"
    upstream_timeout_s: float = Field(default=10.0, gt=0)
    upstream_max_connections: int = Field(default=1000, gt=0)

    policies_file: str = "policies.yaml"
    # What to do when Redis is unreachable: let traffic through, or shed it with 503.
    fail_mode: Literal["open", "closed"] = "open"

    # Only peers inside these networks may set X-Forwarded-For or CDN geo headers.
    # From the environment this is a comma-separated list.
    trusted_proxies: Annotated[list[str], NoDecode] = ["127.0.0.1/32", "::1/128"]
    # IPv6 clients are grouped by prefix; a single host usually owns a whole /64.
    ipv6_prefix: int = Field(default=64, ge=1, le=128)

    enable_check_endpoint: bool = True

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value
