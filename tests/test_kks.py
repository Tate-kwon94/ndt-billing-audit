"""문서번호 다섯째 마디(고정폭 12자: 건물 6 + 계통 6) — SCWEP 와 도면을 잇는 유일한 범위 키."""
from app.extractors import kks
import pytest


@pytest.mark.parametrize("no,bcode,scode,raw", [
    ("NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E", "UMA", "LAH", "0UMA&&LAH&&&"),
    ("NP.D.P000.1.0UGB95GML90&.052.DC.0001.E", "UGB", "GML", "0UGB95GML90&"),
    ("NP.D.P000.3.0USJ&&GUC&&&.052.DC.0001.E", "USJ", "GUC", "0USJ&&GUC&&&"),
    ("NP.D.P000.9.1ULD&&GMM91&.052.BG.0001.E", "ULD", "GMM", "1ULD&&GMM91&"),
])
def test_parse_real_numbers(no, bcode, scode, raw):
    p = kks.parse(no)
    assert (p["building_code"], p["system_code"], p["raw"]) == (bcode, scode, raw)


def test_parse_keeps_full_tokens_and_unit():
    p = kks.parse("NP.D.P000.9.1ULD&&GMM91&.052.BG.0001.E")
    assert p["unit"] == "9" and p["building"] == "1ULD" and p["system"] == "GMM91"


def test_parse_rejects_short_or_synthetic():
    assert kks.parse("NP.D.X..052.DC.0001.E") is None
    assert kks.parse("MD.D.P000.1.0KBA10&&&&.052.DC.0001.E") is None    # 합성 데모 번호 — 계통 마디가 &&&& 뿐
    assert kks.parse(None) is None


def test_same_scope():
    scwep = kks.parse("NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E")
    assert kks.same_scope(scwep, kks.parse("NP.D.P000.1.0UMA&&LAH10&.052.DC.0001.E")) == "yes"
    assert kks.same_scope(scwep, kks.parse("NP.D.P000.1.0UGB95GML90&.052.DC.0001.E")) == "no"
    assert kks.same_scope(scwep, kks.parse("NP.D.P000.1.0UMA&&LBA&&&.052.DC.0001.E")) == "no"   # 같은 건물, 다른 계통
    assert kks.same_scope(scwep, None) == "unknown"


def test_parse_segment_alone():
    p = kks.parse_segment("0UMA&&LAH&&&")
    assert p and (p["building_code"], p["system_code"], p["unit"]) == ("UMA", "LAH", None)
    assert kks.parse_segment("0KBA10&&&&") is None and kks.parse_segment(None) is None
