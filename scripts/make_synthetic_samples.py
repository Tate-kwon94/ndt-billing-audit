"""시연용 합성 샘플 생성 — 실 문서와 **형식만** 같고 내용은 전부 지어낸 것.

실 프로젝트 문서는 이 저장소에 들어올 수 없다. 그렇다고 샘플이 없으면 도구가 무엇을 하는지 보여줄 수
없으므로, 실물의 구조(검사 범위표, 성적서 표1, 청구 압축 표기)를 그대로 흉내 낸 문서를 만든다.
스캔본을 흉내 내기 위해 텍스트 레이어 없이 **이미지로 렌더한 PDF** 를 만든다 — OCR·표 전사 경로가 실제로 돈다.

    python scripts/make_synthetic_samples.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1654, 1169          # A3 가로 150dpi 근사
UNITS = (1, 2, 3, 4)
LINES = ["02BR005", "02BR006"] + [f"{n:02d}BR005" for n in (11, 12, 13, 14, 21, 22, 23, 24)]


def _font(size: int):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _grid(d: ImageDraw.ImageDraw, x0, y0, widths, heights):
    """격자를 긋고 각 칸의 좌상단 좌표를 돌려준다."""
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    ys = [y0]
    for h in heights:
        ys.append(ys[-1] + h)
    for x in xs:
        d.line([(x, ys[0]), (x, ys[-1])], fill="black", width=2)
    for y in ys:
        d.line([(xs[0], y), (xs[-1], y)], fill="black", width=2)
    return xs, ys


def _cell(d, xs, ys, c, r, text, font, pad=6):
    d.text((xs[c] + pad, ys[r] + pad), str(text), fill="black", font=font)


def drawing_pages(unit: int) -> list[Image.Image]:
    """도면 PDF 의 쪽들: 표지 · 일반지시 · 검사 범위표(MJZ0001) · 배관 사양표."""
    f_t, f_h, f_b = _font(34), _font(19), _font(16)
    dwg = f"NP.D.N000.{unit}.0AAA&&BBB&&&.021.DC.0001.E"
    rev = "C02" if unit in (1, 4) else "C01"
    pages = []

    cover = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(cover)
    d.rectangle([60, 60, W - 60, H - 60], outline="black", width=3)
    d.text((90, 110), "DETAILED DESIGN DRAWING", fill="black", font=f_t)
    d.text((90, 180), f"{dwg}  Revision {rev}", fill="black", font=f_h)
    d.text((90, 230), f"Unit {unit}0AAA — Circulating water piping", fill="black", font=f_h)
    d.text((90, H - 140), "(synthetic sample — not a real drawing)", fill="black", font=f_b)
    pages.append(cover)

    gi = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(gi)
    d.text((90, 80), "GENERAL INSTRUCTIONS", fill="black", font=f_t)
    for i, t in enumerate([
        "13. Welding shall comply with SPEC 100-80.",
        "14. The methods and scope of non-destructive testing of the pipeline welded joints",
        f"    are to be adopted in compliance with the document {dwg}-MJZ0001.",
        "15. Destructive testing per SPEC 200-84.",
        "16. Quality assessed per SPEC 200-84; PNAE-style acceptance permitted.",
    ]):
        d.text((90, 170 + i * 40), t, fill="black", font=f_h)
    pages.append(gi)

    mjz = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(mjz)
    d.text((90, 60), "Methods and scope of welded joint inspection", fill="black", font=f_h)
    widths = [250, 190, 250, 130, 150, 130, 130, 130, 150]
    heights = [70, 70] + [52] * 10
    xs, ys = _grid(d, 90, 120, widths, heights)
    hdr = ["KKS code", "Outside diameter\nand thickness, mm", "Document / Category",
           "Visual", "Penetrant or\nmagnetic", "Radiographic", "Ultrasonic",
           "Aux. visual", "Aux. penetrant"]
    for c, t in enumerate(hdr):
        for k, ln in enumerate(t.split("\n")):
            d.text((xs[c] + 6, ys[0] + 8 + k * 22), ln, fill="black", font=f_b)
    d.text((xs[3] + 6, ys[1] + 8), "Scope of inspection, %", fill="black", font=f_b)
    for r, ln in enumerate(LINES):
        row = r + 2
        dim = "88.9x7.1" if r < 2 else "2000x18"
        for c, v in enumerate([f"{unit}0AAA{ln}", dim, "SPEC 100-80\nSPEC 200-84/VV",
                               "100", "-", "-", "100", "100", "-"]):
            for k, s in enumerate(str(v).split("\n")):
                d.text((xs[c] + 6, ys[row] + 6 + k * 20), s, fill="black", font=f_b)
    d.text((W - 560, H - 70), f"{dwg}-MJZ0001", fill="black", font=f_b)
    pages.append(mjz)

    spec = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(spec)
    d.text((90, 60), "List of technical characteristics of pipelines", fill="black", font=f_h)
    widths = [250, 200, 180, 260, 150, 150, 160]
    xs, ys = _grid(d, 90, 120, widths, [70] + [52] * 10)
    for c, t in enumerate(["KKS code", "Operating medium", "Dout x S, mm", "Material",
                           "Safety class", "Seismic", "QA category"]):
        d.text((xs[c] + 6, ys[0] + 20), t, fill="black", font=f_b)
    for r, ln in enumerate(LINES):
        dim = "88.9x7.1" if r < 2 else "2000x18"
        for c, v in enumerate([f"{unit}0AAA{ln}", "Sea water", dim, "STEEL-A 100-2014",
                               "4", "II", "QNC"]):
            d.text((xs[c] + 6, ys[r + 1] + 14), v, fill="black", font=f_b)
    d.text((W - 560, H - 70), f"{dwg}-MJH0001", fill="black", font=f_b)
    pages.append(spec)
    return pages


def report_page(no: str, method: str, date: str, unit: int, joints: list[str], reason: str) -> Image.Image:
    f_h, f_b, f_s = _font(22), _font(17), _font(15)
    img = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(img)   # A4 세로 150dpi
    d.rectangle([50, 50, 1190, 200], outline="black", width=2)
    d.text((70, 70), "Construction Laboratory — Test Report", fill="black", font=f_b)
    d.text((70, 110), f"Conclusion on {method} Testing of Quality of Welded Joints", fill="black", font=f_h)
    d.text((70, 155), f"No.  {no}   dated  {date}", fill="black", font=f_h)
    d.text((980, 155), f"{unit}0AAA", fill="black", font=f_b)
    fields = [("2. Name of facility (item)", f"{unit}0AAA"),
              ("3. Safety class, equipment group", "4 QNC"),
              ("4. KKS code", f"{unit}0AAA02BR005MR008+{unit}0AAA02BR005MR007"),
              ("5. Drawing", f"NP.D.N000.{unit}.0AAA&&BBB&&&.021.DC.0001.E Revision C0{2 if unit in (1,4) else 1}"),
              ("6. Number of welding formular (diagram)", f"NP.008.CCW.ABD.{unit}.021.0004"),
              ("7. Methods of inspection and quality assessment", "SPEC 100-80"),
              ("9. Scope of inspection, %", "100"),
              ("13. Method of testing", method)]
    for i, (k, v) in enumerate(fields):
        y = 230 + i * 38
        d.text((70, y), k + ":", fill="black", font=f_b)
        d.text((560, y), v, fill="black", font=f_b)
    d.text((70, 570), "Testing results", fill="black", font=f_h)
    d.text((1000, 605), "Table No. 1", fill="black", font=f_s)
    widths = [70, 200, 130, 300, 220, 90, 110]
    xs, ys = _grid(d, 70, 630, widths, [90] + [56] * len(joints))
    for c, t in enumerate(["Item\nNo.", "Number of welded\njoint or section", "Testing\narea No",
                           "Characteristics of welded joint\n(section, surfacing, part)",
                           "Characteristics of revealed\ndiscontinuities", "Quality\nassessment",
                           "Date of\ntesting"]):
        for k, ln in enumerate(t.split("\n")):
            d.text((xs[c] + 5, ys[0] + 10 + k * 22), ln, fill="black", font=f_s)
    for r, j in enumerate(joints):
        for c, v in enumerate([r + 1, j, "1", reason, "No defects found", "A", date]):
            d.text((xs[c] + 5, ys[r + 1] + 16), str(v), fill="black", font=f_s)
    y = ys[-1] + 30
    d.text((70, y), "Conclusion on the results of testing: The welded joints are made in accordance with", fill="black", font=f_s)
    d.text((70, y + 24), f"SPEC 100-80 and corresponds NP.D.N000.{unit}.0AAA&&BBB&&&.021.DC.0001.E", fill="black", font=f_s)
    return img


def scwep_pages() -> list[Image.Image]:
    f_h, f_b = _font(22), _font(17)
    pages = []
    cover = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(cover)
    d.text((80, 100), "SPECIAL WELDING EXECUTION PLAN (SCWEP)", fill="black", font=f_h)
    d.text((80, 150), "NP.D.P034.1.0AAA&&BBB&&&.015.KE.0001.E  Revision C01", fill="black", font=f_b)
    d.text((80, 200), "Circulating water pipe installation", fill="black", font=f_b)
    pages.append(cover)
    body = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(body)
    d.text((80, 90), "6.2.3  Preparation and inspection of joints", fill="black", font=f_h)
    for i, t in enumerate([
        "6.2.3.7  Welding places shall be ground flush with the base pipeline metal:",
        "         - the places from which temporary process devices have been removed shall be",
        "           subjected to 100 % visual and measurement inspection and liquid penetrant",
        "           testing (LPT);",
        "6.2.3.8  The joint prepared for welding is to be accepted by the technical control group.",
        "",
        "7.16  Places to be ground (places where the spacer blocks were welded to the pipeline",
        "      surface) shall be examined for cracks using liquid-penetrant test.",
    ]):
        d.text((80, 150 + i * 34), t, fill="black", font=f_b)
    d.text((1000, 1700), "page 18", fill="black", font=f_b)
    pages.append(body)
    return pages


def save_pdf(pages: list[Image.Image], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(path, "PDF", resolution=150, save_all=True, append_images=pages[1:])


def billing(path: Path, rows: list[dict]) -> None:
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "CP-P1(전체)"
    ws.append(["No", "Unit", "Application No.", "Report Number", "Date of Testing", "Type", "Item",
               "Result", "Dimension\n(Ø×t, mm)", "Thickness\n(mm)", "PT Point ", None, None,
               "UT Point", None, None, "Weld Map No."])
    ws.append([None] * 17)
    ws.append([None] * 10 + ["m", "Q'ty", "Total(m)", "m", "Q'ty", "Total(m)", None])
    for i, r in enumerate(rows, 1):
        pt = [0.34, r["qty"], 1.0] if r["method"] == "PT" else [None, None, None]
        ut = [None, None, None] if r["method"] == "PT" else [1.2, r["qty"], 8.4]
        ws.append([i, r["unit"], f"NDT-P{i:04d}", r["report"], r["date"], r["method"], r["item"],
                   "A", r["dim"], 18, *pt, *ut, r["wm"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="samples", help="출력 폴더 (기본 samples/)")
    a = ap.parse_args()
    out = Path(a.out)

    for u in UNITS:
        rev = "C02" if u in (1, 4) else "C01"
        save_pdf(drawing_pages(u), out / "drawings" / f"NP.D.N000.{u}.0AAA&&BBB&&&.021.DC.0001.E_{rev}.pdf")
    save_pdf(scwep_pages(), out / "scwep" / "NP.D.P034.1.0AAA&&BBB&&&.015.KE.0001.E_C01.pdf")

    reports, rows = [], []
    for n, (unit, method, joints, reason) in enumerate([
        (1, "Penetrant", ["FW12", "FW13"], "Removing temporary plate fit-up"),
        (1, "Ultrasonic", ["FW12"], "Butt weld of pipeline spool"),
        (2, "Penetrant", ["SW0105", "SW0106", "SW0107"], "Removing temporary plate fit-up"),
        (3, "Penetrant", ["E1.A", "E2.A"], "Removing temporary plate"),
        (4, "Ultrasonic", ["FW29"], "Butt weld of pipeline spool"),
    ], start=1):
        m = "PT" if method == "Penetrant" else "UT"
        no = f"77-{n:03d}{m}"
        date = f"{n+4:02d}.08.2024"
        reports.append(report_page(no, method, date, unit, joints, reason))
        rows.append({"unit": unit, "report": no, "date": date, "method": m,
                     # 압축 표기는 '시작÷끝' 이다 (SW0105÷0107 = 3개). 개수를 적는 게 아니다.
                     "item": ", ".join(joints) if len(joints) < 3 else f"{joints[0]}÷{joints[-1][-4:]}",
                     "qty": len(joints), "dim": "88.9X7.1" if m == "PT" else "2000x18",
                     "wm": f"NP.008.CCW.ABD.{unit}.021.000{n}"})
    save_pdf(reports, out / "NDT reports" / "NDT Report_synthetic.pdf")
    billing(out / "billing" / "billing_synthetic_20240930.xlsx", rows)

    print(f"합성 샘플 생성 → {out}/")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(out)}  ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
