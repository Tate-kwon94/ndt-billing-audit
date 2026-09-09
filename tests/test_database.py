"""DB schema smoke test."""
from __future__ import annotations

from datetime import date


def test_create_and_query_billing_round(fresh_db):
    m, r = fresh_db
    from app.database.repository import create_billing_round, ensure_initialized
    from app.database.models import get_session

    ensure_initialized()
    with get_session() as s:
        br = create_billing_round(
            s,
            round_no=2,
            discipline="CP-M1",
            billing_date=date(2026, 6, 30),
            billing_xlsx_path="x.xlsx",
            reports_pdf_path="r.pdf",
        )
        s.commit()
        assert br.id is not None

    with get_session() as s:
        from app.database.models import BillingRound
        from sqlalchemy import select

        loaded = s.scalar(select(BillingRound).where(BillingRound.id == br.id))
        assert loaded.round_no == 2
        assert loaded.discipline == "CP-M1"
