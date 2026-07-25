"""Loads config.yaml into a dotted-access object, and secrets from .env.

Why dotted access: ``cfg.limits.max_concurrent_positions`` reads like the PRD
and fails loudly with a clear name if a key is missing, instead of quietly
returning ``None`` the way ``dict.get`` would.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"


class MissingSecret(RuntimeError):
    """Raised when a required credential is absent.

    Deliberately fatal. PRD §9 says fail closed: no guessing, no defaults, no
    proceeding with a half-configured system.
    """


@dataclass(frozen=True)
class Config:
    """Read-only view over config.yaml.

    Nested mappings come back as ``Config`` too, so ``cfg.universe.min_market_cap_usd``
    works all the way down.
    """

    _data: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError:
            raise AttributeError(
                f"config.yaml has no setting '{name}'. "
                f"Available at this level: {sorted(self._data)}"
            ) from None
        return Config(value) if isinstance(value, dict) else value

    def __getitem__(self, name: str) -> Any:
        return self.__getattr__(name)

    def get(self, name: str, default: Any = None) -> Any:
        value = self._data.get(name, default)
        return Config(value) if isinstance(value, dict) else value

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config({sorted(self._data)})"


def load_config(path: str | Path | None = None) -> Config:
    """Read config.yaml. Called once at startup by every entry point."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    # encoding is explicit on purpose — see init_schema in store/db.py. config.yaml
    # has section marks in its comments and would fail to load on Windows without it.
    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data)


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader.

    Written by hand rather than pulled from a library so there is one less
    dependency between a plain text file and the process environment. Values
    already set in the real environment win, which is what CI and cron expect.
    """
    if not path.exists():
        return
    # A name with an accent in EDGAR_USER_AGENT is enough to break this on
    # Windows without the explicit encoding.
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_dotenv(REPO_ROOT / ".env")


def secret(name: str, default: str | None = None) -> str | None:
    """Fetch a credential. Returns ``default`` when unset."""
    value = os.environ.get(name, default)
    return value or default


def require_secret(name: str, why: str) -> str:
    """Fetch a credential, or stop the run with an actionable message."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSecret(
            f"{name} is not set, and it is needed to {why}.\n"
            f"Fix: copy .env.example to .env and fill in {name}.\n"
            f"  cp .env.example .env\n"
            f"See RUNBOOK.md, section 'Setting up credentials'."
        )
    return value
