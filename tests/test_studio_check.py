"""Studio XP 점검 — hcx-check 가 HCX 만 보고 임베딩 경로를 안 보던 구멍을 막는다.

임베딩은 운영 경로다 (embedding.enabled: true, base_url = Studio XP). 그런데 점검 명령은
cfg["api"] (HCX) 만 찔렀다. 임베딩이 죽으면 점검에서도, 실행에서도(0 벡터 통과) 안 보였다.

app.studio_check.run() 은 순수 함수에 가깝게 dict 를 돌려주고, hcx-check 가 그걸 출력한다.
GET /v1/models 와 임베딩 1건은 일일 HCX 호출량을 쓰지 않는다.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import numpy as np
import pytest


@pytest.fixture()
def fake_studio(monkeypatch):
    import tests.fake_hcx_server as fake
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NDT_HCX_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NDT_STUDIO_TOKEN", "testkey")
    monkeypatch.setenv("NDT_HCX_TOKEN", "testkey")
    monkeypatch.setenv("NDT_HCX_MOCK", "0")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    from app.config import load_yaml
    load_yaml.cache_clear()
    yield fake
    srv.shutdown()
    load_yaml.cache_clear()


def test_reports_provider_models_missing_from_served_list(fake_studio, monkeypatch):
    """hcx.yaml 의 Studio provider 모델 중 /v1/models 에 없는 것을 이름으로 보고한다.
    v3(HCX) provider 는 Studio 목록과 무관하므로 대조하지 않는다."""
    monkeypatch.setattr(fake_studio, "_MODELS", ["bge-m3", "gemma4-31b"])
    from app import studio_check
    res = studio_check.run(scan_stored=False)
    assert res["models"]["ok"] is True
    assert "gemma4-31b" in res["models"]["served"]
    missing = {m for _, m in res["models"]["missing"]}
    assert "gpt-oss-120b" in missing
    assert "gemma4-31b" not in missing
    assert "HCX-007" not in missing


def test_embedding_ping_reports_health_and_dim(fake_studio):
    from app import studio_check
    res = studio_check.run(scan_stored=False)
    assert res["embed"]["ok"] is True
    assert res["embed"]["dim"] == 1024
    assert res["embed"]["model"] == "bge-m3"


def test_embedding_model_missing_from_served_list_is_flagged(fake_studio, monkeypatch):
    monkeypatch.setattr(fake_studio, "_MODELS", ["gemma4-31b"])
    from app import studio_check
    res = studio_check.run(scan_stored=False)
    assert res["embed"]["served"] is False


def test_degenerate_embedding_ping_is_not_ok(fake_studio, monkeypatch):
    monkeypatch.setattr(fake_studio, "fake_embed", lambda t, dim=1024: [0.0] * dim)
    from app import studio_check
    res = studio_check.run(scan_stored=False)
    assert res["embed"]["ok"] is False


def test_stored_vector_scan_finds_zero_rows_and_dim_drift(fake_studio, fresh_db):
    """사내 data/index/embeddings/*.npy 에 장애 중 적재된 0 행이 있으면 여기서 보인다 (A-4)."""
    from tests.test_code_indexer import _load_three
    from app.extractors import code_indexer as ci
    from app import config as appcfg
    _load_three(ci, embed=True)
    npys = sorted((appcfg.DATA_DIR / "index" / "embeddings").glob("*.npy"))
    assert len(npys) == 3
    v0 = np.load(npys[0]); v0[0, :] = 0.0; np.save(npys[0], v0)          # 0 행 하나
    v1 = np.load(npys[1]); np.save(npys[1], v1[:, :64])                   # 폭 망가짐
    from app import studio_check
    res = studio_check.run(scan_stored=True)
    by_id = {r["doc_id"]: r for r in res["stored"]}
    assert len(by_id) == 3
    ids = sorted(by_id)
    assert by_id[int(npys[0].stem)]["zero_rows"] == 1
    assert by_id[int(npys[1].stem)]["dim_drift"] is True
    assert by_id[int(npys[2].stem)]["zero_rows"] == 0 and by_id[int(npys[2].stem)]["dim_drift"] is False
    assert res["stored_ok"] is False


def test_hcx_check_cli_prints_studio_stage(fake_studio, fresh_db):
    """사용자가 실제로 보는 것은 hcx-check 출력이다. Studio 단계가 그 안에 있어야 한다."""
    from typer.testing import CliRunner
    from app.main import app
    r = CliRunner().invoke(app, ["hcx-check"])
    assert r.exit_code == 0, r.output
    assert "Studio XP" in r.output
    assert "bge-m3" in r.output


# ─────────────────────────── 검토 C4 (2026-09-06): 첫 설치에서 점검이 죽는 경로 ───────────────────────────

def test_stored_scan_without_db_does_not_crash(fake_studio, tmp_path, monkeypatch):
    """첫 설치에서는 hcx-check 가 ingest-standards 보다 먼저 돈다. DB 테이블이 없을 때
    저장 벡터 스캔이 OperationalError 트레이스백으로 죽으면 점검 명령 자체가 못 쓰는 것이 된다."""
    data = tmp_path / "data"; data.mkdir()
    monkeypatch.setattr("app.config.DATA_DIR", data)
    from app.database import models as m
    monkeypatch.setattr(m, "DATA_DIR", data)
    monkeypatch.setattr(m, "_engine", None, raising=False)
    monkeypatch.setattr(m, "_SessionFactory", None, raising=False)
    from app.extractors import code_indexer as ci
    monkeypatch.setattr(ci, "get_session", m.get_session)
    from app import studio_check
    res = studio_check.run(scan_stored=True)          # init_db 를 부르지 않은 상태
    assert res["stored"] == []
    assert res["stored_ok"] is True
    assert res.get("stored_note")


def test_hcx_check_cli_survives_studio_exception(fake_studio, fresh_db, monkeypatch):
    """Studio 점검 코드 자체가 예외를 내도 사용자에게는 ✗ 한 줄과 판정이 나가야 한다. 트레이스백 금지."""
    from app import studio_check
    def boom(**kw): raise RuntimeError("boom")
    monkeypatch.setattr(studio_check, "run", boom)
    from typer.testing import CliRunner
    from app.main import app
    r = CliRunner().invoke(app, ["hcx-check"])
    assert r.exit_code == 1
    assert "Studio 점검 자체가 실패" in r.output and "RuntimeError" in r.output
    assert "Traceback" not in r.output
