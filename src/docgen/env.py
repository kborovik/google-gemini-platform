from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from docgen.constants import CANONICAL_ENV, REQUIRED_ENV
from docgen.errors import TalosError

OUTPUTS_JSON_RELATIVE = "infra/outputs.json"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def parse_terraform_output(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    values: dict[str, str] = {}
    for key, spec in data.items():
        value: Any
        if isinstance(spec, dict) and "value" in spec:
            value = spec["value"]
        else:
            value = spec
        if value is None:
            continue
        rendered = value if isinstance(value, str) else str(value)
        if rendered:
            values[str(key)] = rendered
    return values


def load_terraform_output() -> dict[str, str]:
    path = repo_root() / OUTPUTS_JSON_RELATIVE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    parsed = parse_terraform_output(text)
    return {
        key: value for key, value in parsed.items() if key in CANONICAL_ENV and value
    }


def fill_missing(env: MutableMapping[str, str], extra: dict[str, str]) -> None:
    for key, value in extra.items():
        if value and not env.get(key):
            env[key] = value


def resolve_env(*, use_terraform: bool) -> dict[str, str]:
    env = dict(os.environ)
    if not use_terraform:
        return env
    fill_missing(env, load_terraform_output())
    return env


def gcs_generate_configured(env: dict[str, str], bucket: str = "") -> bool:
    return bool(bucket or env.get("GCS_BUCKET"))


def resolve_generate_env(*, use_terraform: bool) -> dict[str, str]:
    env = dict(os.environ)
    if not use_terraform:
        return env
    if gcs_generate_configured(env):
        return env
    fill_missing(env, load_terraform_output())
    return env


def missing_required(env: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED_ENV if not env.get(name)]


def require_env(env: dict[str, str]) -> None:
    missing = missing_required(env)
    if missing:
        names = ", ".join(missing)
        raise TalosError(
            f"Google Cloud environment is not configured (missing {names}). "
            "Set the variables, run `gmake terraform-apply` (writes "
            "`infra/outputs.json`), or pass CLI flags.",
            exit_code=2,
        )
