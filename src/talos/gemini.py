from __future__ import annotations

from typing import Any

from talos.constants import DEFAULT_EMBEDDING_PUBLISHER_MODEL


def aiplatform_root(location: str) -> str:
    return f"https://{location}-aiplatform.googleapis.com/v1"


def generate_content_url(project: str, location: str, model: str) -> str:
    return (
        f"{aiplatform_root(location)}/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )


def rag_corpora_url(project: str, location: str) -> str:
    return f"{aiplatform_root(location)}/projects/{project}/locations/{location}/ragCorpora"


def rag_import_url(corpus_name: str, location: str) -> str:
    return f"{aiplatform_root(location)}/{corpus_name}/ragFiles:import"


def operation_url(location: str, name: str) -> str:
    if name.startswith("https://"):
        return name
    return f"{aiplatform_root(location)}/{name}"


def rag_files_url(corpus_name: str, location: str) -> str:
    return f"{aiplatform_root(location)}/{corpus_name}/ragFiles"


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


def corpus_create_body(display_name: str, description: str) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "description": description,
        "ragEmbeddingModelConfig": {
            "vertexPredictionEndpoint": {
                "publisherModel": DEFAULT_EMBEDDING_PUBLISHER_MODEL,
            }
        },
    }


def import_body(
    uris: list[str], *, chunk_size: int, chunk_overlap: int
) -> dict[str, Any]:
    return {
        "importRagFilesConfig": {
            "gcsSource": {"uris": uris},
            "ragFileChunkingConfig": {
                "chunkSize": chunk_size,
                "chunkOverlap": chunk_overlap,
            },
        }
    }


def retrieval_tool(corpus_name: str) -> dict[str, Any]:
    return {
        "retrieval": {
            "vertexRagStore": {
                "ragResources": [{"ragCorpus": corpus_name}],
            }
        }
    }


def file_uri(rag_file: dict[str, Any]) -> str:
    for key in ("gcsSource", "gcs_source"):
        source = rag_file.get(key)
        if isinstance(source, dict):
            uris = source.get("uris") or []
            if isinstance(uris, list) and uris:
                return str(uris[0])
    for key in ("displayName", "display_name", "name"):
        value = rag_file.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
