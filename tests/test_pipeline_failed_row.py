"""감사 ④: _process_one 이 예외로 죽으면 그 청구 행이 결과에서 사라지고 화면은 '완료'.
실패 행은 SUSPECT + 재확인 사유로 남아야 한다."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.analyzers import pipeline


def _round_with_one_item(m, r):
    with m.get_session() as s:
        br = r.create_billing_round(s, round_no=1, discipline="CP-M1", billing_date=date(2026, 6, 30),
                                    billing_xlsx_path="x.xlsx", reports_pdf_path="r.pdf")
        s.flush()
        r.add_billing_items(s, br.id, [{"billing_no": "001", "joint_no": "FW12", "ndt_method": "PT"}])
        s.commit()
        return br.id


def test_failed_row_becomes_suspect_finding(fresh_db, monkeypatch):
    m, r = fresh_db
    br_id = _round_with_one_item(m, r)

    def boom(*a, **k):
        raise TypeError("'<' not supported between instances of 'str' and 'int'")
    monkeypatch.setattr(pipeline, "_process_one", boom)

    stats = pipeline.run(br_id)
    assert stats["processed"] == 0 and stats["failed"] == 1

    with m.get_session() as s:
        f = s.scalar(select(m.Finding))
    assert f is not None and f.verdict == "SUSPECT" and f.needs_review is True
    rules = [x["rule"] for x in f.citations_json["findings"]]
    assert rules == ["pipeline_row_failed"]
    assert "TypeError" in f.review_reasons_json["reasons"][0]


def test_review_writes_failed_row_count_to_progress_log(fresh_db, monkeypatch, tmp_path):
    """I7: 실패 행 수가 data/progress.log 에 남아야 사용자가 사진 1장으로 알아챈다."""
    from app import main as cli
    from app import progress_writer as pw
    from app.extractors import excel_parser, report_segmenter
    from app.report import excel_writer

    m, _ = fresh_db
    data = tmp_path / "pwdata"
    data.mkdir()
    monkeypatch.setattr(pw, "DATA_DIR", data)

    class Parsed:
        rows = [{"billing_no": "001", "joint_no": "FW12", "ndt_method": "PT", "raw_json": {}}]
        sheet_name, header_row, warnings = "Total", 1, []

    class Enrich:
        by_report_no, source_sheets, warnings = {}, [], []

    monkeypatch.setattr(cli, "configure_logging", lambda **k: "testrun")
    monkeypatch.setattr(cli, "init_db", m.init_db)
    monkeypatch.setattr(cli, "get_session", m.get_session)
    monkeypatch.setattr(excel_parser, "parse_billing_xlsx", lambda p, discipline_hint=None: Parsed())
    monkeypatch.setattr(excel_parser, "enrich_from_per_method_sheets", lambda p, discipline: Enrich())
    monkeypatch.setattr(report_segmenter, "segment", lambda p, **kw: [])
    monkeypatch.setattr(report_segmenter, "normalize_and_ingest_segments", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "run",
                        lambda rid: {"items_total": 1, "reports": 0, "processed": 0, "failed": 1})
    monkeypatch.setattr(excel_writer, "write", lambda rid: tmp_path / "review.xlsx")

    pw.reset()
    cli.review(billing=tmp_path / "b.xlsx", reports=tmp_path / "r.pdf", round_no=1,
               date_str="2026-06-30", discipline="CP-M1", verbose=False)
    assert "처리 실패: 1" in (data / "progress.log").read_text(encoding="utf-8")


def test_review_logs_row_progress(fresh_db, monkeypatch, caplog):
    """4,395행을 몇 시간 도는 동안 '진행 [행] k/n' 이 올라가야 런처가 멈춘 게 아님을 안다 (2026-09-07)."""
    import logging
    m, r = fresh_db
    rid = _round_with_one_item(m, r)
    with m.get_session() as s:
        br = s.get(m.BillingRound, rid)
        for i in range(2, 5):
            s.add(m.BillingItem(billing_round_id=br.id, row_index=i, billing_no=f"B{i}", report_no=f"12-00{i}PT",
                                joint_no=f"FW{i}", ndt_method="PT"))
        s.commit()
    monkeypatch.setenv("NDT_HCX_MOCK", "1")
    with caplog.at_level(logging.INFO, logger="app.analyzers.pipeline"):
        pipeline.run(rid)
    from app.gui import progress_parse as pp
    seen = [pp.parse_progress(rec.getMessage()) for rec in caplog.records]
    seen = [p for p in seen if p and p.stage == "행"]
    assert seen and seen[-1].k == seen[-1].n == 4
