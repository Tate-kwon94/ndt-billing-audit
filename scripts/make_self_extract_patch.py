# -*- coding: utf-8 -*-
"""자기추출 패치 생성기 — 사외 → 사내 텍스트 파일 운반.

왜 이 형식인가: 사내 반입 결재는 확장자 기준이다. .py/.yaml/.md/.json 은 결재 무관, .bat/.zip 은 결재.
그래서 바뀐 파일들을 .py 하나에 담아 보내고, 사내에서 그 .py 를 실행하면 자기 자리에 풀린다.
포맷은 dist/fix_20260905_scwep.py 와 동일 (zlib+base64 로 감싼 JSON 목록 [rel, sha256, base64]).

사용:
    python scripts/make_self_extract_patch.py --name 20260906_audit \
        --title "audit follow-ups" --expect "329 passed" app/matchers/deterministic.py ...
    → dist/fix_20260906_audit.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import zlib
from pathlib import Path

ALLOWED_EXT = (".py", ".yaml", ".md", ".json")

TEMPLATE = r'''# -*- coding: utf-8 -*-
"""NDT Assistant patch {name} -- {title} ({n} text files, no zip, no approval).

RUN (from the NDT Assistant folder that holds app\\ and config\\):
    call set_env.bat
    %PYTHON% fix_{name}.py
    %PYTHON% -m pytest tests -q
        OK when the LAST line contains no "failed" and no "error".
        If it does, copy the last 30 lines and send them to the developer.
        (FYI only, expected: {expect})
Every replaced file is first copied into {backup}\\ (own folder). To undo, copy them back.
"""
import base64, hashlib, json, os, shutil, sys, zlib

BACKUP_DIR = "{backup}"
ALLOWED_EXT = {allowed!r}

PAYLOAD = """\
{payload}"""


def fail(msg):
    print("[ERROR] " + msg); sys.exit(1)


def safe_dest(root, rel):
    if os.path.isabs(rel): fail("absolute path in payload: " + rel)
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts): fail("unsafe path in payload: " + rel)
    # "C:evil.py" is drive-qualified on Windows: os.path.join throws the root away.
    if any(":" in p for p in parts): fail("path escapes project folder: " + rel)
    if not rel.lower().endswith(ALLOWED_EXT): fail("disallowed extension in payload: " + rel)
    dest = os.path.realpath(os.path.join(root, *parts))
    try:
        inside = os.path.commonpath([dest, root]) == root
    except ValueError:            # different drive / mixed absolute-relative -> not comparable
        inside = False
    if not inside: fail("path escapes project folder: " + rel)
    return dest


def main():
    root = os.path.realpath(os.getcwd())
    if not (os.path.isdir(os.path.join(root, "app")) and os.path.isdir(os.path.join(root, "config"))):
        fail("Not the NDT Assistant folder: " + root + "\n       Move to the folder that holds app\\ and config\\ then run again.")
    if os.path.isdir(os.path.join(root, BACKUP_DIR)):
        fail(BACKUP_DIR + " already exists -- this patch was applied before. Rename or remove that folder if you really want to apply again.")
    entries = json.loads(zlib.decompress(base64.b64decode("".join(PAYLOAD.split()))).decode("ascii"))
    print("NDT Assistant patch {name} ({title})"); print("target folder : " + root)
    print("files to write: %d" % len(entries)); print("backup folder : " + BACKUP_DIR); print("")
    plan = []
    for rel, want_sha, blob in entries:
        data = base64.b64decode(blob)
        if hashlib.sha256(data).hexdigest() != want_sha: fail("checksum mismatch for " + rel + " -- damaged in transfer")
        plan.append((rel, want_sha, data, safe_dest(root, rel)))
    written = backed = 0
    for rel, want_sha, data, dest in plan:
        if os.path.exists(dest):
            bak = os.path.join(root, BACKUP_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(bak), exist_ok=True); shutil.copy2(dest, bak); backed += 1; mark = "replaced"
        else:
            mark = "new"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh: fh.write(data)
        with open(dest, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != want_sha: fail("write verification failed for " + rel)
        written += 1
        print("  %-8s %s" % (mark, rel.encode("ascii", "backslashreplace").decode("ascii")))
    print(""); print("[OK] wrote %d files, backed up %d into %s." % (written, backed, BACKUP_DIR))
    print("Next:  %PYTHON% -m pytest tests -q")
    print("       OK when the LAST line contains no 'failed' and no 'error'.")
    print("       If it does, copy the last 30 lines and send them to the developer.")
    print("       (FYI only, expected: {expect})")
    print("To undo: copy everything in " + BACKUP_DIR + " back over the originals.")


if __name__ == "__main__":
    main()
'''


def build(root: Path, files: list[str], *, name: str, title: str, expect: str, out: Path) -> Path:
    entries = []
    for rel in files:
        rel = rel.replace("\\", "/")
        if not rel.lower().endswith(ALLOWED_EXT):
            raise SystemExit(f"[ERROR] disallowed extension: {rel} (allowed {ALLOWED_EXT})")
        p = root / rel
        if not p.is_file():
            raise SystemExit(f"[ERROR] not a file: {p}")
        data = p.read_bytes()
        entries.append([rel, hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode("ascii")])
    raw = base64.b64encode(zlib.compress(json.dumps(entries).encode("ascii"), 9)).decode("ascii")
    payload = "\n".join(raw[i:i + 120] for i in range(0, len(raw), 120)) + "\n"
    text = TEMPLATE.format(name=name, title=title, n=len(entries), expect=expect,
                           backup=f"backup_{name}", allowed=ALLOWED_EXT, payload=payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--name", required=True, help="예: 20260906_audit → dist/fix_20260906_audit.py")
    ap.add_argument("--title", required=True)
    ap.add_argument("--expect", required=True,
                    help="참고용 pytest 기대 문구 (예: '329 passed'). 판정 기준이 아니라 안내문으로만 찍힌다 "
                         "— 판정은 '마지막 줄에 failed/error 가 없으면 정상'")
    ap.add_argument("--out", default=None)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    out = Path(a.out) if a.out else root / "dist" / f"fix_{a.name}.py"
    p = build(root, a.files, name=a.name, title=a.title, expect=a.expect, out=out)
    print(f"[OK] {p} ({p.stat().st_size:,} bytes, {len(a.files)} files)")


if __name__ == "__main__":
    main()
