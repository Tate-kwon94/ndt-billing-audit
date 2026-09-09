#!/usr/bin/env python3
"""반입 zip 을 여러 개의 '독립 zip' 으로 나눠서 만든다 (해외 회선 대응).

왜 이게 필요한가
    사외→사내 전송 회선이 느리고 중간에 끊긴다. 579MB 를 한 번에 올리면
    오래 걸리고 실패하면 처음부터 다시다.

왜 분할압축(.z01/.z02)을 쓰면 안 되는가
    ① `z01` `z02` `z03` 은 사내 전송 불가 확장자 목록에 있다.
    ② 분할압축 중간 조각은 zip 헤더가 없어 MIME 이 application/octet-stream 이 되고,
       위변조 검사에서 "정체 불명" 으로 막힌다. (2026-09-01 실측 확인)

이 스크립트가 하는 일
    파일을 크기별로 나눠 **각각 완전한 zip** 을 만든다. 조각이 아니라 온전한
    zip 이므로 MIME 은 application/zip 이고 위변조 검사를 통과한다.
    모든 파트를 같은 폴더에 풀면 원래 트리가 그대로 복원된다.

VS Code 는 왜 빠지는가
    `VSCode-win32-x64-1.135.0.zip` 은 292MB 짜리 **파일 하나**라 더 못 쪼갠다.
    풀어서 나누면 안에 있는 `.node` 25개(실체는 Windows DLL)가 검사 대상이 되어
    위변조로 막힌다. 지금은 중첩 zip 안이라 검사를 받지 않는다.
    → 기본 제외. AI 개발환경은 회선이 여유로울 때 따로 넣는다.

사용
    python scripts/make_import_parts.py                 # 40MB 단위, VS Code 제외
    python scripts/make_import_parts.py --max-mb 25     # 회선이 더 나쁠 때
    python scripts/make_import_parts.py --with-vscode   # VS Code 도 별도 파트로
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_import_gate import scan as gate_scan            # noqa: E402
from make_import_zip import (                              # noqa: E402
    INCLUDE_DIRS, INCLUDE_FILES, INCLUDE_REL_FILES, STAMP, keep,
)

OUTDIR = ROOT / "dist" / "parts"

# 파트를 나눌 때의 묶음 순서. 앞에 오는 것이 먼저 전송된다.
# 코드는 1MB 도 안 되므로 제일 먼저 보내 놓고 안내문을 읽게 한다.
GROUP_ORDER = [
    ("code",      "우리 코드·설정·실행파일·문서"),
    ("python",    "파이썬 설치본"),
    ("tesseract", "OCR 엔진"),
    ("wheels",    "파이썬 라이브러리"),
    ("vscode",    "VS Code + AI 도우미 (선택)"),
]


def group_of(rel: str) -> str:
    if rel.startswith("installer/vscode/"):     return "vscode"
    if rel.startswith("installer/python/"):     return "python"
    if rel.startswith("installer/tesseract/"):  return "tesseract"
    if rel.startswith("installer/wheels/"):     return "wheels"
    return "code"


def collect() -> list[tuple[str, Path]]:
    """반입 대상 (zip 안 경로, 실제 파일) 목록. make_import_zip 과 같은 기준."""
    out: list[tuple[str, Path]] = []
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and keep(p):
                out.append((p.relative_to(ROOT).as_posix(), p))
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists() and keep(p):
            out.append((p.name, p))
    for f in INCLUDE_REL_FILES:
        p = ROOT / f
        if p.exists() and keep(p):
            out.append((Path(f).as_posix(), p))
    return out


def measure(items, outdir: Path) -> dict[str, int]:
    """각 파일의 **압축 후** 크기를 잰다.

    원본 크기로 나누면 안 된다 — libtesseract-5.dll 은 원본 131MB 지만
    zip 안에서는 32MB 이고, 반대로 .whl 은 이미 압축이라 거의 안 줄어든다.
    """
    probe = outdir / "_measure.zip"
    with zipfile.ZipFile(probe, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, src in items:
            z.write(src, rel)
    with zipfile.ZipFile(probe) as z:
        sizes = {i.filename: i.compress_size + len(i.filename) + 76   # 헤더 몫 가산
                 for i in z.infolist() if not i.is_dir()}
    probe.unlink()
    return sizes


def plan_parts(items, sizes: dict[str, int], max_bytes: int):
    """그룹 순서를 지키며, 잰 크기로 파트 구성을 미리 확정한다."""
    parts: list[dict] = []
    for gname, gdesc in GROUP_ORDER:
        members = [it for it in items if group_of(it[0]) == gname]
        if not members:
            continue
        cur: list = []
        cur_size = 0
        for rel, src in members:
            s = sizes.get(rel, src.stat().st_size)
            if cur and cur_size + s > max_bytes:
                parts.append({"group": gname, "desc": gdesc, "items": cur})
                cur, cur_size = [], 0
            cur.append((rel, src))
            cur_size += s
        if cur:
            parts.append({"group": gname, "desc": gdesc, "items": cur})
    return parts


def write_part(path: Path, items) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, src in items:
            z.write(src, rel)          # zipfile 이 비ASCII 명에 UTF-8 flag 자동 설정


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_zip(path: Path, items) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, src in items:
            z.write(src, rel)          # zipfile 이 비ASCII 명에 UTF-8 flag 자동 설정


def guide_text(rows, max_mb: int) -> str:
    """part 01 에 같이 넣는 한글 안내문."""
    total = sum(r["size"] for r in rows)
    lines = [
        "# 나눠 보낸 반입 파일 — 받는 순서와 합치는 방법",
        "",
        f"전체 {len(rows)}개 파트, 합계 약 {total/1e6:,.0f} MB "
        f"(파트당 최대 {max_mb} MB).",
        "",
        "회선이 느리고 중간에 끊기는 문제 때문에 나눴습니다.",
        "**분할압축이 아니라 각각 온전한 zip** 이라 하나씩 따로 풀립니다.",
        "",
        "## 1. 이렇게 하세요",
        "",
        "1. 파트를 **번호 순서대로** 하나씩 사내로 전송합니다.",
        "2. 전부 도착하면 **한 폴더에 모아** 놓습니다.",
        "3. 각 zip 을 **같은 폴더에** 압축 해제합니다. (덮어쓰기 물어보면 예)",
        "4. 폴더 안의 `install.bat` 을 더블클릭합니다.",
        "",
        "순서가 뒤바뀌어도 됩니다. 전부 같은 폴더에 풀리기만 하면 됩니다.",
        "",
        "## 2. 파트 목록",
        "",
        "| 파트 | 내용 | 크기 | 파일 수 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| `{r['name']}` | {r['desc']} | {r['size']/1e6:,.1f} MB | {r['count']} |")
    lines += [
        "",
        "## 3. 도착한 파일이 깨지지 않았는지 확인 (선택)",
        "",
        "전송이 자주 끊긴다면 파트마다 확인하세요. PowerShell 에서:",
        "",
        "```",
        "certutil -hashfile 파일이름.zip SHA256",
        "```",
        "",
        "아래 값과 같으면 정상입니다. 다르면 그 파트만 다시 전송하세요.",
        "",
        "| 파트 | SHA-256 |",
        "|---|---|",
    ]
    for r in rows:
        lines.append(f"| `{r['name']}` | `{r['sha']}` |")
    lines += [
        "",
        "## 4. 자주 나오는 문제",
        "",
        "| 증상 | 원인 | 조치 |",
        "|---|---|---|",
        "| 압축이 안 풀림 | 전송 중 깨짐 | 위 SHA-256 확인 후 그 파트만 재전송 |",
        "| `install.bat` 이 파일이 없다고 함 | 파트 누락 | 위 목록과 대조해 빠진 파트 전송 |",
        "| 파트를 다른 폴더에 품 | 트리가 갈라짐 | 전부 같은 폴더에 다시 풀기 |",
        "",
        "## 5. AI 개발환경 (VS Code) 은?",
        "",
        "기본으로 빠져 있습니다. **없어도 도구는 100% 동작합니다.**",
        "VS Code 본체가 292MB 짜리 파일 하나라 더 쪼갤 수 없어서, 회선이",
        "여유로울 때 따로 넣는 편이 낫습니다.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-mb", type=int, default=40, help="파트 하나의 최대 크기 (기본 40)")
    ap.add_argument("--with-vscode", action="store_true", help="VS Code 도 파트로 포함")
    a = ap.parse_args()

    items = collect()
    if not a.with_vscode:
        items = [it for it in items if group_of(it[0]) != "vscode"]

    if OUTDIR.exists():
        shutil.rmtree(OUTDIR)
    OUTDIR.mkdir(parents=True)

    print("[1/3] 압축 후 크기 측정 중 ...")
    sizes = measure(items, OUTDIR)
    biggest_rel = max(sizes, key=lambda k: sizes[k])
    floor_mb = sizes[biggest_rel] / 1e6
    print(f"      최대 단일 파일 {floor_mb:,.1f} MB  ({biggest_rel})")
    if floor_mb > a.max_mb:
        print(f"\n[중단] 파일 하나가 파트 상한({a.max_mb} MB)보다 큽니다.")
        print(f"       --max-mb 를 {int(floor_mb)+2} 이상으로 지정하세요.")
        shutil.rmtree(OUTDIR)
        return 1

    parts = plan_parts(items, sizes, a.max_mb * 1_000_000)
    n = len(parts) + 1                      # +1 = 안내문이 들어갈 part 01

    print(f"[2/3] 파트 {n}개 생성 중 ...")
    rows = []
    for idx, part in enumerate(parts, start=2):
        final = OUTDIR / f"NDT_{STAMP}_{idx:02d}of{n:02d}_{part['group']}.zip"
        write_part(final, part["items"])
        rows.append({"name": final.name, "desc": part["desc"], "path": final,
                     "size": final.stat().st_size, "count": len(part["items"]),
                     "sha": sha256(final)})

    first_name = f"NDT_{STAMP}_01of{n:02d}_FIRST.zip"
    first = OUTDIR / first_name
    guide = OUTDIR / "_guide.md"
    rows_for_guide = [{"name": first_name, "desc": "이 안내문", "size": 0, "count": 1,
                       "sha": "(이 파일)"}] + rows
    guide.write_text(guide_text(rows_for_guide, a.max_mb), encoding="utf-8")
    with zipfile.ZipFile(first, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(guide, "파트_안내.md")
    guide.unlink()

    all_rows = [{"name": first_name, "desc": "안내문 (제일 먼저 전송)", "path": first,
                 "size": first.stat().st_size, "count": 1, "sha": sha256(first)}] + rows

    # 게이트 검사 — 파트 하나라도 걸리면 전부 폐기
    print("[3/3] 파트별 사내 반입 게이트 확인 ...")
    bad = False
    for r in all_rows:
        hits = gate_scan(r["path"])
        if hits:
            bad = True
            print(f"  [ERROR] {r['name']} — 반려될 파일 {len(hits)}개")
            for rel, reason, detail in hits[:5]:
                print(f"      {rel}  ({reason})")
    if bad:
        shutil.rmtree(OUTDIR)
        print("파트를 폐기했습니다.")
        return 1
    print("       전 파트 이상 없음 — OK\n")

    total = sum(r["size"] for r in all_rows)
    print(f"[완료] {OUTDIR}  —  {len(all_rows)}개 파트 / 합계 {total/1e6:,.0f} MB\n")
    print(f"  {'파일':<44}{'크기':>9}{'개수':>7}  내용")
    print("  " + "-" * 84)
    for r in all_rows:
        print(f"  {r['name']:<44}{r['size']/1e6:>7,.1f}MB{r['count']:>7}  {r['desc']}")
    if not a.with_vscode:
        print("\n  * VS Code + AI 도우미는 제외했습니다 (--with-vscode 로 포함 가능).")
    print("\n  전송 순서: 01 번을 먼저 보내고 압축을 풀면 나머지 안내가 들어 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
