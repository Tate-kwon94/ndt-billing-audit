"""도면 PNG 오버레이 — 3D 재구성 대신 도면 자체에 마킹.

근거 (사용자 피드백 v0.3.1):
  "도면과 3D 가 전혀 맞지 않음" — vector matching 정확도 94% 인데도
  3D 재구성 자체가 도면의 시각적 정보를 완전히 보존 못함.
  → 도면 PNG 그대로 + 추출 정보 마킹이 더 안전.

장점:
  - 도면 = 100% 정확 (재구성 X)
  - 시공자 친숙 (원본 도면 그대로)
  - "다르다" 문제 원천 차단
  - 청구 검토 정보 (메인 NDT Assistant) 와 직접 연결 가능

표시:
  - 각 spool: vector_analyzer 가 찾은 line 위에 두꺼운 색 선
  - 매칭 신뢰도별 색 (높음=초록 / 낮음=노랑)
  - spool 끝에 길이 라벨 (1114mm)
  - valve/Tee/Flange/Cap 의 KKS 라벨 (좌표 모름 → 도면 우측 박스에)
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from ndt_3d.page_renderer import render_page_png
from ndt_3d.vector_analyzer import analyze_sheet

logger = logging.getLogger(__name__)


# ─────────────────────────── 색상 (오버레이) ───────────────────────────

# pdf 좌표는 bottom-left origin, PIL 은 top-left → y flip 필요
COLOR_SPOOL_HI = (0, 180, 0, 200)    # 높은 신뢰도 매칭 — 초록
COLOR_SPOOL_LO = (220, 180, 0, 200)  # 낮은 신뢰도 — 황색
COLOR_SPOOL_NONE = (200, 100, 100, 180)  # 매칭 안 됨 — 빨강 점선
COLOR_LABEL_BG = (255, 255, 230, 230)
COLOR_LABEL_TEXT = (60, 30, 0, 255)

LINE_WIDTH = 5
LABEL_FONT_SIZE = 14


def _load_font(size: int = LABEL_FONT_SIZE):
    """플랫폼별 시스템 폰트 fallback."""
    candidates = [
        # mac
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        # Windows
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/arial.ttf",
        # Linux
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def overlay_sheet(
    pdf_path: Path,
    page_idx: int,
    sheet_data: dict,
    dpi: int = 150,
) -> Optional[bytes]:
    """1 시트 → 도면 PNG + 색 마킹.

    Args:
        pdf_path: DC PDF
        page_idx: 0-based
        sheet_data: extract JSON 의 sheet dict
        dpi: 렌더 해상도

    Returns:
        PNG bytes (마킹 포함). 실패 시 None.
    """
    # 1. 도면 PNG 렌더
    png_bytes = render_page_png(pdf_path, page_idx, dpi=dpi, max_dim=2400)
    if not png_bytes:
        return None
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    W, H = img.size

    # PDF page 의 native size (pt) 와 image size 의 scale factor 계산
    # pdfplumber 의 line 좌표는 pt 단위 (PDF 단위), PIL 은 px
    # scale = px / pt
    import pdfplumber
    with pdfplumber.open(str(pdf_path)) as pdf:
        pdf_page = pdf.pages[page_idx]
        page_w_pt, page_h_pt = pdf_page.width, pdf_page.height
    sx = W / page_w_pt
    sy = H / page_h_pt

    # 2. vector matching 다시 수행 (sheet_data 의 spool 길이로)
    spool_lens = [sp["length_mm"] for sp in sheet_data.get("spools", [])]
    vec_result = analyze_sheet(pdf_path, page_idx, spool_lens)

    # 3. 마킹 오버레이 (반투명 layer 위에)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font()
    font_small = _load_font(11)

    # 3a. 매칭된 spool 들 — vector line 위에 색 선
    matched_idx = {m.spool_idx for m in vec_result.matches}
    for match in vec_result.matches:
        vline = match.line
        # PDF → PIL 좌표 변환 (y flip)
        x0 = vline.x0 * sx
        y0 = H - vline.y0 * sy  # y flip
        x1 = vline.x1 * sx
        y1 = H - vline.y1 * sy
        # 색: 오차 작으면 진한 초록, 크면 황색
        color = COLOR_SPOOL_HI if match.error_pct < 10 else COLOR_SPOOL_LO
        draw.line([(x0, y0), (x1, y1)], fill=color, width=LINE_WIDTH)
        # 길이 라벨 (중간점)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        label = f"{int(match.actual_mm)}mm"
        try:
            bbox = draw.textbbox((mx, my), label, font=font_small)
            pad = 2
            draw.rectangle(
                (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
                fill=COLOR_LABEL_BG,
            )
            draw.text((mx, my), label, fill=COLOR_LABEL_TEXT, font=font_small, anchor="mm")
        except Exception:
            draw.text((mx, my), label, fill=COLOR_LABEL_TEXT, font=font_small)

    # 3b. 매칭 안 된 spool 들 — 도면 좌측 상단에 "재확인 필요" 박스
    unmatched_count = len(spool_lens) - len(matched_idx)
    if unmatched_count > 0:
        warn_text = f"⚠ spool {unmatched_count}개 매칭 실패 — 도면 직접 확인"
        bx0, by0 = 20, 20
        try:
            bbox = draw.textbbox((bx0, by0), warn_text, font=font)
            pad = 5
            draw.rectangle(
                (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
                fill=(255, 235, 235, 230),
            )
            draw.text((bx0, by0), warn_text, fill=(200, 50, 50, 255), font=font)
        except Exception:
            pass

    # 3c. 시트 메타 (좌측 상단)
    branch = sheet_data.get("branch_kks") or ""
    page = sheet_data.get("page", page_idx + 1)
    meta_text = f"p.{page} · {branch} · spool {len(spool_lens)} · 매칭 {len(matched_idx)}/{len(spool_lens)}"
    try:
        bbox = draw.textbbox((20, H - 30), meta_text, font=font)
        draw.rectangle((bbox[0] - 4, bbox[1] - 4, bbox[2] + 4, bbox[3] + 4),
                       fill=(255, 255, 255, 220))
        draw.text((20, H - 30), meta_text, fill=(40, 40, 40, 255), font=font)
    except Exception:
        pass

    # 4. 합성 + PNG 출력
    combined = Image.alpha_composite(img, overlay)
    out_buf = io.BytesIO()
    combined.convert("RGB").save(out_buf, format="PNG", optimize=True)
    return out_buf.getvalue()
