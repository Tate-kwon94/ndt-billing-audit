#!/usr/bin/env python3
"""반입 게이트 사전 검사 — 사내 위변조 검사에 걸릴 파일을 미리 찾는다.

배경 (2026-09-01 실제 반려 45건 / 2026-09-02 교차검증으로 규칙 확정)
    사내 반입 심사는 zip 안의 파일마다 "확장자"와 "실제 내용(MIME)"을 비교해
    등록되지 않은 조합이면 **위변조 파일**로 표시하고 반입을 막는다.

    확인된 통과 조합 (실제 반입 심사를 통과한 것만)
        .whl .zip     -> application/zip
        .py           -> text/plain, text/x-script.python
        .bat          -> text/x-msdos-batch
        .md .txt .yaml .lock -> text/plain
        .json         -> application/json, text/plain
        .exe .dll     -> application/x-dosexec
        .jar          -> application/java-archive, application/zip
        .traineddata  -> application/x-matlab-data, application/octet-stream
        .ttf          -> font/sfnt
        .html         -> text/html, text/plain   (2026-09-02 재제출로 확인)

    확인된 차단 유형
        확장자 없음        VERSION, tessdata/configs/*
        0 바이트 파일      빈 __init__.py -> application/x-empty
        점으로 시작        .continueignore
        미등록 확장자      .node .vsix .train .images .stderr .nochop .user-*
        내용 불일치        viewer.py = text/html, *.vsix = application/zip

    게이트가 하지 않는 것
        - 중첩 zip 내부는 열지 않는다 (그래서 .node 를 zip 안 zip 으로 넣는다)
        - HTML 탐지는 파일 앞 4096 바이트만 본다

★ 2026-09-02 정정 — 게이트가 "우리보다 공격적"인 것이 아니었다.
    로컬 `file` 이 구 viewer.py 를 text/plain 이라 한 것은 magic DB 의 python
    규칙(`0 string/t` + 삼중따옴표, 오프셋 0 / 高strength)이 sgml 의
    `search/4096`(strength 거의 0) 규칙보다 먼저 매치돼 HTML 판정을 가렸기
    때문이다. sgml 규칙만 적용하면 게이트와 정확히 일치한다:

        file -m /usr/share/file/magic/sgml --mime-type -b <파일>

    즉 게이트는 표준 libmagic 그대로 행동하며 로컬 재현이 가능하다.
    이 검사기는 sgml 단독 판정을 1순위로 쓰고, 그것을 못 구하는 환경에서만
    토큰 정규식으로 대체한다 (263개 파일 대조에서 두 방식 불일치 0건).

사용
    python scripts/check_import_gate.py .
    python scripts/check_import_gate.py dist/NDT_Assistant_import_YYYYMMDD.zip
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# 확장자별로 "이 MIME 이면 통과". **실제 반입 심사에서 확인된 조합만** 넣는다.
#
# 2026-09-02 교차검증에서 근거 없이 들어가 있던 4종을 제거했다:
#   .py -> text/x-python / .bat -> text/plain /
#   .md -> text/markdown / .yaml -> application/x-yaml, text/x-yaml
# 반려목록 45건에도 통과목록 263건에도 없던 값이라, 남겨두면 검사기는
# 통과시키고 게이트는 반려하는 구멍이 된다. 실제 사례가 나오면 그때 추가한다.
EXPECTED: dict[str, set[str]] = {
    # --- [확인] 실제 반입 심사 통과 ---
    ".whl":  {"application/zip"},
    ".zip":  {"application/zip"},
    ".py":   {"text/plain", "text/x-script.python"},
    ".bat":  {"text/x-msdos-batch"},
    ".md":   {"text/plain"},
    ".json": {"application/json", "text/plain"},
    ".yaml": {"text/plain"},
    ".txt":  {"text/plain"},
    ".lock": {"text/plain"},
    ".exe":  {"application/x-dosexec"},
    ".dll":  {"application/x-dosexec"},
    ".jar":  {"application/java-archive", "application/zip"},
    ".ttf":  {"font/sfnt"},
    ".traineddata": {"application/x-matlab-data", "application/octet-stream"},
    ".html": {"text/html", "text/plain"},
    # --- [추정] 아직 실물 확인 안 됨. 반려되면 지운다 ---
    ".yml":  {"text/plain"},
    ".csv":  {"text/plain", "text/csv"},
    ".png":  {"image/png"},
    ".jpg":  {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".pdf":  {"application/pdf"},
    ".docx": {"application/zip",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/zip",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}

# 게이트가 "HTML 문서"로 볼 표시. libmagic 의 sgml 규칙과 같은 토큰 집합이다.
# 태그 문자열을 그대로 적으면 이 검사기 자신이 HTML 로 오판되므로 조각내 조립한다.
_LT = b"<"
HTML_TOKENS = re.compile(
    _LT + rb"!doctype\s+html|" + _LT + rb"html|" + _LT + rb"head[\s>]|"
    + _LT + rb"title[\s>]|" + _LT + rb"body[\s>]|" + _LT + rb"script[\s>]|"
    + _LT + rb"style[\s>]|" + _LT + rb"table[\s>]|" + _LT + rb"a\s+href=",
    re.I,
)
HTML_SNIFF_BYTES = 4096
HTML_OK_EXT = {".html", ".htm"}

# HTML 오판이 실제로 문제가 되는 텍스트 확장자만 검사 대상으로 본다.
# 바이너리는 선두 시그니처로 먼저 판정되므로 안쪽 바이트열은 오탐이고,
# 통째로 읽으면 293MB zip 에서 메모리가 튄다.
TEXTY_EXT = {".py", ".txt", ".md", ".json", ".yaml", ".yml", ".csv",
             ".bat", ".ps1", ".sh", ".cfg", ".ini", ".lock", ".html", ".htm"}

# 확장자와 MIME 이 어긋나도 문제 삼지 않을 항목 (사유를 반드시 적을 것)
KNOWN_OK: set[str] = set()


def _sgml_magic_db() -> str | None:
    """sgml 규칙만 담긴 magic 파일 경로. 없으면 None."""
    for c in ("/usr/share/file/magic/sgml", "/usr/share/misc/magic/sgml"):
        if os.path.exists(c):
            return c
    return None


_SGML_DB = _sgml_magic_db()


def mime_sgml(path: Path) -> str | None:
    """sgml 규칙만 적용한 libmagic 판정 = 사내 게이트 재현. 못 구하면 None."""
    if not _SGML_DB:
        return None
    try:
        r = subprocess.run(["file", "-m", _SGML_DB, "--mime-type", "-b", str(path)],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def gate_ext(name: str) -> str:
    """게이트가 보는 확장자 = 마지막 점 뒤. 점으로 시작하는 파일도 그렇게 본다.

    실제 반려 목록에서 `.continueignore` 는 확장자 'continueignore',
    `box.train.stderr` 는 'stderr' 로 표시되었다 — 둘 다 마지막 점 기준.
    """
    return name[name.rfind("."):].lower() if "." in name else ""


def mime_of(path: Path) -> str:
    """libmagic 으로 MIME 판정. file 명령이 없으면 빈 파일 여부만 본다."""
    if path.stat().st_size == 0:
        return "application/x-empty"
    try:
        r = subprocess.run(["file", "--mime-type", "-b", str(path)],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            m = r.stdout.strip()
            return "application/x-empty" if m == "inode/x-empty" else m
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "(판정 불가 - file 명령 없음)"


def html_token_pos(path: Path, limit: int | None = None) -> int | None:
    """첫 HTML 태그의 바이트 위치. 없으면 None.

    차단 판정에는 `limit` 만큼만 읽는다 — 파일을 통째로 메모리에 올리지
    않기 위해서다. 뒤쪽 경고 판정에만 전체를 읽는다.
    """
    try:
        with path.open("rb") as f:
            data = f.read() if limit is None else f.read(limit)
    except OSError:
        return None
    m = HTML_TOKENS.search(data)
    return m.start() if m else None


def html_verdict(path: Path) -> tuple[bool, int | None, str]:
    """게이트가 이 파일을 HTML 로 볼 것인가.

    반환: (HTML 로 보임, 토큰 위치, 판정근거)
    sgml 단독 libmagic 이 있으면 그것이 정답이다 (게이트 재현).
    없으면 토큰 정규식으로 대체한다.
    """
    pos = html_token_pos(path, HTML_SNIFF_BYTES + 64)
    by_regex = pos is not None and pos < HTML_SNIFF_BYTES
    sgml = mime_sgml(path)
    if sgml is None:
        return by_regex, pos, "토큰 정규식"
    return sgml == "text/html", pos, "sgml libmagic"


def check_file(rel: str, path: Path) -> tuple[str, str] | None:
    """문제가 있으면 (사유, 상세) 반환."""
    if rel in KNOWN_OK:
        return None
    name = os.path.basename(rel)
    mime = mime_of(path)

    if mime == "application/x-empty":
        return ("빈 파일", f"내용이 없어 {name} 의 확장자를 증명할 수 없음")
    if name.startswith("."):
        return ("점으로 시작하는 파일",
                f"게이트가 확장자를 '{name[1:]}' 로 읽음 - MIME {mime}")
    if "." not in name:
        return ("확장자 없음", f"MIME {mime} - 게이트가 확장자를 못 읽음")

    ext = gate_ext(name)
    if ext in TEXTY_EXT and ext not in HTML_OK_EXT:
        is_html, pos, how = html_verdict(path)
        if is_html:
            where = f"{pos:,}바이트 지점" if pos is not None else "앞부분"
            return ("HTML 로 오판될 내용",
                    f"{where}에 HTML 태그 - {how} 판정 text/html. "
                    f"로컬 기본 판정은 {mime} 이지만 게이트는 반려한다. "
                    f"HTML 은 .html 파일로 분리할 것")

    ok = EXPECTED.get(ext)
    if ok is None:
        return ("모르는 확장자", f"{ext} - MIME {mime}. 게이트 판정표에 없음")
    if mime not in ok:
        return ("확장자와 내용 불일치",
                f"{ext} 인데 MIME 은 {mime} (기대: {', '.join(sorted(ok))})")
    return None


def check_warning(rel: str, path: Path) -> tuple[str, str] | None:
    """지금은 통과하지만 위태로운 항목 (반입은 막지 않는다)."""
    name = os.path.basename(rel)
    ext = gate_ext(name)
    if ext in HTML_OK_EXT or ext not in TEXTY_EXT:
        return None
    pos = html_token_pos(path)          # 뒤쪽까지 봐야 하므로 전체 읽기
    if pos is not None and pos >= HTML_SNIFF_BYTES:
        return ("HTML 이 뒤쪽에 있어 지금은 통과",
                f"{pos:,}바이트 지점 - 게이트 탐색 범위 {HTML_SNIFF_BYTES:,}바이트 밖. "
                f"이 파일 앞부분이 길어지거나 HTML 이 위로 올라오면 반려된다")
    return None


def _shipped_files() -> list[tuple[str, Path]] | None:
    """반입 zip 에 실제로 들어가는 파일 목록. 못 구하면 None.

    2026-09-02 교차검증 지적: 폴더 검사 모드가 자체 skip 목록을 갖고 있어
    (a) 실제로 zip 에 들어가는 파일을 조용히 건너뛰거나
    (b) 반입하지도 않는 빌드툴(.sh/.ps1)을 반려로 세는
    두 방향의 오차가 모두 있었다. 목록을 이중관리하지 않고
    make_import_parts.collect() 하나만 신뢰한다.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from make_import_parts import collect                     # noqa: PLC0415
        return collect()
    except Exception:
        return None


def iter_targets(target: Path):
    """검사 대상 (상대경로, 실제경로) 를 순회. zip 이면 임시 폴더에 풀어서 본다."""
    if target.is_dir():
        root = Path(__file__).resolve().parent.parent
        if target.resolve() == root:
            shipped = _shipped_files()
            if shipped is not None:
                yield from shipped
                return
        # 프로젝트 루트가 아니거나 목록을 못 구한 경우의 보수적 대체 경로
        for p in sorted(target.rglob("*")):
            if p.is_file() and not any(
                    s in p.parts for s in ("__pycache__", ".git", "venv",
                                           ".pytest_cache", "dist")):
                yield str(p.relative_to(target)), p
        return
    if not zipfile.is_zipfile(target):
        # zip 이 아닌 단일 파일 (예: 텍스트 패치 .txt) — 그 파일만 검사한다.
        yield target.name, target
        return
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(target) as z:
            z.extractall(td)
        base = Path(td)
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(base)), p


def scan(target: Path) -> list[tuple[str, str, str]]:
    """문제 목록 [(경로, 사유, 상세)] 를 돌려준다. 다른 스크립트에서도 쓴다."""
    out = []
    for rel, path in iter_targets(target):
        hit = check_file(rel, path)
        if hit:
            out.append((rel, hit[0], hit[1]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="검사할 zip 파일 또는 폴더")
    ap.add_argument("--quiet", action="store_true", help="사유별 5개까지만 표시")
    a = ap.parse_args()

    target = Path(a.target)
    if not target.exists():
        print(f"[중단] 없는 경로: {target}")
        return 2

    total = 0
    problems: list[tuple[str, str, str]] = []
    warnings: list[tuple[str, str, str]] = []
    for rel, path in iter_targets(target):
        total += 1
        hit = check_file(rel, path)
        if hit:
            problems.append((rel, hit[0], hit[1]))
            continue
        warn = check_warning(rel, path)
        if warn:
            warnings.append((rel, warn[0], warn[1]))

    print(f"검사 대상: {target}")
    print(f"HTML 판정: {'sgml libmagic (게이트 재현)' if _SGML_DB else '토큰 정규식 (대체)'}")
    print(f"파일 {total:,}개 - 반려 확실 {len(problems):,}개 / 주의 {len(warnings):,}개\n")

    def dump(items):
        for rel, reason, detail in items:
            print(f"    {rel}")
            print(f"      -> {detail}")

    if problems:
        by_reason: dict[str, list[tuple[str, str, str]]] = {}
        for row in problems:
            by_reason.setdefault(row[1], []).append(row)
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"■ {reason} - {len(items)}개")
            dump(items[:5] if a.quiet else items)
            if a.quiet and len(items) > 5:
                print(f"    ... 외 {len(items)-5}개")
            print()

    if warnings:
        print("△ 주의 - 지금은 통과하지만 나중에 반려될 수 있는 파일")
        dump(warnings)
        print()

    if problems:
        print("이 목록을 비운 뒤 반입하세요.")
        print("남겨야 하는 항목은 KNOWN_OK 에 사유와 함께 추가합니다.")
        return 1

    print("반려가 확실한 항목은 없습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
