"""7·8단계 — 가이드 금액 분리, 회차 롤업, doc_type 덮어쓰기 거부. 실제 SQLite(tmp) 경유.

몽키패치만 쓰면 정작 확인이 필요한 경로(doc_type 필터·Finding 조인)가 전부 우회된다.
tests/test_table_transcriber.py 의 tmp_path DATA_DIR + reload 패턴을 재사용한다.
"""
from __future__ import annotations

import pytest


def _round_with_rows(m, s, rows):
    """rows: [(ndt_method, amount, finding_rules_or_None)] → billing_round_id"""
    from datetime import date
    br = m.BillingRound(round_no=1, discipline="CP-M1", billing_date=date(2026, 9, 1), billing_xlsx_path="/x.xlsx")
    s.add(br); s.flush()
    for i, (method, amount, rules) in enumerate(rows):
        bi = m.BillingItem(billing_round_id=br.id, row_index=i, billing_no=f"B{i}", joint_no=f"FW{i}",
                           ndt_method=method, drawing_no="NP.D.X..052.DC.0001.E", amount=amount)
        s.add(bi); s.flush()
        if rules is not None:
            findings = [{"rule": r, "details": {"requested_ndt": method, "basis_state": st, "basis_docs": docs,
                                                 "basis_reason": reason}}
                        for r, st, docs, reason in rules]
            s.add(m.Finding(billing_item_id=bi.id, verdict="SUSPECT", risk_score=45,
                            citations_json={"findings": findings, "authority_refs": []}))
    s.commit()
    return br.id


# ─────────────────────────── 7단계: 가이드 금액 분리 ───────────────────────────

def test_가이드는_확정과_근거미확정을_금액까지_가른다(fresh_db):
    m, r = fresh_db
    from app.report import criteria_guide as cg
    with m.get_session() as s:
        rid = _round_with_rows(m, s, [
            ("PT", 100, [("billed_ndt_not_in_requirements", "no_basis_found", ["SCWEP-7"], "no_basis_found")]),
            ("PT", 200, [("billed_ndt_basis_not_submitted", "not_submitted", [], "scwep_not_submitted")]),
            ("PT", 400, [("billed_ndt_covered_by_scwep", "covered", ["SCWEP-7"], "covered_by_conditional")]),
            ("PT", 800, None),                                   # 미검토 → 보수적으로 미확정
        ])
        from sqlalchemy import select
        overbill = [{"billing_item_id": bi.id, "amount": bi.amount, "billed_method": "PT",
                     "drawing_body": "NP.D.X..052", "joint_no": bi.joint_no, "required_ndt": ["VT"]}
                    for bi in s.scalars(select(m.BillingItem))]
        confirmed, unproven = cg._split_overbill_by_basis(s, overbill)
    assert [o["amount"] for o in confirmed] == [100]
    assert sorted(o["amount"] for o in unproven) == [200, 400, 800]
    assert {o["basis_reason"] for o in unproven} == {"not_submitted", "covered", "not_reviewed"}


# ─────────────────────────── 8단계: 회차 롤업 ───────────────────────────

def test_롤업은_상태별_건수와_억제_사유를_센다(fresh_db, caplog):
    m, r = fresh_db
    from app.analyzers import pipeline as pl
    with m.get_session() as s:
        rid = _round_with_rows(m, s, [
            ("PT", 1, [("billed_ndt_basis_not_submitted", "not_submitted", [], "scwep_not_submitted")]),
            ("PT", 1, [("billed_ndt_basis_not_submitted", "not_submitted", [], "scwep_scope_unknown")]),
            ("PT", 1, [("billed_ndt_covered_by_scwep", "covered", ["SCWEP-7"], "x")]),
            ("PT", 1, [("billed_ndt_covered_by_scwep", "covered", ["SCWEP-7"], "x")]),
            ("PT", 1, [("billed_ndt_covered_by_scwep", "covered", ["SCWEP-7"], "x")]),
        ])
        rollup = pl._basis_rollup(s, rid)
    assert rollup["basis_states"] == {"covered": 3, "unclear": 0, "not_submitted": 2, "no_basis_found": 0}
    assert rollup["suppression_reasons"] == {"scwep_not_submitted": 1, "scwep_scope_unknown": 1}
    assert rollup["top_covering_doc"] == {"doc": "SCWEP-7", "rows": 3}
    import logging
    with caplog.at_level(logging.WARNING):
        pl._warn_if_gate_silent(rollup)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "과다청구 확정 0건" in msgs and "scwep_not_submitted 1" in msgs
    assert "SCWEP-7 하나로 3행이 인정됨" in msgs


def test_확정이_있으면_침묵_경고_없음(fresh_db, caplog):
    m, r = fresh_db
    from app.analyzers import pipeline as pl
    import logging
    with caplog.at_level(logging.WARNING):
        pl._warn_if_gate_silent({"basis_states": {"covered": 0, "unclear": 0, "not_submitted": 1, "no_basis_found": 2},
                                 "suppression_reasons": {}, "top_covering_doc": None})
    assert not any("확정 0건" in rec.getMessage() for rec in caplog.records)


# ─────────────────────────── 8단계: doc_type 덮어쓰기 거부 ───────────────────────────

def test_같은_파일을_다른_종류로_재적재하면_거부(fresh_db):
    m, r = fresh_db
    with m.get_session() as s:
        r.upsert_standard(s, file_path="/x/scwep-1.pdf", doc_type="scwep", document_no="S-1",
                          revision=None, extracted_json={})
        s.commit()
        with pytest.raises(ValueError) as e:
            r.upsert_standard(s, file_path="/x/scwep-1.pdf", doc_type="code", document_no="S-1",
                              revision=None, extracted_json={})
        assert "scwep" in str(e.value) and "code" in str(e.value)
        # 같은 종류 재적재는 그대로 허용 (갱신)
        r.upsert_standard(s, file_path="/x/scwep-1.pdf", doc_type="scwep", document_no="S-1r2",
                          revision="2", extracted_json={"_schema_version": 2})
        s.commit()


# ─────────────────────────── 2026-09-07: 게이트가 평가조차 안 된 회차 ───────────────────────────

def test_도면_요구사항이_없어_게이트가_미평가면_경고(fresh_db, caplog):
    """스모크: 4,395행 전부 drawing_requirement_missing → basis_states 전부 0 → 기존 경고는
    과다청구 계열이 1건 이상일 때만 울려서 회차 전체가 조용히 지나갔다. 미평가도 소리내야 한다."""
    m, r = fresh_db
    from app.analyzers import pipeline as pl
    with m.get_session() as s:
        rid = _round_with_rows(m, s, [
            ("PT", 1, [("drawing_requirement_missing", None, [], None)]),
            ("VT", 1, [("drawing_requirement_missing", None, [], None), ("no_matching_report", None, [], None)]),
            ("UT", 1, None),
        ])
        rollup = pl._basis_rollup(s, rid)
    assert rollup["basis_states"] == {"covered": 0, "unclear": 0, "not_submitted": 0, "no_basis_found": 0}
    assert rollup["unevaluated_rows"] == 2          # drawing_requirement_missing 이 붙은 행 수
    assert rollup["rows_with_findings"] == 2
    import logging
    with caplog.at_level(logging.WARNING):
        pl._warn_if_gate_silent(rollup)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "근거 게이트 미평가" in msgs and "2행" in msgs


def test_게이트가_평가된_회차에는_미평가_경고_없음(fresh_db, caplog):
    m, r = fresh_db
    from app.analyzers import pipeline as pl
    import logging
    with caplog.at_level(logging.WARNING):
        pl._warn_if_gate_silent({"basis_states": {"covered": 1, "unclear": 0, "not_submitted": 0, "no_basis_found": 0},
                                 "suppression_reasons": {}, "top_covering_doc": None,
                                 "unevaluated_rows": 3, "rows_with_findings": 4})
    assert not any("미평가" in rec.getMessage() for rec in caplog.records)
