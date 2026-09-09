"""텍스트 패치(.txt 한 장으로 여러 파일 이전) 검증.

이 통로는 사내 결재를 거치지 않으므로, 안전장치가 실제로 동작하는지가 핵심이다:
  - 내용이 한 글자라도 바뀌면 거부되는가 (sha256)
  - 결재 대상 확장자(.bat 등)를 쓰려 하면 거부되는가
  - 프로젝트 밖 경로로 쓰려 하면 거부되는가
  - 앱의 다른 모듈 없이 단독으로 동작하는가
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from app import text_patch as T


def _mk(entries: list[tuple[str, str]], *, corrupt: bool = False) -> str:
    out = [T.HEADER, "created : 2026-09-01T00:00:00", f"files   : {len(entries)}", ""]
    for i, (rel, text) in enumerate(entries, 1):
        sha = "0" * 64 if corrupt else hashlib.sha256(text.encode("utf-8")).hexdigest()
        out += [f"{T.FILE_MARK} {i}/{len(entries)} ---", f"path  : {rel}",
                f"sha256: {sha}", f"lines : {text.count(chr(10))+1}",
                T.BEGIN, *T.encode_body(text), T.END, ""]
    out.append(T.FOOTER)
    return "\n".join(out) + "\n"


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "patch.txt"
    p.write_text(content, encoding="utf-8")
    return p


# ─────────────────────────── 본문 인코딩 왕복 ───────────────────────────


@pytest.mark.parametrize("text", [
    "print('hello')",
    "# 한글 주석이 있는 코드\nx = 1\n",
    "빈 줄이\n\n\n중간에 있는 경우",
    "| 파이프로 시작하는 줄",
    ">>>BEGIN\n<<<END",                 # 구분자 자체가 본문에 있는 경우
    "    들여쓰기\n\tTAB\n",
    "",
])
def test_body_roundtrip(text):
    assert T.decode_body(T.encode_body(text)) == text


def test_delimiter_in_content_survives(tmp_path):
    """본문에 END 구분자가 들어 있어도 파싱이 깨지지 않는다."""
    evil = "line1\n<<<END\nline3"
    T.apply_patch(tmp_path, _write(tmp_path, _mk([("app/x.py", evil)])))
    assert (tmp_path / "app" / "x.py").read_text(encoding="utf-8") == evil


# ─────────────────────────── 적용·백업 ───────────────────────────


def test_apply_creates_and_backs_up(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "old.py").write_text("원본 내용\n", encoding="utf-8")

    st = T.apply_patch(tmp_path, _write(tmp_path, _mk([
        ("app/old.py", "새 내용\n"),
        ("app/new.py", "새로 생긴 파일\n"),
    ])))

    assert (tmp_path / "app" / "old.py").read_text(encoding="utf-8") == "새 내용\n"
    assert (tmp_path / "app" / "new.py").read_text(encoding="utf-8") == "새로 생긴 파일\n"
    assert st["backed_up"] == ["app/old.py"]          # 기존 파일만 백업
    backup = next((tmp_path / "data" / "backups").glob("textpatch-*/app/old.py"))
    assert backup.read_text(encoding="utf-8") == "원본 내용\n"


def test_dry_run_writes_nothing(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("그대로", encoding="utf-8")
    st = T.apply_patch(tmp_path, _write(tmp_path, _mk([("app/a.py", "바뀐 내용")])), dry_run=True)
    assert st["applied"] == ["app/a.py"]
    assert (tmp_path / "app" / "a.py").read_text(encoding="utf-8") == "그대로"
    assert not (tmp_path / "data").exists()


# ─────────────────────────── 안전장치 ───────────────────────────


def test_corrupted_content_rejected(tmp_path):
    st = T.apply_patch(tmp_path, _write(tmp_path, _mk([("app/a.py", "x")], corrupt=True)))
    assert st["applied"] == []
    assert "sha256" in st["rejected"][0][1]
    assert not (tmp_path / "app" / "a.py").exists()


@pytest.mark.parametrize("rel", ["run_me.bat", "app/tool.exe", "lib/x.dll", "s.ps1", "a.sh"])
def test_approval_required_extensions_rejected(tmp_path, rel):
    """결재 대상 확장자는 텍스트 패치로 들어올 수 없다 (정책 우회 방지)."""
    st = T.apply_patch(tmp_path, _write(tmp_path, _mk([(rel, "echo hi")])))
    assert st["applied"] == []
    assert "결재" in st["rejected"][0][1]
    assert not (tmp_path / rel).exists()


@pytest.mark.parametrize("rel", ["../outside.py", "/etc/passwd.py", "app/../../esc.py", "C:/x.py"])
def test_path_escape_rejected(tmp_path, rel):
    st = T.apply_patch(tmp_path, _write(tmp_path, _mk([(rel, "나쁜 내용")])))
    assert st["applied"] == []
    assert st["rejected"]
    assert not (tmp_path.parent / "outside.py").exists()


def test_truncated_file_detected(tmp_path):
    full = _mk([("app/a.py", "내용")])
    with pytest.raises(ValueError, match="잘렸"):
        T.apply_patch(tmp_path, _write(tmp_path, full[: len(full) // 2]))


def test_not_a_patch_file_detected(tmp_path):
    with pytest.raises(ValueError, match="텍스트 패치 파일이 아닙니다"):
        T.apply_patch(tmp_path, _write(tmp_path, "그냥 평범한 메모입니다\n"))


# ─────────────────────────── 내보내기 ───────────────────────────


def test_export_excludes_approval_required(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("코드\n", encoding="utf-8")
    (tmp_path / "run.bat").write_text("echo hi\n", encoding="utf-8")

    st = T.export_patch(tmp_path, tmp_path / "p.txt", ["app/a.py", "run.bat"], note="시험")
    assert st["files"] == ["app/a.py"]
    assert st["skipped"][0][0] == "run.bat" and "결재" in st["skipped"][0][1]
    assert "시험" in (tmp_path / "p.txt").read_text(encoding="utf-8")


def test_export_then_apply_roundtrip(tmp_path):
    """실제 왕복 — 내보낸 뒤 다른 폴더에 적용하면 내용이 같아야 한다."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "app").mkdir(parents=True)
    (dst / "app").mkdir(parents=True)
    original = "# 한글 주석\ndef f():\n    return '|파이프'\n\n\n마지막 줄\n"
    (src / "app" / "m.py").write_text(original, encoding="utf-8")

    T.export_patch(src, tmp_path / "p.txt", ["app/m.py"])
    T.apply_patch(dst, tmp_path / "p.txt")
    assert (dst / "app" / "m.py").read_text(encoding="utf-8") == original


# ─────────────────────────── 독립 실행 ───────────────────────────


def test_runs_standalone_without_app_package(tmp_path):
    """앱을 import 하지 않고 파일 하나만으로 동작해야 한다 (사내 구버전 대응)."""
    lone = tmp_path / "lonely_copy.py"
    lone.write_text(Path(T.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    patch = proj / "p.txt"
    patch.write_text(_mk([("app/z.py", "독립 실행 확인\n")]), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(lone), "apply", str(patch), "--root", str(proj)],
        capture_output=True, text=True, cwd=tmp_path,
        # 자식은 한국어 Windows 콘솔 기본(cp949)으로 찍고 부모(PYTHONUTF8=1)는 utf-8 로 읽어
        # 2026-09-07 사내 pytest 에서 reader thread UnicodeDecodeError 경고가 났다. 양쪽을 utf-8 로 고정.
        encoding="utf-8", errors="replace",
        env={"PATH": "", "SYSTEMROOT": "", "PYTHONIOENCODING": "utf-8"},   # 앱 경로가 전혀 없는 환경
    )
    assert r.returncode == 0, r.stderr
    assert (proj / "app" / "z.py").read_text(encoding="utf-8") == "독립 실행 확인\n"
