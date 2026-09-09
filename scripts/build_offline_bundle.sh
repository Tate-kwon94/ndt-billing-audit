#!/usr/bin/env bash
# NDT Assistant — 오프라인 번들 빌드 (macOS/Linux 에서 Windows 타겟용)
#
# 실행:
#   bash scripts/build_offline_bundle.sh
#
# 산출:
#   installer/python/python-3.11.x-amd64.exe   (정식 인스톨러 — tkinter·pip 포함)
#   installer/wheels/*.whl                      (win_amd64/cp311, marker 보정 포함)
#   requirements-win.lock                       (wheel 파일명 고정 목록)
#   VERSION + deployed_manifest.json            (사내 patch apply 의 baseline)
#
# ⚠ embeddable 배포판은 사용하지 않는다 — tkinter(GUI)·ensurepip(pip 부트스트랩)
#   미포함이라 사내 설치가 깨진다 (2026-08-30 반입 타당성 검토 결론).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/installer"
WHEELS="$INSTALLER/wheels"

PY_VERSION="3.11.9"
PY_INSTALLER_URL="https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-amd64.exe"

mkdir -p "$WHEELS" "$INSTALLER/python" "$INSTALLER/tesseract/tessdata"

# 1) Python 정식 인스톨러 (사용자 계정 silent 설치 지원, tkinter·pip 포함)
if [ ! -f "$INSTALLER/python/python-$PY_VERSION-amd64.exe" ]; then
    echo "[1/4] Python $PY_VERSION 정식 인스톨러 다운로드 ..."
    curl -fsSL "$PY_INSTALLER_URL" -o "$INSTALLER/python/python-$PY_VERSION-amd64.exe"
else
    echo "[1/4] Python 인스톨러 이미 존재 — 건너뜀"
fi

# 2) Wheels — Windows x64 / Python 3.11 타겟 (.tar.gz 반입 불가 → only-binary 강제)
REQ_FILE="$ROOT/requirements.txt"
if [ -f "$ROOT/requirements-win.txt" ]; then
    REQ_FILE="$ROOT/requirements-win.txt"
    echo "  → 경량 requirements-win.txt 사용 (Windows 반입용)"
fi
echo "[2/4] $(basename "$REQ_FILE") → win_amd64 / cp311 wheel 다운로드 ..."
python3 -m pip download \
    -r "$REQ_FILE" \
    --dest "$WHEELS" \
    --platform win_amd64 \
    --python-version 3.11 \
    --only-binary=:all: \
    --implementation cp

# 2-a) ⚠ marker 함정 보정: pip 은 환경 marker 를 "실행 호스트(mac)" 기준으로 평가하므로
#      Windows 전용 조건부 의존성이 조용히 빠진다. 반드시 명시 다운로드 (검토 실증 3종):
#        colorama  (pytest: sys_platform=="win32")
#        watchdog  (streamlit: platform_system!="Darwin")
#        greenlet  (SQLAlchemy: platform_machine==AMD64 — 호스트가 arm64 라 skip 됨)
echo "  → Windows 전용 marker 의존성 명시 다운로드 (colorama/watchdog/greenlet)"
python3 -m pip download colorama watchdog greenlet --no-deps --dest "$WHEELS" \
    --platform win_amd64 --python-version 3.11 --only-binary=:all: --implementation cp

# 2-b) ⚠ click 호환 고정: typer 0.12.5 는 click 8.5+ 와 런타임 비호환 → 8.1.8 로 강제
echo "  → click 8.1.8 고정 (typer 0.12.5 호환)"
find "$WHEELS" -name "click-*.whl" ! -name "click-8.1.8-*" -delete
python3 -m pip download "click==8.1.8" --no-deps --dest "$WHEELS" --only-binary=:all:

# 2-c) pip 자체 wheel (install.bat 의 오프라인 pip 업그레이드용)
python3 -m pip download pip --dest "$WHEELS" --no-deps --only-binary=:all:

# 2-d) 반입 불가 확장자 제거 (안전망)
for ext in "*.tar.gz" "*.tgz"; do
    find "$WHEELS" -name "$ext" -print -delete | sed 's/^/  ⚠ 반입 불가 파일 제거: /' || true
done

# 3) lock 파일 — 이번 세트의 wheel 파일명을 고정 (재빌드 시 대조용)
echo "[3/4] requirements-win.lock 생성"
{
    echo "# NDT Assistant — Windows 반입 wheel 세트 고정 목록 (자동 생성: $(date +%Y-%m-%d))"
    echo "# 재빌드 후 이 목록과 diff 하여 의도치 않은 버전 변동을 탐지할 것."
    ls "$WHEELS" | sort
} > "$ROOT/requirements-win.lock"
echo "  → $(ls "$WHEELS" | wc -l | tr -d ' ')개 wheel, $(du -sh "$WHEELS" | cut -f1)"

# 4) VERSION + deployed_manifest — 사내 patch apply 의 baseline
echo "[4/4] VERSION + deployed_manifest.json 생성"
VERSION_TS="$(date +%Y%m%d-%H%M%S)"
GIT_HASH="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
APP_VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); from app import __version__; print(__version__)" 2>/dev/null || echo unknown)"
echo "${APP_VERSION}+${VERSION_TS}.${GIT_HASH}" > "$ROOT/VERSION.txt"
python3 -c "
import sys; sys.path.insert(0, '$ROOT')
from app.patch import write_deployed_manifest
p = write_deployed_manifest('${APP_VERSION}+${VERSION_TS}.${GIT_HASH}')
print('  manifest:', p)
"

# 5) Tesseract 상태 확인 (portable 은 별도 확보 — 2026-08-30 기준 동봉 완료)
if [ -f "$INSTALLER/tesseract/tesseract.exe" ]; then
    echo "[OK] Tesseract portable 동봉 확인: $(du -sh "$INSTALLER/tesseract" | cut -f1)"
else
    echo "[주의] installer/tesseract/tesseract.exe 없음 — UB-Mannheim 5.x 설치본을 7zz 로 추출해 배치 필요"
fi

echo ""
echo "[완료] 다음 단계: bash scripts/make_import_zip.sh 로 반입 zip 생성"
