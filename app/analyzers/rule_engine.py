"""Rule Engine — deterministic 검증.

OCR/LLM 추출 결과의 명백한 오류 (enum 밖 값, 형식 위반, 범위 초과) 를
규칙 기반으로 검출. False positive 0, false negative 가능.

검증 대상:
  - billing_item (청구 엑셀 행)
  - inspection_report (성적서 정규화 결과)
  - drawing_extract (도면 추출 결과)

호출부 사용:
    from app.analyzers.rule_engine import validate_billing_item
    errors = validate_billing_item(item_dict)
    if errors:
        # needs_review = True, finding 'rule_engine_violation' 추가
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


# ─────────────────────────── ValidationError ───────────────────────────


@dataclass
class ValidationError:
    field: str
    raw_value: Any
    rule: str
    expected: str
    severity: str = "high"   # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "raw_value": self.raw_value,
            "rule": self.rule,
            "expected": self.expected,
            "severity": self.severity,
        }


# ─────────────────────────── Enum 사전 ───────────────────────────


NDT_METHODS = {"VT", "PT", "MT", "RT", "UT", "VMC"}  # VMC 는 VT 와 정규화 동치이나 enum 단계에선 둘 다 허용
RESULTS = {"ACC", "REJ", "PENDING", "A", "R", "P"}
SAFETY_CLASSES = {"1", "2", "3", "4", "1N", "2N", "3N", "4N"}  # 4N 같은 변형도 허용 (실데이터)
SEISMIC_CATEGORIES = {"I", "II", "III"}
QA_CATEGORIES = {"QNC", "QC1", "QC2", "QC3"}    # QNC 외 변형 데이터 확장 시 추가


# ─────────────────────────── 정규식 ───────────────────────────


# the plant 도면번호 (DC/SD/BG/KE 포함)
#   예: NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C02
#   분해: NP.D.{prefix}.{unit}.{KKS_masked}.{phase}.{TYPE}.{seq}.{lang}[_rev]
DRAWING_NO_RE = re.compile(
    r"^NP\.[A-Z]\.[A-Z0-9]+\.\d+\.[A-Z0-9&]+\.\d+\.(DC|SD|BG|KE)\.\d+\.[A-Z](?:_C\d+)?$"
)
# CRST 성적서번호 (예: 77-005PT, 77-013VMC)
REPORT_NO_RE = re.compile(r"^\d{1,3}-\d{1,4}\s?(VT|PT|MT|RT|UT|VMC)$", re.IGNORECASE)
# KKS code (the plant 표기 — 사용자 도메인 검증 시 보강)
KKS_CODE_RE = re.compile(r"^\d{2}[A-Z]{2,4}\d{0,3}[A-Z0-9]{0,6}$")
# Joint No (FW 류, 자유 형식이지만 영문자+숫자 위주)
JOINT_NO_RE = re.compile(r"^[A-Z]{1,4}\d{1,5}[A-Z]?$", re.IGNORECASE)
# Welder ID (DW001 등 영문자+숫자, 공백 구분 다수 가능)
WELDER_ID_TOKEN_RE = re.compile(r"^[A-Z]{1,4}\d{1,4}$", re.IGNORECASE)
# WPS / Welding formular (NP.008.CCW... 형식)
WPS_RE = re.compile(r"^NP\.\d{3}\.[A-Z]+(?:\.[A-Z0-9]+)+$", re.IGNORECASE)


# ─────────────────────────── 숫자 범위 ───────────────────────────


# 합리적 범위 — 도메인 적합성. 범위 밖이면 needs_review 표시.
RANGE_PRESSURE_MPAG = (0.0, 50.0)          # 설계압 0~50 MPag (1차계통도 18MPa 수준)
RANGE_TEMPERATURE_C = (-50.0, 800.0)
RANGE_THICKNESS_MM = (0.1, 500.0)
RANGE_SAMPLING_PCT = (0.0, 100.0)
RANGE_PIPE_DOUT_MM = (1.0, 5000.0)


# ─────────────────────────── 헬퍼 ───────────────────────────


def _is_str(v) -> bool:
    return v is not None and isinstance(v, str) and v.strip() != ""


def _err(field: str, raw: Any, rule: str, expected: str, severity: str = "high") -> ValidationError:
    return ValidationError(field=field, raw_value=raw, rule=rule, expected=expected, severity=severity)


def _validate_enum(field: str, value: Any, allowed: set[str]) -> Optional[ValidationError]:
    if value is None:
        return None
    s = str(value).strip().upper()
    if s not in allowed:
        return _err(field, value, "enum_violation", f"one of {sorted(allowed)}")
    return None


def _validate_regex(field: str, value: Any, pattern: re.Pattern, label: str,
                     severity: str = "medium") -> Optional[ValidationError]:
    if value is None:
        return None
    s = str(value).strip()
    if not pattern.match(s):
        return _err(field, value, "regex_violation", label, severity=severity)
    return None


def _validate_range(field: str, value: Any, rng: tuple[float, float]) -> Optional[ValidationError]:
    if value is None:
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return _err(field, value, "type_violation", "numeric")
    lo, hi = rng
    if not (lo <= f <= hi):
        return _err(field, value, "range_violation", f"{lo} ≤ x ≤ {hi}", severity="medium")
    return None


def _validate_date(field: str, value: Any) -> Optional[ValidationError]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            datetime.strptime(s, fmt)
            return None
        except ValueError:
            continue
    return _err(field, value, "date_format_violation", "YYYY-MM-DD or 일반 일자 형식")


# ─────────────────────────── 외부 API ───────────────────────────


def validate_billing_item(item: dict) -> list[ValidationError]:
    """청구 엑셀 한 행 검증."""
    errs: list[ValidationError] = []
    if e := _validate_enum("ndt_method", item.get("ndt_method"), NDT_METHODS): errs.append(e)
    if e := _validate_enum("result", item.get("result"), RESULTS): errs.append(e)
    if e := _validate_regex("report_no", item.get("report_no"), REPORT_NO_RE,
                             "예: '77-005PT' 또는 '77-013VMC'"): errs.append(e)
    if e := _validate_regex("drawing_no", item.get("drawing_no"), DRAWING_NO_RE,
                             "the plant 도면번호 패턴"): errs.append(e)
    if e := _validate_regex("joint_no", item.get("joint_no"), JOINT_NO_RE,
                             "예: 'FW12', 'FW19A'", severity="low"): errs.append(e)
    if e := _validate_date("inspection_date", item.get("inspection_date")): errs.append(e)
    # 다수 용접사 ID 토큰별 검증
    if welder := item.get("welder_id"):
        for tok in str(welder).split():
            if not WELDER_ID_TOKEN_RE.match(tok):
                errs.append(_err("welder_id", welder, "welder_token_pattern",
                                  "예: 'DW001 DW003' (영문자+숫자)", severity="low"))
                break
    if e := _validate_range("unit_price", item.get("unit_price"), (0.0, 1e9)): errs.append(e)
    if e := _validate_range("amount", item.get("amount"), (0.0, 1e12)): errs.append(e)
    return errs


def validate_inspection_report(rep: dict) -> list[ValidationError]:
    """성적서 정규화 결과 검증."""
    errs: list[ValidationError] = []
    if e := _validate_regex("report_no", rep.get("report_no"), REPORT_NO_RE,
                             "예: '77-005PT'"): errs.append(e)
    if e := _validate_enum("ndt_method", rep.get("ndt_method"), NDT_METHODS): errs.append(e)
    if e := _validate_date("inspection_date", rep.get("inspection_date")): errs.append(e)
    if e := _validate_regex("drawing_no", rep.get("drawing_no"), DRAWING_NO_RE,
                             "the plant 도면번호 패턴"): errs.append(e)

    # joint 단위 검증
    for j_idx, joint in enumerate(rep.get("joints", []) or []):
        if e := _validate_enum(f"joints[{j_idx}].result", joint.get("result"), RESULTS): errs.append(e)
        if e := _validate_regex(f"joints[{j_idx}].joint_no", joint.get("joint_no"),
                                 JOINT_NO_RE, "joint 패턴", severity="low"): errs.append(e)
        if welder := joint.get("welder_id"):
            for tok in str(welder).split():
                if not WELDER_ID_TOKEN_RE.match(tok):
                    errs.append(_err(f"joints[{j_idx}].welder_id", welder,
                                      "welder_token_pattern", "예: 'DW001'", severity="low"))
                    break
    return errs


def validate_drawing_extract(dwg: dict) -> list[ValidationError]:
    """도면 추출 (DC/SD/BG combined) 결과 검증."""
    errs: list[ValidationError] = []
    if e := _validate_regex("drawing_no", dwg.get("drawing_no"), DRAWING_NO_RE,
                             "the plant 도면번호 패턴"): errs.append(e)

    # kks_lines 검증 (DC 의 Safety Class 매트릭스)
    for i, line in enumerate(dwg.get("kks_lines", []) or []):
        if e := _validate_regex(f"kks_lines[{i}].kks_code", line.get("kks_code"),
                                 KKS_CODE_RE, "KKS 코드 패턴", severity="low"): errs.append(e)
        if e := _validate_enum(f"kks_lines[{i}].safety_class",
                                line.get("safety_class_np_001_15"), SAFETY_CLASSES): errs.append(e)
        if e := _validate_enum(f"kks_lines[{i}].seismic",
                                line.get("seismic_category_np_031_01"),
                                SEISMIC_CATEGORIES): errs.append(e)
        if e := _validate_range(f"kks_lines[{i}].design_pressure_mpag",
                                 line.get("design_pressure_mpag"),
                                 RANGE_PRESSURE_MPAG): errs.append(e)
        if e := _validate_range(f"kks_lines[{i}].design_temperature_c",
                                 line.get("design_temperature_c"),
                                 RANGE_TEMPERATURE_C): errs.append(e)

    # inspection_matrix 검증
    for i, ins in enumerate(dwg.get("inspection_matrix", []) or []):
        if e := _validate_regex(f"inspection_matrix[{i}].kks_code", ins.get("kks_code"),
                                 KKS_CODE_RE, "KKS 코드 패턴", severity="low"): errs.append(e)
        for ndt_field in ("vt_pct", "pt_or_mt_pct", "rt_pct", "ut_pct",
                          "auxiliary_vt_pct", "auxiliary_pt_or_mt_pct"):
            v = ins.get(ndt_field)
            if v is not None and v != "" and v != "-":
                if e := _validate_range(f"inspection_matrix[{i}].{ndt_field}",
                                         v, RANGE_SAMPLING_PCT): errs.append(e)

    # 도면 인용 코드 (NP/PNAE/GOST 자유 형식이라 검증 생략)
    return errs


# ─────────────────────────── 통합 API ───────────────────────────


def summarize(errors: list[ValidationError]) -> dict:
    """ValidationError 리스트 → 요약 dict (DB/finding 저장용)."""
    by_sev = {"high": 0, "medium": 0, "low": 0}
    for e in errors:
        by_sev[e.severity] = by_sev.get(e.severity, 0) + 1
    return {
        "violations": [e.to_dict() for e in errors],
        "count_high": by_sev["high"],
        "count_medium": by_sev["medium"],
        "count_low": by_sev["low"],
        "needs_review": by_sev["high"] > 0 or by_sev["medium"] >= 3,
    }
