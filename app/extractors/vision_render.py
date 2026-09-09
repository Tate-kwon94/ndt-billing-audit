"""비전 호출용 페이지 렌더 — 크기 예산과 '이미지가 크다' 거부에 대한 재시도.

2026-09-08 사내 확정: HCX-005 는 PNG 1.41MB 를 `code=40063 "Invalid image size"` 로 거부한다.
같은 이유로 도면 표 전사가 34쪽 중 30쪽 실패했다 — 운영 경로가 막혀 있었다.
**한계값은 모른다.** 추측해 상한을 박는 대신 거부당하면 줄여서 다시 보내고 성공한 크기를 남긴다.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 'Invalid image size' / code=40063 만 크기 문제로 본다. 인증·파라미터 오류를 줄여 보내면 안 된다.
_SIZE_ERROR = re.compile(r"40063|invalid\s*image\s*size", re.I)


def render_png(pdf_path, page_index: int, *, scale: float = 2.0,
               max_bytes: Optional[int] = None, max_pixels: int = 50_000_000) -> bytes:
    """한 쪽을 PNG bytes 로. max_bytes 가 있으면 그 안에 들어올 때까지 축소한다."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_index]
        w, h = page.get_size()
        s = float(scale)
        if (w * s) * (h * s) > max_pixels:
            s = (max_pixels / (w * h)) ** 0.5
        for _ in range(8):
            buf = io.BytesIO()
            page.render(scale=s).to_pil().save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            if max_bytes is None or len(data) <= max_bytes or s <= 0.25:
                return data
            s *= 0.7
        return data
    finally:
        doc.close()


def shrink_png(png: bytes, max_bytes: Optional[int] = None) -> bytes:
    """이미 만든 PNG 를 예산에 맞게 축소한다 (원본 PDF 가 없을 때 — 운영 전사 경로)."""
    if max_bytes is None or len(png) <= max_bytes:
        return png
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(png))
    except Exception as e:      # noqa: BLE001 — 축소 실패가 호출을 막으면 안 된다. 원본으로 보내고 서버 오류를 본다.
        logger.warning("이미지 축소 실패 (%s) — 원본 %.2fMB 그대로 보냄", e, len(png) / 1e6)
        return png
    data = png
    for _ in range(8):
        img = img.resize((max(1, int(img.width * 0.7)), max(1, int(img.height * 0.7))))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or img.width < 200:
            break
    return data


def send_with_shrink(make_png: Callable[[Optional[int]], bytes], send: Callable[[bytes], object],
                     *, attempts: int = 4, start_bytes: Optional[int] = None):
    """보내고, '이미지가 크다' 로 거부당하면 예산을 반으로 줄여 다시 보낸다.

    make_png(max_bytes) 는 그 예산에 맞춘 PNG 를 돌려준다. 반환: (응답, 성공한 바이트 수).
    """
    budget = start_bytes
    last = 0
    for i in range(1, attempts + 1):
        png = make_png(budget)
        last = len(png)
        try:
            out = send(png)
        except Exception as e:      # noqa: BLE001
            if not _SIZE_ERROR.search(str(e)) or i == attempts:
                if _SIZE_ERROR.search(str(e)):
                    raise type(e)(
                        f"{e} — 이미지 크기 때문에 {attempts}회 줄여 보냈으나 모두 거부됨 "
                        f"(마지막 {last/1e6:.2f}MB). 더 줄여 보려면 --dpi 를 낮출 것") from None
                raise
            budget = (budget or last) // 2
            logger.warning("이미지 크기 거부 (%.2fMB) — %.2fMB 예산으로 다시 시도 (%d/%d)",
                           last / 1e6, budget / 1e6, i + 1, attempts)
            continue
        if i > 1:
            logger.warning("이미지 %.2fMB 로 성공 — 이 크기가 사내 상한의 실측값이다 (%d회째)", last / 1e6, i)
        return out, last
    raise RuntimeError("unreachable")
