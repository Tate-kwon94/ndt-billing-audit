"""도면 파일명 회귀 (2026-09-06 실제 샘플에서 발견).

- '95.', '106.', '4. ' 같은 다운로드 순번 접두어가 붙은 DC 파일이 정규식에 안 걸려 drawing_no=None
  → 이번 청구 대상(10~40 UMA) 도면 3건이 KKS 범위 키 없이 조용히 빠진다.
- SCWEP 새 파일 '…KE.0001.E=C01, 10UMA HC WEP CW Pipe 설치.pdf': 개정이 '=C01' 이고 꼬리가 붙음.
- 도면번호는 파일 안 표제란에도 있다 (사용자). 파일명과 다르면 두 값을 needs_review 로.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.extractors.drawing import classifier as cl


def _fake_extract(text: str):
    return lambda file_path: SimpleNamespace(pages=[SimpleNamespace(text=text), SimpleNamespace(text="")])


@pytest.mark.parametrize("name,expected_no,expected_type,expected_rev", [
    ("95.NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf", "NP.D.N000.3.0UMA&&PAB&&&.021", "DC", "C01"),
    ("106.NP.D.N000.4.0UMA&&PAB&&&.021.DC.0001.E_C02.pdf", "NP.D.N000.4.0UMA&&PAB&&&.021", "DC", "C02"),
    ("4. NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C02.pdf", "NP.D.N000.1.0UMA&&PAB&&&.021", "DC", "C02"),
    ("NP.D.N000.2.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf", "NP.D.N000.2.0UMA&&PAB&&&.021", "DC", "C01"),
    ("NP.D.P000.1.0UGB95GML90&.052.SD.0001.E_C03_scan.pdf", "NP.D.P000.1.0UGB95GML90&.052", "SD", "C03"),
])
def test_prefixed_names_yield_clean_drawing_no(monkeypatch, name, expected_no, expected_type, expected_rev):
    monkeypatch.setattr(cl, "extract", _fake_extract(""))     # 표제란 없음 → 대조 생략
    c = cl.classify(Path(name))
    assert c.source == "filename"
    assert c.drawing_no == expected_no
    assert c.drawing_type == expected_type
    assert c.revision == expected_rev
    assert c.needs_review is False


def test_scwep_name_with_equals_revision_and_free_tail(monkeypatch):
    monkeypatch.setattr(cl, "extract", _fake_extract(""))
    c = cl.classify(Path("NP.D.P034.1.0UMA&&PAB&&&.015.KE.0001.E=C01, 10UMA HC WEP CW Pipe 설치.pdf"))
    assert c.drawing_type == "KE"
    assert c.drawing_no == "NP.D.P034.1.0UMA&&PAB&&&.015"
    assert c.revision == "C01"
    assert c.needs_review is True                 # KE = 도면 아님 (기존 규칙)


def test_title_block_mismatch_is_flagged_with_both_numbers(monkeypatch):
    text = "TITLE BLOCK ... Drawing No: NP.D.N000.3.0UMA&&PAB&&&.021.DC.0002 Rev C01"
    monkeypatch.setattr(cl, "extract", _fake_extract(text))
    c = cl.classify(Path("95.NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf"))
    assert c.drawing_no == "NP.D.N000.3.0UMA&&PAB&&&.021"     # 파일명 값은 그대로 둔다 (추론으로 바꾸지 않음)
    assert c.needs_review is True
    joined = " ".join(c.review_reasons)
    assert "NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001" in joined and "NP.D.N000.3.0UMA&&PAB&&&.021.DC.0002" in joined


def test_title_block_match_or_absence_does_not_flag(monkeypatch):
    same = "NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001"
    monkeypatch.setattr(cl, "extract", _fake_extract(f"... {same} ..."))
    assert cl.classify(Path("95.NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf")).needs_review is False
    monkeypatch.setattr(cl, "extract", _fake_extract("no number here"))
    assert cl.classify(Path("95.NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf")).needs_review is False


def test_title_block_extraction_failure_does_not_break_classification(monkeypatch):
    def boom(file_path):
        raise RuntimeError("pdf broken")
    monkeypatch.setattr(cl, "extract", boom)
    c = cl.classify(Path("NP.D.N000.2.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf"))
    assert c.drawing_no == "NP.D.N000.2.0UMA&&PAB&&&.021" and c.needs_review is False


def test_title_block_no_helper_normalizes(monkeypatch):
    monkeypatch.setattr(cl, "extract", _fake_extract("drawing no. np.d.n000.3.0uma&&pab&&&.021.dc.0001 rev c01"))
    assert cl._title_block_no(Path("x.pdf")) == "NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001"


def test_bracket_tag_prefix_is_ignored(monkeypatch):
    """사내 ② 실패 로그의 파일명: '[ABD]NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C03.pdf' — 대괄호 태그 접두어."""
    monkeypatch.setattr(cl, "extract", _fake_extract(""))
    c = cl.classify(Path("[ABD]NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C03.pdf"))
    assert c.source == "filename"
    assert c.drawing_no == "NP.D.N000.1.0UMA&&PAB&&&.021" and c.drawing_type == "DC" and c.revision == "C03"
    c2 = cl.classify(Path("[ABD] 95.NP.D.N000.3.0UMA&&PAB&&&.021.DC.0001.E_C01.pdf"))     # 둘 다 붙어도
    assert c2.drawing_no == "NP.D.N000.3.0UMA&&PAB&&&.021"
