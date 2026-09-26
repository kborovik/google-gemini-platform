"""Unprovision leftover RAG Engine Spanner tiers that hold no corpora.

The demo indexes policies in Agent Search. A Basic or Scaled RAG Engine tier
still bills Spanner while it is idle. Serverless and already unprovisioned
regions are left alone. A region with corpora is refused.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_REGIONS = (
    "us-west1",
    "us-east4",
    "us-south1",
    "us-west4",
    "europe-west1",
    "europe-west4",
    "asia-east1",
    "asia-northeast1",
    "northamerica-northeast1",
)

_POLL_SECONDS = 5
_POLL_ATTEMPTS = 120


def tier_of(config: dict[str, Any]) -> str:
    managed = config.get("ragManagedDbConfig") or {}
    spanner = managed.get("spanner") or {}
    if "unprovisioned" in managed or "unprovisioned" in spanner:
        return "unprovisioned"
    if "serverless" in managed:
        return "serverless"
    if "scaled" in managed or "scaled" in spanner:
        return "scaled"
    if "basic" in managed or "basic" in spanner:
        return "basic"
    return "unknown"


def action_for(tier: str, corpus_count: int | None) -> str:
    if tier in {"unprovisioned", "serverless"}:
        return "skip"
    if tier not in {"basic", "scaled"}:
        raise ValueError(f"unrecognized tier {tier}")
    if corpus_count:
        raise ValueError(f"{corpus_count} corpora")
    return "patch"


def regions_from_env() -> tuple[str, ...]:
    raw = os.environ.get("RAG_IDLE_REGIONS", "")
    if not raw.strip():
        return DEFAULT_REGIONS
    return tuple(raw.split())


def _token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if not token:
        raise SystemExit("gcloud auth print-access-token returned an empty token")
    return token


def _api(
    method: str, url: str, token: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise SystemExit(f"{method} {url} failed: {exc.code} {detail}") from exc
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SystemExit(f"{method} {url} returned {type(parsed).__name__}")
    return parsed


def _config_url(project: str, location: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/ragEngineConfig"
    )


def _corpus_count(project: str, location: str, token: str) -> int:
    total = 0
    page = ""
    while True:
        url = (
            f"https://{location}-aiplatform.googleapis.com/v1/"
            f"projects/{project}/locations/{location}/ragCorpora"
        )
        if page:
            url += "?" + urllib.parse.urlencode({"pageToken": page})
        payload = _api("GET", url, token)
        total += len(payload.get("ragCorpora") or [])
        page = payload.get("nextPageToken") or ""
        if not page:
            return total


def _wait(location: str, payload: dict[str, Any], token: str) -> None:
    if "ragManagedDbConfig" in payload:
        return
    name = payload.get("name") or ""
    if "/operations/" not in name:
        raise SystemExit(f"{location} unprovision returned no operation")
    url = f"https://{location}-aiplatform.googleapis.com/v1/{name}"
    current = payload
    for _ in range(_POLL_ATTEMPTS):
        if current.get("done"):
            error = current.get("error")
            if error:
                raise SystemExit(f"{location} unprovision failed: {json.dumps(error)}")
            return
        time.sleep(_POLL_SECONDS)
        current = _api("GET", url, token)
    raise SystemExit(f"{location} unprovision timed out")


def main() -> None:
    project = os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("PROJECT is required")
    token = _token()
    planned: list[tuple[str, str]] = []
    for location in regions_from_env():
        tier = tier_of(_api("GET", _config_url(project, location), token))
        count = (
            _corpus_count(project, location, token)
            if tier in {"basic", "scaled"}
            else None
        )
        try:
            action = action_for(tier, count)
        except ValueError as exc:
            raise SystemExit(f"{location}: {exc}") from exc
        print(f"{location} {tier} {action}", flush=True)
        planned.append((location, action))
    for location, action in planned:
        if action == "skip":
            continue
        print(f"{location} unprovision", flush=True)
        operation = _api(
            "PATCH",
            _config_url(project, location),
            token,
            {"ragManagedDbConfig": {"spanner": {"unprovisioned": {}}}},
        )
        _wait(location, operation, token)
        confirmed = tier_of(_api("GET", _config_url(project, location), token))
        if confirmed != "unprovisioned":
            raise SystemExit(f"{location} still {confirmed} after unprovision")
        print(f"{location} unprovisioned", flush=True)


if __name__ == "__main__":
    main()
