"""문서번호 다섯째 마디의 KKS 토큰 — 고정폭 12자, 앞 6자 건물, 뒤 6자 계통, 빈자리 '&'.

    NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E   →  건물 0UMA,   계통 LAH
    NP.D.P000.1.0UGB95GML90&.052.DC.0001.E   →  건물 0UGB95, 계통 GML90

SCWEP 표지에는 발주자 공종 코드(CP-M1)가 없다 — 그것은 발주처·시공사 계약 세부내역 항목이다
(2026-09-05 사용자 확정). 시공사 문서와 도면이 공유하는 범위 키는 이 마디뿐이고 양식은 통일이다.
대조는 문자 3자리(building_code, system_code)로 한다. 선행 숫자(유닛 표기)와 후행 번호는 뺀다.
"""
from __future__ import annotations

import re
from typing import Optional

_CODE = re.compile(r"^\d?([A-Z]{3})")


def _split(tok: str) -> Optional[tuple]:
    tok = tok.rstrip("&")
    if not tok:
        return None
    m = _CODE.match(tok)
    return (tok, m.group(1)) if m else None


def parse_segment(raw: Optional[str], *, unit: Optional[str] = None) -> Optional[dict]:
    """다섯째 마디(12자) 하나만 받는다 — LLM 이 applicable_scope.kks.raw 로 돌려준 값용."""
    if not raw:
        return None
    raw = str(raw).strip().upper()
    if len(raw) != 12:
        return None
    b = _split(raw[:6])
    s = _split(raw[6:])
    if b is None or s is None:
        return None
    return {"unit": unit, "building": b[0], "building_code": b[1],
            "system": s[0], "system_code": s[1], "raw": raw}


def parse(no: Optional[str]) -> Optional[dict]:
    """점으로 나뉜 전체 문서번호·도면번호를 받는다."""
    if not no:
        return None
    parts = str(no).strip().upper().split(".")
    if len(parts) < 5:
        return None
    return parse_segment(parts[4], unit=parts[3] if parts[3].isdigit() else None)


def same_scope(a: Optional[dict], b: Optional[dict]) -> str:
    if not a or not b:
        return "unknown"
    return "yes" if (a["building_code"], a["system_code"]) == (b["building_code"], b["system_code"]) else "no"
