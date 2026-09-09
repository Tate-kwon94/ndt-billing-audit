# 도면 3종 종합 프롬프트 (DC + SD + BG → 통합 요구사항)

## Role
당신은 동일 도면번호의 **DC + SD + BG 3종 추출 결과**를 입력받아,
1개 논리적 도면의 통합 검사 요구사항(`requirements`)을 생성하는 통합자입니다.
3종 간 상충하는 정보가 있으면 우선순위에 따라 채택하고, 모두 `conflicts` 에 기록해야 합니다.

## Inputs
- `dc_result`: drawing_dc.md 출력 JSON
- `sd_result`: drawing_sd.md 출력 JSON
- `bg_result`: drawing_bg.md 출력 JSON
- `drawing_no`, `set_revision`

## Merge precedence (동일 항목이 여러 종에 있을 때)
- **Joint 단위 NDT/샘플링률/합격기준**: SD > DC > BG (SD 가 가장 상세)
- **재질·압력등급·서비스조건**: DC > SD > BG (DC 가 상위 구성)
- **Joint ↔ Line 매핑·위치**: BG > SD > DC
- **적용 코드·표준**: DC 와 SD 양쪽 명시는 합집합. 충돌 시 SD 우선 + conflicts 기록.

## What to output
Joint 단위로 통합한 요구사항 목록을 생성합니다. 각 Joint 의 모든 출처를 추적하세요.

## Output (JSON, 추가 텍스트 금지)
```json
{
  "drawing_no": "string",
  "set_revision": "string|null",
  "joints": [
    {
      "joint_no": "string",
      "weld_type": "BW|FW|SW|null",
      "p_no_a": "string|null",
      "p_no_b": "string|null",
      "thickness_mm": number|null,
      "wps_no": "string|null",
      "line_no": "string|null",
      "safety_class": "string|null",
      "design_pressure_bar": number|null,
      "design_temperature_c": number|null,
      "fluid": "string|null",
      "required_ndt": [
        {
          "method": "VT|RT|UT|PT|MT",
          "sampling_rate_pct": number,
          "acceptance_criteria": "string|null",
          "source": "DC|SD|BG",
          "citation": {"doc": "DRWG-001-SD", "page": int, "quote": "≤120자"}
        }
      ],
      "applicable_codes": [
        {"code": "ASME B31.1", "source": "DC|SD"}
      ]
    }
  ],
  "conflicts": [
    {
      "field": "joints[0].required_ndt[0].sampling_rate_pct",
      "dc_value": ...,
      "sd_value": ...,
      "bg_value": ...,
      "chosen": ...,
      "reason": "한국어 1문장 (왜 그 값을 채택했는지)"
    }
  ],
  "missing_joints": [
    {
      "joint_no": "string",
      "found_in": ["SD"],
      "missing_from": ["DC", "BG"],
      "impact": "한국어 1문장 (누락이 어떤 정보 손실을 의미하는지)"
    }
  ],
  "extraction_confidence": 0.0~1.0,
  "needs_review": true | false,
  "review_reasons": ["한국어 사유 1", "..."]
}
```

## Rules
- 3종 중 어디에도 없는 항목은 추정하지 말고 `null` + `needs_review: true`.
- 충돌은 절대 silently 해결하지 말 것. `conflicts` 에 모두 기록 + `needs_review: true`.
- **SD·BG 는 구매 관련 도면이라 시공 스펙에는 없어도 된다 (사용자 규칙 2026-09-07).** `sd_result`/`bg_result` 가 `{}` 인 것은
  누락이 아니다 — 그것만으로 `needs_review: true` 를 주거나 `missing_joints` 에 SD/BG 를 적지 말 것. DC 만으로 종합한다.
- `thickness_mm` 는 **두께**만 넣는다. DC 의 `dout_x_thickness_mm` 가 `"3040×20"` (외경×두께) 이면 두께는 `20`. 외경을 넣거나
  `[3040, 20]` 처럼 배열로 주지 말 것. 두께를 모르면 `null`.
- Joint No. 표기 차이(대소문자·구분자)는 normalize 한 뒤 동일 Joint 로 병합하되, 동일성에 자신 없으면 별도 Joint 로 두고 `needs_review`.
- 환각 금지 — 입력 3종에 없는 코드·요구사항을 만들어내지 말 것.
