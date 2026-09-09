"""임베딩 클라이언트 — Studio XP 의 bge-m3 (OpenAI 호환 /v1/embeddings).

왜 필요한가 (2026-09-03)
    규격 원문은 러시아어·영어(GOST, SP 70)이고 질의는 한국어다. 어휘 매칭(BM25)
    으로는 원리적으로 못 찾는다. bge-m3 는 다국어 임베딩 모델이라 한↔러↔영 교차
    검색이 된다. 사내 Studio XP 의 /v1/models 에 이미 올라와 있다.

원칙
    - 실패하면 None 을 돌려준다. 호출자는 BM25 로 후퇴한다. 임베딩 서버가 죽었다고
      검색이 0건이 되면 안 된다.
    - 키는 환경변수(NDT_STUDIO_TOKEN)에서만 읽는다. 파일에 적지 않는다.
    - 벡터는 L2 정규화해서 돌려준다 → 코사인 = 내적.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
import numpy as np

from app.config import hcx_config

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": True,
    "path": "/v1/embeddings",
    "model": "bge-m3",
    "token_env": "NDT_STUDIO_TOKEN",
    "batch_size": 32,
    "timeout_seconds": 60,
    "max_chars": 6000,        # 청크가 이보다 길면 잘라서 보낸다 (bge-m3 8192 토큰)
    "dim": None,              # 기대 차원. None 이면 검사 안 함. hcx.yaml 이 bge-m3 = 1024 로 둔다.
}


def config() -> dict:
    cfg = hcx_config().get("embedding") or {}
    return {**_DEFAULTS, **cfg}


def available() -> bool:
    """설정이 켜져 있고 키가 있으면 True. 네트워크는 확인하지 않는다."""
    c = config()
    if not c.get("enabled") or not c.get("base_url"):
        return False
    return bool(os.environ.get(c["token_env"], "").strip())


def _headers(c: dict) -> dict:
    token = os.environ.get(c["token_env"], "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _trust_env() -> bool:
    try:
        from app.hcx_client import _trust_env as f   # 프록시 무시 옵션을 chat 과 공유
        return f()
    except Exception:
        return True


def embed_texts(texts: list[str]) -> Optional[np.ndarray]:
    """문장 목록 → (n, dim) float32, L2 정규화. 실패하면 None."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    if not available():
        return None
    c = config()
    url = c["base_url"].rstrip("/") + c["path"]
    bs = int(c["batch_size"])
    out: list[np.ndarray] = []
    try:
        with httpx.Client(timeout=float(c["timeout_seconds"]), trust_env=_trust_env()) as client:
            for i in range(0, len(texts), bs):
                batch = [t[: int(c["max_chars"])] for t in texts[i:i + bs]]
                r = client.post(url, json={"model": c["model"], "input": batch}, headers=_headers(c))
                if r.status_code != 200:
                    logger.warning("임베딩 호출 실패 http=%s: %s", r.status_code, r.text[:200])
                    return None
                j = r.json() or {}
                # 가이드: MSL 모델이 아니면 실패해도 HTTP 200. error 키가 있으면 data 를 믿지 않는다.
                if j.get("error"):
                    logger.warning("임베딩 응답에 error 키 (HTTP 200): %s", str(j.get("error"))[:200])
                    return None
                data = j.get("data") or []
                if len(data) != len(batch):
                    logger.warning("임베딩 응답 개수 불일치 %s != %s", len(data), len(batch))
                    return None
                data.sort(key=lambda d: d.get("index", 0))
                out.append(np.asarray([d["embedding"] for d in data], dtype=np.float32))
    except Exception as e:      # noqa: BLE001 - 어떤 실패든 후퇴
        logger.warning("임베딩 호출 예외: %s: %s", type(e).__name__, e)
        return None
    try:
        vec = np.vstack(out)
    except ValueError as e:          # 배치끼리 폭이 다름 — 호출 도중 서버 모델이 바뀐 경우
        logger.warning("임베딩 배치 폭 불일치 — 호출 도중 서버 모델이 바뀐 듯. 버림: %s", str(e)[:120])
        return None
    if not _usable(vec, c.get("dim")):
        return None
    return vec / np.linalg.norm(vec, axis=1, keepdims=True)


def _usable(vec: np.ndarray, expected_dim) -> bool:
    """받은 벡터가 쓸 만한가. 아니면 경고를 남기고 False — 호출자는 None 을 돌려 BM25 로 후퇴한다.

    0 벡터를 정규화해서 넘기면(예전 코드: norm 0 → 1 로 치환) 코사인이 전부 0 이 되어
    검색이 '0건' 이 아니라 **말뭉치 앞쪽 문서를 순위에 올린다**. 실패가 성공으로 둔갑한다."""
    if vec.ndim != 2 or vec.shape[0] == 0:
        logger.warning("임베딩 모양 이상: %s", vec.shape)
        return False
    if not np.isfinite(vec).all():
        logger.warning("임베딩에 NaN/inf 포함 — 버림")
        return False
    zero_rows = int((np.linalg.norm(vec, axis=1) == 0).sum())
    if zero_rows:
        logger.warning("임베딩 0 벡터 %d/%d 행 — 배치 전체를 버림 (부분만 믿는 인덱스는 없다)",
                       zero_rows, vec.shape[0])
        return False
    expected = _expected_dim(expected_dim)
    if expected and int(vec.shape[1]) != expected:
        logger.warning("임베딩 차원 불일치: 기대 %d, 실제 %d — 서버 쪽 모델이 바뀌었을 수 있음. "
                       "hcx-check 로 확인하고 embedding.dim 을 맞추거나 reindex-standards",
                       expected, int(vec.shape[1]))
        return False
    return True


def _expected_dim(value) -> Optional[int]:
    """설정 embedding.dim 을 정수로. 비었으면 검사 안 함(None). 숫자가 아니면 경고 후 검사 생략 —
    설정 오타로 검색 전체가 BM25 로 내려앉는 것보다 폭 검사 하나를 건너뛰는 쪽이 낫다."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("설정 embedding.dim 값이 숫자가 아님(%r) — 폭 검사 생략. config/hcx.yaml embedding.dim 확인", value)
        return None


def embed_query(text: str) -> Optional[np.ndarray]:
    """질의 1건 → (dim,) 벡터. 실패하면 None."""
    v = embed_texts([text])
    return None if v is None or v.shape[0] == 0 else v[0]
