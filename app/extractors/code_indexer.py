"""Code & Standard 문서 인덱스 — BM25(다국어 토큰) + bge-m3 임베딩 하이브리드 + 표 전사.

역사
    처음에는 "폐쇄망에서 임베딩 모델 없이 동작하도록" 라틴 문자만 보는 키워드
    인덱스였다. 2026-09-03 점검에서 그 토크나이저가 **한국어 질의에 토큰을 0개**
    뽑아 `search()` 가 항상 빈 목록을 돌려주고 있던 것이 확인됐다. code_lookup
    은 그동안 빈 context 로 판정하고 있었다.

지금 구조
    1. 토큰: 라틴(SP, VT, GOST, B31.3) · 숫자코드(70.13330, 7-010-89) · 키릴 ·
       한글 어절 + 한글 2-gram(조사 변형 흡수). 1글자는 버린다.
    2. BM25 (IDF 포함). 예전 점수식은 IDF 가 없어 흔한 단어가 지배했다.
    3. 임베딩(bge-m3)이 되면 코사인 검색을 함께 돌리고 **RRF** 로 순위를 합친다.
    4. 임베딩이 없거나 실패하면 BM25 만으로 조용히 후퇴한다.
    5. **표 페이지**는 이미지 모델이 Markdown 표로 옮기고 OCR 숫자와 대조한다
       (table_transcriber). 청크마다 confidence / needs_review 가 붙어 인용에 전파된다.

벡터 저장
    SQLite JSON 에 넣으면 1024차원 × 수천 청크가 수십 MB 가 되므로
    data/index/embeddings/<doc_id>.npy 사이드카에 float32 로 둔다.
    chunks_json["embedding"] 에 model/dim/n 메타만 적는다.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional, Union

import numpy as np

from app import config as appcfg
from app.database.models import get_session, init_db
from app.database.repository import upsert_standard
from app.extractors.pdf_extractor import extract

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1200
_OVERLAP = 200

# ─────────────────────────── 토큰화 ───────────────────────────

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]*")
_NUM = re.compile(r"\d+(?:[\.\-]\d+)*")
_CYR = re.compile(r"[А-Яа-яЁё]+")
_HAN = re.compile(r"[가-힣]+")


def _tokens(text: str) -> Counter:
    """다국어 토큰. 한글은 어절과 2-gram 을 함께 낸다 (용접부의 → 용접부의·용접·접부·부의)."""
    c: Counter = Counter()

    def _with_head(t: str) -> None:
        """'10922-2012' → 10922-2012 + 10922, 'b31.3' → b31.3 + b31.
        문서는 '10922-2012' 로, 질문은 '10922' 로 적는 일이 흔하다."""
        c[t] += 1
        head = re.split(r"[\-\.]", t, maxsplit=1)[0]
        if head != t and len(head) >= 2:
            c[head] += 1

    for m in _LATIN.finditer(text):
        t = m.group().lower().rstrip(".")
        if len(t) >= 2:
            _with_head(t)
    for m in _NUM.finditer(text):
        t = m.group().rstrip(".")
        if len(t) >= 2:
            _with_head(t)
    for m in _CYR.finditer(text):
        t = m.group().lower()
        if len(t) >= 2:
            c[t] += 1
    for m in _HAN.finditer(text):
        t = m.group()
        if len(t) >= 2:
            c[t] += 1
        if len(t) >= 3:
            for i in range(len(t) - 1):
                c[t[i:i + 2]] += 1
    return c


# ─────────────────────────── 청크 ───────────────────────────

# 페이지 입력 형식 두 가지를 받는다: (page_no, text) 또는
# {"page": n, "text": ..., "source": ..., "confidence": ..., "needs_review": ..., "table_score": ...}
PageInput = Union[tuple, dict]

_PAGE_META_KEYS = ("source", "confidence", "needs_review", "table_score", "table_model")


def _norm_page(p: PageInput) -> dict:
    if isinstance(p, dict):
        return {"page": p.get("page"), "text": p.get("text") or "",
                **{k: p.get(k) for k in _PAGE_META_KEYS if k in p}}
    page_no, text = p
    return {"page": page_no, "text": text or ""}


def _chunk(text: str) -> list[dict]:
    chunks: list[dict] = []
    i = 0
    while i < len(text):
        end = min(len(text), i + _CHUNK_SIZE)
        chunks.append({"text": text[i:end], "char_start": i, "char_end": end})
        if end >= len(text):
            break
        i = end - _OVERLAP
    return chunks


def build_chunks(pages: Iterable[PageInput]) -> list[dict]:
    """페이지 목록 → 청크 목록 (토큰 + 페이지 메타 상속)."""
    out: list[dict] = []
    for raw in pages:
        p = _norm_page(raw)
        meta = {k: p[k] for k in _PAGE_META_KEYS if k in p and p[k] is not None}
        for ch in _chunk(p["text"]):
            ch["page"] = p["page"]
            ch["tokens"] = _tokens(ch["text"])
            ch.update(meta)
            out.append(ch)
    return out


def _serialize_chunk(c: dict) -> dict:
    d = {
        "text": c["text"],
        "page": c["page"],
        "char_start": c["char_start"],
        "char_end": c["char_end"],
        "tokens": dict(c["tokens"]),
    }
    for k in _PAGE_META_KEYS:
        if c.get(k) is not None:
            d[k] = c[k]
    return d


# ─────────────────────────── 벡터 사이드카 ───────────────────────────

def _vectors_dir() -> Path:
    d = appcfg.DATA_DIR / "index" / "embeddings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vectors_path(doc_id: int) -> Path:
    return _vectors_dir() / f"{doc_id}.npy"


def _save_vectors(doc_id: int, vec: np.ndarray) -> None:
    np.save(_vectors_path(doc_id), vec.astype(np.float32))


def _load_vectors(doc_id: int, n_expected: int, dim_expected: Optional[int] = None) -> Optional[np.ndarray]:
    """사이드카 벡터. 행 수가 청크 수와 다르거나 **열 수가 메타 dim 과 다르면** 없는 것으로 본다.
    열 수를 안 보면 다른 폭의 벡터가 mat @ q 에서 ValueError 를 내고 search 전체가 죽는다."""
    p = _vectors_path(doc_id)
    if not p.exists():
        return None
    try:
        v = np.load(p)
    except Exception:       # noqa: BLE001
        return None
    if v.ndim != 2 or v.shape[0] != n_expected:
        return None
    if dim_expected and int(v.shape[1]) != int(dim_expected):
        logger.warning("문서 %s 벡터 폭 %d ≠ 메타 dim %d — 이 문서는 BM25 만으로 검색 (reindex-standards 권장)",
                       doc_id, int(v.shape[1]), int(dim_expected))
        return None
    return v


def _embed_chunks(chunks: list[dict]) -> Optional[np.ndarray]:
    from app import embeddings
    if not embeddings.available():
        return None
    return embeddings.embed_texts([c["text"] for c in chunks])


# ─────────────────────────── 표 전사 ───────────────────────────

from app.extractors.table_pages import page_records, render_for_score  # noqa: E402  (공유 파이프라인)


def _render_for_score(pdf_path: Path, page_index: int, dpi: int):
    """테스트가 바꿔 끼우는 이름 — 실제 구현은 table_pages.render_for_score."""
    return render_for_score(pdf_path, page_index, dpi)


def _page_records(pdf_path: Path, extracted, *, tables: Optional[bool]) -> tuple[list[dict], dict]:
    return page_records(pdf_path, extracted, tables=tables, render=_render_for_score)


# ─────────────────────────── 적재 ───────────────────────────

def index_document(file_path: str, document_no: str, pages: Iterable[PageInput],
                   *, embed: Optional[bool] = None) -> dict:
    """페이지 텍스트를 인덱스에 넣는다. ingest() 와 테스트가 공유한다.

    embed: None 이면 설정(hcx.yaml embedding.enabled)과 키 유무를 따른다.
    """
    chunks = build_chunks(pages)
    init_db()
    with get_session() as s:
        doc = upsert_standard(
            s,
            file_path=str(file_path),
            doc_type="code",
            document_no=document_no,
            revision=None,
            chunks_json={"chunks": [_serialize_chunk(c) for c in chunks]},
        )
        s.flush()
        doc_id = doc.id
        meta = None
        if embed is not False and chunks:
            vec = _embed_chunks(chunks)
            if vec is not None:
                _save_vectors(doc_id, vec)
                from app import embeddings
                meta = {"model": embeddings.config()["model"], "dim": int(vec.shape[1]),
                        "n": int(vec.shape[0])}
            elif embed is True:
                logger.warning("임베딩을 요청했지만 사용할 수 없어 BM25 만 적재: %s", file_path)
        cj = dict(doc.chunks_json or {})
        if meta:
            cj["embedding"] = meta
        else:
            cj.pop("embedding", None)
            _vectors_path(doc_id).unlink(missing_ok=True)
        doc.chunks_json = cj
        s.commit()
    low = sum(1 for c in chunks if c.get("needs_review"))
    return {"file": str(file_path), "chunks": len(chunks), "embedded": bool(meta),
            "needs_review_chunks": low}


def ingest(file_path: Path, *, embed: Optional[bool] = None, tables: Optional[bool] = None) -> dict:
    """PDF 1건 적재. 텍스트 레이어 없으면 pdf_extractor 가 OCR 한다(Adobe 불요 — 수단 통일).

    tables: None 이면 hcx.yaml table_pipeline.enabled 를 따른다.
    """
    file_path = Path(file_path)
    extracted = extract(file_path)
    pages, stats = _page_records(file_path, extracted, tables=tables)
    res = index_document(str(file_path), file_path.stem, pages, embed=embed)
    res["table_stats"] = stats
    return res


def reindex_embeddings() -> dict:
    """이미 적재된 code 문서 전부에 임베딩을 (다시) 계산한다. PDF 재추출은 안 한다."""
    from app.database.models import StandardDocument
    from sqlalchemy import select
    from app import embeddings

    if not embeddings.available():
        return {"ok": False, "reason": "임베딩 사용 불가 (설정 꺼짐 또는 키 없음)"}
    done, failed = [], []
    with get_session() as s:
        docs = list(s.scalars(select(StandardDocument).where(StandardDocument.doc_type == "code")))
        for doc in docs:
            chunks = (doc.chunks_json or {}).get("chunks", [])
            if not chunks:
                continue
            vec = embeddings.embed_texts([c["text"] for c in chunks])
            if vec is None:
                failed.append(doc.document_no or doc.file_path)
                continue
            _save_vectors(doc.id, vec)
            cj = dict(doc.chunks_json or {})
            cj["embedding"] = {"model": embeddings.config()["model"],
                               "dim": int(vec.shape[1]), "n": int(vec.shape[0])}
            doc.chunks_json = cj
            done.append(doc.document_no or doc.file_path)
        s.commit()
    return {"ok": True, "embedded": done, "failed": failed}


# ─────────────────────────── 검색 ───────────────────────────

_K1, _B = 1.5, 0.75
_RRF_K = 60


def _retrieval_cfg() -> dict:
    cfg = appcfg.hcx_config().get("retrieval") or {}
    return {"top_k": 5, "bm25_candidates": 30, "embed_candidates": 30,
            "hybrid": True, "rrf_k": _RRF_K, **cfg}


def _load_corpus() -> list[dict]:
    """모든 code 청크를 평탄화. [{doc_id, doc, file, page, text, tokens, dl, vec, meta…}]"""
    from app.database.models import StandardDocument
    from sqlalchemy import select
    rows: list[dict] = []
    with get_session() as s:
        for doc in s.scalars(select(StandardDocument).where(StandardDocument.doc_type == "code")):
            cj = doc.chunks_json or {}
            chunks = cj.get("chunks", [])
            emeta = cj.get("embedding") or {}
            vecs = _load_vectors(doc.id, len(chunks), emeta.get("dim")) if emeta else None
            for i, ch in enumerate(chunks):
                toks = ch.get("tokens", {})
                rows.append({
                    "doc_id": doc.id, "doc": doc.document_no or "", "file": doc.file_path,
                    "page": ch.get("page"), "text": ch.get("text", ""),
                    "tokens": toks, "dl": sum(toks.values()) or 1,
                    "vec": (vecs[i] if vecs is not None else None),
                    "chunk_source": ch.get("source"),
                    "confidence": ch.get("confidence"),
                    "needs_review": bool(ch.get("needs_review", False)),
                })
    return rows


def _bm25_rank(q_tokens: Counter, corpus: list[dict], n: int) -> list[tuple[int, float]]:
    """(corpus index, score) 상위 n. IDF 는 매 호출 계산 — 코퍼스가 작아 충분히 빠르다."""
    N = len(corpus)
    if N == 0 or not q_tokens:
        return []
    df: Counter = Counter()
    for row in corpus:
        for t in q_tokens:
            if t in row["tokens"]:
                df[t] += 1
    avgdl = sum(r["dl"] for r in corpus) / N
    scored: list[tuple[int, float]] = []
    for idx, row in enumerate(corpus):
        s = 0.0
        for t, qf in q_tokens.items():
            tf = row["tokens"].get(t)
            if not tf:
                continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * row["dl"] / avgdl)) * (1 + 0.1 * (qf - 1))
        if s > 0:
            scored.append((idx, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:n]


def _embed_rank(query: str, corpus: list[dict], n: int) -> Optional[list[tuple[int, float]]]:
    from app import embeddings
    idxs = [i for i, r in enumerate(corpus) if r["vec"] is not None]
    if not idxs:
        return None
    q = embeddings.embed_query(query)
    if q is None:
        return None
    # 인코더가 못 막은 것이 여기까지 왔을 때의 마지막 방어. 죽지 말고 None → BM25.
    if not np.isfinite(q).all() or float(np.linalg.norm(q)) == 0.0:
        logger.warning("질의 벡터 퇴화(0/NaN) — 임베딩 순위 생략, BM25 만")
        return None
    try:
        mat = np.vstack([corpus[i]["vec"] for i in idxs])
    except ValueError as e:          # 저장 벡터끼리 폭이 다름
        logger.warning("저장 벡터 폭 불일치 — 임베딩 순위 생략, BM25 만: %s", e)
        return None
    if q.shape[0] != mat.shape[1]:
        logger.warning("질의 벡터 %d차원 ≠ 저장 %d차원 — 서버 모델 교체 의심. 임베딩 순위 생략, BM25 만",
                       int(q.shape[0]), int(mat.shape[1]))
        return None
    sims = mat @ q
    order = np.argsort(-sims)[:n]
    return [(idxs[int(o)], float(sims[int(o)])) for o in order]


def _rrf(rankings: list[list[tuple[int, float]]], k: int) -> list[tuple[int, float]]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, (idx, _) in enumerate(ranking, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


def search(query: str, *, top_k: Optional[int] = None, hybrid: Optional[bool] = None) -> list[dict]:
    """상위 청크 반환. 각 항목에 source = bm25 | embed | hybrid,
    그리고 청크 신뢰도(confidence / needs_review / chunk_source)가 붙는다."""
    rc = _retrieval_cfg()
    k = int(top_k or rc["top_k"])
    use_hybrid = rc["hybrid"] if hybrid is None else hybrid

    corpus = _load_corpus()
    if not corpus:
        return []

    q_tokens = _tokens(query)
    bm = _bm25_rank(q_tokens, corpus, int(rc["bm25_candidates"]))
    em = _embed_rank(query, corpus, int(rc["embed_candidates"])) if use_hybrid else None

    if em:
        fused = _rrf([bm, em], int(rc["rrf_k"]))
        source = "hybrid" if bm else "embed"
        picked = fused[:k]
    else:
        source = "bm25"
        picked = bm[:k]

    bm_score = dict(bm)
    em_score = dict(em or [])
    out: list[dict] = []
    for idx, score in picked:
        row = corpus[idx]
        out.append({
            "doc": row["doc"], "file": row["file"], "page": row["page"],
            "text": row["text"][:1500], "score": round(float(score), 6),
            "source": source,
            "bm25": round(bm_score.get(idx, 0.0), 4),
            "cosine": round(em_score.get(idx, 0.0), 4) if em else None,
            "chunk_source": row.get("chunk_source"),
            "confidence": row.get("confidence"),
            "needs_review": row.get("needs_review", False),
        })
    return out
