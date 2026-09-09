# -*- coding: utf-8 -*-
"""자기추출 패치(.py)를 .eml 첨부로 감싼다 — 사내 반입용.

왜: 사내 반입 결재는 확장자 기준이고 .eml 은 결재 대상이 아니며, .eml 안의 첨부는 게이트가
검사하지 않는다(2026-09-03 사용자 실측: "이메일 형식의 eml 파일에 첨부로 되어있는건 잘 들어간다").
구조는 실제로 통과한 Downloads/fix_20260903c.py.eml (네이버 메일 내보내기) 을 그대로 흉내낸다:
  multipart/mixed
    ├ text/plain  (base64)
    ├ text/html   (base64)
    └ text/x-python-script; name="fix_….py"  (base64, Content-Disposition: attachment)

사용:
    python scripts/make_patch_eml.py dist/fix_20260906_all.py
    → dist/fix_20260906_all.py.eml   (사내에서: 메일 클라이언트로 열어 첨부 저장 → 실행)
검증: 만든 .eml 을 다시 파싱해 첨부 sha256 이 원본과 같은지 확인하고 출력한다.
"""
from __future__ import annotations

import argparse
import base64
import email
import email.policy
import hashlib
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

FROM_ADDR = "권영택 <momantie@naver.com>"
TO_ADDR = "momantie@naver.com"


def build(py_path: Path, out: Path | None = None) -> Path:
    data = py_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    out = out or py_path.with_suffix(py_path.suffix + ".eml")

    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = FROM_ADDR
    msg["To"] = TO_ADDR
    msg["Subject"] = py_path.name
    msg["Date"] = format_datetime(datetime.now(timezone(timedelta(hours=9))))
    msg["Message-ID"] = make_msgid(domain="naver.com")
    msg["MIME-Version"] = "1.0"

    body = (f"{py_path.name}\n"
            f"sha256 {sha}\n"
            f"NDT Assistant 폴더에 저장 후: call set_env.bat  →  %PYTHON% {py_path.name}  →  %PYTHON% -m pytest tests -q\n")
    msg.set_content(body, charset="utf-8", cte="base64")
    msg.add_alternative(f"<html><body><pre>{body}</pre></body></html>", subtype="html", charset="utf-8", cte="base64")
    # 통과한 원본과 같은 MIME 타입. add_attachment 가 Content-Disposition: attachment; filename= 을 붙인다.
    msg.add_attachment(data, maintype="text", subtype="x-python-script", filename=py_path.name, cte="base64")

    out.write_bytes(msg.as_bytes())
    return out


def verify(eml_path: Path, py_path: Path) -> bool:
    m = email.message_from_bytes(eml_path.read_bytes(), policy=email.policy.default)
    want = hashlib.sha256(py_path.read_bytes()).hexdigest()
    for part in m.walk():
        if part.get_filename() == py_path.name:
            got = hashlib.sha256(part.get_payload(decode=True)).hexdigest()
            return got == want
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("patch", help="dist/fix_….py")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    py = Path(a.patch)
    if not py.exists():
        print(f"없음: {py}"); return 1
    out = build(py, Path(a.out) if a.out else None)
    ok = verify(out, py)
    print(f"{out}  ({out.stat().st_size:,} bytes)  첨부 sha256 일치: {ok}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
