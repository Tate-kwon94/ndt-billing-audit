"""DC PDF 페이지 → PNG 렌더 (3D ↔ 원본 비교용).

pypdfium2 로 isometric 시트 페이지만 추출. 100~120 dpi 가 적당
(브라우저에서 cards 안에 끼우기 좋은 사이즈).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium

logger = logging.getLogger(__name__)


def render_page_png(pdf_path: Path, page_index: int, dpi: int = 110,
                    max_dim: int = 1800) -> Optional[bytes]:
    """1 페이지를 PNG bytes 로 렌더.

    Args:
        pdf_path: DC PDF 경로
        page_index: 0-based
        dpi: 렌더 해상도 (높을수록 글씨 선명, 파일 큼)
        max_dim: 가로/세로 중 큰 쪽 픽셀 한계 (자동 축소)

    Returns:
        PNG bytes 또는 None (오류 시)
    """
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
        if page_index >= len(pdf):
            return None
        page = pdf[page_index]
        scale = dpi / 72.0
        # 너무 크면 축소
        w, h = page.get_size()
        if w * scale > max_dim or h * scale > max_dim:
            scale = max_dim / max(w, h)
        img = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.error("page render failed page=%s: %s", page_index, e)
        return None
