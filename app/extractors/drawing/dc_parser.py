"""DC 도면 파일을 HCX-007 로 구조화 추출.

추출 전략 (정확도 우선):
1. pdfplumber.extract_tables 로 표 직접 추출 — '검사 요구사항 매트릭스'·'Safety Class 매트릭스' 자동 식별
2. 표 + 본문 텍스트를 LLM 에 함께 전달 → LLM 은 정규화·인용만 수행 (환각 방지)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import pdfplumber

from app.extractors.pdf_extractor import extract
from app.extractors.table_pages import page_records
from app.hcx_client import call

logger = logging.getLogger(__name__)


# 검사 매트릭스 헤더 키워드 — 매칭 시 inspection_scope 표로 분류
_INSPECTION_TABLE_KEYWORDS = {
    "scope of inspection", "visual inspection", "radiographic", "ultrasonic",
    "liquid penetrant", "magnetic particle", "joint category", "ndt",
}
# Safety class 표 헤더 키워드
_SAFETY_TABLE_KEYWORDS = {
    "safety class", "seismic", "kks code", "operating medium",
    "np-001-15", "np-031-01", "classification designation",
}


def parse_dc(file_path: Path, *, drawing_no: str, revision: Optional[str]) -> dict | None:
    extracted = extract(file_path)
    # 표 페이지는 HCX-005 로 전사해 tesseract 글자를 대체한다 (규격 ③ 과 같은 파이프라인).
    # 2026-09-07 사내: 스캔 DC 는 pdfplumber 표 0건 + tesseract 글자뿐이라 적용 코드가 3~4개만 나왔다.
    records, table_stats = page_records(file_path, extracted)
    text_full = _join_records(records)
    pre_extracted = _extract_priority_tables(file_path)
    logger.info("표 합계 %s: 표페이지 %d · 전사 %d · 재확인 %d · vlm 실패 %d", file_path.name,
                table_stats.get("table_like", 0), table_stats.get("transcribed", 0),
                table_stats.get("needs_review", 0), table_stats.get("vlm_failed", 0))

    resp = call(
        "drawing_dc",
        {
            "drawing_no": drawing_no,
            "revision": revision,
            "text_full": text_full,
            "language": "english",
            "pre_extracted_tables": pre_extracted,
        },
    )
    parsed = resp.parsed
    if isinstance(parsed, dict):
        # 근거 검증 — 원문에 없는 검사율은 비운다. 2026-09-07 사내: 표를 못 읽은 상태에서 vt/pt/rt/ut 가
        # 전부 100 으로 들어왔다 (실제는 VT·UT 만 100). 근거 없는 값은 틀린 값보다 위험하다.
        from app.analyzers.extraction_guard import verify_matrix
        page_texts = {r["page"]: r.get("text") or "" for r in records}
        for key in ("inspection_matrix", "kks_lines"):
            if parsed.get(key):
                parsed[key], warns = verify_matrix(parsed[key], page_texts)
                if warns:
                    parsed["needs_review"] = True
                    parsed["review_reasons"] = list(parsed.get("review_reasons") or []) + warns
        # 검토자가 show-drawing 으로 페이지별 판독 경로를 볼 수 있게 진단을 결과에 싣는다 (DB extracted_json 에 저장됨).
        parsed["_pages"] = [{k: r.get(k) for k in ("page", "source", "confidence", "table_score",
                                                    "needs_review", "table_model") if k in r}
                            for r in records]
        parsed["_table_stats"] = table_stats
        flagged = [r["page"] for r in records if r.get("needs_review")]
        if flagged:
            parsed["needs_review"] = True
            reasons = list(parsed.get("review_reasons") or [])
            reasons.append("표 전사 재확인 " + ", ".join(f"p.{n}" for n in flagged)
                           + " — 전사 숫자와 OCR 숫자 일치율 낮음")
            parsed["review_reasons"] = reasons
    return parsed


def _join_pages(pages) -> str:
    """SD/BG 파서가 쓰는 원래 함수 (구매 도면 — 표 전사 없이 글자만). 호출자: bg_parser, sd_parser."""
    return "\n\n".join(f"---PAGE {p.page_index + 1}---\n{p.text}" for p in pages)


def _join_records(records) -> str:
    return "\n\n".join(f"---PAGE {r['page']}---\n{r.get('text') or ''}" for r in records)


def _extract_priority_tables(file_path: Path) -> dict:
    """pdfplumber 의 extract_tables 로 도면의 표를 직접 추출.

    헤더 키워드로 두 종류 식별:
      - inspection_scope_tables: NDT 방법별 샘플링률 매트릭스
      - safety_class_tables: KKS code × Safety class·Seismic 매트릭스
      - other_tables: 위 외 (자재 명세·참조 문서 등) — 부속 정보로 전달
    """
    result = {"inspection_scope_tables": [], "safety_class_tables": [], "other_tables": []}
    try:
        with pdfplumber.open(file_path) as pdf:
            for page_idx, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table or len(table) < 2:
                        continue
                    flat = " ".join(str(c or "") for row in table for c in row).lower()
                    is_inspection = sum(1 for kw in _INSPECTION_TABLE_KEYWORDS if kw in flat) >= 2
                    is_safety = sum(1 for kw in _SAFETY_TABLE_KEYWORDS if kw in flat) >= 2
                    entry = {
                        "page": page_idx,
                        "table_index": t_idx,
                        "rows": [[_clean_cell(c) for c in row] for row in table],
                    }
                    if is_inspection:
                        result["inspection_scope_tables"].append(entry)
                    elif is_safety:
                        result["safety_class_tables"].append(entry)
                    else:
                        # 너무 많은 노이즈 방지 — other 는 비교적 작은 표만
                        if len(table) <= 25:
                            result["other_tables"].append(entry)
    except Exception as e:
        logger.warning("표 추출 실패 (%s): %s", file_path, e)

    logger.info(
        "Pre-extracted tables for %s: inspection=%d, safety=%d, other=%d",
        file_path.name,
        len(result["inspection_scope_tables"]),
        len(result["safety_class_tables"]),
        len(result["other_tables"]),
    )
    return result


def _clean_cell(c) -> str:
    if c is None:
        return ""
    return re.sub(r"\s+", " ", str(c)).strip()
