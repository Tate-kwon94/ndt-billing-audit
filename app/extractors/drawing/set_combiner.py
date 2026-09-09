"""DC + SD + BG 3종 추출 결과를 HCX-007 로 종합해 통합 요구사항 생성.

상충/누락은 conflicts/missing_joints 에 기록 → 검토자가 확인 가능.
"""
from __future__ import annotations

from typing import Optional

from app.hcx_client import call


def combine(
    *,
    drawing_no: str,
    set_revision: Optional[str],
    dc_result: dict | None,
    sd_result: dict | None,
    bg_result: dict | None,
) -> dict | None:
    resp = call(
        "drawing_combine",
        {
            "drawing_no": drawing_no,
            "set_revision": set_revision,
            "dc_result": dc_result or {},
            "sd_result": sd_result or {},
            "bg_result": bg_result or {},
        },
    )
    return resp.parsed
