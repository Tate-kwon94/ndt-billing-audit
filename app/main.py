"""CLI 진입점.

Examples:
  python -m app.main ingest-drawings samples/drawings/
  python -m app.main ingest-standards samples/scwep/ --type scwep
  python -m app.main ingest-standards samples/codes_standards/ --type code
  python -m app.main ingest-standards samples/contracts/ --type contract
  python -m app.main review \\
      --billing samples/billing/CP-M1_2차청구.xlsx \\
      --reports samples/reports/CP-M1_2차_성적서.pdf \\
      --round 2 --date 2026-06-30 --discipline CP-M1
  python -m app.main dashboard
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import typer

from app.config import PROJECT_ROOT
from app.database.models import get_session, init_db
from app.database.repository import (
    add_billing_items,
    create_billing_round,
)
from app.logging_setup import configure_logging

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _skip_reason(p: Path, stems: set) -> Optional[str]:
    """같은 문서의 스캔본·러시아어판은 건너뛴다 — 텍스트판 영문본 하나면 충분하고, 스캔본은 OCR 수 시간."""
    if "_scan" in p.stem and any(
            s != p.stem and "_scan" not in s and p.stem.startswith(s + "_") for s in stems):
        return "스캔본 (텍스트판 있음)"      # 짝은 '_scan' 이 없는 텍스트판이어야 하고, 접미는 '_' 로 이어져야 한다
    if p.stem.endswith(".R") and (p.stem[:-2] + ".E") in stems:
        return "러시아어판 (영문판 있음)"
    return None


def _usage_now() -> str:
    from app.hcx_client import usage_line_now
    try:
        return usage_line_now()
    except Exception:       # noqa: BLE001 - 사용량 표시가 본 작업을 막으면 안 된다
        return ""


@app.command("usage")
def usage():
    """HCX 사용량 — 오늘·최근 7일·이번 달·전체 (호출·토큰). 한도가 아니라 확인용 (부서 통보 자료)."""
    from app.hcx_client import usage_summary
    s = usage_summary()
    typer.echo("HCX 사용량 (data\\hcx_daily_count.json 기준)")
    for label, key in (("오늘", "today"), ("이번 주(최근 7일)", "week"), ("이번 달", "month"), ("전체", "all")):
        v = s[key]
        typer.echo(f"  {label:12} 호출 {v['calls']:>8,} 회 · 토큰 {v['tokens']:>12,}")
    typer.echo(f"  기록된 날 수: {s['days']}")


@app.command("ingest-drawings")
def ingest_drawings(
    folder: Path = typer.Argument(..., help="DC/SD/BG PDF 들이 있는 폴더"),
    as_of: Optional[str] = typer.Option(None, help="유효시작일 YYYY-MM-DD (기본 오늘)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """도면 폴더를 적재. 3파일 세트 자동 그룹화 + 통합 요구사항 생성."""
    from app.extractors.drawing.requirements_extractor import ingest_folder

    from app import progress_writer as pw
    run_id = configure_logging(stage="ingest-drawings", verbose=verbose)
    pw.reset(); pw.step("ingest-drawings", "시작", folder.name)
    init_db()
    eff = date.fromisoformat(as_of) if as_of else date.today()
    try:
        stats = ingest_folder(folder, as_of=eff)
    except Exception as e:
        pw.error("ingest-drawings", f"{type(e).__name__}: {e}")
        raise
    pw.step("ingest-drawings", "완료")
    typer.echo(_usage_now())
    for k, v in stats.items():
        pw.kpi(k, v)
    failed = stats.get("failed_sets") or []
    typer.echo(f"{'✗' if failed else '✓'} Ingested: {stats}")
    typer.echo(f"  run_id={run_id}  (로그: data/logs/..., 진행 요약: data/progress.log)")
    if failed:
        # 나머지 세트는 저장됐다. 실패한 세트만 다시 넣으면 된다 — 조용히 ✓ 로 끝내지 않는다.
        pw.error("ingest-drawings", f"세트 {len(failed)}개 실패: {', '.join(failed)}")
        typer.echo(f"✗ 세트 {len(failed)}개는 저장 실패 — 로그의 'Failed to ingest drawing set' 줄을 보낼 것: {failed}")
        raise typer.Exit(code=1)


@app.command("vision-probe")
def vision_probe_cmd(
    providers: Optional[str] = typer.Option(None, "--providers", help="쉼표로 구분 (기본: 전부)"),
):
    """사내 모델 중 어느 것이 이미지를 받는지 확인 — 표 전사 후보를 늘리기 위한 실측 (추측 금지)."""
    from app.vision_probe import run
    names = [x.strip() for x in providers.split(",")] if providers else None
    rows = run(names)
    typer.echo("모델별 이미지 입력 지원 (이 화면을 그대로 보낼 것):")
    typer.echo(f"  {'provider':10s} {'model':16s} {'비전':6s} 응답/오류")
    for r in rows:
        mark = {True: "가능", False: "불가", None: "보류"}[r["vision"]]
        typer.echo(f"  {r['provider']:10s} {str(r['model'])[:16]:16s} {mark:6s} {(r['answer'] or r['error'])[:70]}")
    ok = [r["provider"] for r in rows if r["vision"]]
    typer.echo(f"\n→ 비전 가능: {', '.join(ok) if ok else '없음'}. "
               "두 개 이상이면 표 전사 A/B 대상이 늘어난다.")


@app.command("table-bench")
def table_bench_cmd(
    providers: str = typer.Option(..., "--providers", help="쉼표 구분 (vision-probe 에서 '가능' 나온 것만)"),
    truth: Path = typer.Option(Path("data/정답표/표전사_정답_도면1_p13.json"), "--truth"),
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="도면 PDF 를 직접 지정 (자동 탐색이 실패할 때)"),
    page: Optional[int] = typer.Option(None, "--page", help="쪽 번호를 직접 지정 (표식으로 못 찾을 때)"),
    dpi: int = typer.Option(300, "--dpi"),
):
    """표 전사 정확도 측정 — 정답표가 있는 도면 쪽을 모델별로 전사시켜 채점한다."""
    from app.table_bench import run
    names = [x.strip() for x in providers.split(",") if x.strip()]
    try:
        rows = run(truth, names, pdf=str(pdf) if pdf else None, page=page, dpi=dpi)
    except (ValueError, FileNotFoundError) as e:
        typer.echo(f"✗ {e}")
        raise typer.Exit(code=1)
    typer.echo("표 전사 정확도 (이 화면을 그대로 보낼 것):")
    mb = next((r.get("png_mb") for r in rows if r.get("png_mb")), None)
    if mb:
        typer.echo(f"  보낸 이미지: PNG {mb} MB")
    typer.echo(f"  {'provider':10s} {'KKS':>6} {'완전행':>6} {'오답칸':>6} {'정확도':>7} {'ms':>7}  비고")
    for r in rows:
        if r.get("error"):
            typer.echo(f"  {r['provider']:10s} {'-':>6} {'-':>6} {'-':>6} {'-':>7} {r['ms'] or 0:>7}  실패")
        else:
            worst = ", ".join(f"{c}({r['by_column'][c]}/10)" for c in r["worst_columns"])
            typer.echo(f"  {r['provider']:10s} {r['kks_found']:>4}/10 {r['rows_matched']:>4}/10 "
                       f"{r['wrong_cells']:>6} {r['accuracy']*100:>6.1f}% {r['ms']:>7}  틀린 열: {worst}")
    for r in rows:
        if r.get("error"):
            typer.echo(f"\n  [{r['provider']}] 오류 전문:\n    {r['error']}")
        elif r.get("transcript"):
            typer.echo(f"  [{r['provider']}] 전사본: {r['transcript']}")
    typer.echo("\n정답: KKS 10개 · 완전행 10 · 오답칸 0 · 정확도 100%. "
               "tesseract(psm6) 는 KKS 0/10 · 정확도 0% 였다.")


@app.command("score-review")
def score_review_cmd(
    result: Path = typer.Option(..., "--result", help="① 이 만든 …_검토결과.xlsx"),
    key: Path = typer.Option(Path("data/정답표/답안지_1항차재발행.json"), "--key", help="답안지 JSON"),
):
    """검토결과를 답안지와 행 단위로 대조해 점수를 낸다 (매칭·판정·판정불가 표시를 따로 센다)."""
    import json as _json
    from app.report.score_review import score
    if not Path(key).exists():
        typer.echo(f"✗ 답안지가 없습니다: {key}"); raise typer.Exit(code=1)
    if not Path(result).exists():
        typer.echo(f"✗ 검토결과가 없습니다: {result}"); raise typer.Exit(code=1)
    try:
        s = score(_json.loads(Path(key).read_text(encoding="utf-8")), result)
    except ValueError as e:
        typer.echo(f"✗ {e}"); raise typer.Exit(code=1)
    typer.echo("검토결과 채점 (이 화면을 그대로 보낼 것):")
    typer.echo(f"  채점한 행 {s['rows_scored']} · 검토결과에 없는 행 {s['rows_missing_from_result']}")
    tot = sum(s["matching"].values())
    if tot:
        ok = s["matching"].get("일치", 0)
        typer.echo(f"\n  [매칭] 답안지의 성적서 유무와 일치 {ok}/{tot} = {ok/tot*100:.1f}%")
    if s["verdict"]:
        typer.echo("\n  [판정] 답안지 → 파이프라인")
        for k2, n in sorted(s["verdict"].items(), key=lambda x: -x[1]):
            typer.echo(f"    {k2:28s} {n:5d}")
    if s["unjudgeable"]:
        t2 = sum(s["unjudgeable"].values()); good = s["unjudgeable"].get("표시함", 0)
        typer.echo(f"\n  [판정불가 행] 재확인 표시함 {good}/{t2}"
                   f"  — 놓친 {s['unjudgeable'].get('놓침', 0)}건은 근거 없이 통과시킨 것이다")


@app.command("label-sheet")
def label_sheet_cmd(
    round_no: int = typer.Option(..., "--round", help="회차 번호 (billing_rounds.round_no)"),
    n: int = typer.Option(200, "--n", help="표본 행수"),
    seed: int = typer.Option(0, "--seed", help="표본 재현용 시드"),
    out: Optional[Path] = typer.Option(None, "--out", help="출력 xlsx (기본 data/outputs/정답표_<회차>.xlsx)"),
):
    """정답표(라벨링 시트) — ① 결과를 뼈대로 층화 표본을 뽑아 정답 빈칸이 있는 엑셀을 만든다 (A/B 기준선)."""
    from app.database.models import BillingRound, get_session, init_db
    from app.report import label_sheet
    init_db()
    with get_session() as s:
        br = (s.query(BillingRound).filter(BillingRound.round_no == round_no)
              .order_by(BillingRound.id.desc()).first())
        if br is None:
            typer.echo(f"✗ 회차 {round_no} 가 DB 에 없습니다. ① 검토를 먼저 돌리세요.")
            raise typer.Exit(code=1)
        rows = label_sheet.build_rows(s, br.id)
    picked = label_sheet.sample(rows, n=n, seed=seed)
    from app.config import DATA_DIR
    out = out or (DATA_DIR / "outputs" / f"정답표_{round_no}회차_{br.discipline}.xlsx")
    label_sheet.write(picked, out, round_label=f"{round_no}회차 {br.discipline} {br.billing_date}")
    strata = len({r["층"] for r in picked})
    typer.echo(f"✓ 정답표: {out}  (전체 {len(rows)}행 중 표본 {len(picked)}행, 층 {strata}개)")


@app.command("show-drawing")
def show_drawing(drawing_no: Optional[str] = typer.Argument(None, help="도면번호 일부 (생략하면 전부)")):
    """등록된 도면 세트를 표로 보여준다 — 파일·페이지별 판독 경로·적용 코드·라인별 두께/안전등급 (검토자 대조용)."""
    from app.database.models import DrawingFile, DrawingSet, Requirement, get_session, init_db
    init_db()
    with get_session() as s:
        q = s.query(DrawingSet).filter(DrawingSet.superseded_at.is_(None)).order_by(DrawingSet.drawing_no)
        if drawing_no:
            q = q.filter(DrawingSet.drawing_no.contains(drawing_no))
        sets = q.all()
        if not sets:
            typer.echo("등록된 도면 세트가 없습니다." if not drawing_no else f"'{drawing_no}' 를 포함한 도면 세트 없음.")
            return
        for ds in sets:
            typer.echo(f"\n■ {ds.drawing_no}  rev={ds.set_revision or '-'}  재확인={'예' if ds.needs_review else '아니오'}")
            for f in s.query(DrawingFile).filter(DrawingFile.set_id == ds.id).order_by(DrawingFile.drawing_type).all():
                ex = f.extracted_json or {}
                codes = ex.get("applicable_codes") or []
                st = ex.get("_table_stats") or {}
                typer.echo(f"  [{f.drawing_type}] {Path(f.file_path).name}")
                if st:
                    typer.echo(f"      표페이지 {st.get('table_like', 0)} · 전사 {st.get('transcribed', 0)} · "
                               f"재확인 {st.get('needs_review', 0)} · vlm 실패 {st.get('vlm_failed', 0)}")
                pages = ex.get("_pages") or []
                if pages:
                    typer.echo("      페이지: " + " ".join(
                        f"p.{pg.get('page')}={pg.get('source')}{'!' if pg.get('needs_review') else ''}" for pg in pages))
                else:
                    typer.echo("      페이지 진단 없음 (이 패치 전에 등록된 파일 — ② 를 다시 돌리면 채워짐)")
                typer.echo(f"      적용 코드 {len(codes)}개: " + ", ".join(
                    f"{c.get('code')}(p.{c.get('page_in_drawing')})" if c.get("page_in_drawing") else str(c.get("code"))
                    for c in codes))
                typer.echo(f"      KKS 라인 {len(ex.get('kks_lines') or [])}개 · 검사율표 {len(ex.get('inspection_matrix') or [])}행")
                for r in (f.review_reasons_json or {}).get("reasons") or []:
                    typer.echo(f"      ! {r}")
            reqs = s.query(Requirement).filter(Requirement.drawing_set_id == ds.id).order_by(Requirement.joint_no).all()
            typer.echo(f"  요구사항 {len(reqs)}건:  라인 | 두께mm | 안전등급 | NDT 항목수")
            for r in reqs:
                n_ndt = len((r.required_ndt_json or {}).get("items") or [])
                typer.echo(f"    {r.line_no or r.joint_no:<24} {r.thickness_mm!s:>8} {r.safety_class or '-':>6} {n_ndt:>4}")
        typer.echo("\n두께는 DC 표의 '외경×두께' 뒤 숫자여야 한다 (3040×20 → 20). 3040 같은 큰 수가 보이면 외경이 들어간 것.")


@app.command("hcx-probe")
def hcx_probe(max_output: int = typer.Option(16, "--max-output", help="일부러 작게 줄 출력 상한 (토큰)")):
    """HCX v3 잘림 값 확인 — 출력 상한을 아주 작게 주고 finishReason 실제 문자열을 본다 (확인 목록 A-1)."""
    from app.hcx_client import probe_truncation
    out = probe_truncation(max_completion_tokens=max_output)
    typer.echo("HCX v3 잘림 프로브 (이 화면을 그대로 보낼 것):")
    for k, v in out.items():
        typer.echo(f"  {k:14} {v}")
    typer.echo("→ finish_reason 값이 '정상 종료' 와 다른 문자열이면 그것이 잘림 값. "
               "config/hcx.yaml models.truncation_values 에 그 문자열을 추가한다.")


@app.command("search-standards")
def search_standards(
    question: str = typer.Argument(..., help="한국어 질문 (예: 철근 용접부 VT 는 배치 표본검사인가)"),
    top_k: int = typer.Option(5, "--top-k", "-k"),
    bm25_only: bool = typer.Option(False, "--bm25-only", help="임베딩 없이 어휘 검색만"),
):
    """적재된 규격 문서에서 질문과 가장 가까운 조항을 보여준다 (검색 품질 확인용).

    임베딩(bge-m3)이 되면 source=hybrid, 안 되면 bm25 로 표시된다.
    """
    configure_logging(stage="search-standards")
    from app.extractors import code_indexer
    hits = code_indexer.search(question, top_k=top_k, hybrid=not bm25_only)
    if not hits:
        typer.echo("결과 없음 — 규격 문서가 적재되어 있는지(run_ingest_standards.bat) 확인하세요.")
        raise typer.Exit(1)
    typer.echo(f"질문: {question}")
    typer.echo(f"검색 방식: {hits[0]['source']}   (hybrid = BM25 + bge-m3 임베딩)\n")
    for i, h in enumerate(hits, 1):
        snippet = " ".join(h["text"].split())[:220]
        cos = f"  cos={h['cosine']:.3f}" if h.get("cosine") is not None else ""
        # 표 전사 청크는 신뢰도를 같이 보여준다. [재확인] 이 붙으면 원문 표를 사람이 봐야 한다.
        tag = ""
        if h.get("chunk_source") == "vlm_table":
            conf = h.get("confidence")
            conf_s = f"conf={conf:.2f}" if isinstance(conf, (int, float)) else "conf=?"
            tag = f"  [재확인 {conf_s}]" if h.get("needs_review") else f"  [표 전사 {conf_s}]"
        typer.echo(f"{i}. {h['doc']}  p.{h['page']}   bm25={h['bm25']:.2f}{cos}{tag}")
        typer.echo(f"   {snippet}…\n")


@app.command("reindex-standards")
def reindex_standards(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """이미 적재된 규격 문서에 bge-m3 임베딩을 (다시) 계산. PDF 재추출은 안 함."""
    configure_logging(stage="reindex-standards", verbose=verbose)
    from app.extractors import code_indexer
    from app import embeddings
    if not embeddings.available():
        typer.echo("임베딩 사용 불가 — config\\hcx.yaml 의 embedding.enabled 와 NDT_STUDIO_TOKEN 확인.")
        raise typer.Exit(1)
    res = code_indexer.reindex_embeddings()
    typer.echo(f"✓ 임베딩 완료 {len(res.get('embedded', []))}건")
    for d in res.get("embedded", []):
        typer.echo(f"  • {d}")
    if res.get("failed"):
        typer.echo(f"✗ 실패 {len(res['failed'])}건: {', '.join(res['failed'])}")
        raise typer.Exit(1)


@app.command("ingest-standards")
def ingest_standards(
    folder: Path = typer.Argument(..., help="기준문서 폴더"),
    doc_type: str = typer.Option(..., "--type", help="scwep | code | contract"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """기준 문서(SCWEP/Code/Contract) 폴더 적재."""
    configure_logging(stage=f"ingest-{doc_type}", verbose=verbose)
    init_db()
    folder = Path(folder)
    files = sorted([p for p in folder.rglob("*.pdf") if p.is_file()])
    stems = {p.stem for p in files}
    kept = []
    for p in files:
        # 스캔본·러시아어판 건너뛰기는 SCWEP 폴더 전제의 규칙이다. 규격·계약 폴더에서는
        # 같은 파일명 규칙이 성립하지 않아 멀쩡한 문서를 지운다 (2026-09-05 리뷰).
        why = _skip_reason(p, stems) if doc_type == "scwep" else None
        if why:
            typer.echo(f"  - {p.name}  ← 중복 제외: {why}")
        else:
            kept.append(p)
    files = kept
    if not files:
        typer.echo("적재할 PDF가 없습니다.")
        raise typer.Exit(1)

    if doc_type == "scwep":
        from app.extractors import scwep_parser as mod
    elif doc_type == "code":
        from app.extractors import code_indexer as mod
    elif doc_type == "contract":
        from app.extractors import contract_parser as mod
    else:
        typer.echo(f"알 수 없는 --type: {doc_type}")
        raise typer.Exit(1)

    total = {"pages": 0, "table_like": 0, "transcribed": 0, "needs_review": 0, "vlm_failed": 0}
    from app.progress_fmt import progress_line
    for fi, f in enumerate(files, start=1):
        typer.echo(progress_line("파일", fi, len(files), f.name))
        typer.echo(f"  • {f.name}")
        res = mod.ingest(f)
        ts = (res or {}).get("table_stats") if isinstance(res, dict) else None
        if ts:
            # 표 파이프라인 결과를 파일마다 바로 보여준다 — VLM실패 > 0 이면 ⑥ 연결 점검부터.
            typer.echo(f"      표: {ts['pages']}쪽 중 전사 {ts['transcribed']}, 재확인 {ts['needs_review']}, "
                       f"VLM실패 {ts['vlm_failed']} (표 후보 {ts['table_like']})")
            for k in total:
                total[k] += ts.get(k, 0)
    typer.echo(f"✓ {len(files)}개 적재 완료")
    if total["pages"]:
        typer.echo(f"  표 합계: 전사 {total['transcribed']} / 재확인 {total['needs_review']} / VLM실패 {total['vlm_failed']}")
        if total["vlm_failed"]:
            typer.echo("  ! VLM실패가 있습니다 — 그 페이지는 OCR 텍스트로 적재됐습니다. ⑥ HCX 연결 점검 후 다시 적재하세요.")


@app.command("review")
def review(
    billing: Path = typer.Option(..., help="청구 엑셀 .xlsx"),
    reports: Path = typer.Option(..., help="청구회차 성적서 PDF"),
    round_no: int = typer.Option(..., "--round", help="청구회차 번호 (1,2,...)"),
    date_str: str = typer.Option(..., "--date", help="청구일 YYYY-MM-DD"),
    discipline: str = typer.Option(..., help="CP-M1 | CP-P1 | CP-E1 | CP-A1"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """청구회차 1건 검토 — 엑셀 파싱 → 성적서 분할/정규화 → 3자 매칭 → 검토 엑셀 생성."""
    from app.analyzers.pipeline import run as run_review
    from app.extractors.excel_parser import parse_billing_xlsx
    from app.extractors.report_segmenter import normalize_and_ingest_segments, segment
    from app.report.excel_writer import write as write_excel

    from app import progress_writer as pw
    run_id = configure_logging(stage=f"review-r{round_no}-{discipline}", verbose=verbose)
    pw.step("review", "시작", f"{discipline} {round_no}차 {date_str}")
    typer.echo(f"  run_id={run_id}  (로그: data/logs/..., 진행 요약: data/progress.log)")
    init_db()
    billing = Path(billing)
    reports = Path(reports)
    billing_date = date.fromisoformat(date_str)

    # 1) 청구 회차 메타 + 엑셀 적재 (+ 분야별 시트의 Detailed Drawing 으로 보강)
    from app.extractors.excel_parser import enrich_from_per_method_sheets

    parsed = parse_billing_xlsx(billing, discipline_hint=discipline)
    enrichment = enrich_from_per_method_sheets(billing, discipline=discipline)

    # Total 시트 행에 drawing_no / welding_map 채우기 (report_no 기준 join)
    enriched_count = 0
    for r in parsed.rows:
        rn = r.get("report_no")
        if not rn:
            continue
        extra = enrichment.by_report_no.get(rn)
        if not extra:
            continue
        if not r.get("drawing_no") and extra.get("drawing_no"):
            r["drawing_no"] = extra["drawing_no"]
            enriched_count += 1
        # welding_map 은 raw_json 에 보관 (DB 컬럼 없음)
        if extra.get("welding_map"):
            r["raw_json"]["welding_map"] = extra["welding_map"]

    with get_session() as s:
        br = create_billing_round(
            s,
            round_no=round_no,
            discipline=discipline,
            billing_date=billing_date,
            billing_xlsx_path=str(billing),
            reports_pdf_path=str(reports),
        )
        s.flush()
        n = add_billing_items(s, br.id, parsed.rows)
        s.commit()
        round_id = br.id
        typer.echo(f"✓ 청구 행 {n}건 적재 (sheet={parsed.sheet_name}, header_row={parsed.header_row})")
        typer.echo(
            f"✓ 분야별 시트 {len(enrichment.source_sheets)}개에서 drawing_no/welding_map 보강 "
            f"→ {enriched_count}행 (총 {len(enrichment.by_report_no)}개 매핑)"
        )
        if parsed.warnings:
            for w in parsed.warnings:
                typer.echo(f"  ! {w}")
        for w in enrichment.warnings[:5]:
            typer.echo(f"  ! [enrich] {w}")

    # 2) 성적서 분할 + 정규화 + 적재
    from app.database.models import BillingRound

    # 청구서의 성적서번호 집합을 분할기에 넘긴다 — 헤더 OCR 오독(자릿수 삽입 등)을 닫힌 집합으로 가린다.
    import re as _re
    known_nos = {_re.sub(r"[\s\-_]", "", str(r.get("report_no") or "")).upper()
                 for r in parsed.rows if r.get("report_no")}
    segments = segment(reports, known_report_nos=known_nos or None)
    avg_conf = sum(seg.segmentation_confidence for seg in segments) / max(len(segments), 1)
    typer.echo(f"✓ 성적서 {len(segments)}건 분할 (평균 confidence {avg_conf:.2f})")
    with get_session() as s:
        br_loaded = s.get(BillingRound, round_id)
        meta = {
            "id": br_loaded.id,
            "round_no": br_loaded.round_no,
            "discipline": br_loaded.discipline,
            "billing_date": br_loaded.billing_date.isoformat(),
        }
        saved = normalize_and_ingest_segments(reports, segments, billing_round_meta=meta, session=s)
        s.commit()
        typer.echo(f"✓ 성적서 {saved}건 정규화/적재")

    # 3) 매칭 + 적합성 판정
    stats = run_review(round_id)
    typer.echo(f"✓ 검토 완료: {stats}")

    # 4) 검토 엑셀 출력
    out = write_excel(round_id)
    typer.echo(f"✓ 검토 엑셀: {out}")

    # 5) 진행 요약
    from app.hcx_client import get_call_stats
    pw.kpi("청구 행", stats.get("items_total"))
    pw.kpi("성적서", stats.get("reports"))
    # 실패 행 수도 남긴다 — 예전엔 화면에만 찍혀 사진 1장 피드백에서 사라졌다 (2026-09-05 리뷰)
    pw.kpi("처리 실패", stats.get("failed", 0))
    pw.kpi("HCX 호출", get_call_stats()["total"])
    pw.step("review", "완료", f"검토 엑셀: {out.name}")
    typer.echo(_usage_now())


@app.command("criteria-guide")
def criteria_guide(
    discipline: str = typer.Option(..., help="CP-M1 | CP-P1 | CP-E1 | CP-A1"),
    out_dir: Optional[Path] = typer.Option(None, help="출력 폴더 (기본 data/outputs/)"),
):
    """검사기준 가이드 생성 (검토자 핸드북, 시공사 협의 근거)."""
    from app.report.criteria_guide import generate

    configure_logging(stage=f"criteria-guide-{discipline}")
    stats = generate(discipline=discipline, out_dir=out_dir)
    typer.echo(f"✓ 가이드 생성 완료:")
    for k, v in stats.items():
        typer.echo(f"  {k}: {v}")


@app.command("ceb-crosscheck")
def ceb_crosscheck_cmd(
    reports_pdf: Path = typer.Option(..., help="CRST Civil 성적서 묶음 PDF"),
    ledger: Path = typer.Option(..., help="CRST 지급물량표 xlsx"),
    claims: Path = typer.Option(..., help="CEB Fabrication 청구 xlsm"),
    out: Optional[Path] = typer.Option(None, help="결과 JSON 경로 (기본 data/outputs/)"),
):
    """CEB 3자 수량 교차검증 — 성적서 × CEB 청구 × CRST 지급물량표.

    수량 사슬: 성적서(부재수) × CEB(부재당 용접점) = 물량표 VT = CEB 청구 VT.
    성적서 PDF 는 OCR 디스크 캐시 사용 (최초 실행 시 수십 분 소요 가능).
    """
    from datetime import date
    from app.analyzers.ceb_crosscheck import run_crosscheck
    from app.config import DATA_DIR

    configure_logging(stage="ceb-crosscheck")
    out = out or (DATA_DIR / "outputs" / f"ceb_crosscheck_{date.today().isoformat()}.json")
    res = run_crosscheck(reports_pdf, ledger, claims, out_json=out)

    typer.echo(f"\n══ CEB 3자 수량 교차검증 ══")
    typer.echo(f"  성적서 파싱: {res.n_reports}건 (CRST {res.n_crst}건)")
    typer.echo(f"  물량표 조인: {len(res.joined)}건")
    typer.echo(f"  수량 대사 일치:   {len(res.qty_match)}건")
    typer.echo(f"  ⚠ 수량 대사 불일치: {len(res.qty_mismatch)}건")
    typer.echo(f"  수량 대사 불가:   {len(res.qty_incalculable)}건 (unit 미확보·복수성적서 셀 등)")
    typer.echo(f"  ⚠ 미지급 후보(물량표 없음): {len(res.ledger_missing)}건")
    typer.echo(f"  ⚠ 부재수 초과(성적서>도면): {len(res.piece_overrun)}건")
    typer.echo(f"  {res.coverage_note}")
    typer.echo(f"  상세: {out}")
    if res.qty_mismatch:
        typer.echo("\n  [수량 불일치 상위 5]")
        for r in res.qty_mismatch[:5]:
            typer.echo(f"   - {r['report_no']}: 기대 {r['expected_points']:.0f} vs 물량표 {r['ledger_vt']} ({r['diff_pct']}%)")


@app.command("dashboard")
def dashboard(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8501),
):
    """Streamlit 대시보드 실행 (localhost:8501)."""
    script = PROJECT_ROOT / "app" / "dashboard" / "streamlit_app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(script),
        "--server.address", host, "--server.port", str(port),
        "--server.headless", "true",
    ]
    subprocess.run(cmd, check=False)


@app.command("ocr-check")
def ocr_check():
    """OCR(tesseract) 이 잡히는지 확인. 표 전사가 여기에 걸려 있어 적재 전에 본다."""
    import os as _os
    import subprocess
    from pathlib import Path
    from app.extractors.ocr_engine import resolve_tesseract

    configure_logging(stage="ocr-check")
    cmd_path, tessdata = resolve_tesseract()

    if not cmd_path:
        typer.echo("✗ tesseract 를 찾지 못했습니다.")
        typer.echo("  찾는 순서: 환경변수 NDT_TESSERACT_CMD → installer\\tesseract\\ → PATH")
        typer.echo("  번들에 들어 있어야 정상입니다. installer\\tesseract\\tesseract.exe 가 있는지 보세요.")
        typer.echo("  없으면 표 전사와 스캔 문서 OCR 이 통째로 동작하지 않습니다.")
        raise typer.Exit(1)

    env_cmd = _os.environ.get("NDT_TESSERACT_CMD")
    if env_cmd and env_cmd != cmd_path:
        typer.echo(f"환경변수  : {env_cmd}")
        typer.echo("            ↑ 이 경로에는 파일이 없어 무시했습니다. 고장은 아닙니다.")
        typer.echo("            set_env.bat 의 NDT_TESSERACT_CMD 줄이 낡은 것이니 지워도 됩니다.")
    typer.echo(f"실행 파일 : {cmd_path}" + ("  (번들)" if "installer" in (cmd_path or "") else "  (PATH)"))
    typer.echo(f"언어 데이터: {tessdata or '(기본 위치)'}")
    try:
        out = subprocess.run([cmd_path, "--version"], capture_output=True, text=True, timeout=30)
        typer.echo(f"버전      : {(out.stdout or out.stderr).splitlines()[0].strip()}")
    except Exception as e:      # noqa: BLE001
        typer.echo(f"✗ 실행에 실패했습니다: {type(e).__name__}: {e}")
        raise typer.Exit(1)

    langs = sorted(p.stem for p in Path(tessdata).glob("*.traineddata")) if tessdata else []
    typer.echo(f"언어      : {', '.join(langs) if langs else '(확인 못 함)'}")
    if "eng" not in langs:
        typer.echo("! eng.traineddata 가 없습니다 — 표 분류가 동작하지 않습니다.")
        raise typer.Exit(1)
    typer.echo("\n✓ OCR 정상. 표 전사를 쓸 수 있습니다.")


@app.command("hcx-check")
def hcx_check():
    """HCX 연결 점검 — 사내 최초 1회용. 토큰→방화벽→실호출 순서로 어디서 막히는지 판정."""
    import os
    import httpx
    from app.config import hcx_config
    from app.hcx_client import _mock_enabled, call

    configure_logging(stage="hcx-check")
    cfg = hcx_config()
    api_cfg = cfg.get("api", {})
    base_url = api_cfg.get("base_url", "")
    token_env = api_cfg.get("token_env", "NDT_HCX_TOKEN")
    ok = lambda m: typer.echo(f"  ✓ {m}")
    bad = lambda m: typer.echo(f"  ✗ {m}")

    typer.echo("HCX 연결 점검 (사내 최초 1회 권장)")
    typer.echo(f"  · 접속 주소: {base_url}")

    # 0) mock 모드
    if _mock_enabled():
        typer.echo("  · 현재 모의(mock) 모드 — 실서버에 접속하지 않고 파이프라인만 점검합니다.")
        resp = call("hcx_check", {"ping": 1}, force_refresh=True)
        ok(f"모의 응답 수신: {(resp.content or '')[:60]}")
        typer.echo("판정: 사외/리허설 환경 정상. 사내 실연결 점검은 NDT_HCX_MOCK 변수를 지우고 다시 실행.")
        return

    # 1) 토큰
    if not os.environ.get(token_env, ""):
        bad(f"API Key 미등록 — 환경변수 {token_env} 이 비어 있습니다.")
        typer.echo("판정: 가이드 1절대로 Windows [사용자 환경변수] 에 키 등록 → 로그아웃/재로그인 → 재실행.")
        raise typer.Exit(1)
    ok(f"API Key 등록됨 ({token_env})")

    # 2) 네트워크 도달 (방화벽) — 어떤 HTTP 응답이든 오면 도달 성공
    #
    # 2026-09-02 사내: curl 은 401 을 받는데 여기서만 ConnectTimeout 이 났다.
    # 원인 후보가 둘(프록시 환경변수 / 짧은 타임아웃)이라 둘 다 확인한다.
    #  - httpx 는 HTTPS_PROXY 등을 자동으로 따르므로 curl 과 경로가 달라질 수 있다
    #  - 사내는 TLS 재협상이 여러 번 돌아 8초가 빠듯하다 -> 25초
    proxy_vars = {k: v for k, v in os.environ.items()
                  if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") and v}
    if proxy_vars:
        typer.echo(f"  · 프록시 환경변수 감지: {', '.join(sorted(proxy_vars))}")

    last_err: Optional[Exception] = None
    reached_without_proxy = False
    for trust_env in (True, False):
        try:
            httpx.get(base_url, timeout=25.0, verify=False, trust_env=trust_env)
            reached_without_proxy = not trust_env
            ok("서버 도달 성공 (방화벽 열림)"
               + (" — 프록시를 무시했을 때만 성공" if reached_without_proxy else ""))
            last_err = None
            break
        except Exception as e:      # noqa: BLE001 - 원인 종류를 그대로 보여준다
            last_err = e
        if not proxy_vars:
            break                   # 프록시가 없으면 두 번 시도할 이유가 없다

    if last_err is not None:
        bad(f"서버 도달 실패: {type(last_err).__name__} — {str(last_err)[:120]}")
        typer.echo(f"  · 접속 시도 주소: {base_url}")
        typer.echo("판정: 아래 순서로 확인하세요.")
        typer.echo("  1) PowerShell 에서 curl.exe 로 같은 주소가 열리는지 확인")
        typer.echo(f"     curl.exe -k -i -m 20 {base_url}")
        typer.echo("  2) 401 이 나오면 방화벽은 정상입니다. 이 창의 오류 종류를 개발자에게 알려주세요.")
        typer.echo("  3) 아무 응답도 없으면 방화벽 미개통 — <internal-ip>:8443 아웃바운드 개통 요청.")
        raise typer.Exit(1)

    if reached_without_proxy:
        typer.echo("  · 프록시 환경변수가 접속을 막고 있습니다.")
        typer.echo("    config\\hcx.yaml 의 api.ignore_proxy 를 true 로 바꾸거나,")
        typer.echo("    환경변수 NDT_HCX_IGNORE_PROXY=1 을 등록하면 우회합니다.")

    # 3) 최소 실호출 (일일 카운트 1회 소모)
    try:
        resp = call("hcx_check", {"ping": 1}, force_refresh=True)
        ok(f"실호출 성공 — 모델={resp.model}, 응답: {(resp.content or '')[:60]}")
    except Exception as e:
        bad(f"실호출 실패: {e}")
        typer.echo("판정: 위 메시지에 인증(토큰)/한도/서버 원인이 표시됩니다.")
        typer.echo("      code=40001 이면 config\\hcx.yaml 의 models.reasoning_models 를 확인하세요.")
        typer.echo("      code=4010x 이면 환경변수 값에 키만 넣었는지 확인하세요 (Bearer·따옴표·공백 금지).")
        raise typer.Exit(1)

    # 4) Studio XP — 임베딩(bge-m3)은 운영 경로인데 지금까지 점검이 안 봤다. 일일 HCX 호출량 소모 없음.
    studio_ok = _studio_stage(ok, bad)
    if studio_ok:
        typer.echo("판정: HCX·Studio XP 연결 정상. 이제 소량(성적서 5~10건) 검토로 넘어가세요.")
    else:
        typer.echo("판정: HCX 는 정상, Studio XP(임베딩) 에 문제가 있습니다. 규격 검색이 BM25 만으로 돌아 "
                   "한국어 질의 성능이 떨어집니다. 위 ✗ 항목을 고친 뒤 다시 실행하세요.")
        raise typer.Exit(1)


def _studio_stage(ok, bad) -> bool:
    """hcx-check 4단계. studio_check.run() 결과를 사람이 읽을 문장으로. True = 이상 없음."""
    from app import studio_check
    typer.echo("Studio XP 점검 (임베딩 = 운영 경로)")
    try:
        res = studio_check.run(scan_stored=True)
    except Exception as e:      # noqa: BLE001 - 점검 코드의 예외가 트레이스백으로 사용자에게 가면 안 된다
        logging.getLogger("app.main").debug("studio_check.run 예외", exc_info=True)
        bad(f"Studio 점검 자체가 실패: {type(e).__name__}: {str(e)[:120]} — 이 줄을 개발자에게 보내주세요")
        return False
    typer.echo(f"  · 접속 주소: {res['base_url'] or '(없음)'}")
    if not res["enabled"]:
        typer.echo("  · embedding.enabled 가 꺼져 있어 건너뜁니다 (규격 검색은 BM25 만).")
        return True
    if not res["token_present"]:
        bad(f"토큰 없음: {res['embed']['reason']}")
        return False
    good = True
    m = res["models"]
    if m["ok"]:
        ok(f"서빙 모델 {len(m['served'])}개 조회: {', '.join(m['served'][:8])}{' …' if len(m['served']) > 8 else ''}")
        # Studio 채팅 provider 는 A/B 벤치마크 전용(stage_providers 비어 있음) — 운영 판정에 영향 없음.
        # 그래서 이름이 안 맞아도 경고만 하고 점검을 실패로 만들지 않는다. 임베딩 모델은 아래서 따로 본다.
        for name, model in m["missing"]:
            typer.echo(f"  ! provider '{name}' 의 모델 '{model}' 이 서빙 목록에 없음 — 벤치마크 전용이라 운영엔 무관. "
                       f"A/B 를 돌릴 때 hcx.yaml providers 의 model 이름을 위 목록에 맞추세요")
    else:
        bad(f"GET /v1/models 실패: {m['error']} — 방화벽(<internal-ip>:8443)·토큰 확인")
        good = False
    e = res["embed"]
    if e["ok"]:
        ok(f"임베딩 실호출 성공 — 모델={e['model']}, 차원={e['dim']}"
           + (f" (설정 dim={e['expected_dim']})" if e.get("expected_dim") else ""))
        if e["served"] is False:
            bad(f"임베딩 모델 '{e['model']}' 이 서빙 목록에 없음 — 호출은 됐지만 다른 모델로 답했을 수 있음")
            good = False
    else:
        bad(f"임베딩 실호출 실패: {e['reason']}")
        good = False
    st = res["stored"]
    if st:
        problems = [r for r in st if r["zero_rows"] or r["nan_rows"] or r["dim_drift"] or r["live_dim_drift"] or r["missing"]]
        if problems:
            for r in problems:
                why = []
                if r["missing"]: why.append("사이드카 없음")
                if r["zero_rows"]: why.append(f"0 벡터 {r['zero_rows']}행")
                if r["nan_rows"]: why.append(f"NaN {r['nan_rows']}행")
                if r["dim_drift"]: why.append(f"폭 {r['width']} ≠ 메타 {r['meta_dim']}")
                if r["live_dim_drift"]: why.append(f"폭 {r['width']} ≠ 현재 서버 {res['embed']['dim']}")
                bad(f"저장 벡터 이상 — {r['doc']}: {', '.join(why)}")
            typer.echo("    → python -m app.main reindex-standards 로 다시 계산하세요.")
            good = False
        else:
            ok(f"저장 벡터 {len(st)}개 문서 이상 없음")
    elif res.get("stored_note"):
        typer.echo(f"  · {res['stored_note']}")
    else:
        typer.echo("  · 저장된 규격 벡터 없음 (아직 ingest-standards 전이면 정상)")
    return good


# ─────────────────────────── debug-bundle / patch / version ───────────────────────────


debug_bundle_app = typer.Typer(no_args_is_help=True, help="사내 → 사외 재현용 디버그 번들")
app.add_typer(debug_bundle_app, name="debug-bundle")


@debug_bundle_app.command("export")
def debug_bundle_export(
    out: Path = typer.Option(Path("debug_bundle.zip"), help="출력 zip 경로"),
    days: int = typer.Option(7, help="포함할 로그 일수"),
    include_pdfs: bool = typer.Option(False, help="도면/성적서 PDF 포함 (기본 제외)"),
    include_sqlite: bool = typer.Option(False, help="SQLite DB 사본 포함 (도면 기준정보 포함 — 기본 제외)"),
    include_sensitive_cache: bool = typer.Option(
        False, help="도면 기준이 담긴 민감 stage LLM 캐시 포함 (기본 제외 — 도면 반출 지양)"
    ),
):
    """사내 PC: 로그+캐시+환경+DB 를 한 zip 으로 묶음 (토큰·민감 캐시 제외)."""
    from app.debug_bundle import export_bundle

    configure_logging(stage="debug-export")
    stats = export_bundle(
        out, include_pdfs=include_pdfs, include_sqlite=include_sqlite,
        include_sensitive_cache=include_sensitive_cache, days_of_logs=days,
    )
    typer.echo(f"✓ 번들 생성: {stats}")


@debug_bundle_app.command("import")
def debug_bundle_import(
    zip_path: Path = typer.Argument(..., help="가져올 debug bundle zip"),
    overwrite: bool = typer.Option(False, help="기존 data/ 와 config/ 를 덮어씀 (위험)"),
):
    """사외 mac: 번들 zip 을 풀어 동일 입력 재현 환경 구성."""
    from app.debug_bundle import import_bundle

    configure_logging(stage="debug-import")
    stats = import_bundle(zip_path, overwrite=overwrite)
    typer.echo(f"✓ 복원 완료: {stats['extracted_files']} files "
               f"(cache={stats['cache_files']}, logs={stats['log_files']})")
    if stats.get("note"):
        typer.echo(stats["note"])


patch_app = typer.Typer(no_args_is_help=True, help="사외 → 사내 코드 핫픽스 패치")
app.add_typer(patch_app, name="patch")


@patch_app.command("export")
def patch_export(
    out: Path = typer.Option(Path("patch.zip"), help="출력 zip"),
    baseline: Optional[Path] = typer.Option(
        None, help="비교 baseline manifest. 미지정 시 deployed_manifest.json"
    ),
):
    """사외 mac: 변경된 파일만 zip 으로 묶음."""
    from app.patch import export_patch

    configure_logging(stage="patch-export")
    stats = export_patch(out, baseline_manifest=baseline)
    typer.echo(
        f"✓ 패치 생성: {out}\n"
        f"  baseline={stats['baseline_version']}  current={stats['current_version']}\n"
        f"  added={stats['added']}  changed={stats['changed']}  removed={stats['removed']}\n"
        f"  size={stats['out_size_bytes']}B"
    )


@patch_app.command("export-text")
def patch_export_text(
    out: Path = typer.Option(Path("patch.txt"), help="출력 txt (결재 무관 확장자)"),
    note: str = typer.Option("", help="패치 설명 — 파일 머리말에 기록"),
    baseline: Optional[Path] = typer.Option(
        None, help="비교 baseline manifest. 미지정 시 deployed_manifest.json"
    ),
):
    """사외 mac: 변경된 파일을 .txt 한 장으로 묶음 (사내 결재 없이 반입)."""
    from app.config import PROJECT_ROOT
    from app.patch import _load_manifest, _scan_files
    from app.text_patch import export_patch as export_text

    configure_logging(stage="patch-export-text")
    base = {e.rel_path: e.sha256 for e in _load_manifest(baseline)}
    changed = [e.rel_path for e in _scan_files() if base.get(e.rel_path) != e.sha256]
    stats = export_text(PROJECT_ROOT, out, changed, note=note)

    typer.echo(f"✓ 텍스트 패치 생성: {out}  ({stats['out_size_bytes']:,}B, {len(stats['files'])}개 파일)")
    for rel in stats["files"]:
        typer.echo(f"    · {rel}")
    if stats["skipped"]:
        typer.echo("\n  ⚠ 제외됨 (필요하면 zip 패치로 정식 결재):")
        for rel, why in stats["skipped"]:
            typer.echo(f"    · {rel} — {why}")


@patch_app.command("apply-text")
def patch_apply_text(
    txt_path: Path = typer.Argument(..., help="적용할 patch txt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="실제 적용 안 함, 목록만 출력"),
):
    """사내 PC: 텍스트 패치를 적용. data/backups/textpatch-<ts>/ 에 백업."""
    from app.config import PROJECT_ROOT
    from app.text_patch import apply_patch as apply_text

    configure_logging(stage="patch-apply-text")
    stats = apply_text(PROJECT_ROOT, txt_path, dry_run=dry_run)
    head = "[미리보기] 실제로 바꾸지 않았습니다" if dry_run else "✓ 적용 완료"
    typer.echo(f"{head}  —  대상 {len(stats['applied'])}개 / 백업 {len(stats['backed_up'])}개")
    for rel in stats["applied"]:
        typer.echo(f"    · {rel}" + ("  (덮어씀)" if rel in stats["backed_up"] else "  (새 파일)"))
    if stats["rejected"]:
        typer.echo("\n  ⚠ 거부된 항목:")
        for rel, why in stats["rejected"]:
            typer.echo(f"    · {rel} — {why}")
    if not dry_run and stats["backed_up"]:
        typer.echo(f"\n  백업 위치: {stats['backup_root']}")


@patch_app.command("apply")
def patch_apply(
    zip_path: Path = typer.Argument(..., help="적용할 patch zip"),
    dry_run: bool = typer.Option(False, "--dry-run", help="실제 적용 안 함, 변경 목록만 출력"),
):
    """사내 PC: 패치 zip 을 적용. data/backups/patch-<ts>/ 에 기존 파일 백업."""
    from app.patch import apply_patch

    configure_logging(stage="patch-apply")
    try:
        stats = apply_patch(zip_path, dry_run=dry_run)
    except (ValueError, KeyError) as e:
        # 비개발자에게 파이썬 traceback 을 보이지 않는다.
        # 손으로 묶은 zip(적용안내.md 동봉형)을 여기 넣는 실수가 잦다.
        typer.echo(f"이 zip 은 자동 적용용 패치가 아닙니다: {e}")
        typer.echo("동봉된 '적용안내.md' 를 열어 손으로 덮어쓰는 방식인지 확인하세요.")
        typer.echo("자동 적용이 되는 패치는 'patch export' 로 만든 것뿐입니다.")
        raise typer.Exit(1)
    typer.echo(
        f"{'[DRY-RUN] ' if dry_run else ''}패치 적용 결과\n"
        f"  patched={len(stats['patched_files'])}  backed_up={len(stats['backed_up_files'])}\n"
        f"  to_remove(보존)={len(stats['removed_files'])}\n"
        f"  skipped(화이트리스트 밖)={len(stats['skipped_outside_whitelist'])}\n"
        f"  backup_root={stats['backup_root']}"
    )
    if stats["patched_files"]:
        typer.echo("\n변경 파일:")
        for rel in stats["patched_files"]:
            typer.echo(f"  - {rel}")
    if stats["skipped_outside_whitelist"]:
        typer.echo("\n[경고] 화이트리스트 밖 경로 (적용 안 됨):")
        for rel in stats["skipped_outside_whitelist"]:
            typer.echo(f"  ! {rel}")


@app.command("version")
def version_cmd():
    """버전·환경 정보 출력 (디버깅·결재 첨부용)."""
    from app.debug_bundle import _collect_environment

    env = _collect_environment()
    typer.echo("NDT Assistant 버전 / 환경")
    for k in ("app_version", "host", "os", "python"):
        typer.echo(f"  {k:<20}: {env[k]}")
    typer.echo(f"  config_hashes      : {env['config_hashes']}")
    typer.echo(f"  package_versions   : {env['package_versions']}")


if __name__ == "__main__":
    app()
