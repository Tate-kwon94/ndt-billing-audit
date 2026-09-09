"""SQLite 스키마.

설계 메모:
- 단일 파일 (data/ndt.sqlite) — 사내 공유폴더 배치 가능
- SQLAlchemy 2.x 선언형
- 모든 LLM 추출 결과의 출처(citation: doc/section/page/quote) 보존
- 검토자 판정(`reviewer_notes`)은 누적되어 학습 데이터 역할도 함
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import DATA_DIR


# ─────────────────────────── Engine / Session ───────────────────────────


_engine = None
_SessionFactory = None


def get_engine(db_path: Optional[Path] = None):
    global _engine
    if _engine is None:
        if db_path is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            db_path = DATA_DIR / "ndt.sqlite"
        _engine = create_engine(f"sqlite:///{db_path}", future=True)
    return _engine


def get_session(db_path: Optional[Path] = None) -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(db_path), expire_on_commit=False)
    return _SessionFactory()


def init_db(db_path: Optional[Path] = None) -> None:
    """존재하지 않으면 모든 테이블 생성."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    _migrate_drawing_set_unique(engine)


def _default_sql(c):
    """모델 기본값을 SQL 리터럴로. 없으면 None.

    repr() 은 파이썬 문법이라 SQL 과 어긋난다 (bytes 는 b'..', 따옴표 이스케이프 규칙도 다름).
    SQL 리터럴 규칙 그대로 만든다 — 문자열은 홑따옴표, 안의 홑따옴표는 두 번 (2026-09-05 리뷰).
    """
    if c.default is not None and getattr(c.default, "is_scalar", False):
        v = c.default.arg
        if v is True:
            return "1"
        if v is False:
            return "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    if c.server_default is not None:
        return "CURRENT_TIMESTAMP"        # 이 모델의 server_default 는 func.now() 뿐
    return None


def _copy_drawing_sets_rows(conn, ddl, where: str = "") -> None:
    """drawing_sets_old → drawing_sets 복사. 옛 테이블에 있는 컬럼만.

    사내 DB 는 몇 판을 건너뛰고 올라올 수 있어 모델에 새로 생긴 컬럼이 옛 테이블엔
    없을 수 있다 (없는 컬럼은 모델 기본값). (2026-09-05 리뷰)
    """
    from sqlalchemy import text
    old_cols = {row[1] for row in conn.execute(text("PRAGMA table_info('drawing_sets_old')"))}
    targets, sources = [], []
    for c in ddl.columns:
        d = None if c.nullable else _default_sql(c)
        if c.name in old_cols:
            targets.append(c.name)
            # 옛 행이 NULL 인 NOT NULL 컬럼(예: created_at)은 기본값으로 메운다
            sources.append(f"COALESCE({c.name}, {d})" if d else c.name)
        elif d is not None:
            # 옛 테이블에 없는 NOT NULL 컬럼은 모델 기본값을 리터럴로 채운다 (예: needs_review=False → 0)
            targets.append(c.name); sources.append(d)
    res = conn.execute(text(f"INSERT INTO drawing_sets ({', '.join(targets)}) "
                            f"SELECT {', '.join(sources)} FROM drawing_sets_old{where}"))
    return res.rowcount if res.rowcount is not None and res.rowcount >= 0 else 0


def _backup_sqlite_file(engine) -> None:
    """제약 이전 전에 DB 파일을 통째로 복사해 둔다 (파일 DB 만).

    pysqlite 는 DDL 에서 자동 커밋한다 — engine.begin() 이 RENAME/CREATE 를 감싸주지 못한다.
    그래서 이전 도중에 죽으면 되돌릴 트랜잭션이 없다. 원본 파일 사본이 유일한 안전망이다.
    """
    import shutil
    db_file = getattr(engine.url, "database", None)
    if not db_file or db_file == ":memory:":
        return                       # 메모리 DB — 복사할 파일이 없다
    src = Path(db_file)
    if not src.exists():
        return
    bak = src.with_name(src.name + "." + datetime.now().strftime("bak_%Y%m%d_%H%M%S"))
    shutil.copy2(str(src), str(bak))
    logging.getLogger(__name__).warning("이전 전에 DB 파일을 %s 로 백업했습니다.", bak)


def _drop_named_indexes_of(conn, table_name: str) -> list[str]:
    """table_name 에 붙은 이름 있는 인덱스를 지운다 (sqlite_autoindex_* 는 테이블 제약이라 못 지우고 지울 필요도 없다).

    2026-09-07 사내 실패: ALTER TABLE drawing_sets RENAME TO drawing_sets_old 뒤에도 옛 모델의 index=True 가 만든
    ix_drawing_sets_drawing_no 가 **같은 이름으로** _old 에 붙어 있어, 새 테이블의 같은 이름 인덱스를 만들다
    'already exists' 로 죽었다. SQLite 인덱스 이름은 DB 전체에서 유일하다."""
    from sqlalchemy import text
    names = [r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t AND name NOT LIKE 'sqlite_autoindex%'"),
        {"t": table_name})]
    for n in names:
        conn.execute(text(f'DROP INDEX IF EXISTS "{n}"'))
    return names


def _ensure_indexes(conn, ddl) -> None:
    """새 테이블에 모델의 인덱스가 다 있게 한다 (중단됐던 이전이 인덱스 직전에 죽은 경우 되살림)."""
    for ix in ddl.indexes:
        ix.create(conn, checkfirst=True)


def _resume_interrupted_drawing_set_migration(engine, table_names) -> None:
    """drawing_sets_old 가 남아 있다 = 지난 이전이 복사 전에 끊겼다. 마저 끝낸다.

    끊긴 자리를 그냥 두면 다음 init_db() 가 '이미 새 형식' 으로 보고 그대로 지나가
    도면 세트가 통째로 사라진 채로 운영된다. 어느 쪽이 최신인지 모르는 상황에서는
    추측하지 않고 멈춘다 (모호한 입력이 확신에 찬 결과가 되면 안 된다).
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    ddl = DrawingSet.__table__
    has_new = "drawing_sets" in table_names
    halt = ("도면 세트 이전이 중단된 흔적이 있는데 drawing_sets 와 drawing_sets_old 양쪽에 "
            "데이터가 있습니다 ({why}). 어느 쪽이 최신인지 도구가 판단할 수 없어 임의로 고르지 않습니다. "
            "적재를 중단합니다 — data\\ 폴더 내용(두 테이블 모두)을 개발자에게 보내 주세요.")
    _backup_sqlite_file(engine)          # 유일한 사본이 _old 에 있는 상태 — 지우기 전에 파일부터 남긴다
    with engine.begin() as conn:
        old_n = conn.execute(text("SELECT COUNT(*) FROM drawing_sets_old")).scalar() or 0
        new_n = (conn.execute(text("SELECT COUNT(*) FROM drawing_sets")).scalar() or 0) if has_new else 0
        if old_n and new_n:
            # 정상적인 중단은 새 테이블이 비어 있다(복사+DROP 이 한 트랜잭션). 양쪽에 행이 있으면
            # 어느 행을 살릴지 rowid 로 고르는 것은 추측이다 — 크기에 관계없이 멈춘다.
            raise RuntimeError(halt.format(why=f"drawing_sets {new_n}행, drawing_sets_old {old_n}행"))
        _drop_named_indexes_of(conn, "drawing_sets_old")     # 같은 이름의 인덱스가 새 테이블 생성을 막는다
        if not has_new:
            ddl.create(conn)
        _ensure_indexes(conn, ddl)                              # 인덱스 직전에 죽었던 경우 여기서 되살린다
        copied = 0
        if old_n:
            try:
                copied = _copy_drawing_sets_rows(conn, ddl)
            except IntegrityError as e:
                raise RuntimeError(halt.format(why=f"옛 행이 새 제약과 충돌: {e.orig}")) from e
            if copied != old_n:
                raise RuntimeError(halt.format(why=f"복사된 행 {copied} ≠ 옛 행 {old_n}"))
        conn.execute(text("DROP TABLE drawing_sets_old"))
    logging.getLogger(__name__).warning(
        "중단됐던 도면 세트 이전을 마저 끝냈습니다 — drawing_sets_old 의 %d행을 되살리고 옛 테이블을 지웠습니다.",
        copied)


def _migrate_drawing_set_unique(engine) -> None:
    """옛 DB 의 UNIQUE(drawing_no) 를 UNIQUE(drawing_no, set_revision) 로.

    SQLite 는 제약을 ALTER 로 못 바꾼다. 테이블을 새로 만들어 복사한다.
    legacy_alter_table=ON 이 없으면 RENAME 이 drawing_files.set_id 등의 FK 참조를
    _old 쪽으로 따라 바꿔 버린다 (SQLite 3.26+). 그래서 켠다.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    names = insp.get_table_names()
    if "drawing_sets_old" in names:
        _resume_interrupted_drawing_set_migration(engine, names)
        insp = inspect(engine)
        names = insp.get_table_names()
    if "drawing_sets" not in names:
        return
    uniques = insp.get_unique_constraints("drawing_sets") + [
        {"column_names": ix["column_names"]} for ix in insp.get_indexes("drawing_sets") if ix.get("unique")]
    if not any(u["column_names"] == ["drawing_no"] for u in uniques):
        return          # 이미 새 형식
    ddl = DrawingSet.__table__
    _backup_sqlite_file(engine)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text("PRAGMA legacy_alter_table=ON"))
        conn.execute(text("ALTER TABLE drawing_sets RENAME TO drawing_sets_old"))
        _drop_named_indexes_of(conn, "drawing_sets_old")     # RENAME 이 인덱스 이름을 바꿔 주지 않는다
        ddl.create(conn)
        _copy_drawing_sets_rows(conn, ddl)
        conn.execute(text("DROP TABLE drawing_sets_old"))
        conn.execute(text("PRAGMA legacy_alter_table=OFF"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
    logging.getLogger(__name__).warning(
        "drawing_sets 제약을 (drawing_no, set_revision) 로 이전했습니다 — 1회성, 도면 개정 재적재 가능.")


# ─────────────────────────── Models ───────────────────────────


class Base(DeclarativeBase):
    pass


class DrawingFile(Base):
    """개별 DC/SD/BG 파일."""

    __tablename__ = "drawing_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    file_hash: Mapped[str] = mapped_column(String, nullable=False)         # 변경 감지용
    drawing_no: Mapped[str] = mapped_column(String, index=True, nullable=False)
    drawing_type: Mapped[str] = mapped_column(String(4), nullable=False)   # "DC" | "SD" | "BG"
    revision: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    classification_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extracted_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    set_id: Mapped[Optional[int]] = mapped_column(ForeignKey("drawing_sets.id"), nullable=True, index=True)
    drawing_set: Mapped[Optional["DrawingSet"]] = relationship(back_populates="files")


class DrawingSet(Base):
    """동일 도면번호의 DC+SD+BG 3파일 묶음 (1 논리적 도면).

    사용자 정책 (2026-05-21 확인): 같은 도면번호면 1세트로 묶음.
    각 파일의 rev 는 독립 진화 가능 (예: BG=C01 인데 DC/SD=None/C00).
    set_revision 은 종류별 rev 합본 표시 (예: 'DC=-,SD=-,BG=C01').
    """

    __tablename__ = "drawing_sets"
    __table_args__ = (UniqueConstraint("drawing_no", "set_revision", name="uq_drawing_set_no_rev"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_no: Mapped[str] = mapped_column(String, index=True, nullable=False)
    set_revision: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    has_dc: Mapped[bool] = mapped_column(Boolean, default=False)
    has_sd: Mapped[bool] = mapped_column(Boolean, default=False)
    has_bg: Mapped[bool] = mapped_column(Boolean, default=False)

    effective_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    combined_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    conflicts_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    files: Mapped[list["DrawingFile"]] = relationship(back_populates="drawing_set")
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="drawing_set")


class Requirement(Base):
    """통합 검사 요구사항 (Joint 단위). drawing_set 우선, 상위 문서 보강."""

    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    drawing_set_id: Mapped[int] = mapped_column(ForeignKey("drawing_sets.id"), index=True)
    joint_no: Mapped[str] = mapped_column(String, index=True, nullable=False)

    weld_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    p_no_a: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    p_no_b: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    thickness_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wps_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    line_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    safety_class: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    required_ndt_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    applicable_codes_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    citations_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    drawing_set: Mapped["DrawingSet"] = relationship(back_populates="requirements")


class StandardDocument(Base):
    """SCWEP / Code / Contract 등 상위 권위 문서."""

    __tablename__ = "standard_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    doc_type: Mapped[str] = mapped_column(String, nullable=False)   # "scwep" | "code" | "contract"
    document_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    revision: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extracted_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    chunks_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # code_indexer 가 채움
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BillingRound(Base):
    """청구 회차 메타데이터 (1차/2차/...)."""

    __tablename__ = "billing_rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    discipline: Mapped[str] = mapped_column(String, nullable=False)   # "CP-M1" | "CP-P1" | ...
    billing_date: Mapped[date] = mapped_column(Date, nullable=False)
    billing_xlsx_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reports_pdf_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    billing_items: Mapped[list["BillingItem"]] = relationship(back_populates="billing_round")
    inspection_reports: Mapped[list["InspectionReport"]] = relationship(back_populates="billing_round")


class BillingItem(Base):
    """청구 엑셀 한 행."""

    __tablename__ = "billing_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_round_id: Mapped[int] = mapped_column(ForeignKey("billing_rounds.id"), index=True)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    billing_no: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # 청구 엑셀에 적힌 성적서번호 (예: '77-005PT'). CRST PDF 와 결정매칭 키.
    report_no: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # Weld Joint No (예: 'FW12'). 사용자 확인: 고유.
    joint_no: Mapped[str] = mapped_column(String, index=True, nullable=False)
    line_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    welder_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    ndt_method: Mapped[str] = mapped_column(String, index=True, nullable=False)
    drawing_no: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    inspection_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 호기/건물 — 청구회차 내 위치 식별용
    unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bldg: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    # 치수 (예: '3040x20')
    dimension: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    billing_round: Mapped["BillingRound"] = relationship(back_populates="billing_items")


class InspectionReport(Base):
    """청구회차 PDF 에서 분할된 개별 성적서."""

    __tablename__ = "inspection_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_round_id: Mapped[int] = mapped_column(ForeignKey("billing_rounds.id"), index=True)

    source_pdf: Mapped[str] = mapped_column(String, nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, nullable=False)

    report_no: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    ndt_method: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    inspection_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    inspector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    approver: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    procedure_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    drawing_no: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)

    extracted_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    segmentation_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extraction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    billing_round: Mapped["BillingRound"] = relationship(back_populates="inspection_reports")


class Match(Base):
    """청구 ↔ 성적서 매칭 결과."""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_item_id: Mapped[int] = mapped_column(ForeignKey("billing_items.id"), unique=True, index=True)
    inspection_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("inspection_reports.id"), nullable=True, index=True
    )
    matched_joint_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    match_method: Mapped[str] = mapped_column(String, nullable=False)  # "deterministic" | "fuzzy" | "llm" | "none"
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discrepancies_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class Finding(Base):
    """적합성 판정 결과 (근거 인용 포함)."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_item_id: Mapped[int] = mapped_column(ForeignKey("billing_items.id"), index=True)
    verdict: Mapped[str] = mapped_column(String, nullable=False)       # "OK" | "SUSPECT" | "NONCOMPLIANT"
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    explanation_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    citations_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    recommended_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReviewerNote(Base):
    """검토자가 대시보드/엑셀에서 남긴 판정·메모. 학습 데이터로도 활용."""

    __tablename__ = "reviewer_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    billing_item_id: Mapped[int] = mapped_column(ForeignKey("billing_items.id"), index=True)
    reviewer: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    verdict_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
