"""OCR 깨진 값 → HCX-007 context correction.

Rule Engine 이 검증 실패한 필드 1개씩을 LLM 으로 복원 시도.
환각 방지: 후보(other variants, neighboring context, 단일 글자 fix) 안에서만 보정.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.hcx_client import call

logger = logging.getLogger(__name__)


def correct(
    *,
    broken_field: str,
    raw_value: Any,
    expected_pattern: str,
    rule_engine_error: dict,
    neighboring_context: str = "",
    other_ocr_variants: Optional[list[str]] = None,
) -> dict:
    """단일 필드 보정 시도.

    Returns:
        {
          "corrected_value": str | None,
          "decision": "auto_corrected" | "needs_review",
          "evidence": str,
          "candidate_source": str | None,
          "confidence": float,
        }
    """
    payload = {
        "broken_field": broken_field,
        "raw_value": raw_value,
        "expected_pattern": expected_pattern,
        "neighboring_context": neighboring_context[:1500],
        "other_ocr_variants": other_ocr_variants or [],
        "rule_engine_error": rule_engine_error,
    }
    try:
        resp = call("ocr_correct", payload)
    except Exception as e:
        logger.warning("OCR correction LLM 호출 실패: %s", e)
        return {
            "corrected_value": None,
            "decision": "needs_review",
            "evidence": f"LLM 호출 실패: {e}",
            "candidate_source": None,
            "confidence": 0.0,
        }

    parsed = resp.parsed
    if not parsed:
        return {
            "corrected_value": None,
            "decision": "needs_review",
            "evidence": "LLM 응답 JSON 파싱 실패",
            "candidate_source": None,
            "confidence": 0.0,
        }

    # 안전장치: corrected_value 가 너무 다르면 (길이 ±50% 초과) 거부
    corrected = parsed.get("corrected_value")
    if corrected and raw_value:
        rv = str(raw_value)
        cv = str(corrected)
        if rv and (len(cv) < len(rv) * 0.5 or len(cv) > len(rv) * 1.5):
            return {
                "corrected_value": None,
                "decision": "needs_review",
                "evidence": f"보정값 길이가 원본 대비 ±50% 초과 ({len(rv)}→{len(cv)}) — 의심",
                "candidate_source": parsed.get("candidate_source"),
                "confidence": 0.0,
            }

    # confidence 임계
    if float(parsed.get("confidence", 0)) < 0.8:
        parsed["decision"] = "needs_review"
        parsed["corrected_value"] = None

    return {
        "corrected_value": parsed.get("corrected_value"),
        "decision": parsed.get("decision", "needs_review"),
        "evidence": parsed.get("evidence", ""),
        "candidate_source": parsed.get("candidate_source"),
        "confidence": float(parsed.get("confidence", 0.0)),
    }


def correct_violations(
    item: dict,
    violations: list,
    neighboring_context: str = "",
    other_ocr_variants: Optional[list[str]] = None,
    *,
    vision_fallback: Optional[dict] = None,
) -> tuple[dict, list[dict]]:
    """다수 violation 일괄 보정.

    vision_fallback: {'pdf_path': Path, 'page_index': int, 'context_hint': str}
        주어지면 text correction needs_review 시 HCX-005 vision 호출 (NDT_VISION_VERIFY=1 필요)

    Returns:
        (수정된 item, correction log 목록)
    """
    from app.extractors import vision_verifier
    corrected_item = dict(item)
    log: list[dict] = []
    vision_on = vision_verifier.is_enabled() and vision_fallback is not None

    for v in violations:
        field = v.field if hasattr(v, "field") else v.get("field")
        raw = v.raw_value if hasattr(v, "raw_value") else v.get("raw_value")
        rule = v.rule if hasattr(v, "rule") else v.get("rule")
        expected = v.expected if hasattr(v, "expected") else v.get("expected")
        if not field:
            continue
        result = correct(
            broken_field=field,
            raw_value=raw,
            expected_pattern=expected,
            rule_engine_error={"rule": rule, "expected": expected},
            neighboring_context=neighboring_context,
            other_ocr_variants=other_ocr_variants,
        )

        # Vision fallback — text correction needs_review + vision_fallback 주어진 경우
        if (result["decision"] == "needs_review" and vision_on
                and vision_fallback.get("pdf_path") is not None):
            vresult = vision_verifier.verify_field(
                pdf_path=vision_fallback["pdf_path"],
                page_index=vision_fallback.get("page_index", 0),
                broken_field=field,
                tesseract_value=raw,
                expected_pattern=expected,
                context_hint=vision_fallback.get("context_hint", ""),
            )
            if vresult["decision"] == "vision_corrected" and vresult["corrected_value"]:
                result = {
                    "corrected_value": vresult["corrected_value"],
                    "decision": "auto_corrected",
                    "evidence": f"[vision] {vresult['evidence']}",
                    "candidate_source": "hcx_005_vision",
                    "confidence": vresult["confidence"],
                }
            elif vresult["decision"] == "ocr_was_correct":
                # OCR 가 사실 맞았던 경우 — vision 이 확정
                result = {
                    "corrected_value": str(raw),
                    "decision": "auto_corrected",
                    "evidence": f"[vision 확인] OCR 결과 정확. {vresult['evidence']}",
                    "candidate_source": "hcx_005_vision_confirmed",
                    "confidence": vresult["confidence"],
                }
            # else: vision 도 needs_review — 원 result 유지

        entry = {"field": field, "raw_value": raw, **result}
        log.append(entry)
        if result["decision"] == "auto_corrected" and result["corrected_value"]:
            if "." not in field and "[" not in field:
                corrected_item[field] = result["corrected_value"]
    return corrected_item, log
