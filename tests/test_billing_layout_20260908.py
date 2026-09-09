"""1항차 재발행 청구서(2026-09-08) 양식 변화 — VT 제외, 열 재배치, 날짜가 문자열이 됐다.

실측:
  · 시트 CP-P1(전체) 24열 (이전 31열). VT 수량 열 5개와 'Rebar & etc' 가 통째로 사라졌다.
  · PT Point 가 11~13, UT Point 가 14~16, Weld Map No. 가 17 로 앞당겨졌다 (이전 18·21·24).
  · **날짜가 `05.08.2024` 문자열**이다 (이전엔 datetime). 이전 파일의 같은 성적서번호가 2024-08-05 이므로
    **일.월.연도**로 확정 — 추측이 아니라 대조로 정했다.
  · 수량은 방법마다 다른 열에 있다 (PT 는 PT Point 의 Q'ty, UT 는 UT Point 의 Q'ty).
"""
from datetime import date

from app.extractors import excel_parser as ep


def test_dotted_day_first_date_is_read():
    assert ep._to_date("05.08.2024") == date(2024, 8, 5)
    assert ep._to_date("22.01.2025") == date(2025, 1, 22)
    assert ep._to_date(" 31.12.2024 ") == date(2024, 12, 31)


def test_impossible_day_first_date_is_refused_not_swapped():
    """13.13.2024 처럼 어느 쪽으로도 안 되는 값을 임의로 뒤집어 읽으면 조용히 틀린 날짜가 된다."""
    assert ep._to_date("13.13.2024") is None
    assert ep._to_date("2024.08.05") == date(2024, 8, 5)      # 연도 먼저는 그대로


def test_quantity_comes_from_the_method_group(tmp_path):
    """PT 행은 'PT Point' 묶음의 Q'ty, UT 행은 'UT Point' 묶음의 Q'ty 를 수량으로 쓴다."""
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "CP-P1(전체)"
    ws.append(["No", "Unit", "Application No.", "Report Number", "Date of Testing", "Type", "Item",
               "Result", "Dimension\n(Ø×t, mm)", "Thickness\n(mm)", "PT Point ", None, None,
               "UT Point", None, None, "Weld Map No."])
    ws.append([None] * 17)
    ws.append([None] * 10 + ["m", "Q'ty", "Total(m)", "m", "Q'ty", "Total(m)", None])
    ws.append([1, 1, "NDT-P0330", "77-071PT", "05.08.2024", "PT", "SW0105", "A", "108X5", 5,
               0.339, 3, 1.017, None, None, None, "NP.008.CCW.ABD.1.021.0001"])
    ws.append([2, 1, "NDT-U0001", "77-005UT", "06.08.2024", "UT", "FW12", "A", "3040x20", 20,
               None, None, None, 1.2, 7, 8.4, "NP.008.CCW.ABD.1.021.0002"])
    f = tmp_path / "b.xlsx"; wb.save(f)
    rows = ep.parse_billing_xlsx(f, discipline_hint="CP-P1").rows
    by = {r["joint_no"]: r for r in rows}
    assert by["SW0105"]["quantity"] == 3 and by["SW0105"]["inspection_date"] == date(2024, 8, 5)
    assert by["FW12"]["quantity"] == 7 and by["FW12"]["ndt_method"] == "UT"
    assert by["SW0105"]["drawing_no"] is None          # 도면번호 열은 여전히 없다
