"""End-to-end smoke for debug-bundle (export/import) and patch (export/apply)."""
from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path


def test_debug_bundle_roundtrip(tmp_path, monkeypatch):
    # Redirect DATA_DIR to a clean tmp tree.
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path / "data")
    (tmp_path / "data" / "logs" / "2026-05-21").mkdir(parents=True)
    (tmp_path / "data" / "logs" / "2026-05-21" / "test.log").write_text("hello log", encoding="utf-8")
    (tmp_path / "data" / "llm_cache").mkdir(parents=True)
    # 정상 stage 캐시 — 포함되되 _api 는 제거되어야 함
    (tmp_path / "data" / "llm_cache" / "abc123.json").write_text(
        json.dumps({"content": "{\"ok\": true}", "stage": "matching_judge",
                    "_api": {"result": {"usage": {"totalTokens": 99}}}}),
        encoding="utf-8",
    )
    # 민감 stage 캐시 (도면 기준) — 기본 제외되어야 함
    (tmp_path / "data" / "llm_cache" / "sens456.json").write_text(
        json.dumps({"content": "{\"required_ndt\": \"UT\"}", "stage": "compliance_explain"}),
        encoding="utf-8",
    )
    # stage 미기록 구캐시 — 보수적 제외
    (tmp_path / "data" / "llm_cache" / "legacy789.json").write_text(
        json.dumps({"content": "{}"}), encoding="utf-8"
    )

    from app.debug_bundle import export_bundle, import_bundle

    out = tmp_path / "bundle.zip"
    stats = export_bundle(out, include_pdfs=False, include_sqlite=False, days_of_logs=30)
    assert out.exists()
    assert stats["log_files"] >= 1
    assert stats["cache_files"] == 1                    # 정상 stage 만
    assert stats["cache_skipped_sensitive"] == 1        # compliance_explain 제외
    assert stats["cache_skipped_unknown_stage"] == 1    # legacy 제외

    # 포함된 캐시에서 _api(원본응답) 제거 확인
    with zipfile.ZipFile(out) as zf:
        cached = json.loads(zf.read("data/llm_cache/abc123.json"))
        assert "_api" not in cached
        assert cached["content"] == "{\"ok\": true}"
        names = zf.namelist()
        assert not any("sens456" in n for n in names)

    # Import to a different root
    import_root = tmp_path / "mac_side"
    import_root.mkdir()
    istats = import_bundle(out, target_root=import_root, overwrite=False)
    assert istats["extracted_files"] >= 2
    assert "llm_cache" in istats["note"]


def test_debug_bundle_redacts_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True)

    # Create a config dir with a fake token line
    fake_cfg = tmp_path / "config_fake"
    fake_cfg.mkdir()
    (fake_cfg / "secret.yaml").write_text(
        "api:\n  token: super-secret-123\n  other: ok\n", encoding="utf-8"
    )
    monkeypatch.setattr("app.config.CONFIG_DIR", fake_cfg)
    monkeypatch.setattr("app.debug_bundle.CONFIG_DIR", fake_cfg)

    from app.debug_bundle import export_bundle

    out = tmp_path / "bundle.zip"
    export_bundle(out, include_pdfs=False, include_sqlite=False, days_of_logs=1)
    with zipfile.ZipFile(out) as zf:
        content = zf.read("config/secret.yaml").decode("utf-8")
    assert "super-secret-123" not in content
    assert "REDACTED" in content


def test_patch_export_and_apply(tmp_path, monkeypatch):
    """Modify a file, export patch, apply on a fresh tree, verify content."""
    # Build a mini project tree
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "config").mkdir()
    (proj / "data").mkdir()
    (proj / "app" / "hello.py").write_text("def greet(): return 'old'\n", encoding="utf-8")
    (proj / "config" / "x.yaml").write_text("key: 1\n", encoding="utf-8")

    monkeypatch.setattr("app.patch.PROJECT_ROOT", proj)

    from app.patch import export_patch, write_deployed_manifest

    # Stamp baseline manifest
    write_deployed_manifest("baseline-1.0", out_path=proj / "deployed_manifest.json")

    # Modify a file
    (proj / "app" / "hello.py").write_text("def greet(): return 'new'\n", encoding="utf-8")
    (proj / "app" / "new_module.py").write_text("X = 1\n", encoding="utf-8")

    patch_zip = tmp_path / "patch.zip"
    stats = export_patch(patch_zip, baseline_manifest=proj / "deployed_manifest.json")
    assert stats["changed"] == 1
    assert stats["added"] == 1

    # Apply on a fresh tree (simulating closed network PC at baseline)
    fresh = tmp_path / "fresh"
    (fresh / "app").mkdir(parents=True)
    (fresh / "config").mkdir()
    (fresh / "data").mkdir()
    (fresh / "app" / "hello.py").write_text("def greet(): return 'old'\n", encoding="utf-8")
    (fresh / "config" / "x.yaml").write_text("key: 1\n", encoding="utf-8")

    monkeypatch.setattr("app.patch.PROJECT_ROOT", fresh)
    from app.patch import apply_patch

    apply_stats = apply_patch(patch_zip, dry_run=False)
    assert "app/hello.py" in apply_stats["patched_files"]
    assert "app/new_module.py" in apply_stats["patched_files"]
    assert (fresh / "app" / "hello.py").read_text() == "def greet(): return 'new'\n"
    assert (fresh / "app" / "new_module.py").exists()

    # Backup exists for the modified file
    backups = list((fresh / "data" / "backups").rglob("app/hello.py"))
    assert backups, "Backup should exist for modified file"


def test_patch_rejects_path_traversal(tmp_path, monkeypatch):
    from app.patch import _is_safe_rel_path

    assert _is_safe_rel_path("app/foo.py") is True
    assert _is_safe_rel_path("config/x.yaml") is True
    assert _is_safe_rel_path("../etc/passwd") is False
    assert _is_safe_rel_path("/etc/passwd") is False
    assert _is_safe_rel_path("app/../../secret.py") is False
    assert _is_safe_rel_path("randomfile.py") is False   # 루트 화이트리스트 밖
