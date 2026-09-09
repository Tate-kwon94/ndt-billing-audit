"""규격 검색기 회귀 테스트 — 다국어 토큰 · BM25 · 임베딩 하이브리드 · 후퇴.

2026-09-03 발견: 예전 토크나이저가 라틴 문자만 봐서 한국어 질의에 토큰 0개 →
search() 가 항상 [] → code_lookup 이 빈 context 로 판정하고 있었다.
이 파일은 그 회귀를 막고, 임베딩이 없을 때 조용히 BM25 로 후퇴하는지 지킨다.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest


# ─────────────────────────── 토큰화 (DB 불필요) ───────────────────────────

def test_korean_query_yields_tokens_not_zero():
    """회귀: 한국어 질의가 토큰 0개가 되면 안 된다."""
    from app.extractors.code_indexer import _tokens
    t = _tokens("철근 케이지 용접부의 VT 검사 범위 근거는?")
    assert len(t) > 0
    assert "vt" in t
    assert "검사" in t
    assert "용접" in t            # 2-gram 이 조사 변형을 흡수한다


def test_short_codes_numbers_and_cyrillic():
    from app.extractors.code_indexer import _tokens
    t = _tokens("SP 70.13330 이 지정하는 기준. GOST 10922-2012, PNAEG 7-010-89. Сварные соединения")
    for expected in ("sp", "70.13330", "gost", "10922-2012", "7-010-89", "pnaeg", "сварные"):
        assert expected in t, expected
    assert "이" not in t          # 1글자는 버린다


# ─────────────────────────── DB 격리 fixture ───────────────────────────

@pytest.fixture()
def indexer(fresh_db):
    """임시 DATA_DIR + 새 SQLite. fresh_db 가 code_indexer 의 get_session/init_db 를 재바인딩한다."""
    from app.extractors import code_indexer as ci
    return ci


GOST_TEXT = ("GOST 10922-2012. 6.7 Visual inspection of welded joints shall be performed "
             "batch-wise. Not less than 3 pieces shall be selected from each batch. "
             "6.17 If a batch fails, inspection shall be extended to 100 % of the batch.")
SP70_TEXT = ("SP 70.13330.2012. 10.4 Welded reinforcement assemblies and embedded parts shall be "
             "inspected in accordance with GOST 10922. Acceptance shall follow the design documents.")
NOISE_TEXT = ("ASME B31.3 Process Piping. Radiographic examination of girth welds shall use an IQI. "
              "Film density shall be between 1.8 and 4.0.")


def _load_three(ci, embed=None):
    ci.index_document("gost10922.pdf", "GOST 10922-2012", [(14, GOST_TEXT)], embed=embed)
    ci.index_document("sp70.pdf", "SP 70.13330.2012", [(88, SP70_TEXT)], embed=embed)
    ci.index_document("asme.pdf", "ASME B31.3", [(12, NOISE_TEXT)], embed=embed)


# ─────────────────────────── BM25 ───────────────────────────

def test_bm25_korean_query_with_code_is_not_empty(indexer, monkeypatch):
    """한국어 질의라도 코드(GOST 10922)가 섞이면 그 코드를 언급하는 문서들이 나와야 한다.

    GOST 원문과 SP 70 원문 **둘 다** GOST 10922 를 언급하므로 둘 다 정당한 후보다.
    어느 쪽이 1위인지는 BM25 길이 정규화에 달린 동전 던지기라 단정하지 않는다.
    한국어 단어만으로 영어 조항을 고르는 것은 BM25 의 일이 아니라 임베딩의 일이다.
    """
    monkeypatch.setattr("app.embeddings.available", lambda: False)
    _load_three(indexer)
    hits = indexer.search("GOST 10922 배치 표본 검사", top_k=3)
    assert hits, "한국어+코드 혼합 질의가 0건이면 회귀"
    docs = {h["doc"] for h in hits}
    assert "GOST 10922-2012" in docs and "SP 70.13330.2012" in docs
    assert "ASME B31.3" not in docs            # 무관한 문서는 끼지 않는다
    assert all(h["source"] == "bm25" for h in hits)


def test_bm25_discriminating_terms_rank_gost_first(indexer, monkeypatch):
    monkeypatch.setattr("app.embeddings.available", lambda: False)
    _load_three(indexer)
    hits = indexer.search("GOST 10922 batch-wise pieces per batch", top_k=3)
    assert hits[0]["doc"] == "GOST 10922-2012"
    assert "batch-wise" in hits[0]["text"]


def test_hyphenated_code_matches_bare_number():
    """문서의 '10922-2012' 를 질문의 '10922' 로 찾을 수 있어야 한다."""
    from app.extractors.code_indexer import _tokens
    doc = _tokens("GOST 10922-2012. Requirements. ASME B31.3 piping.")
    for expected in ("10922-2012", "10922", "b31.3", "b31"):
        assert expected in doc, expected


def test_bm25_sp70_number_token(indexer, monkeypatch):
    monkeypatch.setattr("app.embeddings.available", lambda: False)
    _load_three(indexer)
    hits = indexer.search("SP 70.13330 이 지정하는 검사 기준", top_k=2)
    assert hits and hits[0]["doc"] == "SP 70.13330.2012"


def test_search_empty_corpus_returns_empty(indexer):
    assert indexer.search("아무거나") == []


# ─────────────────────────── 임베딩 하이브리드 / 후퇴 ───────────────────────────

@pytest.fixture()
def fake_embed_server(monkeypatch):
    import tests.fake_hcx_server as fake
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NDT_HCX_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NDT_STUDIO_TOKEN", "testkey")
    from app.config import load_yaml
    load_yaml.cache_clear()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    load_yaml.cache_clear()


def test_embeddings_client_returns_normalized_vectors(fake_embed_server):
    from app import embeddings
    assert embeddings.available()
    v = embeddings.embed_texts(["batch-wise inspection", "배치 표본 검사"])
    assert v is not None and v.shape[0] == 2
    import numpy as np
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)


def test_hybrid_search_uses_embeddings_and_rrf(indexer, fake_embed_server):
    _load_three(indexer, embed=True)
    # 사이드카가 생겼는지
    from app import config as appcfg
    assert any((appcfg.DATA_DIR / "index" / "embeddings").glob("*.npy"))
    hits = indexer.search("GOST 10922 batch-wise inspection", top_k=3)
    assert hits
    assert hits[0]["source"] == "hybrid"
    assert hits[0]["cosine"] is not None
    assert hits[0]["doc"] == "GOST 10922-2012"


def test_falls_back_to_bm25_when_embedding_unavailable(indexer, monkeypatch):
    """임베딩이 죽어도 검색이 0건으로 떨어지면 안 된다."""
    monkeypatch.setattr("app.embeddings.available", lambda: False)
    res = indexer.index_document("gost10922.pdf", "GOST 10922-2012", [(14, GOST_TEXT)])
    assert res["embedded"] is False
    hits = indexer.search("GOST 10922 batch", top_k=1)
    assert hits and hits[0]["source"] == "bm25" and hits[0]["cosine"] is None


def test_embedding_failure_midway_falls_back(indexer, fake_embed_server, monkeypatch):
    """적재 때는 임베딩이 됐는데 검색 때 서버가 죽은 경우 → BM25 로."""
    _load_three(indexer, embed=True)
    from app import embeddings
    monkeypatch.setattr(embeddings, "embed_query", lambda text: None)
    hits = indexer.search("GOST 10922 batch", top_k=1)
    assert hits and hits[0]["source"] == "bm25"


def test_reindex_embeddings_adds_vectors_to_existing_docs(indexer, fake_embed_server):
    # 처음엔 임베딩 없이 적재 (embed=False 로 명시 — fixture 를 건드리지 않는다)
    _load_three(indexer, embed=False)
    assert not any((indexer.appcfg.DATA_DIR / "index" / "embeddings").glob("*.npy"))
    res = indexer.reindex_embeddings()
    assert res["ok"] and len(res["embedded"]) == 3 and not res["failed"]
    hits = indexer.search("GOST 10922 batch-wise", top_k=1)
    assert hits[0]["source"] == "hybrid"


# ─────────────────────────── 검색 쪽 방어 (2026-09-06) ───────────────────────────
# 인코더가 막지 못한 것이 검색까지 왔을 때. 원칙: search 는 절대 예외로 죽지 않고,
# 임베딩이 못 미더우면 source="bm25" 로 내려간다.

def test_zero_query_vector_does_not_fabricate_embed_ranking(indexer, fake_embed_server, monkeypatch):
    """0 벡터 질의 → 코사인 전부 0 → argsort 가 말뭉치 순서로 30개를 돌려줌 → RRF 가 그걸
    진짜 순위처럼 섞는다. 먼저 적재된 문서가 질의와 무관하게 올라온다. 그러면 안 된다."""
    import numpy as np
    _load_three(indexer, embed=True)
    from app import embeddings
    monkeypatch.setattr(embeddings, "embed_query", lambda text: np.zeros(1024, dtype=np.float32))
    hits = indexer.search("GOST 10922 batch", top_k=1)
    assert hits and hits[0]["source"] == "bm25" and hits[0]["cosine"] is None


def test_query_dim_mismatch_falls_back_to_bm25_without_exception(indexer, fake_embed_server, monkeypatch):
    """저장 1024차원, 질의 128차원 (서버 모델 교체) → mat @ q 가 ValueError 를 내면 search 전체가
    죽고 BM25 후퇴까지 같이 사라진다. 죽지 말고 bm25 로."""
    import numpy as np
    _load_three(indexer, embed=True)
    from app import embeddings
    q = np.ones(128, dtype=np.float32); q /= np.linalg.norm(q)
    monkeypatch.setattr(embeddings, "embed_query", lambda text: q)
    hits = indexer.search("GOST 10922 batch", top_k=1)
    assert hits and hits[0]["source"] == "bm25"


def test_stored_vectors_with_wrong_width_are_dropped(indexer, fake_embed_server, caplog):
    """사이드카 .npy 열 수가 메타 dim 과 다르면 (부분 손상·다른 모델로 재색인) **그 문서만** 벡터를
    버리고 나머지는 임베딩으로 검색한다. 검색 전체가 BM25 로 내려가면 이 규칙이 검증된 게 아니다."""
    import numpy as np
    from app import config as appcfg
    _load_three(indexer, embed=True)
    npys = sorted((appcfg.DATA_DIR / "index" / "embeddings").glob("*.npy"))
    assert npys
    bad = np.load(npys[0])
    np.save(npys[0], bad[:, :64])          # 폭만 망가뜨린다 (행 수는 그대로)
    with caplog.at_level("WARNING", logger="app.extractors.code_indexer"):
        hits = indexer.search("GOST 10922 batch-wise inspection", top_k=3)
    assert hits                                                    # 예외 없이
    assert any(h["source"] == "hybrid" for h in hits)              # 나머지 문서 벡터는 살아 있다
    bad_id = npys[0].stem
    assert any(bad_id in r.getMessage() and "폭" in r.getMessage() for r in caplog.records)
