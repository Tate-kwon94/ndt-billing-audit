#!/usr/bin/env bash
# NDT Assistant — 사내 반입 zip 생성 (얇은 래퍼)
#
# 이 스크립트는 make_import_zip.py 를 호출한다. 직접 `zip` 명령을 쓰지 않는다.
# 이유: macOS 의 `zip` 은 비ASCII 파일명에 UTF-8 flag(bit 11)를 세우지 않아,
#       한국어 Windows 의 Expand-Archive/tar 가 한글 파일명(installer/*.md)을
#       CP949 로 오독해 추출이 깨진다 (VM 리허설 발견 1 = 블로커).
#       Python zipfile 은 UTF-8 flag 를 자동 설정하므로 이 문제를 근본 해결한다.
#
# 실행:  bash scripts/make_import_zip.sh
# 산출:  dist/NDT_Assistant_import_YYYYMMDD.zip  (make_import_zip.py 와 동일)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"
exec "$PY" "$ROOT/scripts/make_import_zip.py" "$@"
