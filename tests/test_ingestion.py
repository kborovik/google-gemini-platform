from __future__ import annotations

import pytest

from talos.constants import (
    DEFAULT_APPLICATION_OUTPUT_RELATIVE,
    DEFAULT_CORPUS,
    MIN_APPLICATION_INDEXED_ITEMS,
    MIN_INDEXED_ITEMS,
)
from talos.deploy import VertexRagOps, local_corpus_size, split_counts
from talos.env import repo_root
from talos.gemini import rag_location
from talos.rest import RequestsRest
from tests.live_support import corpus_name

pytestmark = pytest.mark.ingestion


def _indexed_counts(live_env: dict[str, str]) -> tuple[int, int]:
    location = rag_location(live_env["GOOGLE_CLOUD_LOCATION"])
    ops = VertexRagOps(RequestsRest(), live_env["GOOGLE_CLOUD_PROJECT"], location)
    uris = ops.list_file_uris(corpus_name(live_env))
    return split_counts(uris)


def test_policy_files_indexed(live_env: dict[str, str]) -> None:
    policies, _applications = _indexed_counts(live_env)
    assert policies >= MIN_INDEXED_ITEMS, DEFAULT_CORPUS


def test_application_files_indexed(live_env: dict[str, str]) -> None:
    _policies, applications = _indexed_counts(live_env)
    local = local_corpus_size(repo_root() / DEFAULT_APPLICATION_OUTPUT_RELATIVE)
    if local == 0:
        pytest.skip("no local application corpus")
    floor = max(MIN_APPLICATION_INDEXED_ITEMS, local)
    assert applications >= floor
