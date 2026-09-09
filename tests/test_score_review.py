"""① 검토결과를 답안지와 행 단위로 대조해 점수를 낸다 (2026-09-09).

사내에서 필요한 것은 답안지 엑셀을 눈으로 보는 게 아니라 **숫자**다. 답안지 JSON 은 패치로 들어가고
(엑셀은 반입 정책상 불가), 검토결과 xlsx 는 사내 출력물이므로 그 둘을 대조하는 명령을 넣는다.
"""
import openpyxl

from app.report import score_review as sr


def _result(tmp_path, rows):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["No", "Report Number", "Item", "매칭_방법", "적합성_판정", "재확인_필요"])
    for r in rows: ws.append(r)
    f = tmp_path / "r.xlsx"; wb.save(f); return f


def test_scores_matching_and_verdict_separately(tmp_path):
    key = {"rows": [
        {"성적서번호": "77-071PT", "Item": "SW0105", "최종정답": "OK", "성적서_있음": "True"},
        {"성적서번호": "77-999PT", "Item": "FW01", "최종정답": "성적서없음", "성적서_있음": "False"},
    ]}
    res = _result(tmp_path, [[1, "77-071PT", "SW0105", "deterministic", "OK", None],
                             [2, "77-999PT", "FW01", "none", "SUSPECT", "예"]])
    s = sr.score(key, res)
    assert s["matching"]["일치"] == 2 and s["matching"]["불일치"] == 0
    assert s["verdict"]["OK→OK"] == 1
    assert s["rows_scored"] == 2 and s["rows_missing_from_result"] == 0


def test_report_missing_row_is_counted_not_silently_dropped(tmp_path):
    key = {"rows": [{"성적서번호": "77-001PT", "Item": "FW1", "최종정답": "OK", "성적서_있음": "True"}]}
    s = sr.score(key, _result(tmp_path, []))
    assert s["rows_scored"] == 0 and s["rows_missing_from_result"] == 1


def test_unjudgeable_rows_are_correct_only_when_flagged(tmp_path):
    """'성적서없음'·'도면미보유' 는 OK 로 통과시키면 안 된다 — 재확인 표시가 있어야 맞은 것이다."""
    key = {"rows": [
        {"성적서번호": "A", "Item": "1", "최종정답": "성적서없음", "성적서_있음": "False"},
        {"성적서번호": "B", "Item": "2", "최종정답": "도면미보유", "성적서_있음": "True"},
    ]}
    res = _result(tmp_path, [[1, "A", "1", "none", "OK", None], [2, "B", "2", "deterministic", "SUSPECT", "예"]])
    s = sr.score(key, res)
    assert s["unjudgeable"]["표시함"] == 1 and s["unjudgeable"]["놓침"] == 1


def test_normalizes_report_number_spacing(tmp_path):
    key = {"rows": [{"성적서번호": "77-071PT", "Item": "SW0105", "최종정답": "OK", "성적서_있음": "True"}]}
    s = sr.score(key, _result(tmp_path, [[1, "77-071PT", "SW0105", "deterministic", "OK", None]]))
    assert s["rows_scored"] == 1
