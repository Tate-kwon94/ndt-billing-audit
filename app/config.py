"""런타임 설정 로더. config/*.yaml 파일을 dict로 읽어 캐시한다."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
DATA_DIR = PROJECT_ROOT / "data"


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """프롬프트 템플릿 (.md) 을 문자열로 로드."""
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def hcx_config() -> dict:
    cfg = load_yaml("hcx.yaml")
    # base_url 환경변수 오버라이드 (사내 endpoint 교체·가짜서버 테스트용).
    # yaml 캐시를 오염시키지 않도록 얕은 복사 후 api 만 교체.
    import os
    override = os.environ.get("NDT_HCX_BASE_URL")
    if override:
        cfg = dict(cfg)
        cfg["api"] = {**cfg.get("api", {}), "base_url": override}
        # providers 도 함께 돌린다. 이게 없으면 A/B 하네스를 사외에서 가짜
        # 서버로 검증할 수 없고, 사내에 들어간 뒤에야 문제를 알게 된다.
        # (2026-09-03: 실제로 providers 만 실주소로 남아 검증이 막혔다)
        if cfg.get("providers"):
            cfg["providers"] = {
                name: {**pv, "base_url": override}
                for name, pv in cfg["providers"].items()
            }
        if cfg.get("embedding"):
            cfg["embedding"] = {**cfg["embedding"], "base_url": override}
    return cfg


def templates_config() -> dict:
    return load_yaml("templates.yaml")


def matching_rules() -> dict:
    return load_yaml("matching_rules.yaml")
