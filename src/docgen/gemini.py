from __future__ import annotations

from typing import Any


_MULTI_REGION_ROOTS = {
    "us": "https://aiplatform.us.rep.googleapis.com/v1",
    "eu": "https://aiplatform.eu.rep.googleapis.com/v1",
}


def aiplatform_root(location: str) -> str:
    if location == "global":
        return "https://aiplatform.googleapis.com/v1"
    multi = _MULTI_REGION_ROOTS.get(location)
    if multi is not None:
        return multi
    return f"https://{location}-aiplatform.googleapis.com/v1"


def model_publish_location(location: str) -> str:
    """gemini-3.8-flash is published on global and us/eu, not on a single region."""
    if location in {"global", "us", "eu"}:
        return location
    return "global"


def generate_content_url(project: str, location: str, model: str) -> str:
    publish = model_publish_location(location)
    return (
        f"{aiplatform_root(publish)}/projects/{project}/locations/{publish}"
        f"/publishers/google/models/{model}:generateContent"
    )


def retrieval_tool(data_store: str) -> dict[str, Any]:
    """Ground generateContent on one Agent Search data store."""
    return {
        "retrieval": {
            "vertexAiSearch": {
                "datastore": data_store,
            }
        }
    }


def candidate_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)
