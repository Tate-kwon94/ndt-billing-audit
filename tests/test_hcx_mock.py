"""Sanity test for the HCX client mock path (no network)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def force_mock_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("NDT_HCX_MOCK", "1")
    # Redirect cache into a temp dir so tests do not pollute the real cache.
    from app import hcx_client

    monkeypatch.setattr(
        hcx_client, "_cache_path", lambda key: tmp_path / f"{key}.json"
    )
    yield


def test_report_segment_returns_parsed_json():
    from app.hcx_client import call

    resp = call("report_segment", {"prev_page_header": "...", "current_page_header": "..."})
    assert resp.parsed is not None
    assert "is_new_report" in resp.parsed


def test_drawing_dc_extraction_shape():
    from app.hcx_client import call

    resp = call("drawing_dc", {"drawing_no": "X", "text_full": "..."})
    assert resp.parsed["drawing_type"] == "DC"
    assert "inspection_matrix" in resp.parsed


def test_cache_hit_second_call():
    from app.hcx_client import call

    first = call("matching_judge", {"billing_row": {}, "candidates": []})
    second = call("matching_judge", {"billing_row": {}, "candidates": []})
    assert first.parsed == second.parsed
    assert second.cached is True
