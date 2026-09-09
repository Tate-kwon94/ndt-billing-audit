"""반입 게이트 검사기 회귀 테스트.

2026-09-01 에 45개 파일이 "위변조 파일" 로 반려된 대가로 얻은 규칙들이다.
보호 장치가 없으면 누군가 "표준 조합이니까" 라며 EXPECTED 에 항목을 추가해도
아무도 못 잡는다 (2026-09-02 교차검증 지적).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# 태그 문자열을 그대로 적으면 이 테스트 파일 자신이 반입 게이트에 걸린다.
# (검사기가 실제로 잡아냈다 — 조각내 조립한다.)
_LT = b"<"
_DOCTYPE = _LT + b"!doctype html>"
_HTML_OPEN = _LT + b"html>"
_TITLE = _LT + b"title>x" + _LT + b"/title>"

# 이 검사기는 **사외 빌드 도구**다. 사내 배포본에는 scripts/ 가 빠지고(빌드툴이라 제외)
# libmagic 의 `file` 명령도 없다. 2026-09-03 사내에서 이 파일이 수집 단계에서
# ModuleNotFoundError 를 내 pytest 전체가 멈췄다 — 반입 게이트와 무관한 곳이 막힌 것이다.
# 그래서 도구가 없는 환경에서는 통째로 건너뛴다. 사외(빌드 머신)에서는 그대로 다 돈다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

if shutil.which("file") is None:
    pytest.skip("libmagic 'file' 명령 없음 — 반입 게이트 검사는 사외 빌드 머신에서만",
                allow_module_level=True)
gate = pytest.importorskip(
    "check_import_gate",
    reason="scripts/check_import_gate.py 없음 — 반입 게이트는 사외 빌드 전용 도구")


# ─────────────────────── HTML 4096 바이트 경계 ───────────────────────

def _write_html_at(tmp_path: Path, offset: int, suffix: str = ".py") -> Path:
    """앞을 채워 HTML 태그가 정확히 `offset` 바이트 지점에서 시작하게 만든다."""
    p = tmp_path / f"probe{suffix}"
    # 첫 줄을 '#' 주석으로 두어 magic 의 python 규칙이 끼어들지 않게 한다.
    filler = b"# " + b"a" * (offset - 3) + b"\n"
    assert len(filler) == offset
    p.write_bytes(filler + _DOCTYPE + b"\n")
    return p


def test_html_just_inside_window_is_blocked(tmp_path):
    """4095 바이트 지점의 HTML 은 게이트가 잡는다."""
    p = _write_html_at(tmp_path, 4095)
    assert gate.html_token_pos(p, gate.HTML_SNIFF_BYTES + 64) == 4095
    hit = gate.check_file("probe.py", p)
    assert hit is not None and hit[0] == "HTML 로 오판될 내용"


def test_html_just_outside_window_passes(tmp_path):
    """4096 바이트 지점이면 libmagic 탐색 범위 밖이라 통과한다."""
    p = _write_html_at(tmp_path, 4096)
    assert gate.check_file("probe.py", p) is None
    warn = gate.check_warning("probe.py", p)
    assert warn is not None and "뒤쪽" in warn[0]


def test_docstring_first_line_does_not_hide_html(tmp_path):
    """삼중따옴표로 시작해도 게이트는 HTML 로 본다.

    구 viewer.py 가 정확히 이 형태였다. 로컬 기본 `file` 은 python 규칙이
    먼저 매치돼 text/plain 을 주지만, 게이트(sgml 규칙)는 text/html 로 본다.
    """
    p = tmp_path / "viewer.py"
    p.write_bytes(b'"""docstring."""\n' + _DOCTYPE + b"\n" + _HTML_OPEN + b"\n")
    hit = gate.check_file("viewer.py", p)
    assert hit is not None and hit[0] == "HTML 로 오판될 내용"


def test_html_extension_may_contain_html(tmp_path):
    """.html 파일은 HTML 이어도 당연히 통과한다."""
    p = tmp_path / "page.html"
    p.write_bytes(_DOCTYPE + b"\n" + _HTML_OPEN + _TITLE + _LT + b"/html>\n")
    assert gate.check_file("page.html", p) is None


# ─────────────────────── 차단 유형 4종 ───────────────────────

def test_empty_file_blocked(tmp_path):
    p = tmp_path / "__init__.py"
    p.write_bytes(b"")
    hit = gate.check_file("__init__.py", p)
    assert hit is not None and hit[0] == "빈 파일"


def test_dotfile_blocked(tmp_path):
    p = tmp_path / ".continueignore"
    p.write_text("samples/\n", encoding="utf-8")
    hit = gate.check_file(".continueignore", p)
    assert hit is not None and hit[0] == "점으로 시작하는 파일"


def test_no_extension_blocked(tmp_path):
    p = tmp_path / "VERSION"
    p.write_text("0.1.0\n", encoding="utf-8")
    hit = gate.check_file("VERSION", p)
    assert hit is not None and hit[0] == "확장자 없음"


def test_unknown_extension_blocked(tmp_path):
    p = tmp_path / "eng.user-words"
    p.write_text("word\n", encoding="utf-8")
    hit = gate.check_file("eng.user-words", p)
    assert hit is not None and hit[0] == "모르는 확장자"


def test_gate_ext_uses_last_dot():
    """게이트는 마지막 점 뒤를 확장자로 본다 (실제 반려 목록 근거)."""
    assert gate.gate_ext("box.train.stderr") == ".stderr"
    assert gate.gate_ext(".continueignore") == ".continueignore"
    assert gate.gate_ext("continue-2.1.0-win.vsix.zip") == ".zip"
    assert gate.gate_ext("VERSION") == ""


# ─────────────────────── 판정표 자체를 지킨다 ───────────────────────

# 2026-09-01 반려 45건 / 통과 263건 + 2026-09-02 재제출로 실증된 조합.
# 여기 없는 MIME 을 EXPECTED 의 [확인] 구역에 넣으면 이 테스트가 막는다.
CONFIRMED = {
    ".whl": {"application/zip"},
    ".zip": {"application/zip"},
    ".py": {"text/plain", "text/x-script.python"},
    ".bat": {"text/x-msdos-batch"},
    ".md": {"text/plain"},
    ".json": {"application/json", "text/plain"},
    ".yaml": {"text/plain"},
    ".txt": {"text/plain"},
    ".lock": {"text/plain"},
    ".exe": {"application/x-dosexec"},
    ".dll": {"application/x-dosexec"},
    ".jar": {"application/java-archive", "application/zip"},
    ".ttf": {"font/sfnt"},
    ".traineddata": {"application/x-matlab-data", "application/octet-stream"},
    ".html": {"text/html", "text/plain"},
}


@pytest.mark.parametrize("ext,mimes", sorted(CONFIRMED.items()))
def test_expected_table_matches_evidence(ext, mimes):
    """실증된 확장자의 허용 MIME 이 근거보다 넓어지지 않았는지."""
    assert ext in gate.EXPECTED, f"{ext} 가 판정표에서 사라졌다"
    assert gate.EXPECTED[ext] == mimes, (
        f"{ext}: 근거 {sorted(mimes)} 와 다름 -> {sorted(gate.EXPECTED[ext])}. "
        "실제 반려/통과 사례 없이 MIME 을 추가하면 검사기는 통과시키고 게이트는 반려한다."
    )
