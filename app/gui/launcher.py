"""NDT Assistant GUI 런처 — 완전 비전문가용.

- Python tkinter (표준 라이브러리, 추가 의존성 0)
- 검은 cmd 창 안 보임 — 모든 기능 버튼/dialog
- 한국어 상태 메시지
- 진행률 + 결과 자동 열기 (브라우저/엑셀)

실행:
  Windows: NDT_Assistant.bat 더블클릭 → 이 launcher 자동 실행
  mac dev: python -m app.gui.launcher
"""
from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable
ENV_DEFAULTS = {
    "NDT_HCX_MOCK": os.environ.get("NDT_HCX_MOCK", ""),    # 사용자 환경변수 그대로
    "NDT_OCR_WORKERS": "4",
    "NDT_OCR_LANGS": "eng+rus",
}


class NDTLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NDT Assistant — 발주처 비파괴검사 기성검토")
        self.geometry("900x700")
        self.minsize(800, 600)

        # 한글 폰트 + 큰 글씨 (검토자 친화)
        try:
            self.option_add("*Font", "맑은고딕 11")
        except Exception:
            pass

        self._build_ui()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.after(200, self._drain_log_queue)

    # 색상 팔레트 (시안 A — 정돈)
    C_HEADER = "#1a3a6b"; C_REVIEW = "#c62828"; C_SETUP = "#2563a8"
    C_GUIDE = "#2e7d32"; C_TOOL = "#0e7c86"; C_MAINT = "#6a1b9a"; C_MUTED = "#69737f"
    C_STRIP = "#eef1f6"; C_INK2 = "#525e6e"; C_LINE = "#dce2eb"

    def _build_ui(self):
        # ── 상단 헤더 ──
        header = tk.Frame(self, bg=self.C_HEADER, padx=22, pady=13)
        header.pack(fill="x")
        tk.Label(header, text="🔎 NDT Assistant", fg="white", bg=self.C_HEADER,
                 font=("맑은고딕", 16, "bold")).pack(side="left")
        tk.Label(header, text="비파괴검사 기성검토", fg="#c9d8ee", bg=self.C_HEADER,
                 font=("맑은고딕", 11)).pack(side="left", padx=12)

        # ── 상태 스트립 (데이터·HCX·마지막 검토) ──
        strip = tk.Frame(self, bg=self.C_STRIP, padx=2, pady=1)
        strip.pack(fill="x")
        self._status_cells = {}
        for key, lab in [("data", "적재 데이터"), ("hcx", "HCX 연결"), ("last", "마지막 검토")]:
            cell = tk.Frame(strip, bg=self.C_STRIP, padx=16, pady=7)
            cell.pack(side="left", fill="y")
            tk.Label(cell, text=lab, bg=self.C_STRIP, fg="#7c8797",
                     font=("맑은고딕", 8)).pack(anchor="w")
            v = tk.StringVar(value="확인 중…")
            tk.Label(cell, textvariable=v, bg=self.C_STRIP, fg="#161d28",
                     font=("맑은고딕", 10, "bold")).pack(anchor="w")
            self._status_cells[key] = v
        tk.Frame(self, bg=self.C_LINE, height=1).pack(fill="x")

        # ── 좌측 입력 (분야·회차·일자) ──
        side = tk.Frame(self, padx=15, pady=16)
        side.pack(side="left", fill="y")
        tk.Label(side, text="분야", font=("맑은고딕", 10, "bold"), fg=self.C_INK2).pack(anchor="w")
        self.discipline = tk.StringVar(value="CP-P1")
        for d, label in [("CP-P1", "CP-P1 (배관)"), ("CP-M1", "CP-M1 (기계)"),
                          ("CP-E1", "CP-E1 (계전)"), ("CP-A1", "CP-A1 (토건)")]:
            tk.Radiobutton(side, text=label, variable=self.discipline, value=d,
                           font=("맑은고딕", 10)).pack(anchor="w")
        tk.Label(side, text="").pack(pady=6)
        tk.Label(side, text="청구 회차", font=("맑은고딕", 10, "bold"), fg=self.C_INK2).pack(anchor="w")
        self.round_no = tk.StringVar(value="1")
        tk.Entry(side, textvariable=self.round_no, width=10).pack(anchor="w")
        tk.Label(side, text="").pack(pady=2)
        tk.Label(side, text="청구일자 (YYYY-MM-DD)", font=("맑은고딕", 10, "bold"), fg=self.C_INK2).pack(anchor="w")
        self.bill_date = tk.StringVar(value=date.today().isoformat())
        tk.Entry(side, textvariable=self.bill_date, width=14).pack(anchor="w")

        # ── 메인 (그룹 버튼 + 진행) ──
        main = tk.Frame(self, padx=14, pady=14)
        main.pack(side="left", fill="both", expand=True)

        def group_label(text):
            tk.Label(main, text=text, font=("맑은고딕", 9, "bold"), fg="#7c8797",
                     anchor="w").pack(fill="x", pady=(10, 3))

        def btn(parent, text, cmd, color, big=False, sub=None):
            f = tk.Frame(parent, bg=color)
            f.pack(fill="x", pady=2, side=("top"))
            b = tk.Button(f, text=text, command=cmd, bg=color, fg="white",
                          font=("맑은고딕", 12 if big else 10, "bold"),
                          padx=14, pady=(12 if big else 7), relief="flat",
                          cursor="hand2", anchor="w", justify="left",
                          activebackground=color, activeforeground="white", bd=0)
            b.pack(fill="x")
            if sub:
                tk.Label(f, text=sub, bg=color, fg="#f0f0f0",
                         font=("맑은고딕", 8), anchor="w", justify="left").pack(fill="x", padx=15, pady=(0, 6))
            return f

        # 이번 달 할 일 — hero
        group_label("이번 달 할 일")
        btn(main, "① 청구회차 검토 시작", self.run_review, self.C_REVIEW, big=True,
            sub="청구 엑셀 × 성적서 3자 매칭 → 검토결과 엑셀 자동 생성   (청구 엑셀→samples\\billing, 성적서→samples\\reports)")

        # 처음 한 번만
        group_label("처음 한 번만 (자료 입수 시)")
        row1 = tk.Frame(main); row1.pack(fill="x")
        for txt, cmd, hint in [("② 도면 등록", self.run_ingest_drawings, "samples\\drawings (DC+SD+BG)"),
                                ("③ 표준·SCWEP 등록", self.run_ingest_standards, "samples\\codes_standards")]:
            col = tk.Frame(row1); col.pack(side="left", fill="both", expand=True, padx=(0, 6))
            btn(col, txt, cmd, self.C_SETUP, sub=hint)

        # 도구·유지보수
        group_label("도구 · 유지보수")
        rowt = tk.Frame(main); rowt.pack(fill="x")
        for txt, cmd, color in [("④ 검사기준 가이드", self.run_guide, self.C_GUIDE),
                                 ("⑤ 대시보드", self.run_dashboard, self.C_TOOL),
                                 ("⑥ HCX 연결 점검", self.run_hcx_check, self.C_TOOL)]:
            col = tk.Frame(rowt); col.pack(side="left", fill="both", expand=True, padx=(0, 5))
            btn(col, txt, cmd, color)
        rowm = tk.Frame(main); rowm.pack(fill="x", pady=(3, 0))
        for txt, cmd, color in [("⑦ 업데이트 적용", self.apply_update, self.C_MAINT),
                                 ("⑧ 문제 보고", self.make_support_bundle, self.C_MUTED)]:
            col = tk.Frame(rowm); col.pack(side="left", fill="both", expand=True, padx=(0, 5))
            btn(col, txt, cmd, color)

        # 진행률
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill="x", pady=(12, 4))

        # 상태줄 (사람말)
        statusbar = tk.Frame(main, bg=self.C_STRIP, padx=12, pady=7)
        statusbar.pack(fill="x")
        self.status_var = tk.StringVar(value="● 준비됨 — ① 을 눌러 이번 회차 검토를 시작하세요.")
        tk.Label(statusbar, textvariable=self.status_var, bg=self.C_STRIP,
                 font=("맑은고딕", 10), fg="#2e7d32", anchor="w").pack(fill="x")
        # 사용량 (호출·토큰 — 이번 실행·오늘·이번 주·이번 달). 한도가 아니라 확인용.
        self.usage_var = tk.StringVar(value=self._usage_text())
        tk.Label(statusbar, textvariable=self.usage_var, bg=self.C_STRIP,
                 font=("맑은고딕", 9), fg="#5b6675", anchor="w").pack(fill="x")
        self._tracker = None
        self._run_label = ""

        # 진행 상황 (로그)
        tk.Label(main, text="진행 상황", font=("맑은고딕", 9, "bold"), fg="#7c8797",
                 anchor="w").pack(fill="x", pady=(10, 2))
        log_frame = tk.Frame(main)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=9, font=("Consolas", 9), bg="#fafafa",
                                relief="flat", bd=1, highlightthickness=1,
                                highlightbackground=self.C_LINE)
        scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self._log("NDT Assistant 시작됨. 이번 달 검토는 ① 버튼으로 시작합니다.\n")

        # 상태 스트립 값 채우기 (백그라운드 — GUI 블로킹 방지)
        threading.Thread(target=self._refresh_status, daemon=True).start()

    def _refresh_status(self):
        """상태 스트립 값을 best-effort 로 채운다 (실패해도 '—')."""
        data = hcx = last = "—"
        try:
            import json
            dc = PROJECT_ROOT / "data" / "hcx_daily_count.json"
            if dc.exists():
                j = json.loads(dc.read_text(encoding="utf-8"))
                cnt = j.get("count") if isinstance(j, dict) else j
                hcx = f"오늘 {cnt}회" if cnt is not None else "미점검"
            else:
                hcx = "미점검 (⑥)"
        except Exception:
            hcx = "미점검"
        try:
            # 적재 데이터 — DB 카운트 best-effort
            from app.database.repository import Repository  # type: ignore
            data = "적재됨"
        except Exception:
            # 폴백: 샘플 폴더 존재로 대략 표기
            dr = PROJECT_ROOT / "samples" / "drawings"
            n = len(list(dr.glob("*.pdf"))) if dr.exists() else 0
            data = f"도면 {n}건" if n else "없음 (②③)"
        try:
            import glob, os
            cands = glob.glob(str(PROJECT_ROOT / "samples" / "billing" / "*_검토결과.xlsx"))
            if cands:
                newest = max(cands, key=os.path.getmtime)
                last = os.path.basename(newest)[:18]
            else:
                last = "없음"
        except Exception:
            last = "—"
        for k, v in [("data", data), ("hcx", hcx), ("last", last)]:
            self._status_cells[k].set(v)

    # ─────────────────────────── 작업 실행 ───────────────────────────

    def run_review(self):
        billing_dir = PROJECT_ROOT / "samples" / "billing"
        reports_dir = PROJECT_ROOT / "samples" / "reports"
        # 빈 폴더 사전 체크
        if not billing_dir.exists() or not any(billing_dir.glob("*.xlsx")):
            messagebox.showwarning(
                "청구 엑셀 없음",
                f"먼저 시공사 청구 엑셀 (.xlsx) 1개를 다음 폴더에 두세요:\n\n{billing_dir}\n\n"
                "사내 경로 예: D:\\NDT_Assistant\\samples\\billing\\",
            )
            self._open_path(billing_dir if billing_dir.exists() else PROJECT_ROOT / "samples")
            return
        if not reports_dir.exists() or not any(reports_dir.glob("*.pdf")):
            messagebox.showwarning(
                "성적서 PDF 없음",
                f"먼저 CRST 성적서 PDF (해당 회차 묶음) 1개를 다음 폴더에 두세요:\n\n{reports_dir}\n\n"
                "사내 경로 예: D:\\NDT_Assistant\\samples\\reports\\",
            )
            self._open_path(reports_dir if reports_dir.exists() else PROJECT_ROOT / "samples")
            return
        billing = filedialog.askopenfilename(
            title="청구 엑셀 파일 선택 (samples\\billing\\ 안)",
            filetypes=[("Excel", "*.xlsx"), ("All", "*.*")],
            initialdir=str(billing_dir),
        )
        if not billing:
            return
        reports = filedialog.askopenfilename(
            title="비파괴검사 성적서 PDF 선택 (samples\\reports\\ 안)",
            filetypes=[("PDF", "*.pdf"), ("All", "*.*")],
            initialdir=str(reports_dir),
        )
        if not reports:
            return
        args = [
            "review",
            "--billing", billing,
            "--reports", reports,
            "--round", self.round_no.get(),
            "--date", self.bill_date.get(),
            "--discipline", self.discipline.get(),
        ]
        self._run_cli(args, "청구회차 검토", on_done=self._open_latest_review_xlsx)

    def run_ingest_drawings(self):
        default_dir = PROJECT_ROOT / "samples" / "drawings"
        if not default_dir.exists() or not any(default_dir.glob("*.pdf")):
            messagebox.showwarning(
                "도면 PDF 없음",
                f"먼저 도면 PDF (DC + SD + BG 3파일 한 세트) 를 다음 폴더에 두세요:\n\n{default_dir}\n\n"
                "사내 경로 예: D:\\NDT_Assistant\\samples\\drawings\\",
            )
            self._open_path(default_dir if default_dir.exists() else PROJECT_ROOT / "samples")
            return
        folder = filedialog.askdirectory(
            title="등록할 도면 PDF 폴더 선택 (보통 samples\\drawings\\ 그대로)",
            initialdir=str(default_dir),
        )
        if not folder:
            return
        self._run_cli(["ingest-drawings", folder], "신규 도면 등록")

    def run_ingest_standards(self):
        std_dir = PROJECT_ROOT / "samples" / "codes_standards"
        scwep_dir = PROJECT_ROOT / "samples" / "scwep"
        contract_dir = PROJECT_ROOT / "samples" / "contracts"
        if not any(
            (d.exists() and any(d.glob("*.pdf"))) for d in (std_dir, scwep_dir, contract_dir)
        ):
            messagebox.showwarning(
                "등록할 PDF 없음",
                "다음 중 하나에 PDF 를 두세요:\n\n"
                f"  표준 (NP/PNAE/GOST 등) → {std_dir}\n"
                f"  SCWEP 시공절차서       → {scwep_dir}\n"
                f"  계약서                   → {contract_dir}\n\n"
                "사내 경로 예: D:\\NDT_Assistant\\samples\\codes_standards\\",
            )
            self._open_path(PROJECT_ROOT / "samples")
            return
        folder = filedialog.askdirectory(
            title="등록할 폴더 선택 (codes_standards / scwep / contracts 중 1)",
            initialdir=str(std_dir),
        )
        if not folder:
            return
        # 폴더 이름으로 종류를 **추측**한 뒤 반드시 사람이 확인한다 (2026-09-05).
        # 예전엔 추측을 그대로 믿었다 — 폴더명에 'scwep' 이 없으면 SCWEP 이 '표준' 으로 들어가고,
        # 근거 판정은 doc_type=='scwep' 만 보므로 그 문서는 영원히 '미제출' 이 된다. 비개발자 전제:
        # 자동 추론을 조용히 믿게 두지 않는다.
        from tkinter import messagebox
        guess = "code"
        if "scwep" in folder.lower():
            guess = "scwep"
        elif "contract" in folder.lower():
            guess = "contract"
        names = {"code": "표준 (NP/PNAE/GOST)", "scwep": "SCWEP 시공절차서", "contract": "계약서"}
        if messagebox.askyesno("문서 종류 확인",
                               f"이 폴더를 [{names[guess]}] 로 등록합니다.\n\n{folder}\n\n맞습니까?"):
            doc_type = guess
        else:
            if messagebox.askyesno("문서 종류 선택", "SCWEP 시공절차서 입니까?\n(아니오 → 다음 질문)"):
                doc_type = "scwep"
            elif messagebox.askyesno("문서 종류 선택", "계약서 입니까?\n(아니오 → 표준으로 등록)"):
                doc_type = "contract"
            else:
                doc_type = "code"
        label = f"{names[doc_type]} 등록"
        self._run_cli(["ingest-standards", folder, "--type", doc_type], label)

    def run_guide(self):
        self._run_cli(["criteria-guide", "--discipline", self.discipline.get()],
                      "검사기준 가이드 생성",
                      on_done=self._open_latest_guide_html)

    def run_dashboard(self):
        # 별도 프로세스로 streamlit 띄우고 브라우저 자동 열기
        self.status_var.set("대시보드 시작 중...")
        threading.Thread(target=self._start_dashboard, daemon=True).start()

    def _start_dashboard(self):
        try:
            cmd = [PYTHON_BIN, "-m", "app.main", "dashboard"]
            env = self._build_env()
            subprocess.Popen(cmd, env=env, cwd=str(PROJECT_ROOT),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NEW_CONSOLE if platform.system() == "Windows" else 0)
            self.after(3000, lambda: webbrowser.open("http://localhost:8501"))
            self.log_queue.put("✓ 대시보드 시작됨. 잠시 후 브라우저 자동 열림.\n")
            self.status_var.set("대시보드 실행 중 (브라우저 자동 열림)")
        except Exception as e:
            self.log_queue.put(f"✗ 대시보드 시작 실패: {e}\n")

    def run_hcx_check(self):
        self._run_cli(["hcx-check"], "HCX 연결 점검")

    def apply_update(self):
        """사외에서 받은 patch zip 적용 — ①미리보기(dry-run) ②사용자 확인 ③적용(자동 백업)."""
        zip_path = filedialog.askopenfilename(
            title="사외에서 받은 패치 zip 선택",
            filetypes=[("패치 zip", "*.zip"), ("All", "*.*")],
            initialdir=str(PROJECT_ROOT),
        )
        if not zip_path:
            return

        def after_preview():
            ok = messagebox.askyesno(
                "업데이트 적용 확인",
                "위 로그의 [DRY-RUN] 목록이 이번에 바뀌는 파일들입니다.\n\n"
                "적용할까요?\n\n"
                "· 적용 전 기존 파일은 data\\backups\\ 에 자동 백업됩니다\n"
                "· 적용 후 프로그램을 닫았다가 다시 열면 완료됩니다",
            )
            if ok:
                self._run_cli(["patch", "apply", zip_path], "업데이트 적용",
                              on_done=lambda: messagebox.showinfo(
                                  "업데이트 완료",
                                  "업데이트가 적용되었습니다.\n\n"
                                  "이 창을 닫고 NDT_Assistant.bat 를 다시 실행하세요.\n"
                                  "(문제가 생기면 ⑧ 버튼으로 보고 파일을 만들어 주세요)"))
            else:
                self._log("업데이트 적용을 취소했습니다 (변경 없음).\n")

        self._run_cli(["patch", "apply", zip_path, "--dry-run"],
                      "업데이트 미리보기", on_done=after_preview)

    def make_support_bundle(self):
        """문제 보고 zip 자동 생성 후 지원 안내 표시."""
        out = PROJECT_ROOT / "data" / f"문제보고_{date.today().isoformat()}.zip"

        def after_export():
            msg = (
                "문제 보고 파일이 만들어졌습니다:\n"
                f"  {out}\n\n"
                "사외 지원 요청 — 2 단계\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "① 위 zip 파일을 사외(개발자)에게 전달\n"
                "   (안 되면: 휴대폰으로 진행 로그 화면을 촬영해 전달)\n"
                "   ※ 도면·성적서 원본, HCX 토큰, 도면 기준 DB 는 자동 제외되어 있음\n\n"
                "② 받은 패치 적용\n"
                "   • patch export 로 만든 zip → ⑦ 업데이트 적용 버튼 (자동 백업 포함)\n"
                "   • .py / .yaml / .whl 낱개 파일 → 해당 폴더에 덮어쓰기 (결재 무관)\n\n"
                "→ 확인을 누르면 zip 이 있는 폴더가 열립니다."
            )
            messagebox.showinfo("사외 지원 요청 안내", msg)
            if out.parent.exists():
                self._open_path(out.parent)

        self._run_cli(["debug-bundle", "export", "--out", str(out)],
                      "문제 보고 파일 생성", on_done=after_export)

    # ─────────────────────────── 공통 ───────────────────────────

    def _build_env(self) -> dict:
        env = dict(os.environ)
        for k, v in ENV_DEFAULTS.items():
            if v and not env.get(k):
                env[k] = v
        # Windows 파이프 인코딩 — 자식 python 출력을 UTF-8 로 고정 (cp949 깨짐 방지)
        env.setdefault("PYTHONUTF8", "1")
        # tesseract 경로 (사내 설치본)
        tess = PROJECT_ROOT / "installer" / "tesseract" / "tesseract.exe"
        if tess.exists():
            env["NDT_TESSERACT_CMD"] = str(tess)
            env["NDT_TESSDATA_PREFIX"] = str(tess.parent / "tessdata")
        return env

    def _run_cli(self, args: list[str], label: str, on_done=None):
        from app.gui.progress_parse import ProgressTracker
        self._tracker = ProgressTracker()
        self._run_label = label
        self.status_var.set(f"{label} 진행 중...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(20)
        self._log(f"\n=== {label} 시작 ===\n")
        threading.Thread(
            target=self._run_subprocess, args=(args, label, on_done), daemon=True
        ).start()

    def _run_subprocess(self, args: list[str], label: str, on_done):
        cmd = [PYTHON_BIN, "-m", "app.main", *args]
        try:
            proc = subprocess.Popen(
                cmd, env=self._build_env(), cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            assert proc.stdout
            for line in proc.stdout:
                self.log_queue.put(line)
            proc.wait()
            self.log_queue.put(f"\n=== {label} 종료 (exit={proc.returncode}) ===\n")
            if proc.returncode == 0:
                self.log_queue.put(f"✓ {label} 완료\n")
                if on_done:
                    self.after(500, on_done)
            else:
                self.log_queue.put(f"✗ {label} 실패 — 위 로그 확인 또는 ⑧ 문제 보고 버튼\n")
        except Exception as e:
            self.log_queue.put(f"\n✗ 실행 오류: {e}\n")
        finally:
            self.after(0, self._finish_progress, label, proc.returncode if 'proc' in locals() else -1)

    def _finish_progress(self, label: str, exit_code: int):
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.usage_var.set(self._usage_text())
        if exit_code == 0:
            self.status_var.set(f"{label} 완료 ✓")
        else:
            self.status_var.set(f"{label} 실패 ✗ (로그 확인 또는 사외 지원 요청)")

    def _drain_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.insert("end", line)
                self.log_text.see("end")
                self._maybe_progress(line)
        except queue.Empty:
            pass
        self.after(200, self._drain_log_queue)

    def _maybe_progress(self, line: str) -> None:
        """'진행 [단계] k/n' 이면 바를 확정 모드로 바꾸고 남은 시간을, '사용량 [HCX] …' 이면 사용량줄을 갱신."""
        from app.gui.progress_parse import parse_progress, parse_usage
        import time as _t
        p = parse_progress(line)
        if p and self._tracker is not None:
            text = self._tracker.update(p, now=_t.time())
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=1000)
            self.progress.configure(value=int(self._tracker.fraction * 1000))
            self.status_var.set(f"{self._run_label} — {text}")
            return
        u = parse_usage(line)
        if u:
            extra = ""
            if u.week is not None:
                extra += f" · 이번 주 {u.week:,}회"
            if u.month is not None:
                extra += f" · 이번 달 {u.month:,}회"
            self.usage_var.set(f"HCX 사용량 — 이번 실행 {u.calls:,}회 · 오늘 {u.day:,}회 · 토큰 {u.tokens:,}{extra}")

    def _usage_text(self) -> str:
        try:
            from app.hcx_client import usage_summary
            s = usage_summary()
            return (f"HCX 사용량 — 오늘 {s['today']['calls']:,}회 · 이번 주 {s['week']['calls']:,}회 · "
                    f"이번 달 {s['month']['calls']:,}회 · 토큰(이번 달) {s['month']['tokens']:,}   (한도 아님 · 확인용)")
        except Exception:       # noqa: BLE001 - 표시가 런처를 막으면 안 된다
            return "HCX 사용량 — (읽기 실패)"

    def _log(self, msg: str):
        self.log_text.insert("end", msg)
        self.log_text.see("end")

    # ─────────────────────────── 결과 자동 열기 ───────────────────────────

    def _open_latest_review_xlsx(self):
        billing_dir = PROJECT_ROOT / "samples" / "billing"
        results = sorted(billing_dir.glob("*_검토결과.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
        if results:
            self._log(f"\n결과 파일: {results[0]}\n")
            self._open_path(results[0])

    def _open_latest_guide_html(self):
        outputs = PROJECT_ROOT / "data" / "outputs"
        htmls = sorted(outputs.glob("검사기준_가이드_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        if htmls:
            self._log(f"\n가이드: {htmls[0]}\n")
            webbrowser.open(htmls[0].as_uri())

    def _open_path(self, path: Path):
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))
            elif platform.system() == "Darwin":
                subprocess.run(["open", str(path)])
            else:
                subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(f"파일 열기 실패: {e}\n")


def main():
    app = NDTLauncher()
    app.mainloop()


if __name__ == "__main__":
    main()
