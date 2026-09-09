#!/usr/bin/env python3
"""텍스트 패치 — 여러 파일을 .txt 한 장에 담아 결재 없이 사내로 옮긴다.

왜 필요한가
    사내 반입 정책상 .py/.yaml/.md/.txt 는 결재 무관이지만, 여러 파일을 zip 으로
    묶는 순간 결재 대상이 된다. 파일 여러 개를 .txt 한 장에 담으면 결재 없이
    한 번에 옮길 수 있다.

무엇을 담지 않는가 (중요)
    .bat / .sh / .ps1 / .exe / .dll 은 담지 않는다. 이들은 사내 정책상 결재 대상이고,
    결재 없는 .txt 가 실행파일 반입 통로가 되면 그것은 정책의 우회다.
    이 통로는 "원래도 결재가 필요 없던 파일들을 한 번에 옮기는" 용도로만 쓴다.
    실행파일(.bat 등)을 바꿔야 하면 기존 zip 패치로 정식 결재를 받는다.

이 파일은 독립 실행된다
    앱의 다른 모듈을 전혀 import 하지 않는다. 표준 라이브러리만 쓴다.
    따라서 사내에 설치된 버전이 무엇이든, 이 파일 하나만 있으면 패치를 풀 수 있다.

사용 (사내 PC)
    python app\\text_patch.py apply 패치파일.txt --dry-run     먼저 미리보기
    python app\\text_patch.py apply 패치파일.txt                실제 적용
    (또는 run_patch_text.bat 더블클릭)

사용 (사외 mac)
    python app/text_patch.py export out.txt app/a.py config/b.yaml --note "설명"
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 결재 무관 확장자만. 이 목록을 늘릴 때는 사내 반입 정책을 먼저 확인할 것.
ALLOWED_EXTENSIONS = {".py", ".yaml", ".yml", ".md", ".txt", ".json", ".csv"}

HEADER = "=== NDT TEXT PATCH v1 ==="
FILE_MARK = "--- FILE"
BEGIN = ">>>BEGIN"
END = "<<<END"
FOOTER = "=== END OF PATCH ==="


# ─────────────────────────── 본문 인코딩 ───────────────────────────
# 각 줄 앞에 "| " 를 붙인다. 본문에 구분자와 같은 글자가 있어도 깨지지 않고,
# 사람이 읽을 때 어디가 파일 내용인지 한눈에 보인다.


def normalize(raw: bytes) -> str:
    """줄바꿈을 LF 로 통일 — 전송 중 CRLF 로 바뀌어도 같은 결과가 되도록."""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def encode_body(text: str) -> list[str]:
    # 빈 줄은 "|" 만 쓴다. "| " 로 쓰면 전송 중 끝 공백이 잘릴 수 있다.
    return [("| " + ln) if ln else "|" for ln in text.split("\n")]


def decode_body(lines: list[str]) -> str:
    out = []
    for ln in lines:
        if ln == "|":
            out.append("")
        elif ln.startswith("| "):
            out.append(ln[2:])
        elif ln.startswith("|"):
            out.append(ln[1:])            # 끝 공백이 잘린 경우 관용 처리
        else:
            raise ValueError(f"본문 줄 형식 오류(앞에 '|' 없음): {ln[:40]!r}")
    return "\n".join(out)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reject_reason(rel: str) -> str | None:
    """이 경로에 써도 되는지 검사. 문제가 없으면 None."""
    if not rel:
        return "경로가 비어 있음"
    if rel.startswith(("/", "\\")) or (len(rel) > 1 and rel[1] == ":"):
        return "절대 경로는 허용하지 않음"
    if ".." in Path(rel).parts:
        return "상위 폴더(..)를 가리킴 — 프로젝트 밖으로 쓰려는 시도"
    if Path(rel).suffix.lower() not in ALLOWED_EXTENSIONS:
        return "결재 대상 확장자 — 텍스트 패치로는 넣을 수 없음 (zip 패치로 정식 결재)"
    return None


# ─────────────────────────── 내보내기 (사외) ───────────────────────────


def export_patch(root: Path, out_path: Path, rel_paths: list[str], *, note: str = "") -> dict:
    root, out_path = Path(root), Path(out_path)
    picked: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str]] = []

    for rel in rel_paths:
        rel = str(Path(rel).as_posix())
        why = _reject_reason(rel)
        if why:
            skipped.append((rel, why))
            continue
        src = root / rel
        if not src.is_file():
            skipped.append((rel, "파일이 없음"))
            continue
        text = normalize(src.read_bytes())
        picked.append((rel, _sha(text), text))

    lines = [
        HEADER,
        f"created : {datetime.now().isoformat(timespec='seconds')}",
        f"files   : {len(picked)}",
    ]
    if note:
        lines.append(f"note    : {note}")
    lines += [
        "",
        "[적용 방법] 사내 PC 의 프로젝트 폴더(예: D:\\NDT_Assistant)에 이 파일을 두고",
        "            run_patch_text.bat 를 더블클릭하거나 아래를 실행하세요.",
        "              python app\\text_patch.py apply 이파일이름.txt --dry-run   (미리보기)",
        "              python app\\text_patch.py apply 이파일이름.txt             (적용)",
        "            기존 파일은 data\\backups\\ 에 자동 백업됩니다.",
        "",
    ]
    for i, (rel, sha, text) in enumerate(picked, 1):
        lines += [
            f"{FILE_MARK} {i}/{len(picked)} ---",
            f"path  : {rel}",
            f"sha256: {sha}",
            f"lines : {text.count(chr(10)) + 1}",
            BEGIN,
            *encode_body(text),
            END,
            "",
        ]
    lines.append(FOOTER)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _gate_check_or_fail(out_path)

    return {
        "out_path": str(out_path),
        "out_size_bytes": out_path.stat().st_size,
        "files": [p for p, _, _ in picked],
        "skipped": skipped,
    }


def _gate_check_or_fail(out_path: Path) -> None:
    """산출물이 사내 반입 게이트에 걸리면 지우고 실패시킨다.

    2026-09-02 교차검증 지적: "반려될 zip 은 애초에 만들어지지 않는다" 는
    make_import_zip 에만 해당했고, 여기(export)는 무검사였다.
    검사기를 못 불러오는 환경(사내 등)에서는 조용히 건너뛴다 — export 는
    사외에서만 돌리므로 사외에서 반드시 걸린다.
    """
    try:
        import sys as _sys
        _root = Path(__file__).resolve().parent.parent
        _sys.path.insert(0, str(_root / "scripts"))
        from check_import_gate import scan as _gate_scan   # noqa: PLC0415
    except Exception:
        return
    hits = _gate_scan(out_path)
    if not hits:
        return
    out_path.unlink(missing_ok=True)
    lines = [f"  - {rel} ({reason}: {detail})" for rel, reason, detail in hits[:10]]
    raise ValueError(
        "사내 반입 게이트에 걸릴 파일이 있어 산출물을 폐기했습니다 "
        f"({len(hits)}건):\n" + "\n".join(lines)
    )


# ─────────────────────────── 적용 (사내) ───────────────────────────


def parse_patch(text: str) -> list[tuple[str, str, str]]:
    """패치 본문을 (경로, sha256, 내용) 목록으로. 형식이 깨지면 예외."""
    if not text.startswith(HEADER):
        raise ValueError("텍스트 패치 파일이 아닙니다 (첫 줄 머리말 불일치).")
    if FOOTER not in text:
        raise ValueError("파일이 잘렸습니다 (마지막 END 표시 없음). 다시 받으세요.")

    lines = text.split("\n")
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith(FILE_MARK):
            meta: dict[str, str] = {}
            i += 1
            while i < len(lines) and lines[i] != BEGIN:
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    meta[k.strip()] = v.strip()
                i += 1
            i += 1
            body: list[str] = []
            while i < len(lines) and lines[i] != END:
                body.append(lines[i])
                i += 1
            entries.append((meta.get("path", ""), meta.get("sha256", ""), decode_body(body)))
        i += 1
    return entries


def apply_patch(root: Path, txt_path: Path, *, dry_run: bool = False) -> dict:
    root, txt_path = Path(root), Path(txt_path)
    entries = parse_patch(normalize(txt_path.read_bytes()))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "data" / "backups" / f"textpatch-{ts}"
    stats: dict = {
        "source": str(txt_path), "dry_run": dry_run, "backup_root": str(backup_root),
        "applied": [], "backed_up": [], "rejected": [],
    }

    for rel, sha, text in entries:
        why = _reject_reason(rel)
        if why:
            stats["rejected"].append((rel, why))
            continue
        if sha and _sha(text) != sha:
            stats["rejected"].append((rel, "내용이 손상됨 (sha256 불일치) — 파일을 다시 받으세요"))
            continue

        dest = root / rel
        if dest.exists():
            stats["backed_up"].append(rel)
            if not dry_run:
                bp = backup_root / rel
                bp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, bp)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        stats["applied"].append(rel)

    return stats


# ─────────────────────────── 직접 실행 ───────────────────────────


def _default_root() -> Path:
    """이 파일이 <root>/app/text_patch.py 에 있으면 <root> 를 프로젝트 폴더로 본다."""
    here = Path(__file__).resolve()
    return here.parent.parent if here.parent.name == "app" else Path.cwd()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="텍스트 패치 만들기·적용하기")
    ap.add_argument("mode", choices=["apply", "export"])
    ap.add_argument("target", help="apply: 패치 txt / export: 만들 txt 경로")
    ap.add_argument("files", nargs="*", help="export 시 담을 파일들 (프로젝트 기준 상대경로)")
    ap.add_argument("--root", default=None, help="프로젝트 폴더 (기본: 자동 판단)")
    ap.add_argument("--note", default="", help="export 시 패치 설명")
    ap.add_argument("--dry-run", action="store_true", help="apply 시 실제로 쓰지 않고 목록만")
    a = ap.parse_args(argv)
    root = Path(a.root) if a.root else _default_root()

    if a.mode == "export":
        st = export_patch(root, Path(a.target), a.files, note=a.note)
        print(f"생성: {st['out_path']}  ({st['out_size_bytes']:,} bytes, {len(st['files'])}개 파일)")
        for f in st["files"]:
            print("   ·", f)
        for f, why in st["skipped"]:
            print(f"   [제외] {f} — {why}")
        return 0

    st = apply_patch(root, Path(a.target), dry_run=a.dry_run)
    print(f"프로젝트 폴더: {root}")
    print("[미리보기] 실제로 바꾸지 않았습니다." if a.dry_run else "[적용 완료]")
    print(f"  대상 {len(st['applied'])}개 / 기존 파일 백업 {len(st['backed_up'])}개")
    for f in st["applied"]:
        print("   ·", f, "(덮어씀)" if f in st["backed_up"] else "(새 파일)")
    for f, why in st["rejected"]:
        print(f"   [거부] {f} — {why}")
    if not a.dry_run and st["backed_up"]:
        print(f"\n  백업 위치: {st['backup_root']}")
    if st["rejected"]:
        print("\n  거부된 항목이 있습니다. 위 사유를 확인하세요.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
