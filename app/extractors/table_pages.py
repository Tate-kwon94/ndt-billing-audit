"""표 페이지 파이프라인 — 규격(③)과 도면(②)이 같은 코드를 쓴다.

extract() 결과의 페이지마다 저해상도로 렌더해 표 점수를 매기고, 표 페이지면 HCX-005 로 전사한 뒤
OCR 숫자와 대조해 needs_review 를 붙인다. 원래 code_indexer 안에 있었는데 (2026-09-03), 도면 DC 는
tesseract 글자만 HCX-007 에 넘기고 있어서 스캔 도면의 코드 표가 3~4개로 줄어드는 것이 사내에서 보였다
(2026-09-07 사용자 지적). 그래서 여기로 빼서 둘이 공유한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Callable, Optional

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

_SCORE_SCHEMA = 1


def _score_cache_path(pdf_path: Path, dpi: int) -> Optional[Path]:
    """표 점수 캐시 — (경로, 크기, mtime, dpi, 스키마) 키. 성적서 1,167쪽을 실행마다 다시 재지 않는다."""
    if not pdf_path.exists():       # 가짜 경로(테스트)·사라진 파일 — 캐시 없이 진행
        return None
    d = DATA_DIR / "table_score_cache"
    d.mkdir(parents=True, exist_ok=True)
    st = pdf_path.stat()
    h = hashlib.sha256(f"{pdf_path.resolve()}|{st.st_size}:{st.st_mtime_ns}|dpi={dpi}|schema={_SCORE_SCHEMA}".encode()).hexdigest()[:16]
    return d / f"{pdf_path.stem}.{h}.json"


def _load_scores(path: Path) -> dict:
    try:
        return {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()} if path.exists() else {}
    except Exception:       # noqa: BLE001 — 캐시 깨짐은 재계산으로
        return {}


def _save_scores(path: Path, scores: dict) -> None:
    try:
        path.write_text(json.dumps({str(k): v for k, v in scores.items()}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("표 점수 캐시 저장 실패 %s: %s", path, e)


def render_for_score(pdf_path: Path, page_index: int, dpi: int):
    """분류용 저해상도 렌더. 실패하면 None."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            return doc[page_index].render(scale=dpi / 72).to_pil()
        finally:
            doc.close()
    except Exception as e:      # noqa: BLE001
        logger.warning("분류용 렌더 실패 %s p%s: %s", pdf_path.name, page_index + 1, e)
        return None


def page_records(pdf_path: Path, extracted, *, tables: Optional[bool] = None,
                 render: Optional[Callable] = None) -> tuple[list[dict], dict]:
    """extract() 결과 → 페이지 dict 목록. 표 파이프라인이 켜져 있으면 표 페이지를 전사한다.

    render: 분류용 렌더 함수 (테스트에서 바꿔 끼움). 기본 render_for_score.
    """
    render = render or render_for_score
    from app.extractors import table_transcriber as tt
    c = tt.config()
    run_tables = c.get("enabled") if tables is None else tables
    stats = {"pages": 0, "table_like": 0, "transcribed": 0, "needs_review": 0, "vlm_failed": 0}
    pages: list[dict] = []
    from app.progress_fmt import progress_line
    n_pages = len(extracted.pages)
    every = max(1, n_pages // 20)
    cache_path = _score_cache_path(Path(pdf_path), int(c["score_dpi"])) if run_tables else None
    scores = _load_scores(cache_path) if cache_path else {}
    dirty = False
    for p in extracted.pages:
        stats["pages"] += 1
        if run_tables and n_pages > 20 and (stats["pages"] % every == 0 or stats["pages"] == n_pages):
            logger.info(progress_line("표", stats["pages"], n_pages, Path(pdf_path).name))
        rec = {"page": p.page_index + 1, "text": p.text or "", "source": getattr(p, "source", "ocr"),
               "confidence": getattr(p, "confidence", None)}
        if run_tables:
            sc = scores.get(p.page_index)
            img = None
            if sc is None:
                img = render(pdf_path, p.page_index, int(c["score_dpi"]))
                if img is not None:
                    try:
                        sc = tt.table_score(img, lang=c["lang"])
                    except Exception as e:      # noqa: BLE001
                        sc = {"score": 0.0, "multiword_lines": 0}
                        logger.warning("table_score 실패 %s p%s: %s", pdf_path.name, p.page_index + 1, e)
                    scores[p.page_index] = sc; dirty = True
            if sc is not None:
                png = None
                rec["table_score"] = round(sc["score"], 3)
                if sc["multiword_lines"] >= int(c["min_multiword_lines"]) and sc["score"] >= float(c["min_table_score"]):
                    stats["table_like"] += 1
                    from app.extractors.vision_verifier import render_page_png
                    png = render_page_png(pdf_path, p.page_index)
                    if img is None:
                        img = render(pdf_path, p.page_index, int(c["score_dpi"]))
                    res = tt.process_page(img, png, list(getattr(p, "ocr_variants", None) or []),
                                          doc_hint=pdf_path.stem, page_no=p.page_index + 1,
                                          ocr_text=p.text or "", score=sc)
                    if res.get("markdown"):
                        stats["transcribed"] += 1
                        tail = (p.text or "")[: int(c["ocr_tail_chars"])] if c.get("keep_ocr_text") else ""
                        rec["text"] = res["markdown"] + (("\n\n[OCR]\n" + tail) if tail else "")
                        rec["source"] = "vlm_table"
                        rec["confidence"] = res["confidence"]
                        rec["needs_review"] = bool(res["needs_review"])
                        rec["table_model"] = res.get("model")
                        if res["needs_review"]:
                            stats["needs_review"] += 1
                        logger.info("표 전사 %s p%s conf=%.2f %s", pdf_path.name, p.page_index + 1,
                                    res["confidence"] or 0.0, res.get("note", ""))
                    else:
                        if "VLM 실패" in (res.get("note") or ""):
                            stats["vlm_failed"] += 1
                        logger.info("표 페이지지만 전사 없음 %s p%s: %s", pdf_path.name, p.page_index + 1, res.get("note"))
        pages.append(rec)
    if cache_path and dirty:
        _save_scores(cache_path, scores)
    return pages, stats


