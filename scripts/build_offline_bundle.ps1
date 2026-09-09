# NDT Assistant — 오프라인 번들 빌드 (Windows 호스트용)
#
# ⚠ 표준 빌드 경로는 macOS 의 scripts/build_offline_bundle.sh 입니다 (2026-08-30 일원화).
#   이 스크립트는 사외 Windows 를 쓸 일이 생길 때의 참고용 미러이며,
#   .sh 와 동일한 3가지 보정을 반드시 유지해야 합니다:
#     1) Python 은 embeddable 이 아니라 정식 인스톨러(python-3.11.x-amd64.exe)
#        — embeddable 은 tkinter(GUI)·ensurepip(pip) 미포함으로 사내 설치가 깨짐.
#     2) marker 함정 보정: colorama·watchdog·greenlet 명시 다운로드
#        (pip 은 환경 marker 를 실행 호스트 기준으로 평가 — 타 플랫폼에서 빌드 시 누락됨).
#     3) click==8.1.8 고정 (typer 0.12.5 호환) + pip wheel 동봉 + --only-binary=:all: 강제.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Wheels = Join-Path $Root "installer\wheels"
$PyDir  = Join-Path $Root "installer\python"
New-Item -ItemType Directory -Force -Path $Wheels, $PyDir | Out-Null

$PyVersion = "3.11.9"
$PyUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe"
$PyExe = Join-Path $PyDir "python-$PyVersion-amd64.exe"
if (-not (Test-Path $PyExe)) {
    Write-Host "[1/4] Python $PyVersion 정식 인스톨러 다운로드"
    Invoke-WebRequest -Uri $PyUrl -OutFile $PyExe
}

$Req = Join-Path $Root "requirements-win.txt"
if (-not (Test-Path $Req)) { $Req = Join-Path $Root "requirements.txt" }
Write-Host "[2/4] wheel 다운로드 (win_amd64 / cp311 / only-binary)"
python -m pip download -r $Req --dest $Wheels --platform win_amd64 --python-version 3.11 --only-binary=:all: --implementation cp
python -m pip download colorama watchdog greenlet --no-deps --dest $Wheels --platform win_amd64 --python-version 3.11 --only-binary=:all: --implementation cp
Get-ChildItem $Wheels -Filter "click-*.whl" | Where-Object { $_.Name -notlike "click-8.1.8-*" } | Remove-Item
python -m pip download click==8.1.8 --no-deps --dest $Wheels --only-binary=:all:
python -m pip download pip --no-deps --dest $Wheels --only-binary=:all:
Get-ChildItem $Wheels -Include "*.tar.gz","*.tgz" -Recurse | Remove-Item

Write-Host "[3/4] requirements-win.lock 생성"
$LockPath = Join-Path $Root "requirements-win.lock"
"# NDT Assistant — Windows 반입 wheel 세트 고정 목록 (자동 생성: $(Get-Date -Format yyyy-MM-dd))" | Set-Content $LockPath
(Get-ChildItem $Wheels -Name | Sort-Object) | Add-Content $LockPath

Write-Host "[4/4] VERSION + deployed_manifest 는 macOS .sh 경로에서 생성하세요 (app.patch.write_deployed_manifest)"
