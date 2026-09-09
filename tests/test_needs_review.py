"""Smoke test: needs_review flag propagates through DB + writer helpers."""
from __future__ import annotations

from datetime import date


def test_needs_review_aggregation_in_writer(fresh_db):
    m, r = fresh_db
    from app.database.repository import (
        add_billing_items,
        add_inspection_report,
        create_billing_round,
        ensure_initialized,
        save_finding,
        save_match,
    )
    from app.database.models import BillingItem, get_session
    from app.report.excel_writer import _aggregate_needs_review, _collect_review_reasons

    ensure_initialized()
    with get_session() as s:
        br = create_billing_round(
            s,
            round_no=1,
            discipline="CP-M1",
            billing_date=date(2026, 5, 1),
            billing_xlsx_path="x.xlsx",
            reports_pdf_path="r.pdf",
        )
        s.flush()
        add_billing_items(s, br.id, [
            {"joint_no": "J-001", "ndt_method": "RT", "welder_id": "W1"},
        ])
        s.flush()
        item = s.query(BillingItem).first()
        rep = add_inspection_report(
            s,
            billing_round_id=br.id,
            source_pdf="r.pdf",
            start_page=0,
            end_page=1,
            needs_review=True,
            review_reasons_json={"reasons": ["OCR 신뢰도 낮음"]},
        )
        s.flush()
        save_match(
            s,
            billing_item_id=item.id,
            inspection_report_id=rep.id,
            match_method="fuzzy",
            match_score=82.0,
            needs_review=True,
            review_reasons_json={"reasons": ["fuzzy 점수 낮음"]},
        )
        save_finding(
            s,
            billing_item_id=item.id,
            verdict="SUSPECT",
            risk_score=45,
            summary="모호함",
            needs_review=True,
            review_reasons_json={"reasons": ["verdict=SUSPECT"]},
        )
        s.commit()

        from app.database.models import Finding, Match
        from sqlalchemy import select

        m_obj = s.scalar(select(Match).where(Match.billing_item_id == item.id))
        f_obj = s.scalar(select(Finding).where(Finding.billing_item_id == item.id))
        rep_obj = rep

        assert _aggregate_needs_review(m_obj, f_obj, rep_obj) is True
        reasons = _collect_review_reasons(m_obj, f_obj, rep_obj)
        assert any("[적합성]" in r for r in reasons)
        assert any("[매칭]" in r for r in reasons)
        assert any("[성적서]" in r for r in reasons)


def test_grouper_separates_unclassified():
    from app.extractors.drawing.classifier import Classification
    from app.extractors.drawing.grouper import group

    c1 = Classification(drawing_type="DC", drawing_no="DRW-001", revision="r1", confidence=0.99, source="filename")
    c2 = Classification(drawing_type="SD", drawing_no="DRW-001", revision="r1", confidence=0.99, source="filename")
    c3 = Classification(drawing_type=None, drawing_no=None, revision=None, confidence=0.0, source="unknown",
                        needs_review=True, review_reasons=["분류 실패"])
    c1.__dict__["_file_path"] = "/tmp/a.pdf"
    c2.__dict__["_file_path"] = "/tmp/b.pdf"
    c3.__dict__["_file_path"] = "/tmp/c.pdf"

    result = group([c1, c2, c3])
    assert len(result.sets) == 1
    # 2026-09-07 사용자 규칙: DC 만 있으면 완전한 세트 (SD/BG 는 구매 도면). BG 가 없어도 누락이 아니다.
    assert result.sets[0].is_complete is True
    assert result.sets[0].missing == []
    assert len(result.unclassified) == 1


def test_classifier_needs_review_on_low_confidence(monkeypatch, tmp_path):
    monkeypatch.setenv("NDT_HCX_MOCK", "1")
    # Override classifier to return a known low-confidence response by overriding the LLM call.
    from app.extractors.drawing import classifier as clsmod

    # Skip filename pattern by giving a non-matching name
    fake_pdf = tmp_path / "ambiguous.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n%mock\n")

    def fake_extract(p):
        from app.extractors.pdf_extractor import ExtractedPDF, PageText
        return ExtractedPDF(path=p, pages=[PageText(0, "ambiguous title", "text_layer")], text_layer_present=True)

    def fake_call(stage, payload):
        class R:
            parsed = {
                "drawing_type": "DC",
                "drawing_no": None,
                "revision": None,
                "confidence": 0.5,
                "reasoning": "low conf",
                "needs_review": True,
                "review_reasons": ["불명확"],
            }
        return R()

    monkeypatch.setattr(clsmod, "extract", fake_extract)
    monkeypatch.setattr(clsmod, "call", fake_call)

    c = clsmod.classify(fake_pdf)
    assert c.needs_review is True
    assert c.confidence < 0.8
    assert "불명확" in c.review_reasons
