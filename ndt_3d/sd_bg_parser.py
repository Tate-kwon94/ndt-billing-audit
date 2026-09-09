"""SD (Standard Detail) + BG (Bills of Goods) → valve/component 풀 spec 추출.

SD 파일 안 "Specification for equipment and valves" 표에서:
  KKS → valve type (gate/check/ball/butterfly/globe)
       + DN
       + safety class (1N/2N/3N/4N)
       + model (AZ110-100, KSh-025-1.6 등)
       + mass (kg)
       + connection (flanged/welded/interflanged)
       + position (inside/outside containment)

DC 추출의 KKS 마커에 join 해서 3D 에 정확한 valve 모양·정보 표시.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import pdfplumber

logger = logging.getLogger(__name__)


# ─────────────────────────── 패턴 ───────────────────────────


# SD 표 1행 예시:
# "1 90GMM91AA001 Isolation gate valve AZ 110-100- DN 100; - 4N; cs pcs 1 49.46 49.46 91ULD;"
# "12 90GUD91AA501 Ball valve KSh-025-1.6- DN 25; TU 3712-005- 4N; cs pcs 1 1.8 1.8 91ULD;"
# "4 90GMM91AA601 Check valve AZ 460-100- DN 100; - 4N; cs pcs 1 17.8 17.8 91ULD;"

RE_VALVE_ROW = re.compile(
    r"\b(\d{1,3})\s+"                              # row number
    r"(\d{2}[A-Z]{2,3}\d{1,2}AA\d{3,4})\s+"        # KKS AA-code (valve)
    r"((?:Isolation\s+)?(?:gate|check|ball|globe|butterfly|isolation)\s+valve)\s+"  # valve type
    # model code — 공백 사이에 있을 수 있고 마지막에 -DN 또는 - 가 붙음
    # 예: "AZ 110-100-" → 모델 "AZ 110-100"
    #     "KSh-025-1.6-" → 모델 "KSh-025-1.6"
    #     "19s76nzh" → 모델 "19s76nzh"
    r"([A-Za-z0-9][A-Za-z0-9.\- ]{2,30}?)"
    r"[-;,\s]*"
    r"DN\s*(\d{1,4})",                              # DN
    re.IGNORECASE,
)

# safety class — 4N / 3N / 2N / 1N
RE_SAFETY_CLASS = re.compile(r"\b([1-4][N])\b")

# mass — 보통 "pcs 1 49.46 49.46" 패턴 (qty + unit_mass + total_mass)
# 단순화: "pcs\s+\d+\s+([\d.]+)\s+([\d.]+)" 으로 unit/total mass
RE_MASS = re.compile(r"pcs\s+\d+\s+([\d.]+)\s+([\d.]+)", re.IGNORECASE)


# ─────────────────────────── 데이터 ───────────────────────────


@dataclass
class ValveSpec:
    """SD 에서 추출한 valve 풀 spec."""

    kks: str
    valve_type: str         # "gate" / "check" / "ball" / "globe" / "butterfly"
    model: Optional[str] = None       # "AZ110-100", "KSh-025-1.6"
    dn_mm: Optional[int] = None       # 25/50/100/etc
    safety_class: Optional[str] = None  # "4N"/"3N"/"2N"/"1N"
    mass_kg: Optional[float] = None
    connection: Optional[str] = None  # "flanged" / "welded" / "interflanged"
    description: str = ""             # 원문 전체

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SDExtract:
    """SD PDF 1개 추출 결과."""

    source: str
    valves: dict[str, ValveSpec] = field(default_factory=dict)  # KKS → spec

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "valves": {k: v.to_dict() for k, v in self.valves.items()},
        }


# ─────────────────────────── 추출 ───────────────────────────


def _normalize_valve_type(raw: str) -> str:
    """원문 valve 타입 → 단순 카테고리."""
    low = raw.lower()
    if "check" in low:
        return "check"
    if "ball" in low:
        return "ball"
    if "globe" in low:
        return "globe"
    if "butterfly" in low:
        return "butterfly"
    if "gate" in low or "isolation" in low:
        return "gate"
    return "gate"  # 기본 fallback (가장 흔함)


def _detect_connection(text: str) -> Optional[str]:
    low = text.lower()
    if "interflang" in low:
        return "interflanged"
    if "flang" in low:
        return "flanged"
    if "welded" in low:
        return "welded"
    return None


def parse_sd(pdf_path: Path) -> SDExtract:
    """SD PDF 1개 파싱 → valves dict."""
    extract = SDExtract(source=str(pdf_path))
    if not pdf_path.exists():
        return extract
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for m in RE_VALVE_ROW.finditer(text):
                kks = m.group(2)
                if kks in extract.valves:
                    continue
                vtype_raw = m.group(3)
                model = m.group(4).rstrip("-;,")
                dn = int(m.group(5))
                # 같은 row 의 ±200자 context 에서 safety class / mass / connection
                start = max(0, m.start() - 50)
                end = min(len(text), m.end() + 500)
                ctx = text[start:end]
                sc_m = RE_SAFETY_CLASS.search(ctx)
                ms_m = RE_MASS.search(ctx)
                spec = ValveSpec(
                    kks=kks,
                    valve_type=_normalize_valve_type(vtype_raw),
                    model=model,
                    dn_mm=dn,
                    safety_class=sc_m.group(1) if sc_m else None,
                    mass_kg=float(ms_m.group(1)) if ms_m else None,
                    connection=_detect_connection(ctx),
                    description=ctx[:200].replace("\n", " ").strip(),
                )
                extract.valves[kks] = spec
    return extract


def merge_into_dc_extract(dc_extract_data: dict, sd_extract: SDExtract) -> dict:
    """DC 추출 결과의 components 에 SD valve spec join.

    각 sheet 의 components 리스트에서 type=valve 인 항목에 SD spec attach.
    """
    if not sd_extract.valves:
        return dc_extract_data
    for sheet in dc_extract_data.get("sheets", []):
        for comp in sheet.get("components", []):
            if comp.get("type") != "valve":
                continue
            kks = comp.get("kks")
            if kks in sd_extract.valves:
                spec = sd_extract.valves[kks].to_dict()
                comp["sd_spec"] = spec
                # type 도 SD 의 정확 valve_type 으로 교체 (gate/check/ball)
                comp["valve_subtype"] = spec["valve_type"]
    return dc_extract_data
