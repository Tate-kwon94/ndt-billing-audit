"""PDF vector graphics → isometric routing 직접 추출.

근거 (1ULD p.21 PoC):
  - pdfplumber.lines 의 도면 영역 line 들이 isometric 30°/90° 컨벤션 정확히 따름
  - spec 표 spool 길이 ↔ vector line 길이 매칭이 ±5% 안 정확
  - 6641mm spool → 598px @ 30° (오차 0%)

text 기반 추론보다 압도적으로 정확. v0.3 의 핵심 모듈.

알고리즘:
  1. 도면 영역 line 식별 (frame/title 제외)
  2. ISO 표준 각도 (30°/90°/150°/-30°) 로 클러스터링
  3. spec 표 spool 길이로 px→mm scale 자동 추정
  4. Greedy 매칭 — 각 spool 을 가장 가까운 미사용 line 에 할당
  5. line 방향 (정확한 각도 + 위치) → routing 방향 결정
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber

logger = logging.getLogger(__name__)


# ─────────────────────────── 상수 ───────────────────────────


# ISO drawing 표준 각도 (modulo 180°)
# 노형 공급사/일반: 30° (X), 90° (Z), 150° (Y, = -30° = 180-30)
ISO_AXES_DEG = {
    "X+": 30.0,    # 등각 우상
    "X-": 210.0 % 180,  # 등각 좌하 = 30°
    "Y+": 150.0,   # 등각 좌상
    "Y-": 330.0 % 180,  # 등각 우하 = 150°
    "Z+": 90.0,    # 수직 위
    "Z-": 90.0,    # 수직 아래
}

ANGLE_TOLERANCE_DEG = 5.0
MIN_LINE_LENGTH_PX = 30.0      # 이보다 짧으면 symbol 부속 (spool 후보 X)
MAX_FRAME_LINE_PX = 500.0      # 이보다 긴 horizontal/vertical = 도면 외곽 frame
MATCH_ERROR_PCT = 25.0         # spool length 매칭 허용 오차


# ─────────────────────────── 데이터 ───────────────────────────


@dataclass
class VectorLine:
    """도면 안 한 line segment."""

    x0: float
    y0: float
    x1: float
    y1: float
    length_px: float
    angle_deg: float  # 0~180 modulo
    axis: str         # "X", "Y", "Z", "other"
    sign_hint: int    # +1 / -1 / 0 (좌표상 양/음 추정)


@dataclass
class VectorMatch:
    """spool ↔ vector line 매칭."""

    spool_idx: int
    line: VectorLine
    predicted_mm: float
    actual_mm: float
    error_pct: float
    axis: str        # 'x' / 'y' / 'z'
    sign: int


@dataclass
class SheetVectorAnalysis:
    """1 시트의 vector 분석 결과."""

    page: int
    total_lines: int
    iso_lines: list[VectorLine] = field(default_factory=list)
    scale_mm_per_px: Optional[float] = None
    matches: list[VectorMatch] = field(default_factory=list)
    unmatched_spools: list[int] = field(default_factory=list)


# ─────────────────────────── 분석 ───────────────────────────


def _line_info(line: dict) -> tuple[float, float]:
    dx = line["x1"] - line["x0"]
    dy = line["y1"] - line["y0"]
    L = math.hypot(dx, dy)
    ang = math.degrees(math.atan2(dy, dx)) % 180
    return L, ang


def _classify_axis(angle: float) -> tuple[str, str]:
    """각도 → ISO 축 분류.

    Returns: (axis_label, axis_xyz) where axis_xyz ∈ {'x', 'y', 'z', 'other'}
    """
    a = angle % 180
    if abs(a - 30) < ANGLE_TOLERANCE_DEG:
        return ("30°", "x")  # ISO X 또는 Y 의 한 방향
    if abs(a - 150) < ANGLE_TOLERANCE_DEG:
        return ("150°", "y")  # ISO 다른 등각 방향
    if abs(a - 90) < ANGLE_TOLERANCE_DEG:
        return ("90°", "z")
    if abs(a - 60) < ANGLE_TOLERANCE_DEG:
        return ("60°", "y")  # alt Y (도면 회전 시)
    if a < ANGLE_TOLERANCE_DEG or abs(a - 180) < ANGLE_TOLERANCE_DEG:
        return ("0°", "other")  # frame 가능성 큼
    return (f"{a:.0f}°", "other")


def extract_iso_lines(pdf_path: Path, page_idx: int) -> list[VectorLine]:
    """pdfplumber 로 isometric 후보 line 만 추출."""
    iso: list[VectorLine] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        if page_idx >= len(pdf.pages):
            return iso
        page = pdf.pages[page_idx]
        for ln in page.lines:
            L, ang = _line_info(ln)
            if L < MIN_LINE_LENGTH_PX:
                continue
            axis_label, axis_xyz = _classify_axis(ang)
            # 도면 외곽 frame 제외 (긴 0°/90° line)
            if L > MAX_FRAME_LINE_PX and axis_xyz in ("z", "other"):
                continue
            # sign hint — dx/dy 부호
            dx = ln["x1"] - ln["x0"]
            dy = ln["y1"] - ln["y0"]
            sign = 1 if (dx + dy) >= 0 else -1
            iso.append(VectorLine(
                x0=ln["x0"], y0=ln["y0"], x1=ln["x1"], y1=ln["y1"],
                length_px=L, angle_deg=ang, axis=axis_xyz,
                sign_hint=sign,
            ))
    iso.sort(key=lambda v: -v.length_px)
    return iso


def estimate_scale(iso_lines: list[VectorLine], spool_lengths_mm: list[float]) -> Optional[float]:
    """px → mm scale 자동 추정 — top N 후보 × line consumption greedy.

    근거 (단일 최장 매칭의 frame 오인 극복):
      1. 가장 긴 spool 의 매칭 후보를 top 5 line 으로 다양화
      2. 각 후보 scale 로 나머지 spool 들을 greedy 매칭 (line 1번씩만 사용)
      3. 매칭 성공 개수 + 평균 오차로 점수
      4. 최고 점수 scale 채택

    단일 매칭의 fragility 와 다중 점수의 짧은 line 선호 둘 다 회피.
    """
    if not iso_lines or not spool_lengths_mm:
        return None

    sorted_spools = sorted(spool_lengths_mm, reverse=True)
    sorted_lines = sorted(iso_lines, key=lambda v: -v.length_px)
    if not sorted_lines or sorted_lines[0].length_px < 10:
        return None

    longest_spool = sorted_spools[0]
    # 가장 긴 spool 의 매칭 후보 — top 5 line
    candidates = [
        (longest_spool / line.length_px, line)
        for line in sorted_lines[:5]
        if line.length_px > 10
    ]
    # 0.1 ~ 100 mm/px 범위만
    candidates = [(s, l) for s, l in candidates if 0.1 <= s <= 100]
    if not candidates:
        return None

    best_scale = None
    best_score = (-1, float("inf"))  # (matched_count, avg_err) — 큰 매칭 + 작은 오차 우선

    for scale, anchor_line in candidates:
        used_lines: set[int] = {sorted_lines.index(anchor_line)}
        matched = 1  # 가장 긴 spool 매칭됨 (anchor)
        total_err = abs(anchor_line.length_px * scale - longest_spool) / longest_spool

        for sp in sorted_spools[1:]:
            best_err = float("inf")
            best_idx = None
            for i, vl in enumerate(sorted_lines):
                if i in used_lines:
                    continue
                err = abs(vl.length_px * scale - sp) / sp
                if err < best_err:
                    best_err = err
                    best_idx = i
            if best_err < 0.25 and best_idx is not None:
                used_lines.add(best_idx)
                matched += 1
                total_err += best_err

        avg_err = total_err / matched if matched else float("inf")
        score = (matched, -avg_err)  # 매칭 많을수록·오차 적을수록 큰 점수
        if score > (best_score[0], -best_score[1]):
            best_score = (matched, avg_err)
            best_scale = scale

    if best_scale is not None:
        logger.info(
            "scale=%.2f mm/px, matched %d/%d spools, avg err %.1f%%",
            best_scale, best_score[0], len(sorted_spools), best_score[1] * 100,
        )
    return best_scale


def match_spools_to_lines(
    iso_lines: list[VectorLine],
    spool_lengths_mm: list[float],
    scale: float,
) -> tuple[list[VectorMatch], list[int]]:
    """spool 들을 line 들과 greedy 매칭.

    Returns: (matches, unmatched_spool_indices)
    """
    matches: list[VectorMatch] = []
    unmatched: list[int] = []
    used_lines: set[int] = set()

    # 긴 spool 부터 매칭 (가장 확실)
    spool_order = sorted(range(len(spool_lengths_mm)),
                         key=lambda i: -spool_lengths_mm[i])
    for si in spool_order:
        target_mm = spool_lengths_mm[si]
        best_idx = None
        best_err = float("inf")
        for li, vline in enumerate(iso_lines):
            if li in used_lines:
                continue
            pred_mm = vline.length_px * scale
            err = abs(pred_mm - target_mm) / target_mm
            if err < best_err:
                best_err = err
                best_idx = li
        if best_idx is not None and best_err * 100 < MATCH_ERROR_PCT:
            vline = iso_lines[best_idx]
            used_lines.add(best_idx)
            matches.append(VectorMatch(
                spool_idx=si,
                line=vline,
                predicted_mm=vline.length_px * scale,
                actual_mm=target_mm,
                error_pct=best_err * 100,
                axis=vline.axis,
                sign=vline.sign_hint,
            ))
        else:
            unmatched.append(si)

    # spool 순서대로 정렬
    matches.sort(key=lambda m: m.spool_idx)
    return matches, unmatched


def analyze_sheet(
    pdf_path: Path, page_idx: int, spool_lengths_mm: list[float],
) -> SheetVectorAnalysis:
    """1 시트의 vector 분석 — 외부 API.

    Returns 매칭된 spool 들의 line/축/방향 정보.
    """
    result = SheetVectorAnalysis(page=page_idx + 1, total_lines=0)
    iso_lines = extract_iso_lines(pdf_path, page_idx)
    result.iso_lines = iso_lines
    result.total_lines = len(iso_lines)

    if not spool_lengths_mm or not iso_lines:
        result.unmatched_spools = list(range(len(spool_lengths_mm)))
        return result

    scale = estimate_scale(iso_lines, spool_lengths_mm)
    result.scale_mm_per_px = scale
    if scale is None:
        result.unmatched_spools = list(range(len(spool_lengths_mm)))
        return result

    matches, unmatched = match_spools_to_lines(iso_lines, spool_lengths_mm, scale)
    result.matches = matches
    result.unmatched_spools = unmatched
    return result
