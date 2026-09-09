"""ndt_3d.router 단위 테스트 — 결정론 routing 검증."""
from __future__ import annotations

import pytest

from ndt_3d.router import reconstruct_routing, _collect_deltas


def test_simple_two_spool_two_delta():
    """p.25 케이스 — 2 spool 이 2 deltas 와 정확히 매칭."""
    sheet = {
        "spools": [
            {"od_mm": 57, "wall_mm": 4, "length_mm": 4150},
            {"od_mm": 57, "wall_mm": 4, "length_mm": 1525},
        ],
        "bends": [],
        "tie_ins": [
            {"x": -77075, "y": 132350, "z": -1836},
            {"x": -77150, "y": 132350, "z": -1836},
            {"x": -77150, "y": 128050, "z": -1827},
            {"x": -78750, "y": 128050, "z": -1827},
        ],
    }
    placements, meta = reconstruct_routing(sheet)
    # v0.3 부터 method 명 "deterministic_tie_in" (vector graphics 우선이지만 pdf 없으므로 tie-in)
    assert meta["method"] in ("deterministic", "deterministic_tie_in")
    assert meta["matched_count"] == 2
    assert meta["fallback_count"] == 0
    # spool 0 (4150) → Y- (delta -4300)
    assert placements[0].axis == "y" and placements[0].sign == -1
    assert placements[0].confidence > 0.9
    # spool 1 (1525) → X- (delta -1600)
    assert placements[1].axis == "x" and placements[1].sign == -1


def test_zero_tie_in_fallback():
    """tie-in 없으면 fallback cyclic."""
    sheet = {
        "spools": [
            {"od_mm": 108, "wall_mm": 6, "length_mm": 1114},
            {"od_mm": 108, "wall_mm": 6, "length_mm": 165},
        ],
        "bends": [],
        "tie_ins": [],
    }
    placements, meta = reconstruct_routing(sheet)
    assert meta["method"] == "fallback_cyclic"
    assert meta["fallback_count"] == 2
    assert meta["matched_count"] == 0
    assert all(p.confidence == 0.3 for p in placements)


def test_one_tie_in_fallback():
    """tie-in 1개도 deltas 계산 불가 → fallback."""
    sheet = {
        "spools": [{"od_mm": 57, "wall_mm": 4, "length_mm": 860}],
        "bends": [],
        "tie_ins": [{"x": -77050, "y": 127452, "z": -2340}],
    }
    placements, meta = reconstruct_routing(sheet)
    assert meta["method"] == "fallback_cyclic"


def test_no_spool_empty():
    """spool 0 → 빈 결과."""
    placements, meta = reconstruct_routing({"spools": [], "bends": [], "tie_ins": []})
    assert placements == []
    assert meta["spool_count"] == 0


def test_collect_deltas_ignores_small():
    """50mm 미만 delta 는 무시 (slope 등 정렬 오차)."""
    tie_ins = [
        {"x": 100, "y": 0, "z": 0},
        {"x": 110, "y": 5000, "z": 10},  # ΔX=10 (무시), ΔY=5000, ΔZ=10 (무시)
    ]
    deltas = _collect_deltas(tie_ins)
    assert len(deltas) == 1
    assert deltas[0][0] == "y"
    assert deltas[0][1] == 5000


def test_signed_direction():
    """delta 부호가 spool 방향에 반영."""
    sheet = {
        "spools": [{"od_mm": 100, "wall_mm": 5, "length_mm": 1000}],
        "bends": [],
        "tie_ins": [
            {"x": 0, "y": 0, "z": 0},
            {"x": 0, "y": -1000, "z": 0},  # Y- 1000mm
        ],
    }
    placements, _ = reconstruct_routing(sheet)
    assert placements[0].axis == "y"
    assert placements[0].sign == -1
