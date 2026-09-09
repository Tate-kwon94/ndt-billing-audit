"""청구서 Item(조인트) 칸의 압축 표기 해석 — 1항차 실 데이터 4,354행 실측 기반 (2026-09-07).

표기는 닫힌 문법이었다 (실측: 단순분리로 안 맞는 504행이 전부 아래 3종, '기타' 0건):
  · `a÷b`        범위          Sp11.1÷6      → Sp11.1 … Sp11.6            (6개)
  · `(a÷b)X-y,z` 곱셈          (1÷65)SC2-1,2 → 65 × {SC2-1, SC2-2}        (130개)
  · `X,Y,Z-p,q`  접미사 곱셈   R.10,11,12-up,down → {10,11,12}×{up,down}  (6개)
확장 개수는 청구 수량 열과 대조한다. 어긋나면 그 행이 진짜 불일치다 (과다청구 후보).
"""
import pytest

from app.extractors import item_notation as it


@pytest.mark.parametrize("text, n, first, last", [
    ("FW12", 1, "FW12", "FW12"),
    ("Sp11.1÷6", 6, "Sp11.1", "Sp11.6"),
    ("1/3÷1/4", 2, "1/3", "1/4"),                       # 분수형 범위는 못 세면 원문 유지
    ("R.10,11,12-up,down", 6, "R.10-up", "R.12-down"),
    ("R1,2,3-up,dn", 6, "R1-up", "R3-dn"),
    ("(1÷65)SC2-1,2", 130, "1SC2-1", "65SC2-2"),
    ("S.1E-(up-1,2,dn-1,2)", 4, "S.1E-up-1", "S.1E-dn-2"),
    ("10GML90BR002-G01.(1÷4)", 4, "10GML90BR002-G01.1", "10GML90BR002-G01.4"),   # 뒤에 붙는 괄호 범위
    ("R.4.(1÷3)-dn,up", 6, "R.4.1-dn", "R.4.3-up"),          # 중간 괄호범위 × 접미사
    ("R.(07÷09)-(dn,up)", 6, "R.07-dn", "R.09-up"),           # 중간 괄호범위 × 괄호 접미사
    ("FW12, FW29", 2, "FW12", "FW29"),
    ("FW1\nFW2", 2, "FW1", "FW2"),
    ("", 0, None, None),
])
def test_expand_counts_and_ends(text, n, first, last):
    out = it.expand(text)
    assert len(out) == n, out
    if n:
        assert out[0] == first and out[-1] == last, out


def test_expand_is_deduped_and_ordered():
    assert it.expand("FW1, FW1, FW2") == ["FW1", "FW2"]


def test_unparseable_text_survives_as_one_item():
    assert it.expand("설명 문장 그대로") == ["설명 문장 그대로"]


def test_count_matches_quantity_flags_only_real_gaps():
    assert it.reconcile("Sp11.1÷6", 6) == {"expanded": 6, "billed": 6, "ok": True, "delta": 0}
    r = it.reconcile("FW12", 3)
    assert r["ok"] is False and r["delta"] == 2      # 조인트 1개인데 3개 청구 = 과다 후보


def test_reconcile_tolerates_missing_quantity():
    assert it.reconcile("FW12", None)["ok"] is None


def test_normalize_weld_map_cell_splits_and_cleans():
    # 실측 오염: 앞따옴표 7행, 콤마+줄바꿈 혼용 1,437행, 앞뒤 공백 27행
    assert it.weld_maps("'NP.008.CCW.ABD.1.021.0005") == ["NP.008.CCW.ABD.1.021.0005"]
    assert it.weld_maps("NP.008.UGB.ABD.1.052.0005,NP.008.UGB.ABD.1.052.0006\nNP.008.UGB.ABD.1.052.0007") == [
        "NP.008.UGB.ABD.1.052.0005", "NP.008.UGB.ABD.1.052.0006", "NP.008.UGB.ABD.1.052.0007"]
    assert it.weld_maps("  NP.008.CCW.ABD.1.021.0001  ") == ["NP.008.CCW.ABD.1.021.0001"]
    assert it.weld_maps(None) == []
    assert it.weld_maps("NP.A, NP.A") == ["NP.A"]
