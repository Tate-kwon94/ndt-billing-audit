"""Rule Engine validation tests."""
from __future__ import annotations

from app.analyzers.rule_engine import (
    validate_billing_item,
    validate_inspection_report,
    validate_drawing_extract,
    summarize,
)


def test_valid_billing_passes():
    item = {
        "report_no": "77-005PT",
        "ndt_method": "PT",
        "result": "ACC",
        "joint_no": "FW12",
        "drawing_no": "NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C02",
        "welder_id": "DW003 DW005 DW006",
        "inspection_date": "2024-02-19",
    }
    assert validate_billing_item(item) == []


def test_bad_ndt_method_caught():
    item = {"ndt_method": "UTC", "result": "ACC", "joint_no": "FW1"}
    errs = validate_billing_item(item)
    assert any(e.field == "ndt_method" and e.rule == "enum_violation" for e in errs)


def test_bad_date_caught():
    item = {"ndt_method": "RT", "result": "ACC", "joint_no": "FW1", "inspection_date": "32-13-2024"}
    errs = validate_billing_item(item)
    assert any(e.field == "inspection_date" for e in errs)


def test_bad_drawing_no_caught():
    item = {"ndt_method": "RT", "drawing_no": "GARBAGE-XYZ"}
    errs = validate_billing_item(item)
    assert any(e.field == "drawing_no" and e.rule == "regex_violation" for e in errs)


def test_drawing_extract_kks_safety():
    dwg = {
        "drawing_no": "NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E",
        "kks_lines": [
            {"kks_code": "90GMM91BR001", "safety_class_np_001_15": "4",
             "seismic_category_np_031_01": "III", "design_pressure_mpag": 0.17,
             "design_temperature_c": 60},
            {"kks_code": "BAD!CODE", "safety_class_np_001_15": "9",  # 잘못된 값 둘
             "seismic_category_np_031_01": "IV", "design_pressure_mpag": 1e6},
        ],
        "inspection_matrix": [
            {"kks_code": "90GMM91BR001", "vt_pct": 100, "rt_pct": 100, "ut_pct": 100},
        ],
    }
    errs = validate_drawing_extract(dwg)
    fields = {e.field for e in errs}
    assert any("safety_class" in f for f in fields)   # safety_class=9 catch
    assert any("seismic" in f for f in fields)        # IV catch
    assert any("design_pressure" in f for f in fields)  # 1e6 out of range


def test_summarize():
    from app.analyzers.rule_engine import ValidationError
    errs = [ValidationError("ndt_method", "X", "enum_violation", "...", "high"),
            ValidationError("welder_id", "?", "welder_token_pattern", "...", "low")]
    s = summarize(errs)
    assert s["count_high"] == 1
    assert s["count_low"] == 1
    assert s["needs_review"] is True
