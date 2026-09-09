"""사내 → 사외 사진 피드백 친화 progress 파일.

핵심 정보만 한국어 짧게 출력 → 사용자가 휴대폰 사진 1장으로 사외 전달 가능.
data/progress.log 에 매 stage 시작·끝·에러를 짧게 기록.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR


def _path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "progress.log"


def reset() -> None:
    """새 chain 시작 시 progress 파일 초기화."""
    p = _path()
    p.write_text(f"=== NDT Assistant 진행 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n", encoding="utf-8")


def step(stage: str, status: str = "시작", note: str = "") -> None:
    """stage 진행 마커 1줄 추가. status: 시작|완료|실패|진행"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {stage:<25} {status}"
    if note:
        line += f"  ({note})"
    with _path().open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def error(stage: str, summary: str) -> None:
    """에러 — 사용자가 사외 전달할 핵심 정보."""
    with _path().open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ 에러  {stage:<25} {summary}\n")
        f.write(f"  → 자세한 로그: data/logs/{datetime.now().strftime('%Y-%m-%d')}/*.log\n")
        f.write(f"  → 사외 전달 시 본 파일 + 위 로그 파일 마지막 30줄 사진 첨부\n")


def kpi(label: str, value) -> None:
    """검토 결과 요약 KPI 1줄 (예: '청구 2930행, 매칭 1899, 과다 187')."""
    with _path().open("a", encoding="utf-8") as f:
        f.write(f"  · {label}: {value}\n")
