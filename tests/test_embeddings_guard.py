"""임베딩 인코더 방어 — "받은 벡터가 쓸 만한가" 를 인코더가 직접 묻는다.

이 파일이 지키는 한 문장:
    **퇴화한 벡터는 어떤 경로로도 정상 벡터로 돌아가지 않는다. None 으로 돌아간다.**

None 이어야 code_indexer 가 설계대로 BM25 로 후퇴한다. 0 벡터가 정상으로 통과하면
후퇴가 안 일어나고, 검색은 "0건" 이 아니라 **엉뚱한 문서를 순위에 올린다**
(argsort 가 0 점 동점을 말뭉치 순서로 돌려주기 때문). 2026-09-06 검토.
"""
from __future__ import annotations

import math
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def fake_embed_server(monkeypatch):
    import tests.fake_hcx_server as fake
    fake._real_fake_embed = fake.fake_embed          # 폭을 바꿔 끼우는 테스트가 원본을 부른다
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NDT_HCX_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NDT_STUDIO_TOKEN", "testkey")
    from app.config import load_yaml
    load_yaml.cache_clear()
    yield fake
    srv.shutdown()
    load_yaml.cache_clear()


def _with_cfg(monkeypatch, **extra):
    """embeddings.config() 에 키를 덧씌운다 (yaml 캐시는 건드리지 않는다)."""
    from app import embeddings
    base = embeddings.config()
    monkeypatch.setattr(embeddings, "config", lambda: {**base, **extra})


def test_zero_vector_response_returns_none(fake_embed_server, monkeypatch, caplog):
    """서버가 0 벡터를 주면 정규화해서 넘기지 않고 None."""
    monkeypatch.setattr(fake_embed_server, "fake_embed", lambda t, dim=1024: [0.0] * dim)
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        assert embeddings.embed_texts(["배치 표본 검사"]) is None
    assert any("0 벡터" in r.getMessage() for r in caplog.records)      # 폭 불일치가 아니라 0 벡터로 걸려야 한다


def test_nan_vector_response_returns_none(fake_embed_server, monkeypatch, caplog):
    monkeypatch.setattr(fake_embed_server, "fake_embed", lambda t, dim=1024: [math.nan] * dim)
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        assert embeddings.embed_texts(["x"]) is None
    assert any("NaN" in r.getMessage() for r in caplog.records)


def test_one_bad_row_in_batch_rejects_whole_batch(fake_embed_server, monkeypatch):
    """배치 32건 중 1건만 0 이어도 그 배치는 통째로 None — 절반만 믿는 인덱스는 없다."""
    import tests.fake_hcx_server as fake
    real = fake.fake_embed
    monkeypatch.setattr(fake, "fake_embed",
                        lambda t, dim=1024: ([0.0] * dim) if t == "BAD" else real(t, dim))
    from app import embeddings
    assert embeddings.embed_texts(["good one", "BAD", "good two"]) is None


def test_dim_mismatch_with_configured_dim_returns_none(fake_embed_server, monkeypatch, caplog):
    """설정이 1024 를 기대하는데 256 이 오면 None. 서버 쪽 모델이 바뀐 신호다."""
    _with_cfg(monkeypatch, dim=1024)
    monkeypatch.setattr(fake_embed_server, "fake_embed",
                        lambda t, dim=256: fake_embed_server.__dict__["_real_fake_embed"](t, 256))
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        assert embeddings.embed_texts(["x"]) is None
    assert any("1024" in r.getMessage() and "256" in r.getMessage() for r in caplog.records)


def test_dim_unset_accepts_any_consistent_dim(fake_embed_server, monkeypatch):
    """dim 을 안 적으면 검사하지 않는다 (가짜 서버 256, 사내 bge-m3 1024 둘 다 돌아야 한다)."""
    _with_cfg(monkeypatch, dim=None)
    monkeypatch.setattr(fake_embed_server, "fake_embed",
                        lambda t, dim=256: fake_embed_server.__dict__["_real_fake_embed"](t, 256))
    from app import embeddings
    v = embeddings.embed_texts(["x", "y"])
    assert v is not None and v.shape == (2, 256)


def test_http_200_with_error_key_returns_none(fake_embed_server, monkeypatch, caplog):
    """가이드: MSL 모델이 아니면 실패해도 HTTP 200. error 키가 있으면 data 가 멀쩡해 보여도 None."""
    _with_cfg(monkeypatch, model="error-200")
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        assert embeddings.embed_texts(["x"]) is None
    assert any("error" in r.getMessage().lower() for r in caplog.records)


def test_query_vector_follows_same_guard(fake_embed_server, monkeypatch):
    monkeypatch.setattr(fake_embed_server, "fake_embed", lambda t, dim=1024: [0.0] * dim)
    from app import embeddings
    assert embeddings.embed_query("x") is None


# ─────────────────────────── 검토 C2 (2026-09-06): 인코더가 예외로 죽는 경로 ───────────────────────────

def test_batches_with_different_widths_return_none(fake_embed_server, monkeypatch, caplog):
    """배치 1 은 1024폭, 배치 2 는 256폭 (호출 도중 서버 모델이 바뀜). vstack 이 ValueError 를
    내며 embed_texts 가 죽으면 호출자의 후퇴가 없다. None 이어야 한다."""
    _with_cfg(monkeypatch, batch_size=1, dim=None)
    real = fake_embed_server.__dict__["_real_fake_embed"]
    monkeypatch.setattr(fake_embed_server, "fake_embed",
                        lambda t, dim=1024: real(t, 256 if t == "second" else 1024))
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        assert embeddings.embed_texts(["first", "second"]) is None


def test_non_numeric_dim_config_skips_width_check_with_warning(fake_embed_server, monkeypatch, caplog):
    """설정 오타(dim: abc)로 검색이 통째로 죽으면 안 된다. 폭 검사만 건너뛰고 경고."""
    _with_cfg(monkeypatch, dim="abc")
    from app import embeddings
    with caplog.at_level("WARNING", logger="app.embeddings"):
        v = embeddings.embed_texts(["x"])
    assert v is not None and v.shape == (1, 1024)
    assert any("embedding.dim" in r.getMessage() for r in caplog.records)
