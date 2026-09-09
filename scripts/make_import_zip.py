#!/usr/bin/env python3
"""NDT Assistant - build the closed-network import zip (single approval bundle).

macOS 의 `zip` 명령은 파일명 UTF-8 플래그(bit 11)를 세우지 않아, 한국어 Windows 의
Expand-Archive / tar 가 한글 파일명을 CP949 로 오독해 추출이 깨진다 (VM 리허설 F1).
Python zipfile 은 비ASCII 파일명에 UTF-8 플래그를 자동 설정하므로 이 문제를 근본 해결한다.

포함: 코드(app/tests/ndt_3d/config) + 실행 bat + installer(python·wheels·tesseract)
      + requirements + VERSION/deployed_manifest(patch baseline) + README
제외: venv, data, samples, docs, scripts, .git/.DS_Store/__pycache__
"""
from __future__ import annotations
import sys, zipfile, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = datetime.date.today().strftime("%Y%m%d")
OUT = ROOT / "dist" / f"NDT_Assistant_import_{STAMP}.zip"

INCLUDE_DIRS = ["app", "tests", "ndt_3d", "config", "installer"]
INCLUDE_FILES = (
    [p.name for p in ROOT.glob("*.bat")]
    + ["requirements.txt", "requirements-win.txt", "requirements-win.lock",
       "requirements-extra.txt",          # 선택 라이브러리 (install_extras.bat)
       "continueignore.txt",              # AI 도우미가 읽지 않을 폴더 지정
                                          #   (설치 시 .continueignore 로 복원)
       "VERSION.txt", "deployed_manifest.json", "README.md",
       "시작하기.md",                      # 압축 해제 후 첫 안내 (한글명 — UTF-8 flag 자동)
       "사내IT_신청양식.md"]                # 방화벽·hosts 요청서 (IT 전달용)
)
# scripts/ 전체는 빌드툴이라 제외하되, 사내에서 돌릴 A/B 하네스만 상대경로 보존해 포함.
# (하네스는 <root>/scripts/ 위치에서 app·tests 를 형제로 import → 경로 유지 필수)
INCLUDE_REL_FILES = ["scripts/ab_model_test.py", "scripts/ab_cases.sample.json",
                     "scripts/ab_cases_ndt.json", "scripts/ab_gold_ndt.json",
                     "scripts/vision_check.py"]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_import_gate import scan as gate_scan     # noqa: E402

EXCLUDE_PARTS = {"__pycache__", ".git", "venv", ".pytest_cache"}
EXCLUDE_NAMES = {".DS_Store", ".gitkeep"}

def keep(p: Path) -> bool:
    if any(part in EXCLUDE_PARTS for part in p.parts): return False
    if p.name in EXCLUDE_NAMES: return False
    if p.suffix == ".pyc": return False
    return True

def preflight():
    errs = []
    if not list((ROOT / "installer/python").glob("python-3.11*-amd64.exe")):
        errs.append("installer/python/ 에 Python 인스톨러 없음 (build_offline_bundle.sh 먼저)")
    if not (ROOT / "installer/tesseract/tesseract.exe").exists():
        errs.append("installer/tesseract/tesseract.exe 없음")
    n_whl = len(list((ROOT / "installer/wheels").glob("*.whl")))
    if n_whl < 60:
        errs.append(f"installer/wheels 미완성 ({n_whl}개)")
    for f in ("VERSION.txt", "deployed_manifest.json"):
        if not (ROOT / f).exists():
            errs.append(f"{f} 없음 (build_offline_bundle.sh 먼저)")
    return errs

def main():
    errs = preflight()
    if errs:
        print("[ERROR] 사전 점검 실패:")
        for e in errs: print("  -", e)
        sys.exit(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists(): OUT.unlink()
    # 게이트 검사를 통과하기 전에는 최종 이름을 쓰지 않는다.
    # 검사 중 중단(용량부족·Ctrl-C)되면 검사 안 된 zip 이 정상 산출물과
    # 똑같은 이름으로 남아 비개발자가 구분할 수 없기 때문이다.
    TMP = OUT.with_suffix(".zip.building")
    if TMP.exists(): TMP.unlink()

    n = 0
    with zipfile.ZipFile(TMP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if not base.exists(): continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and keep(p):
                    z.write(p, p.relative_to(ROOT).as_posix())  # zipfile: 비ASCII 명에 UTF-8 flag 자동
                    n += 1
        for f in INCLUDE_FILES:
            p = ROOT / f
            if p.exists() and keep(p):
                z.write(p, p.name)
                n += 1
        for f in INCLUDE_REL_FILES:
            p = ROOT / f
            if p.exists() and keep(p):
                z.write(p, Path(f).as_posix())  # 상대경로 유지 (scripts/…)
                n += 1

    # 사내 반입 게이트 검사 — 걸릴 파일이 하나라도 있으면 zip 을 내보내지 않는다.
    # 2026-09-01 에 45개 파일이 "위변조 파일" 로 반려된 뒤 상시 검사로 넣었다.
    print("[검사] 사내 반입 게이트 사전 확인 ...")
    try:
        hits = gate_scan(TMP)
    except BaseException:
        TMP.unlink(missing_ok=True)
        raise
    if hits:
        print(f"[ERROR] 반입 심사에서 반려될 파일 {len(hits)}개 — zip 을 폐기합니다.")
        for rel, reason, detail in hits[:20]:
            print(f"  - {rel}")
            print(f"      {reason}: {detail}")
        if len(hits) > 20:
            print(f"  ... 외 {len(hits)-20}개")
        print("\n  위 파일을 고친 뒤 다시 빌드하세요.")
        print("  전체 목록: python scripts/check_import_gate.py .")
        TMP.unlink(missing_ok=True)
        sys.exit(1)
    print("       걸릴 파일 없음 — OK")
    TMP.rename(OUT)

    size_mb = OUT.stat().st_size / 1e6
    # UTF-8 플래그 검증
    with zipfile.ZipFile(OUT) as z:
        nonascii = [i.filename for i in z.infolist() if not i.filename.isascii()]
        flagged = [i.filename for i in z.infolist() if (i.flag_bits & 0x800)]
    print(f"[완료] {OUT} ({size_mb:.0f} MB, {n} files)")
    print(f"  비ASCII 파일명 {len(nonascii)}개, 그중 UTF-8 flag 설정 {len(flagged)}개")
    if nonascii:
        ok = all(f in flagged for f in nonascii)
        print(f"  UTF-8 flag 전건 설정: {'OK' if ok else 'FAIL'}")
        for f in nonascii[:6]: print("   ·", f)

if __name__ == "__main__":
    main()
