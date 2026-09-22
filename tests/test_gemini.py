from __future__ import annotations

import pytest

from talos.gemini import (
    aiplatform_root,
    generate_content_url,
    model_publish_location,
    rag_corpora_url,
)

pytestmark = pytest.mark.unit


def test_regional_workload_publishes_model_on_global() -> None:
    assert model_publish_location("us-east1") == "global"
    url = generate_content_url("lab5-gemini-dev1", "us-east1", "gemini-3.8-flash")
    assert url == (
        "https://aiplatform.googleapis.com/v1/projects/lab5-gemini-dev1"
        "/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    )


def test_explicit_publish_locations_keep_their_hosts() -> None:
    assert aiplatform_root("global") == "https://aiplatform.googleapis.com/v1"
    assert aiplatform_root("us") == "https://aiplatform.us.rep.googleapis.com/v1"
    assert generate_content_url("p", "us", "gemini-3.8-flash") == (
        "https://aiplatform.us.rep.googleapis.com/v1/projects/p/locations/us"
        "/publishers/google/models/gemini-3.8-flash:generateContent"
    )


def test_rag_stays_on_the_workload_region() -> None:
    assert rag_corpora_url("lab5-gemini-dev1", "us-east1") == (
        "https://us-east1-aiplatform.googleapis.com/v1/projects/lab5-gemini-dev1"
        "/locations/us-east1/ragCorpora"
    )
