"""자기추출 패치 생성기 — 지금까지는 손으로 만들어 절차가 어느 문서에도 없었다."""
from __future__ import annotations

import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "scripts" / "make_self_extract_patch.py"


def test_generated_patch_applies_to_copy(tmp_path):
    src = tmp_path / "src"; (src / "app").mkdir(parents=True); (src / "config").mkdir()
    (src / "app" / "x.py").write_text("print('new')\n", encoding="utf-8")
    out = tmp_path / "fix_t.py"
    subprocess.run([sys.executable, str(GEN), "--root", str(src), "--name", "t", "--title", "test",
                    "--expect", "1 passed", "--out", str(out), "app/x.py"], check=True)
    assert out.exists()
    for bad in ("run.bat", "a.zip"):
        r = subprocess.run([sys.executable, str(GEN), "--root", str(src), "--name", "t2", "--title", "x",
                            "--expect", "1 passed", "--out", str(tmp_path / "f2.py"), bad],
                           capture_output=True, text=True)
        assert r.returncode != 0 and "disallowed" in (r.stdout + r.stderr)

    target = tmp_path / "target"; (target / "app").mkdir(parents=True); (target / "config").mkdir()
    (target / "app" / "x.py").write_text("print('old')\n", encoding="utf-8")
    subprocess.run([sys.executable, str(out)], cwd=target, check=True)
    assert (target / "app" / "x.py").read_text(encoding="utf-8") == "print('new')\n"
    assert (target / "backup_t" / "app" / "x.py").read_text(encoding="utf-8") == "print('old')\n"
    r = subprocess.run([sys.executable, str(out)], cwd=target, capture_output=True, text=True)
    assert r.returncode != 0 and "applied before" in (r.stdout + r.stderr)


def _swap_payload(patch_text: str, entries) -> str:
    """생성된 패치의 PAYLOAD 만 갈아끼운다 — safe_dest 가 실제로 막는지 보려면 필요하다."""
    import base64, json, zlib
    raw = base64.b64encode(zlib.compress(json.dumps(entries).encode("ascii"), 9)).decode("ascii")
    body = "\n".join(raw[i:i + 120] for i in range(0, len(raw), 120)) + "\n"
    head, rest = patch_text.split('PAYLOAD = """\\\n', 1)
    _, tail = rest.split('"""', 1)
    return head + 'PAYLOAD = """\\\n' + body + '"""' + tail


def _make_patch(tmp_path, name):
    src = tmp_path / ("src_" + name); (src / "app").mkdir(parents=True); (src / "config").mkdir()
    (src / "app" / "x.py").write_text("print('new')\n", encoding="utf-8")
    out = tmp_path / ("fix_" + name + ".py")
    subprocess.run([sys.executable, str(GEN), "--root", str(src), "--name", name, "--title", "t",
                    "--expect", "1 passed", "--out", str(out), "app/x.py"], check=True)
    return out


def test_patch_rejects_drive_qualified_paths(tmp_path):
    """M15: 'C:evil.py' 는 Windows 에서 프로젝트 폴더 밖으로 나간다. 콜론이 든 조각은 거절."""
    import base64, hashlib

    patch = _make_patch(tmp_path, "m15")
    data = b"print('pwned')\n"
    entry = ["C:evil.py", hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode("ascii")]
    bad = tmp_path / "fix_bad.py"
    bad.write_text(_swap_payload(patch.read_text(encoding="utf-8"), [entry]), encoding="utf-8")

    target = tmp_path / "t15"; (target / "app").mkdir(parents=True); (target / "config").mkdir()
    r = subprocess.run([sys.executable, str(bad)], cwd=target, capture_output=True, text=True)
    assert r.returncode != 0 and "path escapes project folder" in (r.stdout + r.stderr)
