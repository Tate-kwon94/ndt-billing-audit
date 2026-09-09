"""DB 접근 헬퍼. 호출부가 SQLAlchemy ORM 세부를 몰라도 되도록 추상화."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    BillingItem,
    BillingRound,
    DrawingFile,
    DrawingSet,
    Finding,
    InspectionReport,
    Match,
    Requirement,
    ReviewerNote,
    StandardDocument,
    get_session,
    init_db,
)


def ensure_initialized() -> None:
    init_db()


# ─────────────────────────── Drawing ───────────────────────────


def upsert_drawing_file(session: Session, **fields) -> DrawingFile:
    file_path = fields["file_path"]
    existing = session.scalar(select(DrawingFile).where(DrawingFile.file_path == file_path))
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing
    obj = DrawingFile(**fields)
    session.add(obj)
    return obj


def get_or_create_drawing_set(
    session: Session, drawing_no: str, set_revision: Optional[str] = None
) -> DrawingSet:
    existing = session.scalar(
        select(DrawingSet).where(
            DrawingSet.drawing_no == drawing_no, DrawingSet.set_revision == set_revision
        )
    )
    if existing:
        return existing
    obj = DrawingSet(drawing_no=drawing_no, set_revision=set_revision)
    session.add(obj)
    session.flush()
    return obj


def effective_drawing_set(
    session: Session, drawing_no: str, as_of: Optional[date] = None
) -> Optional[DrawingSet]:
    """청구일 기준 유효한(rev 가장 최신, superseded 안 된) drawing_set 반환."""
    q = select(DrawingSet).where(DrawingSet.drawing_no == drawing_no).order_by(
        DrawingSet.created_at.desc()
    )
    for ds in session.scalars(q):
        if ds.superseded_at is None:
            return ds
    return None


# ─────────────────────────── Standards ───────────────────────────


def upsert_standard(session: Session, **fields) -> StandardDocument:
    file_path = fields["file_path"]
    existing = session.scalar(
        select(StandardDocument).where(StandardDocument.file_path == file_path)
    )
    if existing:
        # 같은 파일을 다른 종류로 재적재하는 것을 막는다. SCWEP 를 'code' 로 잘못 넣으면
        # 근거 판정(doc_type=='scwep' 필터)에서 조용히 사라져 그 문서는 영원히 '미제출' 이 된다.
        new_type = fields.get("doc_type")
        if new_type and existing.doc_type and new_type != existing.doc_type:
            raise ValueError(
                f"이미 '{existing.doc_type}' 로 적재된 파일을 '{new_type}' 로 다시 넣을 수 없습니다: "
                f"{file_path}\n  종류를 바꾸려면 먼저 그 문서를 삭제하거나, 등록 시 종류를 확인하세요."
            )
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing
    obj = StandardDocument(**fields)
    session.add(obj)
    return obj


# ─────────────────────────── Billing round ───────────────────────────


def create_billing_round(
    session: Session,
    *,
    round_no: int,
    discipline: str,
    billing_date: date,
    billing_xlsx_path: Optional[str] = None,
    reports_pdf_path: Optional[str] = None,
) -> BillingRound:
    obj = BillingRound(
        round_no=round_no,
        discipline=discipline,
        billing_date=billing_date,
        billing_xlsx_path=billing_xlsx_path,
        reports_pdf_path=reports_pdf_path,
    )
    session.add(obj)
    session.flush()
    return obj


def add_billing_items(session: Session, billing_round_id: int, rows: Iterable[dict]) -> int:
    n = 0
    for i, row in enumerate(rows):
        session.add(BillingItem(billing_round_id=billing_round_id, row_index=i, **row))
        n += 1
    return n


def add_inspection_report(session: Session, **fields) -> InspectionReport:
    obj = InspectionReport(**fields)
    session.add(obj)
    return obj


def save_match(session: Session, **fields) -> Match:
    obj = Match(**fields)
    session.add(obj)
    return obj


def save_finding(session: Session, **fields) -> Finding:
    obj = Finding(**fields)
    session.add(obj)
    return obj


def add_reviewer_note(session: Session, **fields) -> ReviewerNote:
    obj = ReviewerNote(**fields)
    session.add(obj)
    return obj
