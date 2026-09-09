"""Vision 검증 — HCX-005 로 도면 페이지 이미지 직접 확인.

트리거: Rule Engine high 위반 + HCX-007 텍스트 correction 도 needs_review.
동작: 해당 페이지 PDF 렌더 → HCX-005 호출 → 자연어 응답 → HCX-007 으로 정규화 chain.

기본 on. 끄려면 환경변수 NDT_VISION_VERIFY=0. (예전 default off 의 이유 '사내 인프라 부하' 는 사용자 확정으로 무효 — 2026-09-07)
"""
from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Optional

from app.hcx_client import call as hcx_call, call_vision

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.environ.get("NDT_VISION_VERIFY", "1") != "0"


def render_page_png(pdf_path: Path, page_index: int, max_pixels: int = 50_000_000) -> Optional[bytes]:
    """페이지를 PNG bytes 로 렌더링. HCX-005 전송 최적화 — 50MP 초과 시 다운스케일."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 미설치 — vision verify 불가")
        return None

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[page_index]
        w_pt, h_pt = page.get_size()
        scale = 2.0   # vision 은 200dpi 면 충분
        if (w_pt * scale) * (h_pt * scale) > max_pixels:
            scale = (max_pixels / (w_pt * h_pt)) ** 0.5
        pil = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    finally:
        pdf.close()


def verify_field(
    *,
    pdf_path: Path,
    page_index: int,
    broken_field: str,
    tesseract_value,
    expected_pattern: str,
    context_hint: str = "",
) -> dict:
    """vision 검증 1건.

    Returns:
        {
          "decision": "vision_corrected" | "ocr_was_correct" | "needs_review",
          "corrected_value": str | None,
          "evidence": str,
          "confidence": float,
        }
    """
    if not is_enabled():
        return {
            "decision": "needs_review",
            "corrected_value": None,
            "evidence": "vision verify 비활성 (NDT_VISION_VERIFY=1 필요)",
            "confidence": 0.0,
        }

    img_bytes = render_page_png(pdf_path, page_index)
    if not img_bytes:
        return {
            "decision": "needs_review",
            "corrected_value": None,
            "evidence": "페이지 렌더 실패",
            "confidence": 0.0,
        }

    payload = {
        "broken_field": broken_field,
        "tesseract_value": tesseract_value,
        "expected_pattern": expected_pattern,
        "context_hint": context_hint,
        "page": page_index + 1,
    }

    try:
        resp = call_vision("vision_table_verify", payload, [img_bytes])
    except Exception as e:
        logger.warning("Vision verify LLM 호출 실패: %s", e)
        return {
            "decision": "needs_review",
            "corrected_value": None,
            "evidence": f"vision LLM 호출 실패: {e}",
            "confidence": 0.0,
        }

    # HCX-005 응답 = 자연어. 간단 파싱:
    text = (resp.content or "").strip()
    corrected = _parse_corrected(text)
    confidence = _parse_confidence(text)
    evidence = _parse_evidence(text)

    if not corrected or corrected.lower() in ("needs_review", "확인 불가", "확인불가"):
        return {
            "decision": "needs_review",
            "corrected_value": None,
            "evidence": evidence or text[:200],
            "confidence": confidence,
        }

    # OCR 값이 정확하다고 vision 이 확인한 경우
    if corrected == str(tesseract_value).strip():
        return {
            "decision": "ocr_was_correct",
            "corrected_value": corrected,
            "evidence": evidence or "vision 시각 확인: OCR 결과 정확",
            "confidence": confidence,
        }

    # vision 이 다른 값 제시 — 보정
    return {
        "decision": "vision_corrected",
        "corrected_value": corrected,
        "evidence": evidence,
        "confidence": confidence,
    }


# ─────────────────────────── Helpers ───────────────────────────


_CORRECTED_RE = re.compile(r"검증\s*결과\s*[:\-]\s*(.+?)(?:\n|근거|신뢰도|$)", re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"근거\s*[:\-]\s*(.+?)(?:\n|신뢰도|$)", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"신뢰도\s*[:\-]\s*([\d\.]+)", re.IGNORECASE)


def _parse_corrected(text: str) -> Optional[str]:
    m = _CORRECTED_RE.search(text)
    if not m:
        return None
    v = m.group(1).strip().strip('"\'')
    return v or None


def _parse_evidence(text: str) -> str:
    m = _EVIDENCE_RE.search(text)
    return m.group(1).strip() if m else ""


def _parse_confidence(text: str) -> float:
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return 0.5
    try:
        return float(m.group(1))
    except ValueError:
        return 0.5
