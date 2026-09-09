"""감사 ⑨ 실증: 같은 도면번호의 새 rev 를 적재하면 IntegrityError 로 적재가 멈춘다.
개정은 새 세트 + 옛 세트 superseded 여야 한다."""
from __future__ import annotations

from datetime import date

from app.extractors.drawing.revision_manager import find_effective, supersede_old_revisions


def test_new_revision_supersedes_old(fresh_db):
    m, r = fresh_db
    with m.get_session() as s:
        a = r.get_or_create_drawing_set(s, "MD.D.P000.1.0KBA10.052.DC.0001.E", "DC=C00")
        s.commit()
        b = r.get_or_create_drawing_set(s, "MD.D.P000.1.0KBA10.052.DC.0001.E", "DC=C01")   # 예전엔 여기서 IntegrityError
        supersede_old_revisions(s, drawing_no=a.drawing_no, new_set_id=b.id, as_of=date(2026, 7, 1))
        s.commit()
        eff = find_effective(s, a.drawing_no)
        assert eff.id == b.id and eff.set_revision == "DC=C01"
        assert s.get(m.DrawingSet, a.id).superseded_at is not None


def test_migration_rewrites_old_unique_index(fresh_db):
    """옛 DB(테이블 제약 UNIQUE(drawing_no)) 를 흉내 내고 init_db 가 제약을 바꾸는지.

    실제 사내 스키마 확인 결과 (2026-09-05): SQLAlchemy UniqueConstraint 는 SQLite 에서
    테이블 제약(sqlite_autoindex_drawing_sets_N)으로 잡히고, 이름 있는 인덱스가 아니다.
    그래서 DROP/CREATE INDEX 로는 옛 스키마를 재현할 수 없다 — 테이블을 옛 DDL 로
    다시 만들어야 한다.
    """
    from sqlalchemy import inspect, text

    m, r = fresh_db
    engine = m.get_engine()

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))
        conn.execute(
            text(
                """
                CREATE TABLE drawing_sets (
                    id INTEGER NOT NULL PRIMARY KEY,
                    drawing_no VARCHAR NOT NULL,
                    set_revision VARCHAR,
                    has_dc BOOLEAN,
                    has_sd BOOLEAN,
                    has_bg BOOLEAN,
                    effective_from DATE,
                    superseded_at DATETIME,
                    combined_json JSON,
                    conflicts_json JSON,
                    needs_review BOOLEAN,
                    review_reasons_json JSON,
                    created_at DATETIME,
                    CONSTRAINT uq_drawing_set_no UNIQUE (drawing_no)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO drawing_sets "
                "(id, drawing_no, set_revision, has_dc, has_sd, has_bg, needs_review, created_at) "
                "VALUES (1, 'MD.D.P000.1.0KBA10.052.DC.0001.E', 'DC=C00', 1, 0, 0, 0, "
                "'2026-05-23 00:00:00')"
            )
        )

    # 옛 스키마 상태: 재적재(새 rev)가 IntegrityError 로 죽는지 확인 (마이그레이션 전)
    with m.get_session() as s:
        try:
            r.get_or_create_drawing_set(s, "MD.D.P000.1.0KBA10.052.DC.0001.E", "DC=C01")
            s.commit()
            pre_migration_crashed = False
        except Exception:
            s.rollback()
            pre_migration_crashed = True
    assert pre_migration_crashed, "옛 스키마에서는 재적재가 IntegrityError 로 죽어야 한다 (전제 확인)"

    insp = inspect(engine)
    uniques_before = insp.get_unique_constraints("drawing_sets")
    assert any(u["column_names"] == ["drawing_no"] for u in uniques_before)

    m.init_db()   # 마이그레이션 실행

    insp = inspect(engine)
    uniques_after = insp.get_unique_constraints("drawing_sets")
    assert not any(u["column_names"] == ["drawing_no"] for u in uniques_after)

    # 기존 행 보존
    with m.get_session() as s:
        rows = list(s.execute(text("SELECT id, drawing_no, set_revision FROM drawing_sets")))
    assert len(rows) == 1
    assert rows[0][1] == "MD.D.P000.1.0KBA10.052.DC.0001.E"
    assert rows[0][2] == "DC=C00"

    # 이제 새 rev 재적재가 성공해야 한다
    with m.get_session() as s:
        a = s.get(m.DrawingSet, 1)
        b = r.get_or_create_drawing_set(s, "MD.D.P000.1.0KBA10.052.DC.0001.E", "DC=C01")
        s.commit()
        assert a.id != b.id

    # FK 참조가 drawing_sets_old 가 아니라 drawing_sets 를 가리키는지
    with engine.begin() as conn:
        fk_rows = list(conn.execute(text("PRAGMA foreign_key_list('drawing_files')")))
    assert all(row[2] == "drawing_sets" for row in fk_rows), fk_rows

    # 멱등성: 두 번째 init_db() 는 아무 것도 바꾸지 않아야 한다
    m.init_db()
    insp = inspect(engine)
    uniques_idempotent = insp.get_unique_constraints("drawing_sets")
    assert not any(u["column_names"] == ["drawing_no"] for u in uniques_idempotent)
    with m.get_session() as s:
        rows2 = list(s.execute(text("SELECT id, drawing_no, set_revision FROM drawing_sets ORDER BY id")))
    assert len(rows2) == 2


def test_migration_tolerates_missing_columns_in_old_table(fresh_db):
    """옛 DB 가 모델보다 컬럼이 적어도(판을 건너뛴 배포) 마이그레이션이 죽지 않고 있는 컬럼만 옮긴다."""
    from sqlalchemy import inspect, text

    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))
        conn.execute(text("""
            CREATE TABLE drawing_sets (
                id INTEGER NOT NULL PRIMARY KEY,
                drawing_no VARCHAR NOT NULL,
                set_revision VARCHAR,
                has_dc BOOLEAN, has_sd BOOLEAN, has_bg BOOLEAN,
                created_at DATETIME,
                CONSTRAINT uq_drawing_set_no UNIQUE (drawing_no)
            )"""))
        conn.execute(text("INSERT INTO drawing_sets (id, drawing_no, set_revision, has_dc, has_sd, has_bg) "
                          "VALUES (1, 'NP.D.P000.1.0UGB95GML90&.052.DC.0001.E', 'DC=C03', 1, 0, 0)"))
    m.init_db()
    with m.get_session() as s:
        row = s.get(m.DrawingSet, 1)
        assert row is not None and row.set_revision == "DC=C03" and row.review_reasons_json is None
        r.get_or_create_drawing_set(s, row.drawing_no, "DC=C04")     # 새 rev 삽입이 이제 된다
        s.commit()
    assert not any(u["column_names"] == ["drawing_no"] for u in inspect(engine).get_unique_constraints("drawing_sets"))


# ─────────── 중단된 이전(migration) 복구 — C1 (2026-09-05 전체점검) ───────────

_OLD_DDL = """
    CREATE TABLE drawing_sets_old (
        id INTEGER NOT NULL PRIMARY KEY,
        drawing_no VARCHAR NOT NULL,
        set_revision VARCHAR,
        has_dc BOOLEAN, has_sd BOOLEAN, has_bg BOOLEAN,
        effective_from DATE, superseded_at DATETIME,
        combined_json JSON, conflicts_json JSON,
        needs_review BOOLEAN, review_reasons_json JSON,
        created_at DATETIME,
        CONSTRAINT uq_drawing_set_no_old UNIQUE (drawing_no)
    )"""

_OLD_ROW = (
    "INSERT INTO drawing_sets_old "
    "(id, drawing_no, set_revision, has_dc, has_sd, has_bg, needs_review, created_at) "
    "VALUES (1, 'MD.D.P000.1.0KBA10.052.DC.0001.E', 'DC=C00', 1, 0, 0, 0, '2026-05-23 00:00:00')"
)


def test_interrupted_migration_is_finished_not_silently_dropped(fresh_db):
    """RENAME+create 뒤 복사 전에 프로세스가 죽은 상태를 재현. 데이터가 조용히 사라지면 안 된다."""
    from sqlalchemy import inspect, text

    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))       # 새 스키마 빈 테이블은 init_db 가 다시 만든다
        conn.execute(text(_OLD_DDL))
        conn.execute(text(_OLD_ROW))

    m.init_db()

    insp = inspect(engine)
    assert "drawing_sets_old" not in insp.get_table_names()
    with m.get_session() as s:
        rows = list(s.execute(text("SELECT id, drawing_no, set_revision FROM drawing_sets")))
    assert len(rows) == 1 and rows[0][1] == "MD.D.P000.1.0KBA10.052.DC.0001.E" and rows[0][2] == "DC=C00"
    with engine.begin() as conn:
        fk_rows = list(conn.execute(text("PRAGMA foreign_key_list('drawing_files')")))
    assert all(row[2] == "drawing_sets" for row in fk_rows), fk_rows


def test_ambiguous_interrupted_migration_halts_instead_of_guessing(fresh_db):
    """양쪽에 데이터가 있으면 어느 쪽이 최신인지 모른다 — 추측하지 않고 멈춘다."""
    import pytest
    from sqlalchemy import text

    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        conn.execute(text(_OLD_DDL))
        conn.execute(text(_OLD_ROW))
        conn.execute(text(
            "INSERT INTO drawing_sets (id, drawing_no, set_revision, has_dc, has_sd, has_bg, needs_review) "
            "VALUES (2, 'NP.D.P000.1.0UGB95GML90&.052.DC.0001.E', 'DC=C03', 1, 0, 0, 0)"))

    with pytest.raises(RuntimeError) as e:
        m.init_db()
    assert "개발자" in str(e.value) and "중단" in str(e.value)


def test_migration_backs_up_the_sqlite_file_first(fresh_db):
    """제약 이전 전에 DB 파일을 통째로 복사해 둔다 — 중간에 죽어도 원본이 남는다."""
    from pathlib import Path
    from sqlalchemy import text

    m, r = fresh_db
    engine = m.get_engine()
    db_file = Path(engine.url.database)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))
        conn.execute(text("""
            CREATE TABLE drawing_sets (
                id INTEGER NOT NULL PRIMARY KEY,
                drawing_no VARCHAR NOT NULL,
                set_revision VARCHAR,
                has_dc BOOLEAN, has_sd BOOLEAN, has_bg BOOLEAN,
                created_at DATETIME,
                CONSTRAINT uq_drawing_set_no UNIQUE (drawing_no)
            )"""))
        conn.execute(text("INSERT INTO drawing_sets (id, drawing_no, set_revision, has_dc, has_sd, has_bg) "
                          "VALUES (1, 'MD.D.P000.1.0KBA10.052.DC.0001.E', 'DC=C00', 1, 0, 0)"))
    m.init_db()
    baks = sorted(db_file.parent.glob(db_file.name + ".bak_*"))
    assert len(baks) == 1 and baks[0].stat().st_size > 0


def test_ambiguous_interrupted_migration_halts_even_when_new_is_smaller(fresh_db):
    """옛 5행·새 2행처럼 새 쪽이 작아도 rowid 로 골라 합치지 않는다 — 리뷰 재현(OLD1·OLD2 소실)."""
    import pytest
    from sqlalchemy import text

    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        conn.execute(text(_OLD_DDL))
        for i in range(1, 6):
            conn.execute(text("INSERT INTO drawing_sets_old (id, drawing_no, set_revision, has_dc, has_sd, has_bg) "
                              f"VALUES ({i}, 'OLD{i}', 'C00', 1, 0, 0)"))
        for i in (1, 2):
            conn.execute(text("INSERT INTO drawing_sets (id, drawing_no, set_revision, has_dc, has_sd, has_bg, needs_review) "
                              f"VALUES ({i}, 'NEW{i}', 'C00', 1, 0, 0, 0)"))
    with pytest.raises(RuntimeError) as e:
        m.init_db()
    assert "개발자" in str(e.value)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM drawing_sets_old")).scalar() == 5     # 아무것도 안 지웠다


def test_resume_backs_up_file_before_dropping_old(fresh_db):
    from pathlib import Path
    from sqlalchemy import text

    m, r = fresh_db
    engine = m.get_engine()
    db_file = Path(engine.url.database)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))
        conn.execute(text(_OLD_DDL))
        conn.execute(text(_OLD_ROW))
    m.init_db()
    assert len(list(db_file.parent.glob(db_file.name + ".bak_*"))) == 1


# ─────────────────────────── 2026-09-07 사내 실패: 이름 있는 인덱스가 RENAME 을 따라간다 ───────────────────────────

_OLD_INDEX = "CREATE INDEX ix_drawing_sets_drawing_no ON {table} (drawing_no)"


def _legacy_table_with_named_index(conn, text):
    """사내 c 상태 DB 의 drawing_sets: 테이블 제약 UNIQUE(drawing_no) + 옛 모델의 index=True 가 만든
    이름 있는 인덱스 ix_drawing_sets_drawing_no. 사외 테스트는 이 인덱스를 빼먹어 실패를 못 잡았다."""
    conn.execute(text("DROP TABLE drawing_sets"))
    conn.execute(text(_OLD_DDL.replace("drawing_sets_old", "drawing_sets").replace("uq_drawing_set_no_old", "uq_drawing_set_no")))
    conn.execute(text(_OLD_INDEX.format(table="drawing_sets")))
    conn.execute(text(_OLD_ROW.replace("drawing_sets_old", "drawing_sets")))


def test_migration_survives_named_index_on_old_table(fresh_db):
    """RENAME TO drawing_sets_old 뒤에도 ix_drawing_sets_drawing_no 는 그 이름 그대로 _old 에 붙어 있다.
    새 테이블을 만들며 같은 이름의 인덱스를 또 만들면 'already exists' — 사내 ③ SCWEP 등록이 여기서 죽었다."""
    from sqlalchemy import inspect, text
    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        _legacy_table_with_named_index(conn, text)

    m.init_db()                                             # 죽으면 안 된다

    insp = inspect(engine)
    assert "drawing_sets_old" not in insp.get_table_names()
    with engine.begin() as conn:
        rows = list(conn.execute(text("SELECT id, drawing_no, set_revision FROM drawing_sets")))
        idx = list(conn.execute(text("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='ix_drawing_sets_drawing_no'")))
    assert len(rows) == 1 and rows[0][1] == "MD.D.P000.1.0KBA10.052.DC.0001.E"
    assert idx == [("drawing_sets",)]                       # 인덱스는 새 테이블에, 하나만
    uq = insp.get_unique_constraints("drawing_sets")
    assert any(u["column_names"] == ["drawing_no", "set_revision"] for u in uq)


def test_resume_after_index_collision_recreates_index(fresh_db):
    """사내에 지금 남아 있는 상태 그대로: drawing_sets_old(데이터 + 이름 있는 인덱스) 와
    인덱스 없이 만들어진 빈 drawing_sets. 다음 init_db 가 마저 끝내고 인덱스도 되살려야 한다."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError
    m, r = fresh_db
    engine = m.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE drawing_sets"))
        conn.execute(text(_OLD_DDL))
        conn.execute(text(_OLD_INDEX.format(table="drawing_sets_old")))
        conn.execute(text(_OLD_ROW))
        try:
            m.DrawingSet.__table__.create(conn)             # 테이블은 생기고 인덱스에서 죽는다 — 사내와 같은 순서
        except OperationalError:
            pass
    with engine.begin() as conn:
        assert "drawing_sets" in inspect(engine).get_table_names()
        assert conn.execute(text("SELECT COUNT(*) FROM drawing_sets")).scalar() == 0

    m.init_db()

    insp = inspect(engine)
    assert "drawing_sets_old" not in insp.get_table_names()
    with engine.begin() as conn:
        rows = list(conn.execute(text("SELECT drawing_no FROM drawing_sets")))
        idx = list(conn.execute(text("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='ix_drawing_sets_drawing_no'")))
    assert [r[0] for r in rows] == ["MD.D.P000.1.0KBA10.052.DC.0001.E"]
    assert idx == [("drawing_sets",)]
