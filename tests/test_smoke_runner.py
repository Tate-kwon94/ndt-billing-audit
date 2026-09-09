"""스모크 러너의 순수 로직만 고정한다 (실제 샘플·서버는 러너 실행이 담당).

- route: stage 별 mock/로컬 전환과 stage 별 로컬 호출 상한
- inject_local: 설정 dict 에 로컬 provider 를 심고 default_provider 를 돌린다 (파일 수정 없음)
- CaseBank: payload 를 stage 별 JSONL 로 남긴다 (사내 모델 비교의 입력)
- write_excerpt: 앞 N쪽 발췌 PDF
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "smoke_samples", Path(__file__).resolve().parent.parent / "scripts" / "smoke_samples.py")
sm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sm)


def test_route_mock_mode_never_goes_local():
    counts = {}
    assert sm.route("matching_judge", "mock", {"matching_judge"}, counts, cap=5) == "mock"


def test_route_local_only_for_listed_stages_up_to_cap():
    counts = {}
    local = {"matching_judge"}
    got = [sm.route("matching_judge", "local", local, counts, cap=2) for _ in range(3)]
    assert got == ["local", "local", "mock"]           # 상한 뒤에는 mock 으로 후퇴
    assert sm.route("ocr_normalize", "local", local, counts, cap=2) == "mock"   # 목록 밖(1회 시험 대상도 아님)은 항상 mock


def test_inject_local_rewrites_provider_and_embedding():
    cfg = {"api": {"base_url": "https://hcx:8443"}, "providers": {"hcx007": {"base_url": "https://hcx:8443"}},
           "embedding": {"base_url": "https://studio:8443", "model": "bge-m3", "dim": 1024}}
    sm.inject_local(cfg, base_url="http://127.0.0.1:11434", model="qwen2.5:7b-instruct",
                    embed_model="bge-m3", embed_dim=1024)
    assert cfg["default_provider"] == "local"
    p = cfg["providers"]["local"]
    assert p["api_style"] == "openai" and p["chat_path"] == "/v1/chat/completions"
    assert p["base_url"] == "http://127.0.0.1:11434" and p["model"] == "qwen2.5:7b-instruct"
    assert cfg["embedding"]["base_url"] == "http://127.0.0.1:11434" and cfg["embedding"]["dim"] == 1024
    assert cfg["providers"]["hcx007"]["base_url"] == "https://hcx:8443"     # 다른 provider 는 건드리지 않음


def test_capture_writes_one_json_line_per_call(tmp_path):
    cap = sm.CaseBank(tmp_path)
    cap.record("matching_judge", {"billing_row": {"report_no": "77-005PT"}, "candidates": []})
    cap.record("matching_judge", {"billing_row": {"report_no": "77-013VMC"}, "candidates": []})
    cap.record("code_lookup", {"question": "q", "context_snippets": []})
    lines = (tmp_path / "matching_judge.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["stage"] == "matching_judge" and rec["payload"]["billing_row"]["report_no"] == "77-005PT"
    assert rec["payload_bytes"] > 0
    assert cap.sizes() == {"matching_judge": 2, "code_lookup": 1}


def test_write_excerpt_keeps_first_n_pages(tmp_path):
    import pypdfium2 as pdfium
    src = tmp_path / "src.pdf"
    d = pdfium.PdfDocument.new()
    for _ in range(5):
        d.new_page(200, 200)
    d.save(src)
    dst = tmp_path / "head.pdf"
    assert sm.write_excerpt(src, dst, 3) == 3
    assert len(pdfium.PdfDocument(dst)) == 3
