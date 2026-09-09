"""PaddleOCR backend — PP-Structure 로 표 인식 강화.

Tesseract 대체 또는 보완. 환경변수 `NDT_OCR_BACKEND` 로 선택:
  - "tesseract" (기본, mac M-series 호환)
  - "paddle" (사내 Windows/Linux 권장 — PP-OCR + PP-Structure 표 인식 best)
  - "both"  (둘 다 호출 + diff compare)

Paddle 미설치 시 자동 폴백 (warning + Tesseract). mac M-series 는 paddle 공식
wheel 미지원이라 사실상 사내에서만 활성화.

설치 (사내 Windows):
  pip install paddleocr paddlepaddle
  # 모델 자동 다운로드 (첫 호출 시) 또는 paddleocr_home 디렉토리에 사전 배치
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class PaddleOCRResult:
    text: str                         # 페이지 전체 텍스트 (라인 join)
    lines: list[dict] = field(default_factory=list)   # [{text, bbox, confidence}, ...]
    tables: list[list[list[str]]] = field(default_factory=list)   # 표 셀 매트릭스
    mean_confidence: Optional[float] = None
    backend: str = "paddle"


# ─────────────────────────── Lazy import ───────────────────────────


_paddle_singleton = None


def _get_paddle():
    """Paddle OCR engine singleton (첫 호출 시 모델 로드)."""
    global _paddle_singleton
    if _paddle_singleton is not None:
        return _paddle_singleton

    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError:
        logger.warning(
            "paddleocr 미설치 — `pip install paddleocr paddlepaddle` (사내 Windows) 후 재시도. "
            "mac M-series 는 공식 wheel 미지원 — tesseract backend 사용."
        )
        raise

    # 영어 + 러시아어. PP-Structure 표 인식은 별도 호출 (테이블 모드)
    _paddle_singleton = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _paddle_singleton


def _get_paddle_table():
    """PP-Structure 표 인식 engine 별도 singleton."""
    try:
        from paddleocr import PPStructure  # type: ignore
    except ImportError:
        return None
    return PPStructure(show_log=False, table=True, ocr=True, layout=False)


# ─────────────────────────── OCR ───────────────────────────


def ocr_page_paddle(pil_image) -> PaddleOCRResult:
    """페이지 이미지 → PaddleOCR (텍스트 + 표).

    mac/Linux/Windows 동일 인터페이스. Paddle 미설치면 ImportError 발생 → 호출부가 폴백.
    """
    import numpy as np

    arr = np.array(pil_image.convert("RGB"))

    # 텍스트 OCR
    engine = _get_paddle()
    try:
        result = engine.ocr(arr, cls=True)
    except Exception as e:
        logger.warning("Paddle OCR 호출 실패: %s", e)
        return PaddleOCRResult(text="", lines=[], tables=[], mean_confidence=0.0)

    lines: list[dict] = []
    if result and result[0]:
        for item in result[0]:
            try:
                bbox = item[0]
                text_conf = item[1]
                text = text_conf[0]
                conf = float(text_conf[1])
                lines.append({"text": text, "bbox": bbox, "confidence": conf})
            except (IndexError, TypeError, ValueError):
                continue

    full_text = "\n".join(l["text"] for l in lines)
    mean_conf = sum(l["confidence"] for l in lines) / len(lines) if lines else 0.0

    # 표 인식 (PP-Structure) — 별도 호출
    tables: list[list[list[str]]] = []
    table_engine = _get_paddle_table()
    if table_engine is not None:
        try:
            structure_result = table_engine(arr)
            for region in structure_result:
                if region.get("type") == "table" and region.get("res"):
                    html_or_cells = region["res"]
                    # PP-Structure 결과는 cell 좌표·내용 list 또는 html 문자열
                    if isinstance(html_or_cells, dict) and "cells" in html_or_cells:
                        # cell-level structured output
                        cells_2d = _cells_to_matrix(html_or_cells["cells"])
                        tables.append(cells_2d)
                    elif isinstance(html_or_cells, dict) and "html" in html_or_cells:
                        # HTML → cell matrix parse
                        tables.append(_html_table_to_matrix(html_or_cells["html"]))
        except Exception as e:
            logger.warning("Paddle PP-Structure 표 인식 실패: %s", e)

    return PaddleOCRResult(
        text=full_text, lines=lines, tables=tables,
        mean_confidence=mean_conf, backend="paddle",
    )


def _cells_to_matrix(cells: list[dict]) -> list[list[str]]:
    """PP-Structure cell list → 2D matrix.

    cells = [{"row_idx": int, "col_idx": int, "text": str, ...}, ...]
    """
    if not cells:
        return []
    max_r = max((c.get("row_idx", 0) for c in cells), default=0) + 1
    max_c = max((c.get("col_idx", 0) for c in cells), default=0) + 1
    matrix = [["" for _ in range(max_c)] for _ in range(max_r)]
    for c in cells:
        r, col = c.get("row_idx", 0), c.get("col_idx", 0)
        if 0 <= r < max_r and 0 <= col < max_c:
            matrix[r][col] = c.get("text", "")
    return matrix


def _html_table_to_matrix(html: str) -> list[list[str]]:
    """HTML 표 → 2D matrix (간단 정규식 파서, BeautifulSoup 의존 회피)."""
    import re

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.IGNORECASE | re.DOTALL)
    matrix: list[list[str]] = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.IGNORECASE | re.DOTALL)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if clean:
            matrix.append(clean)
    return matrix


# ─────────────────────────── Diff compare ───────────────────────────


def diff_text_lines(tesseract_text: str, paddle_text: str) -> dict:
    """두 OCR 결과의 라인 비교. 일치율 + 다른 라인 목록 반환."""
    t_lines = [l.strip() for l in tesseract_text.splitlines() if l.strip()]
    p_lines = [l.strip() for l in paddle_text.splitlines() if l.strip()]

    common = set(t_lines) & set(p_lines)
    union = set(t_lines) | set(p_lines)

    return {
        "tesseract_only": sorted(set(t_lines) - common)[:50],
        "paddle_only": sorted(set(p_lines) - common)[:50],
        "common_lines": len(common),
        "total_unique_lines": len(union),
        "agreement_ratio": (len(common) / len(union)) if union else 1.0,
    }


def diff_tables(tesseract_tables: list, paddle_tables: list) -> dict:
    """표 비교 — Paddle 의 PP-Structure 결과를 1순위 신뢰. Tesseract 표는 보조."""
    return {
        "tesseract_table_count": len(tesseract_tables),
        "paddle_table_count": len(paddle_tables),
        "paddle_total_cells": sum(
            sum(1 for cell in row if cell.strip())
            for table in paddle_tables for row in table
        ),
        "tesseract_total_cells": sum(
            sum(1 for cell in (row or []) if cell and str(cell).strip())
            for table in (tesseract_tables or []) for row in table
        ) if tesseract_tables else 0,
    }
