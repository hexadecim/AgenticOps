"""Typed resilience configuration, loadable from deploy/resilience.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BreakerConfig(BaseModel):
    fail_max: int = Field(default=5, ge=1, description="Consecutive failures that open the circuit")
    reset_timeout: float = Field(default=30.0, gt=0, description="Seconds open before a half-open probe")
    half_open_max_calls: int = Field(default=1, ge=1, description="Concurrent probes allowed when half-open")


class RetryConfig(BaseModel):
    attempts: int = Field(default=3, ge=1, description="Total attempts including the first")
    base_delay: float = Field(default=0.2, ge=0)
    max_delay: float = Field(default=2.0, ge=0)


class ResilienceConfig(BaseModel):
    timeout: float = Field(default=10.0, gt=0, description="Per-attempt timeout in seconds")
    bulkhead: int = Field(default=20, ge=1, description="Max concurrent calls")
    bulkhead_wait: float = Field(default=0.5, ge=0, description="Seconds to wait for a bulkhead slot")
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class ResilienceSettings(BaseModel):
    """All dependencies' settings; unknown names fall back to `defaults`."""

    defaults: ResilienceConfig = Field(default_factory=ResilienceConfig)
    dependencies: dict[str, ResilienceConfig] = Field(default_factory=dict)

    def for_dependency(self, name: str) -> ResilienceConfig:
        return self.dependencies.get(name, self.defaults)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ResilienceSettings:
        return cls.from_mapping(yaml.safe_load(Path(path).read_text()) or {})

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ResilienceSettings:
        """Each dependency block is a partial override deep-merged over `defaults`."""
        base = ResilienceConfig.model_validate(raw.get("defaults") or {}).model_dump()
        deps = {
            name: ResilienceConfig.model_validate(_deep_merge(base, body or {}))
            for name, body in (raw.get("dependencies") or {}).items()
        }
        return cls(defaults=ResilienceConfig.model_validate(base), dependencies=deps)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
