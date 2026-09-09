"""공용 픽스처.

fresh_db — tmp_path 아래 일회용 SQLite. app.database.models 를 reload 하지 않는다.

왜 reload 가 아닌가: reload 는 모듈을 다시 실행해 declarative 레지스트리를 하나 더 만든다.
relationship("DrawingFile") 같은 문자열 관계는 선언 당시 Base 의 레지스트리에서 이름을 찾으므로
한 세션에서 reload 가 2회 이상 일어나면 나중 테스트가 매퍼를 처음 구성할 때
    InvalidRequestError: expression 'DrawingFile' failed to locate a name
로 죽는다. 순서 의존이라 pairwise 로는 재현되지 않는다. 엔진/세션팩토리를 리셋하고
DATA_DIR 만 tmp 로 돌리면 레지스트리 복제 없이 같은 격리를 얻는다 (2026-09-05 데모 repo 교훈).
"""
from __future__ import annotations

import importlib

import pytest

_DB_CONSUMERS = (
    "app.database.repository", "app.analyzers.pipeline", "app.analyzers.compliance",
    "app.extractors.report_segmenter", "app.extractors.drawing.requirements_extractor",
    "app.extractors.scwep_parser", "app.report.excel_writer", "app.extractors.code_indexer",
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """(models, repository) — 빈 SQLite 에 바인딩."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr("app.config.DATA_DIR", data)
    from app.database import models as m
    monkeypatch.setattr(m, "DATA_DIR", data)
    monkeypatch.setattr(m, "_engine", None, raising=False)
    monkeypatch.setattr(m, "_SessionFactory", None, raising=False)
    m.init_db()
    for name in _DB_CONSUMERS:
        try:
            mod = importlib.import_module(name)
        except Exception:      # noqa: BLE001 - 선택적 소비자
            continue
        for fn in ("get_session", "init_db"):
            if hasattr(mod, fn):
                monkeypatch.setattr(mod, fn, getattr(m, fn))
    from app.database import repository as r
    from app.analyzers import scwep_basis
    scwep_basis.reset_cache()
    return m, r
