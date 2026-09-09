"""NDT-3D CLI — DC PDF → 3D + HTML 한 줄.

사용:
  python -m ndt_3d.cli build samples/drawings/NP.D.P000.9.1ULD....DC.0001.E.pdf
  python -m ndt_3d.cli build <dc_pdf> --output data/outputs/3d/

  # 폴더 일괄
  python -m ndt_3d.cli batch samples/drawings/
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ndt_3d.coord_extractor import extract_drawing
from ndt_3d.pipe_builder import build_drawing_glb
from ndt_3d.sd_bg_parser import parse_sd, merge_into_dc_extract
from ndt_3d.viewer import build_html, _load_template

logger = logging.getLogger(__name__)


def cmd_build(args: argparse.Namespace) -> int:
    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"✗ 파일 없음: {pdf_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📄 DC PDF: {pdf_path.name}")
    print(f"📁 출력: {output_dir}")
    print()

    # 1. 좌표 추출 (DC)
    print("① 좌표·스풀·곡관 추출 중 (DC)...")
    extract = extract_drawing(pdf_path)
    extract_data = extract.to_dict()
    n_sheets = len(extract.sheets)
    n_spools = sum(len(s.spools) for s in extract.sheets)
    n_bends = sum(len(s.bends) for s in extract.sheets)
    print(f"   ✓ isometric 시트 {n_sheets}개, spool {n_spools}개, bend {n_bends}개")

    # 1b. SD 자동 발견 + valve 풀 spec join
    sd_path = Path(str(pdf_path).replace(".DC.", ".SD."))
    if sd_path.exists():
        print(f"① SD 발견 — valve 풀 spec join: {sd_path.name}")
        sd = parse_sd(sd_path)
        extract_data = merge_into_dc_extract(extract_data, sd)
        n_valves_join = sum(
            1 for s in extract_data["sheets"]
            for c in s.get("components", [])
            if c.get("sd_spec")
        )
        print(f"   ✓ {len(sd.valves)}개 valve SD spec 추출, {n_valves_join}개 DC 컴포넌트와 join")
    else:
        print(f"⚠ SD 파일 없음 — valve 풀 spec 누락 (찾으려 한 경로: {sd_path.name})")

    json_path = output_dir / f"{pdf_path.stem}_extract.json"
    json_path.write_text(
        json.dumps(extract_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if extract.needs_review:
        print(f"   ⚠ {len(extract.needs_review)}개 경고:")
        for w in extract.needs_review:
            print(f"      - {w}")

    # 2. 3D glb 생성 (vector graphics routing 활성)
    print()
    print("② 3D 메쉬 생성 중 (vector routing + 시트별 .glb)...")
    glb_subdir = output_dir / pdf_path.stem
    glb_results = build_drawing_glb(extract_data, glb_subdir, pdf_path=pdf_path)
    n_glb = sum(1 for r in glb_results.values() if r.get("glb_path"))
    print(f"   ✓ {n_glb}/{n_sheets}개 시트 3D 생성")
    failed = [(p, r) for p, r in glb_results.items() if not r.get("glb_path")]
    if failed:
        print(f"   ⚠ 실패 {len(failed)}개:")
        for page, r in failed:
            reasons = r.get("meta", {}).get("warnings", []) + r.get("meta", {}).get("needs_review", [])
            print(f"      p.{page}: {'; '.join(reasons) or '원인 불명'}")

    # 3. HTML 뷰어 (원본 페이지 PNG 임베드 → spec 검증 가능)
    print()
    print("③ HTML 뷰어 생성 중 (원본 PDF 페이지 ↔ 3D ↔ spec 표)...")
    html_path = output_dir / f"{pdf_path.stem}.html"
    build_html(
        extract_data, glb_results, html_path,
        source_pdf=pdf_path,
        render_dpi=args.dpi,
    )
    size_mb = html_path.stat().st_size / 1024 / 1024
    print(f"   ✓ {html_path} ({size_mb:.1f} MB)")

    print()
    print(f"🎉 완료. 결과:")
    print(f"   추출 JSON: {json_path}")
    print(f"   3D 폴더:   {glb_subdir}")
    print(f"   HTML:      {html_path}")
    print()
    print(f"브라우저로 HTML 열기:")
    print(f"   open '{html_path}'  (mac)")
    print(f"   start '{html_path}'  (Windows)")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"✗ 폴더 없음: {folder}", file=sys.stderr)
        return 2
    dc_files = sorted(folder.glob("*.DC.*.pdf"))
    if not dc_files:
        print(f"✗ DC PDF 없음: {folder}", file=sys.stderr)
        return 2
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📁 폴더 일괄 처리: {folder}")
    print(f"📄 발견된 DC PDF: {len(dc_files)}개")
    print()

    results = []
    for pdf in dc_files:
        print(f"━━━ {pdf.name} ━━━")
        sub_args = argparse.Namespace(
            pdf=str(pdf), output=str(output_dir), dpi=args.dpi,
        )
        try:
            rc = cmd_build(sub_args)
            results.append((pdf.name, rc == 0))
        except Exception as e:
            print(f"  ✗ 처리 실패: {e}")
            results.append((pdf.name, False))
        print()

    print("━━━━━━ 일괄 처리 요약 ━━━━━━")
    for name, ok in results:
        status = "✓" if ok else "✗"
        print(f"  {status}  {name}")
    n_ok = sum(1 for _, ok in results if ok)
    print(f"\n{n_ok}/{len(results)}개 성공")
    # 인덱스 HTML 생성
    index_html = output_dir / "index.html"
    html_links = []
    for pdf in dc_files:
        html = output_dir / f"{pdf.stem}.html"
        if html.exists():
            # 태그는 templates/ 에서 읽는다 - .py 앞부분에 HTML 이 있으면
            # 사내 반입 게이트가 파일을 HTML 로 보고 위변조로 반려한다.
            html_links.append(
                _load_template("index_item.html")
                .replace("__HREF__", html.name)
                .replace("__LABEL__", pdf.name)
            )
    index_html.write_text(
        _load_template("index.html")
        .replace("__COUNT__", f"{n_ok}/{len(results)}개")
        .replace("__ITEMS__", "\n".join(html_links)),
        encoding="utf-8",
    )
    print(f"\n인덱스: {index_html}")
    return 0 if n_ok == len(results) else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="ndt_3d",
        description="NDT-3D — DC 도면 → 시공 보조 3D 시각화",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="DC PDF 1개 → 3D + HTML")
    p_build.add_argument("pdf", help="DC PDF 경로")
    p_build.add_argument(
        "--output", "-o", default="data/outputs/3d",
        help="출력 폴더 (기본: data/outputs/3d)",
    )
    p_build.add_argument(
        "--dpi", type=int, default=110,
        help="원본 PDF 페이지 렌더 DPI (기본 110)",
    )
    p_build.set_defaults(func=cmd_build)

    p_batch = sub.add_parser("batch", help="폴더 안 모든 DC PDF 일괄")
    p_batch.add_argument("folder", help="DC PDF 들이 있는 폴더")
    p_batch.add_argument("--output", "-o", default="data/outputs/3d")
    p_batch.add_argument("--dpi", type=int, default=110)
    p_batch.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
