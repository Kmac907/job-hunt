"""Configuration loading and preflight checks."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(ValueError):
    """A configuration error safe to show to an operator."""


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: float = Field(default=0.60, ge=0, le=1)
    preferences: float = Field(default=0.25, ge=0, le=1)
    company: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def totals_one(self) -> "ScoringWeights":
        if abs(self.requirements + self.preferences + self.company - 1) > 1e-9:
            raise ValueError("scoring weights must total 1.0")
        return self


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locations: list[str] | None = None
    remote: bool | None = None
    salary_min: int | None = Field(default=None, ge=0)


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    titles: list[str] | None = None
    locations: list[str] | None = None
    remote: bool | None = None


class Company(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    careers_url: str | None = None
    since: date | None = None
    scope: Scope | None = None


class CompaniesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    companies: list[Company]


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    max_concurrency: int = Field(default=4, ge=1, le=32)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model must not be blank")
        return value


class CollectorConfig(BaseModel):
    """Network limits for one portal; secret values stay in environment variables."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    destinations: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_redirects: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0, le=30)
    requests_per_second: float = Field(default=2, gt=0, le=100)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    credential_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def configured_destinations(self) -> "CollectorConfig":
        hosts = {host.casefold().rstrip(".") for host in self.allowed_hosts if host.strip()}
        for destination in self.destinations:
            parsed = urlsplit(destination)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("collector destinations must be HTTPS origins without credentials")
            hosts.add(parsed.hostname.casefold().rstrip("."))
        if not hosts:
            raise ValueError("collector requires at least one configured destination")
        self.allowed_hosts = sorted(hosts)
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    resume_path: Path
    companies_path: Path
    output_dir: Path
    as_of: date
    timezone: str
    match_threshold: float = Field(default=0.85, ge=0, le=1)
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
    preferences: Preferences | None = None
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    collectors: list[str | CollectorConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    def snapshot(self, companies: CompaniesFile | None = None) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if companies is not None:
            data["companies"] = companies.model_dump(mode="json")["companies"]
        return data


def _read_yaml(path: Path, label: str) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} file does not exist: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {label} file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {label} file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} file must contain a YAML mapping: {path}")
    return data


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    try:
        return AppConfig.model_validate(_read_yaml(path, "configuration"))
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {path}: {_validation_message(exc)}") from exc


def load_companies(path: str | Path) -> CompaniesFile:
    path = Path(path)
    try:
        return CompaniesFile.model_validate(_read_yaml(path, "companies"))
    except ValidationError as exc:
        raise ConfigError(f"invalid companies file {path}: {_validation_message(exc)}") from exc


def _validation_message(exc: ValidationError) -> str:
    error = exc.errors(include_url=False)[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}" if location else error["msg"]


def resolve_safe(base: Path, configured: Path, label: str) -> Path:
    base = base.resolve()
    candidate = (base / configured).resolve() if not configured.is_absolute() else configured.resolve()
    if candidate != base and base not in candidate.parents:
        raise ConfigError(f"unsafe {label} path escapes the configuration directory: {configured}")
    return candidate


def preflight(config_path: str | Path) -> tuple[AppConfig, CompaniesFile]:
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    base = config_path.parent
    resume = resolve_safe(base, config.resume_path, "resume")
    companies_path = resolve_safe(base, config.companies_path, "companies")
    output = resolve_safe(base, config.output_dir, "output")

    if not resume.is_file():
        raise ConfigError(f"resume file does not exist: {resume}")
    companies = load_companies(companies_path)
    if config.collectors:
        names = ", ".join(item if isinstance(item, str) else item.name for item in config.collectors)
        raise ConfigError(f"configured collectors are not supported yet: {names}")
    model = config.runtime.model or os.getenv("CODEX_MODEL")
    if not model:
        raise ConfigError("runtime model is unset; set runtime.model or CODEX_MODEL")
    config.runtime.model = model

    writable = output if output.exists() else next((p for p in output.parents if p.exists()), None)
    if writable is None or not writable.is_dir() or not os.access(writable, os.W_OK):
        raise ConfigError(f"output location is not writable: {output}")
    return config, companies

