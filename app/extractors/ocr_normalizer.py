"""성적서 OCR 결과를 표준 스키마 dict 로 정규화 (HCX-007).

`ocr_normalize.md` 프롬프트 사용. 페이지별 OCR variants 가 있으면 함께 전달해
LLM 이 교차 검증하도록 한다.
"""
from __future__ import annotations

from typing import Any

from app.hcx_client import call


def normalize_report(
    *,
    report_id: str,
    billing_round_meta: dict,
    pages_text: list[str],
    ocr_variants_per_page: list[list[str]] | None = None,
) -> dict | None:
    """HCX 정규화 결과 dict 반환. 실패 시 None."""
    payload: dict[str, Any] = {
        "report_id": report_id,
        "billing_round": billing_round_meta,
        "pages": [
            {"page_index": i, "text": t} for i, t in enumerate(pages_text)
        ],
    }
    if ocr_variants_per_page:
        payload["ocr_variants"] = [
            {"page_index": i, "variants": v} for i, v in enumerate(ocr_variants_per_page)
        ]

    resp = call("ocr_normalize", payload)
    return resp.parsed
