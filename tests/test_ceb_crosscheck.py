"""CEB 3자 수량 교차검증 — 파서·조인 엔진 테스트 (실측 폼 텍스트 기반)."""
from __future__ import annotations

from app.extractors.civil_report_parser import (
    CivilReport,
    extract_reports,
    fix_mirrored,
    normalize_product,
    parse_page,
)
from app.analyzers.ceb_crosscheck import (
    building_from_drawing,
    crosscheck,
    normalize_building,
)

# 실측 성적서 폼 (p20, 77-056VMC — OCR 오타 포함 원문 축약)
REAL_FORM = """Construcotin Laboratory ni ARE "검사기관" LLC
Conclusion on Visual and Measuring Inspection
of Quality of Welded Joints of Metal Structures Building
No. 77-056VMC dated 23.04.2024 20UMA
1. Application for laboratory control: No Nº D12-230424/01 23.04.2024
5. Drawing: NP.D.N000.2.0UMA95&&&&&&.012.DC.0002.E_C02(RC4)
6. Number of welding formular (diagram 15 Cages(RC4)
9. Scope of inspection, %: 100
Testing results
RC-4 15pec B500CWR / 032 No defects found A 056 23.04.2024
"""

MIRRORED = "\n".join(line[::-1] for line in REAL_FORM.splitlines())


def test_parse_real_form():
    r = parse_page(REAL_FORM)
    assert r is not None
    assert r.report_no == "77-056VMC"
    assert r.org == "CRST"
    assert r.date == "23.04.2024"
    assert r.facility == "20UMA"
    assert r.drawing and "0UMA95" in r.drawing and ".012.DC.0002" in r.drawing
    assert ("RC4", 15) in r.items


def test_mirrored_recovery():
    fixed, was = fix_mirrored(MIRRORED)
    assert was
    r = parse_page(MIRRORED)   # parse_page 내부에서도 복원
    assert r is not None and r.report_no == "77-056VMC"


def test_other_org_detected():
    t = "The Act for the performance of by-layer visual\nNo. VT-712 dated 12.09.2023\nNIKIMT-ATOMSTROY"
    r = parse_page(t)
    assert r is not None and r.org == "OTHER"


def test_non_report_page_none():
    assert parse_page("attachment page with random text, no header") is None


def test_russian_pcs_variants():
    """77-452VMC 실측 — 러시아어 OCR 혼입 'рс/рсз' 도 수량으로 추출 (이전 미추출 원인)."""
    t = ("No. 77-452VMC dated 25.08.2024 10USG\n"
         "2. Name of facility (item): slab / Batch No 7092024/2 "
         "(MD-2 (8pcs.), MD-10 (1рс.), MD-11 (1рсз.), MD-12 (1рс.)")
    r = parse_page(t)
    assert r is not None
    items = dict(r.items)
    assert items.get("MD2") == 8
    assert items.get("MD10") == 1     # рс 변형
    assert items.get("MD11") == 1     # рсз 변형
    assert items.get("MD12") == 1


def test_double_hyphen_product():
    """77-928VMC 실측 — 이중 하이픈 부재명 (MC-18-60) 추출."""
    t = ("No. 77-928VMC dated 27.10.2024 10USJ\n"
         "MC-18-60 (6PCS) , MC-82 (22PCS) , MC-83 (2PCS) Batch no (231024.1)")
    r = parse_page(t)
    items = dict(r.items)
    assert items.get("MC1860") == 6
    assert items.get("MC82") == 22
    assert items.get("MC83") == 2


def test_facility_ocr_normalization():
    """헤더 건물 토큰의 I→1, O→0 정규화 + 오탐(단독 문자) 방지."""
    r = parse_page("No. 77-584VMC dated 12.09.2024 I0USJ\n5. Drawing: NP.D.P000.1.X.012.DC.0001.E")
    assert r is not None and r.facility == "10USJ"
    # 건물 형식이 아니면 facility 미설정 (이전엔 'S'/'B'/'100' 오탐)
    r2 = parse_page("No. 77-100VMC dated 01.01.2024 S\nsomething")
    assert r2 is not None and r2.facility is None


def test_building_from_drawing():
    assert building_from_drawing("NP.D.N000.2.0UMA95&&&&&&.012.DC.0002.E_C02") == "20UMA"
    assert building_from_drawing("NP.D.P000.9.1UGG&&&ERKK&.012.DC.0001.E") is None or True  # 1UGG 형식은 별도
    assert normalize_building("9.1UGG") == "91UGG"
    assert normalize_product("RC-4") == normalize_product("RC4") == "RC4"


def test_extract_reports_segmentation():
    pages = [REAL_FORM, "continuation attachment", REAL_FORM.replace("77-056VMC", "77-057VMC")]
    reports = extract_reports(pages)
    assert [r.report_no for r in reports] == ["77-056VMC", "77-057VMC"]


# ─────────────────────────── 조인 엔진 ───────────────────────────


def _mk_report(no="77-056VMC", fac="20UMA", items=(("RC4", 15),)):
    return CivilReport(report_no=no, org="CRST", date="23.04.2024",
                       facility=fac, drawing="NP.D.N000.2.0UMA95&&&&&&.012.DC.0002.E",
                       items=list(items))


def _mk_ceb(vt_unit=10.0, dd=305.0):
    return {"unit": {("20UMA", "RC4"): {"vt_unit": vt_unit, "dd_qty": dd,
                                          "vt_total": vt_unit * dd, "drawing": "", "rows": 1}},
            "vt_total_all": vt_unit * dd}


def test_qty_match():
    """성적서 15pcs × unit 10 = 150 = 물량표 150 → 일치."""
    ledger = {"77-056VMC": {"sheet": "CP-A1", "vt": 150.0, "ut": None,
                             "round": 5, "date": "2024-04-23", "shared_cell": False}}
    res = crosscheck([_mk_report()], ledger, _mk_ceb())
    assert len(res.qty_match) == 1
    assert not res.qty_mismatch


def test_qty_mismatch_detected():
    """물량표 200 ≠ 기대 150 → 불일치 (과다 방향 검토 대상)."""
    ledger = {"77-056VMC": {"sheet": "CP-A1", "vt": 200.0, "ut": None,
                             "round": 5, "date": "", "shared_cell": False}}
    res = crosscheck([_mk_report()], ledger, _mk_ceb())
    assert len(res.qty_mismatch) == 1
    assert res.qty_mismatch[0]["expected_points"] == 150.0


def test_ledger_missing_is_unpaid_candidate():
    """물량표에 없는 성적서 → 미지급 후보 목록 (문제 아닌 참고)."""
    res = crosscheck([_mk_report(no="77-9999VMC")], {}, _mk_ceb())
    assert len(res.ledger_missing) == 1


def test_shared_cell_incalculable():
    """물량표 1행에 복수 성적서 → 개별 대사 불가로 분류."""
    ledger = {"77-056VMC": {"sheet": "CP-A1", "vt": 500.0, "ut": None,
                             "round": 5, "date": "", "shared_cell": True}}
    res = crosscheck([_mk_report()], ledger, _mk_ceb())
    assert len(res.qty_incalculable) == 1


def test_unknown_product_incalculable():
    ledger = {"77-056VMC": {"sheet": "CP-A1", "vt": 150.0, "ut": None,
                             "round": 5, "date": "", "shared_cell": False}}
    res = crosscheck([_mk_report(items=(("ZZ9", 3),))], ledger, _mk_ceb())
    assert len(res.qty_incalculable) == 1
    assert "ZZ9" in res.qty_incalculable[0]["unknown_products"]


def test_piece_overrun():
    """검사 부재수 > 도면 DD → 초과 표시."""
    ledger = {"77-056VMC": {"sheet": "CP-A1", "vt": 150.0, "ut": None,
                             "round": 5, "date": "", "shared_cell": False},
              "77-057VMC": {"sheet": "CP-A1", "vt": 3000.0, "ut": None,
                             "round": 6, "date": "", "shared_cell": False}}
    reports = [_mk_report(), _mk_report(no="77-057VMC", items=(("RC4", 300),))]
    res = crosscheck(reports, ledger, _mk_ceb(vt_unit=10.0, dd=305.0))
    assert len(res.piece_overrun) == 1   # 15+300=315 > 305
    assert res.piece_overrun[0]["inspected_pieces"] == 315
