"""결정론적 routing 재구성 — 우선순위:

  1. **Vector graphics 매칭** (v0.3, 가장 정확) — pdfplumber.lines 에서 isometric line
     추출 → spool 길이 매칭. 1ULD PoC 94% 정확.
  2. **tie-in 좌표 delta** (v0.2) — 좌표 차이로 spool 방향 추론.
  3. **fallback cyclic** — 둘 다 실패 시 X/Y/Z 순환.

vision/LLM 불필요. 환각 없음. 검증 가능.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# 매칭 tolerance — spool 길이 대비
LENGTH_TOL_FRAC = 0.20  # ±20%
MIN_DELTA_MM = 50       # 이보다 작은 delta 는 정렬 오차로 무시


@dataclass
class SpoolPlacement:
    """1 spool 의 공간 배치 결정 결과."""

    spool_idx: int          # spools 리스트의 index
    axis: str               # 'x' / 'y' / 'z'
    sign: int               # +1 / -1
    length_mm: float        # 도면 length
    matched_delta_mm: Optional[float] = None  # 매칭된 tie-in delta (None=fallback)
    confidence: float = 1.0  # 0~1


def _collect_deltas(tie_ins: list[dict]) -> list[tuple[str, float, int, int]]:
    """연속 tie-in 사이 의미 있는 deltas 모두 수집.

    Returns:
        [(axis, signed_delta, from_idx, to_idx), ...]
    """
    deltas = []
    for i in range(len(tie_ins) - 1):
        a, b = tie_ins[i], tie_ins[i + 1]
        for key in ("x", "y", "z"):
            av, bv = a.get(key), b.get(key)
            if av is None or bv is None:
                continue
            d = bv - av
            if abs(d) >= MIN_DELTA_MM:
                deltas.append((key, float(d), i, i + 1))
    return deltas


def _try_vector_matching(
    sheet: dict, pdf_path: Optional[Path],
) -> tuple[Optional[list[SpoolPlacement]], dict]:
    """v0.3 vector graphics 매칭 시도. 실패 시 (None, info).

    Returns: (placements or None, info dict)
    """
    if not pdf_path or not pdf_path.exists():
        return None, {"reason": "no_pdf"}
    try:
        from ndt_3d.vector_analyzer import analyze_sheet
    except ImportError:
        return None, {"reason": "no_module"}

    page = sheet.get("page", 0)
    spools = sheet.get("spools", [])
    spool_lens = [sp.get("length_mm", 0) for sp in spools]
    if not spool_lens:
        return None, {"reason": "no_spools"}

    result = analyze_sheet(pdf_path, page - 1, spool_lens)
    if not result.matches:
        return None, {"reason": "no_matches", "iso_lines": result.total_lines}

    # vector match → SpoolPlacement
    placements: list[Optional[SpoolPlacement]] = [None] * len(spools)
    for vm in result.matches:
        # confidence = 1 - error_pct/100, 최소 0.5
        conf = max(0.5, 1.0 - vm.error_pct / 100)
        placements[vm.spool_idx] = SpoolPlacement(
            spool_idx=vm.spool_idx,
            axis=vm.axis if vm.axis in ("x", "y", "z") else "x",
            sign=vm.sign if vm.sign in (-1, 1) else 1,
            length_mm=vm.actual_mm,
            matched_delta_mm=vm.predicted_mm,
            confidence=conf,
        )

    info = {
        "method": "vector_graphics",
        "matched_count": len(result.matches),
        "unmatched": result.unmatched_spools,
        "scale_mm_per_px": result.scale_mm_per_px,
        "iso_lines": result.total_lines,
    }
    return placements, info


def reconstruct_routing(
    sheet: dict, pdf_path: Optional[Path] = None,
) -> tuple[list[SpoolPlacement], dict]:
    """sheet → spool placements + metadata.

    우선순위:
      1. Vector graphics 매칭 (pdf_path 주어지면) — v0.3
      2. tie-in delta 매칭 — v0.2
      3. fallback cyclic — v0.1
    """
    spools = sheet.get("spools", [])
    tie_ins = sheet.get("tie_ins", [])

    meta = {
        "tie_in_count": len(tie_ins),
        "spool_count": len(spools),
        "matched_count": 0,
        "fallback_count": 0,
        "manhattan_total_mm": 0.0,
        "spool_total_mm": sum(s.get("length_mm", 0) for s in spools),
        "method": "fallback_cyclic",
    }

    if not spools:
        return [], meta

    # 0. Vector graphics 우선 (v0.3)
    vec_placements, vec_info = _try_vector_matching(sheet, pdf_path)
    if vec_placements is not None and vec_info.get("matched_count", 0) > 0:
        meta["method"] = vec_info["method"]
        meta["vector_scale_mm_per_px"] = vec_info.get("scale_mm_per_px")
        meta["vector_iso_lines"] = vec_info.get("iso_lines", 0)
        # vector 매칭 못한 spool 은 tie-in delta 또는 fallback 으로 보강
        placements: list[SpoolPlacement] = []
        for i, sp in enumerate(spools):
            if vec_placements[i] is not None:
                placements.append(vec_placements[i])
                meta["matched_count"] += 1
            else:
                # 보조: cyclic fallback
                cyclic_axes = ["x", "y", "z"]
                placements.append(SpoolPlacement(
                    spool_idx=i, axis=cyclic_axes[i % 3], sign=1,
                    length_mm=float(sp["length_mm"]),
                    matched_delta_mm=None, confidence=0.3,
                ))
                meta["fallback_count"] += 1
        return placements, meta

    # v0.2 — tie-in delta 매칭 (vector 실패 시)
    placements = []
    if len(tie_ins) >= 2:
        meta["method"] = "deterministic_tie_in"
    else:
        # Fallback: tie-in 부족 시 단순 cyclic
        cyclic_axes = ["x", "y", "z"]
        for i, sp in enumerate(spools):
            placements.append(SpoolPlacement(
                spool_idx=i, axis=cyclic_axes[i % 3], sign=1,
                length_mm=sp["length_mm"], matched_delta_mm=None,
                confidence=0.3,
            ))
            meta["fallback_count"] += 1
        return placements, meta

    deltas = _collect_deltas(tie_ins)
    meta["manhattan_total_mm"] = sum(abs(d[1]) for d in deltas)

    # spool 매칭 — greedy
    available = list(deltas)  # mutable copy
    for i, sp in enumerate(spools):
        L = float(sp["length_mm"])
        # 가장 가까운 |Δ| 찾기
        best_idx = None
        best_diff = float("inf")
        for j, (axis, d, _fi, _ti) in enumerate(available):
            diff = abs(abs(d) - L)
            if diff < best_diff:
                best_diff = diff
                best_idx = j

        if best_idx is not None and best_diff < L * LENGTH_TOL_FRAC:
            axis, d, _fi, _ti = available.pop(best_idx)
            sign = 1 if d > 0 else -1
            conf = max(0.0, 1.0 - best_diff / L)
            placements.append(SpoolPlacement(
                spool_idx=i, axis=axis, sign=sign, length_mm=L,
                matched_delta_mm=d, confidence=conf,
            ))
            meta["matched_count"] += 1
        else:
            # fallback — cyclic
            cyclic_axes = ["x", "y", "z"]
            placements.append(SpoolPlacement(
                spool_idx=i, axis=cyclic_axes[i % 3], sign=1, length_mm=L,
                matched_delta_mm=None, confidence=0.3,
            ))
            meta["fallback_count"] += 1

    # 매칭 안 된 spool 이 절반 이상이면 method 표기 변경
    if meta["matched_count"] < len(spools) * 0.5:
        meta["method"] = "deterministic_partial"

    return placements, meta


# ─────────────────────────── helpers ───────────────────────────


_AXIS_VEC = {
    "x": np.array([1.0, 0.0, 0.0]),
    "y": np.array([0.0, 1.0, 0.0]),
    "z": np.array([0.0, 0.0, 1.0]),
}


def axis_vector(axis: str, sign: int = 1) -> np.ndarray:
    """축 이름 → 단위 벡터."""
    return _AXIS_VEC[axis] * sign
