"""도면 세트 rev 관리. 신규 rev 입력 시 이전 세트를 superseded 처리."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DrawingSet


def supersede_old_revisions(
    session: Session, *, drawing_no: str, new_set_id: int, as_of: Optional[date] = None
) -> int:
    """동일 drawing_no 의 기존 세트들을 superseded_at 으로 마킹.

    반환: 마킹된 세트 수.
    """
    now_dt = datetime.combine(as_of, datetime.min.time()) if as_of else datetime.now()
    rows = session.scalars(
        select(DrawingSet).where(
            DrawingSet.drawing_no == drawing_no,
            DrawingSet.id != new_set_id,
            DrawingSet.superseded_at.is_(None),
        )
    ).all()
    for ds in rows:
        ds.superseded_at = now_dt
    return len(rows)


def find_effective(session: Session, drawing_no: str, as_of: Optional[date] = None) -> Optional[DrawingSet]:
    """청구일(as_of) 시점에 유효한(superseded 안 됨) 세트 중 가장 최신 채택.

    `as_of` 가 None 이면 현재 유효 세트.
    """
    q = (
        select(DrawingSet)
        .where(DrawingSet.drawing_no == drawing_no)
        .order_by(DrawingSet.created_at.desc())
    )
    for ds in session.scalars(q):
        if ds.superseded_at is None:
            return ds
        if as_of is not None and ds.superseded_at.date() > as_of:
            # as_of 시점에는 아직 유효했음
            return ds
    return None
