"""검토 대시보드 — localhost:8501.

기능:
- 청구회차 선택, 필터(공종/용접사/검사방법/판정/위험도)
- 위험도 정렬 테이블
- 행 클릭 시: 청구 정보, 매칭 성적서, 도면 요구사항, LLM 설명, 근거 인용
- 검토자 판정(타당/의심/불일치) + 메모 기록 → reviewer_notes 저장
- 원본 성적서 PDF 페이지 다운로드 링크 (사내 파일서버 경로)
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.database.models import (
    BillingItem,
    BillingRound,
    Finding,
    InspectionReport,
    Match,
    Requirement,
    ReviewerNote,
    get_session,
    init_db,
)


st.set_page_config(page_title="NDT Assistant", layout="wide", page_icon="🔎")
st.title("NDT Assistant — 비파괴검사 기성검토")

init_db()

# ─────────────────────────── Mode selector ───────────────────────────


_MODE_LAUNCHER = "🎯 작업 실행 (버튼만 누르면 됩니다)"
_MODE_MAIN = "📊 검토 대시보드 (행 단위 보기)"
_MODE_CONFLICT = "⚠ 정정 입력 전용 (규칙 위반·OCR 보정 행)"
_mode = st.sidebar.radio("화면 선택", [_MODE_LAUNCHER, _MODE_MAIN, _MODE_CONFLICT], index=0)
st.sidebar.divider()


# ─────────────────────────── Launcher mode ───────────────────────────


if _mode == _MODE_LAUNCHER:
    import subprocess
    import sys
    from pathlib import Path
    from datetime import date as _date

    PROJ = Path(__file__).resolve().parent.parent.parent
    PY = sys.executable

    st.header("🎯 작업 실행")
    st.caption("명령어 입력 없음. 버튼만 누르면 됩니다.")

    # 분야·회차 입력
    c1, c2, c3 = st.columns(3)
    with c1:
        disc = st.selectbox("분야", ["CP-P1", "CP-M1", "CP-E1", "CP-A1"], index=0)
    with c2:
        round_no = st.number_input("회차 번호", min_value=1, value=1, step=1)
    with c3:
        bill_date = st.date_input("청구일자", value=_date.today())

    st.divider()
    st.subheader("1. 청구회차 검토 (가장 중요 — 매월)")
    st.caption(
        "**📁 미리 파일 두는 위치** (이미 둔 파일은 자동 인식):\n"
        f"- 청구 엑셀 → `{PROJ / 'samples' / 'billing'}`\n"
        f"- 성적서 PDF → `{PROJ / 'samples' / 'reports'}`\n\n"
        "사내 PC 경로 예: `D:\\NDT_Assistant\\samples\\billing\\` / `D:\\NDT_Assistant\\samples\\reports\\`"
    )

    def _list_files(folder: Path, pat: str) -> list[str]:
        if not folder.exists():
            return []
        return sorted([str(p) for p in folder.glob(pat)])

    billing_options = _list_files(PROJ / "samples" / "billing", "*.xlsx")
    reports_options = _list_files(PROJ / "samples" / "reports", "*.pdf")

    if not billing_options:
        st.warning(
            f"⚠ `samples/billing/` 폴더가 비어 있습니다. "
            f"시공사이 보낸 청구 엑셀 (.xlsx) 1개를 `{PROJ / 'samples' / 'billing'}` 에 둔 다음 페이지를 새로고침하세요."
        )
    if not reports_options:
        st.warning(
            f"⚠ `samples/reports/` 폴더가 비어 있습니다. "
            f"CRST 성적서 PDF (해당 회차 묶음) 1개를 `{PROJ / 'samples' / 'reports'}` 에 둔 다음 페이지를 새로고침하세요."
        )

    sel_billing = st.selectbox(
        "청구 엑셀 (samples/billing/ 의 .xlsx 자동 목록)",
        [""] + billing_options,
        format_func=lambda p: Path(p).name if p else "(선택)",
    )
    sel_reports = st.selectbox(
        "성적서 PDF (samples/reports/ 의 .pdf 자동 목록)",
        [""] + reports_options,
        format_func=lambda p: Path(p).name if p else "(선택)",
    )

    review_clicked = st.button("▶ 청구회차 검토 시작", type="primary", disabled=not (sel_billing and sel_reports))

    st.divider()
    cl, cr = st.columns(2)
    with cl:
        st.subheader("2. 도면·표준 등록")
        st.caption(
            "**신규 도면/표준 PDF 가 들어왔을 때만 1회**. 등록하면 도구가 영구 기억해서 "
            "이후 청구 검토 때 자동 참조 (과다·누락 판정 기준)."
        )
        st.caption(
            f"**📁 PDF 두는 위치** (등록 시작 누르기 전에 미리 두기):\n"
            f"- 도면 → `{PROJ / 'samples' / 'drawings'}`\n"
            f"- 표준 → `{PROJ / 'samples' / 'codes_standards'}`\n"
            f"- SCWEP → `{PROJ / 'samples' / 'scwep'}`"
        )
        ingest_target = st.radio("등록할 자료 종류", [
            "도면 (DC/SD/BG) — samples/drawings/",
            "표준 (NP/PNAE/GOST) — samples/codes_standards/",
            "SCWEP 시공절차서 — samples/scwep/ (중요 배관·기계만)",
        ], horizontal=False)
        # 선택된 자료의 실제 폴더 표시 + 파일 개수 (안 두면 0 표시)
        target_folder = {
            "도면 (DC/SD/BG) — samples/drawings/": PROJ / "samples" / "drawings",
            "표준 (NP/PNAE/GOST) — samples/codes_standards/": PROJ / "samples" / "codes_standards",
            "SCWEP 시공절차서 — samples/scwep/ (중요 배관·기계만)": PROJ / "samples" / "scwep",
        }.get(ingest_target)
        if target_folder:
            cnt = len(list(target_folder.glob("*.pdf"))) if target_folder.exists() else 0
            if cnt == 0:
                st.warning(f"⚠ `{target_folder}` 에 PDF 0개. PDF 를 먼저 두고 누르세요.")
            else:
                st.info(f"📄 `{target_folder}` 안 PDF: {cnt}개 발견 — 등록 가능")
        ingest_clicked = st.button("▶ 등록 시작")
    with cr:
        st.subheader("3. 검사기준 가이드 생성")
        st.caption("HTML + Excel + Markdown 3종 동시 생성")
        guide_clicked = st.button("▶ 가이드 생성", type="primary")

    st.divider()
    st.subheader("실행 결과 (실시간)")
    log_area = st.empty()

    def _run(args: list[str], label: str):
        with st.spinner(f"{label} 진행 중..."):
            env = dict(__import__("os").environ)
            env.setdefault("NDT_OCR_WORKERS", "4")
            env.setdefault("NDT_OCR_LANGS", "eng+rus")
            proc = subprocess.Popen(
                [PY, "-m", "app.main", *args],
                cwd=str(PROJ), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            output = []
            for line in proc.stdout:
                output.append(line)
                if len(output) % 5 == 0:
                    log_area.code("".join(output[-30:]), language="text")
            proc.wait()
            log_area.code("".join(output[-50:]), language="text")
            if proc.returncode == 0:
                st.success(f"✓ {label} 완료")
            else:
                st.error(f"✗ {label} 실패 (exit {proc.returncode})")

    if review_clicked:
        _run([
            "review",
            "--billing", sel_billing,
            "--reports", sel_reports,
            "--round", str(round_no),
            "--date", bill_date.isoformat(),
            "--discipline", disc,
        ], "청구회차 검토")
        # 결과 파일 안내
        result_xlsx = Path(sel_billing).with_name(Path(sel_billing).stem + "_검토결과.xlsx")
        if result_xlsx.exists():
            st.info(f"📄 결과 엑셀: `{result_xlsx}`")

    if ingest_clicked:
        if "drawings" in ingest_target:
            _run(["ingest-drawings", str(PROJ / "samples" / "drawings")], "도면 적재")
        elif "scwep" in ingest_target.lower():
            _run(["ingest-standards", str(PROJ / "samples" / "scwep"), "--type", "scwep"], "SCWEP 적재")
        else:
            _run(["ingest-standards", str(PROJ / "samples" / "codes_standards"), "--type", "code"], "표준 적재")

    if guide_clicked:
        _run(["criteria-guide", "--discipline", disc], "검사기준 가이드 생성")
        latest = sorted((PROJ / "data" / "outputs").glob(f"검사기준_가이드_{disc}_*.html"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if latest:
            st.info(f"📄 가이드: `{latest[0]}`")
            with open(latest[0], "rb") as f:
                st.download_button("⬇ HTML 가이드 다운로드", f.read(), file_name=latest[0].name)

    st.stop()


# ─────────────────────────── Conflict-only mode ───────────────────────────


if _mode == _MODE_CONFLICT:
    st.header("⚠ 정정 입력 전용 — 규칙 위반·OCR 자동보정 행")
    st.caption(
        "도구가 **명확한 규칙 위반**(검사방법 오타·결과값 형식 오류 등) 또는 "
        "**OCR 자동 보정**(원본 스캔이 깨져 LLM 이 추정)을 표시한 행만 모아 보여줍니다. "
        "검토자가 정정값을 직접 입력하면 SQLite 에 누적되어 다음 가이드 재생성에 반영됩니다."
    )

    with get_session() as s:
        all_rounds = list(s.scalars(select(BillingRound).order_by(BillingRound.billing_date.desc())))
    if not all_rounds:
        st.info("등록된 청구회차가 없습니다. 먼저 ① 청구회차 검토를 실행하세요.")
        st.stop()
    conflict_round = st.sidebar.selectbox(
        "청구회차 선택",
        all_rounds,
        format_func=lambda br: f"{br.discipline} | {br.round_no}차 ({br.billing_date.isoformat()})",
    )

    # Conflict-rule filter — 영문 rule key → 한글 라벨
    RULE_LABELS = {
        "rule_engine_violation": "규칙 위반 (형식·범위·enum)",
        "ocr_auto_corrected": "OCR 자동 보정 (LLM 추정)",
        "drawing_no_is_ke_misentry": "KE 오기 (시공사 정정 요청 대상)",
        "no_matching_report": "매칭 성적서 없음",
    }
    RULE_REVERSE = {v: k for k, v in RULE_LABELS.items()}
    selected_labels = st.sidebar.multiselect(
        "표시할 위반 유형",
        list(RULE_LABELS.values()),
        default=[RULE_LABELS["rule_engine_violation"], RULE_LABELS["ocr_auto_corrected"]],
    )
    conflict_rules = [RULE_REVERSE[s] for s in selected_labels]

    rows = []
    with get_session() as s:
        for item in s.scalars(select(BillingItem).where(BillingItem.billing_round_id == conflict_round.id)):
            f = s.scalar(select(Finding).where(Finding.billing_item_id == item.id))
            if not f:
                continue
            findings_in_citations = (f.citations_json or {}).get("findings", []) if f.citations_json else []
            matched_rules = [fnd.get("rule") for fnd in findings_in_citations if fnd.get("rule") in conflict_rules]
            if not matched_rules:
                continue
            rows.append({
                "행번호": item.id,
                "Joint No.": item.joint_no,
                "검사방법": item.ndt_method,
                "성적서번호": item.report_no,
                "도면번호": item.drawing_no,
                "위반유형": ", ".join(sorted({RULE_LABELS.get(r, r) for r in matched_rules})),
                "위험도": f.risk_score,
                "요약": f.summary,
                "_needs_review": f.needs_review,
                "_findings_full": findings_in_citations,
                "_reasons": (f.review_reasons_json or {}).get("reasons", []) if f.review_reasons_json else [],
            })

    st.metric("정정 검토 대상 행", len(rows))
    if not rows:
        st.success("✓ 선택된 유형의 위반 없음. 정정 입력 불필요.")
        st.stop()

    df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    st.dataframe(df, hide_index=True, use_container_width=True)

    # Detail + correction form
    st.divider()
    st.subheader("✍ 정정 입력 (행 선택)")
    st.caption("위 표의 '행번호' 컬럼 숫자를 아래에 입력하면 그 행의 위반 사유와 정정 입력란이 나옵니다.")
    sel_id = st.number_input(
        "행번호 입력", min_value=0, value=int(rows[0]["행번호"]) if rows else 0, step=1
    )
    sel_row = next((r for r in rows if r["행번호"] == sel_id), None)
    if sel_row:
        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("**🔍 위반 사유**")
            for reason in sel_row["_reasons"]:
                st.markdown(f"- {reason}")
            if sel_row["_findings_full"]:
                st.markdown("**상세 (도구 내부 기록)**")
                for f in sel_row["_findings_full"]:
                    rule = f.get("rule")
                    if rule not in conflict_rules:
                        continue
                    label = RULE_LABELS.get(rule, rule)
                    with st.expander(label):
                        details = f.get("details", {})
                        if isinstance(details, dict) and details:
                            rows_kv = [{"항목": k, "값": str(v)} for k, v in details.items()]
                            st.dataframe(pd.DataFrame(rows_kv), hide_index=True, use_container_width=True)
                        else:
                            st.write(details or "(상세 없음)")
        with col_r:
            st.markdown("**✏ 정정값 입력**")
            field = st.text_input(
                "정정할 필드명",
                help="예: 검사방법 → ndt_method / 성적서번호 → report_no / 도면번호 → drawing_no",
            )
            corrected = st.text_input("정정값 (올바른 값)")
            verdict_kr = st.selectbox(
                "판정 직접 지정 (선택)",
                ["", "OK (문제 없음)", "SUSPECT (의심)", "NONCOMPLIANT (불일치 확정)"],
                index=0,
            )
            v_map = {
                "OK (문제 없음)": "OK",
                "SUSPECT (의심)": "SUSPECT",
                "NONCOMPLIANT (불일치 확정)": "NONCOMPLIANT",
            }
            verdict = v_map.get(verdict_kr, "")
            note = st.text_area("메모 (선택 — 정정 사유, CRST·시공사 응답 등)")
            reviewer = st.text_input("검토자 이니셜·ID", value="")
            if st.button("💾 저장", type="primary"):
                with get_session() as s2:
                    s2.add(ReviewerNote(
                        billing_item_id=sel_id,
                        reviewer=reviewer or None,
                        verdict_override=verdict or None,
                        note=(
                            f"[정정] {field} → '{corrected}'\n"
                            + (note or "")
                        ),
                    ))
                    s2.commit()
                st.success(f"✓ 저장됨 (행번호 {sel_id}). 다음 가이드 재생성 시 반영됩니다.")

    st.stop()   # 메인 대시보드 코드 진입 차단


# ─────────────────────────── Sidebar ───────────────────────────


@st.cache_data(ttl=30)
def _load_rounds() -> list[dict]:
    with get_session() as s:
        return [
            {
                "id": br.id,
                "label": f"{br.discipline} | {br.round_no}차 ({br.billing_date.isoformat()})",
            }
            for br in s.scalars(select(BillingRound).order_by(BillingRound.billing_date.desc()))
        ]


rounds = _load_rounds()
if not rounds:
    st.info("등록된 청구회차가 없습니다. `python -m app.main review ...` 를 먼저 실행하세요.")
    st.stop()

with st.sidebar:
    sel = st.selectbox("청구회차 선택", rounds, format_func=lambda r: r["label"])
    st.divider()
    st.subheader("필터")
    only_needs_review = st.checkbox("⚠ 재확인 필요만 보기", value=False,
                                     help="도구가 자신없어 사람 판단을 요청한 행만 표시")
    verdict_filter = st.multiselect(
        "판정 (자동)",
        ["OK", "SUSPECT", "NONCOMPLIANT"],
        default=["SUSPECT", "NONCOMPLIANT"],
        help="OK=문제없음 / SUSPECT=의심 / NONCOMPLIANT=명백 불일치",
    )
    risk_min = st.slider("최소 위험도 (0~100)", 0, 100, 30,
                          help="높을수록 우선 검토 대상")
    method_filter = st.multiselect("검사 방법", ["VT", "RT", "UT", "PT", "MT"], default=[])
    welder_filter = st.text_input("용접사 ID (부분 검색)")


# ─────────────────────────── Data ───────────────────────────


@st.cache_data(ttl=10)
def _load_items(round_id: int) -> pd.DataFrame:
    rows = []
    with get_session() as s:
        for item in s.scalars(select(BillingItem).where(BillingItem.billing_round_id == round_id)):
            m = s.scalar(select(Match).where(Match.billing_item_id == item.id))
            f = s.scalar(select(Finding).where(Finding.billing_item_id == item.id))
            rep = (
                s.get(InspectionReport, m.inspection_report_id)
                if (m and m.inspection_report_id)
                else None
            )
            review_flag = bool(
                (f and f.needs_review) or (m and m.needs_review) or (rep and rep.needs_review)
            )
            review_reasons = []
            if f and f.needs_review:
                review_reasons += [f"[적합성] {r}" for r in (f.review_reasons_json or {}).get("reasons", [])]
            if m and m.needs_review:
                review_reasons += [f"[매칭] {r}" for r in (m.review_reasons_json or {}).get("reasons", [])]
            if rep and rep.needs_review:
                review_reasons += [f"[성적서] {r}" for r in (rep.review_reasons_json or {}).get("reasons", [])]
            rows.append({
                "행번호": item.id,
                "⚠재확인": "✓" if review_flag else "",
                "Joint No.": item.joint_no,
                "검사방법": item.ndt_method,
                "용접사": item.welder_id,
                "도면번호": item.drawing_no,
                "검사일": item.inspection_date,
                "결과": item.result,
                "매칭성적서": rep.report_no if rep else None,
                "매칭방식": m.match_method if m else None,
                "매칭점수": m.match_score if m else None,
                "판정": f.verdict if f else None,
                "위험도": f.risk_score if f else 0,
                "needs_review": review_flag,  # 내부용
                "재확인사유": " | ".join(review_reasons) if review_reasons else None,
                "요약": f.summary if f else None,
            })
    return pd.DataFrame(rows)


df = _load_items(sel["id"])
if df.empty:
    st.warning("이 회차에는 검토 항목이 없습니다.")
    st.stop()

# Apply filters
mask = df["위험도"].fillna(0) >= risk_min
if only_needs_review:
    mask &= df["needs_review"]
if verdict_filter:
    mask &= df["판정"].isin(verdict_filter)
if method_filter:
    mask &= df["검사방법"].isin(method_filter)
if welder_filter:
    mask &= df["용접사"].fillna("").str.contains(welder_filter, case=False)
# 정렬: 재확인 필요가 항상 위, 그다음 위험도 순
df_view = df[mask].sort_values(["needs_review", "위험도"], ascending=[False, False])


# ─────────────────────────── KPIs ───────────────────────────


c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("전체 청구 행", len(df))
c2.metric("필터 후 표시", len(df_view))
c3.metric("⚠ 재확인 필요", int(df["needs_review"].sum()))
c4.metric("명백 불일치", int((df["판정"] == "NONCOMPLIANT").sum()),
          help="NONCOMPLIANT — 규칙 위반·매칭 실패 등 사람 확인 필요")
c5.metric("매칭 성적서 없음", int(df["매칭성적서"].isna().sum()))


# ─────────────────────────── Table ───────────────────────────


st.subheader("청구 행 (재확인·위험도 내림차순)")
st.caption("⚠ 표시된 행 = 도구가 자신 없어 사람 판단 요청. 빨강(NONCOMPLIANT)보다 우선.")
# 내부용 needs_review 컬럼은 표시하지 않음
df_show = df_view.drop(columns=["needs_review"], errors="ignore")
st.dataframe(df_show, hide_index=True, use_container_width=True)


# ─────────────────────────── Detail panel ───────────────────────────


st.subheader("상세 보기 (행 선택)")
st.caption("위 표의 '행번호' 컬럼 숫자를 아래에 입력하면 그 청구 행의 상세·근거·검토 메모 화면이 나옵니다.")
selected_id = st.number_input(
    "행번호 입력",
    min_value=0,
    value=int(df_view.iloc[0]["행번호"]) if len(df_view) else 0,
    step=1,
    help="위 표 첫 컬럼(행번호)에 표시된 숫자",
)


def _kv_table(d: dict) -> None:
    """JSON dump 대신 한글 라벨 + 값으로 깔끔하게 표시."""
    rows = [{"항목": k, "값": ("" if v is None else str(v))} for k, v in d.items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


if selected_id:
    with get_session() as s:
        item = s.get(BillingItem, int(selected_id))
        if item is None:
            st.warning("해당 번호의 청구 행이 없습니다. 위 표의 '행번호' 값을 확인하세요.")
        else:
            f = s.scalar(select(Finding).where(Finding.billing_item_id == item.id))
            m = s.scalar(select(Match).where(Match.billing_item_id == item.id))
            rep = (
                s.get(InspectionReport, m.inspection_report_id)
                if (m and m.inspection_report_id)
                else None
            )
            req = None
            if item.drawing_no:
                req = s.scalar(
                    select(Requirement).where(Requirement.joint_no == item.joint_no)
                )

            colA, colB = st.columns(2)
            with colA:
                st.markdown("**📋 청구 정보**")
                _kv_table({
                    "Joint No.": item.joint_no,
                    "검사 방법": item.ndt_method,
                    "용접사": item.welder_id,
                    "도면번호": item.drawing_no,
                    "검사일": item.inspection_date.isoformat() if item.inspection_date else None,
                    "결과": item.result,
                    "청구액": item.amount,
                })
                if rep:
                    st.markdown("**📄 매칭된 성적서**")
                    _kv_table({
                        "성적서번호": rep.report_no,
                        "검사 방법": rep.ndt_method,
                        "검사일": rep.inspection_date.isoformat() if rep.inspection_date else None,
                        "승인자": rep.approver,
                        "원본 PDF": rep.source_pdf,
                        "페이지 범위": f"{rep.start_page + 1}~{rep.end_page + 1}",
                    })
                else:
                    st.info("📄 매칭된 성적서 없음 — 누락 의심")

            with colB:
                if f:
                    color = {"OK": "🟢", "SUSPECT": "🟡", "NONCOMPLIANT": "🔴"}.get(f.verdict, "⚪")
                    st.markdown(f"### {color} 판정: `{f.verdict}`  ·  위험도 **{f.risk_score}/100**")
                    if f.needs_review or (m and m.needs_review) or (rep and rep.needs_review):
                        st.warning("⚠ **재확인 필요** — 도구가 자신없어 사람 판단을 요청합니다. 아래 사유 참조.")
                        all_reasons = []
                        if f.needs_review:
                            all_reasons += [f"[적합성] {r}" for r in (f.review_reasons_json or {}).get("reasons", [])]
                        if m and m.needs_review:
                            all_reasons += [f"[매칭] {r}" for r in (m.review_reasons_json or {}).get("reasons", [])]
                        if rep and rep.needs_review:
                            all_reasons += [f"[성적서] {r}" for r in (rep.review_reasons_json or {}).get("reasons", [])]
                        for reason in all_reasons:
                            st.markdown(f"- {reason}")
                    if f.summary:
                        st.markdown("**요약**")
                        st.write(f.summary)
                    findings_explained = (f.explanation_json or {}).get("findings_explained", [])
                    if findings_explained:
                        st.markdown("**🔍 근거 (LLM 설명 + 원문 인용)**")
                        for fe in findings_explained:
                            with st.expander(f"{fe.get('rule', '항목')}"):
                                st.markdown(fe.get("explanation_korean", ""))
                                for c in fe.get("evidence_citations", []) or []:
                                    st.caption(
                                        f"> 📑 [{c.get('doc')}] p.{c.get('page')}\n>\n> {c.get('quote')}"
                                    )
                if req:
                    st.markdown("**📐 도면 요구사항 (이 Joint)**")
                    _kv_table({
                        "Joint No.": req.joint_no,
                        "요구 NDT": ", ".join(req.required_ndt_json) if req.required_ndt_json else None,
                        "WPS 번호": req.wps_no,
                        "두께 (mm)": req.thickness_mm,
                        "Safety Class": req.safety_class,
                    })

            st.divider()
            st.markdown("**✍ 검토자 판정 기록 (저장 시 SQLite 누적, 다음 가이드 재생성 시 반영)**")
            v_override = st.selectbox(
                "판정 직접 지정 (도구 판정을 덮어쓰려면 선택)",
                ["", "OK (문제 없음)", "SUSPECT (의심)", "NONCOMPLIANT (불일치 확정)"],
                index=0,
            )
            # 한글 라벨 → 영문 값 매핑
            v_map = {
                "OK (문제 없음)": "OK",
                "SUSPECT (의심)": "SUSPECT",
                "NONCOMPLIANT (불일치 확정)": "NONCOMPLIANT",
            }
            v_value = v_map.get(v_override, "")
            note = st.text_area("메모 (선택 — 정정 사유, 시공사 응답 등)")
            reviewer = st.text_input("검토자 이니셜·ID", value="")
            if st.button("💾 저장", type="primary"):
                with get_session() as s2:
                    s2.add(ReviewerNote(
                        billing_item_id=item.id,
                        reviewer=reviewer or None,
                        verdict_override=v_value or None,
                        note=note or None,
                    ))
                    s2.commit()
                st.success("✓ 저장됐습니다. 다음 가이드 재생성 시 반영됩니다.")
                _load_items.clear()
