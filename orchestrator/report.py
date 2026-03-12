"""
report.py
Reads trading_memory.json and generates a PDF executive report.

Usage:
    python report.py                        # saves report to reports/ dir
    python report.py --out /path/to/out.pdf # custom output path
"""

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)

# ── Paths ────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
MEMORY_PATH = HERE / "trading_memory.json"
REPORT_DIR  = HERE / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# ── Palette ──────────────────────────────────────────────────
BLACK      = colors.HexColor("#0d0d0d")
WHITE      = colors.white
DARK_BG    = colors.HexColor("#111827")
CARD_BG    = colors.HexColor("#1f2937")
ACCENT     = colors.HexColor("#6366f1")   # indigo
GREEN      = colors.HexColor("#10b981")
RED        = colors.HexColor("#ef4444")
MUTED      = colors.HexColor("#6b7280")
LIGHT_TEXT = colors.HexColor("#e5e7eb")
MID_TEXT   = colors.HexColor("#9ca3af")


# ── Styles ───────────────────────────────────────────────────
def build_styles():
    base = getSampleStyleSheet()

    def s(name, **kw):
        return ParagraphStyle(name, **kw)

    return {
        "report_title": s("report_title",
            fontName="Helvetica-Bold", fontSize=22,
            textColor=WHITE, spaceAfter=4),
        "subtitle": s("subtitle",
            fontName="Helvetica", fontSize=10,
            textColor=MID_TEXT, spaceAfter=20),
        "section_header": s("section_header",
            fontName="Helvetica-Bold", fontSize=12,
            textColor=ACCENT, spaceBefore=18, spaceAfter=8),
        "label": s("label",
            fontName="Helvetica-Bold", fontSize=9,
            textColor=MID_TEXT, spaceAfter=2),
        "value": s("value",
            fontName="Helvetica", fontSize=10,
            textColor=LIGHT_TEXT, spaceAfter=6),
        "thesis": s("thesis",
            fontName="Helvetica-Oblique", fontSize=9,
            textColor=MID_TEXT, spaceAfter=4, leading=13),
        "decision_row": s("decision_row",
            fontName="Helvetica", fontSize=8,
            textColor=LIGHT_TEXT, leading=11),
        "footer": s("footer",
            fontName="Helvetica", fontSize=8,
            textColor=MUTED),
        "normal": base["Normal"],
    }


def pct(val, decimals=2):
    if val is None:
        return "—"
    return f"{val:+.{decimals}f}%"


def dollar(val):
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.2f}"


def fmt_date(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d, %Y  %H:%M UTC")
    except Exception:
        return iso[:16]


def color_for(val):
    if val is None:
        return MID_TEXT
    return GREEN if val >= 0 else RED


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        raise FileNotFoundError(f"Memory file not found: {MEMORY_PATH}")
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Page background ──────────────────────────────────────────
def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(DARK_BG)
    canvas.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)
    # footer
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.6 * inch, 0.4 * inch, "AUTOTRADER — CONFIDENTIAL")
    canvas.drawRightString(
        letter[0] - 0.6 * inch, 0.4 * inch,
        f"Page {doc.page}"
    )
    canvas.restoreState()


# ── Build report ─────────────────────────────────────────────
def build_report(memory: dict, out_path: Path):
    ST = build_styles()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )

    story = []
    W = letter[0] - 1.2 * inch   # usable width

    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y  %H:%M UTC")

    # ── Header ──────────────────────────────────────────────
    story.append(Paragraph("Autotrader", ST["report_title"]))
    story.append(Paragraph(f"Executive Report  ·  Generated {generated_at}", ST["subtitle"]))
    story.append(HRFlowable(width=W, color=ACCENT, thickness=1, spaceAfter=16))

    # ── Portfolio Overview ───────────────────────────────────
    story.append(Paragraph("PORTFOLIO OVERVIEW", ST["section_header"]))

    start_nlv  = memory.get("portfolio_start_nlv")
    last_run   = fmt_date(memory.get("last_run_at", ""))
    decisions  = memory.get("recent_decisions", [])
    last_dec   = decisions[-1] if decisions else None
    current_nlv   = last_dec["nlv"] if last_dec else None
    current_holdings = last_dec["holdings"] if last_dec else "—"

    return_pct = None
    if start_nlv and current_nlv and float(start_nlv) > 0:
        return_pct = ((float(current_nlv) - float(start_nlv)) / float(start_nlv)) * 100

    overview_data = [
        ["Starting NLV",   f"${float(start_nlv):,.2f}" if start_nlv else "—",
         "Current NLV",    f"${float(current_nlv):,.2f}" if current_nlv else "—"],
        ["Return (inception)", pct(return_pct),
         "Last Run",       last_run],
        ["Current Holdings", current_holdings,
         "Total Runs",     str(len(decisions))],
    ]

    ov_table = Table(overview_data, colWidths=[1.4*inch, 1.9*inch, 1.4*inch, 2.4*inch])
    ov_table.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), CARD_BG),
        ("FONTNAME",     (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("FONTNAME",     (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",     (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR",    (0, 0), (0, -1), MID_TEXT),
        ("TEXTCOLOR",    (2, 0), (2, -1), MID_TEXT),
        ("TEXTCOLOR",    (1, 0), (1, -1), LIGHT_TEXT),
        ("TEXTCOLOR",    (3, 0), (3, -1), LIGHT_TEXT),
        ("TEXTCOLOR",    (1, 1), (1, 1),  color_for(return_pct)),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [CARD_BG, colors.HexColor("#253044")]),
        ("TOPPADDING",   (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 7),
        ("LEFTPADDING",  (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("ROUNDEDCORNERS", (0, 0), (-1, -1), [4, 4, 4, 4]),
    ]))
    story.append(ov_table)

    # ── Active Convictions ───────────────────────────────────
    convictions = memory.get("convictions", {})
    story.append(Paragraph("ACTIVE CONVICTIONS", ST["section_header"]))

    if not convictions:
        story.append(Paragraph("No active convictions — portfolio is in cash.", ST["value"]))
    else:
        for sym, c in convictions.items():
            entry   = c.get("entry_price")
            last_px = c.get("last_price")
            pnl_pct = c.get("pnl_pct_since_conviction")
            alloc   = c.get("current_target_pct", 0) * 100
            first_added = c.get("first_added", "")[:10]
            reaffirms   = c.get("reaffirm_count", 0)
            initial_thesis = c.get("initial_thesis", "—")
            latest_thesis  = c.get("latest_thesis", "")

            # conviction header row
            header_data = [[
                Paragraph(f"<b>{sym}</b>", ParagraphStyle("sh", fontName="Helvetica-Bold",
                    fontSize=11, textColor=WHITE)),
                Paragraph(f"Opened {first_added}", ParagraphStyle("sd", fontName="Helvetica",
                    fontSize=8, textColor=MID_TEXT)),
                Paragraph(f"Alloc: {alloc:.0f}%", ParagraphStyle("sa", fontName="Helvetica-Bold",
                    fontSize=9, textColor=ACCENT)),
                Paragraph(pct(pnl_pct), ParagraphStyle("sp", fontName="Helvetica-Bold",
                    fontSize=10, textColor=color_for(pnl_pct))),
            ]]
            h_table = Table(header_data, colWidths=[1.0*inch, 2.2*inch, 1.1*inch, 1.0*inch])
            h_table.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), ACCENT),
                ("TOPPADDING",    (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ]))
            story.append(h_table)

            # conviction detail rows
            ep_str = f"${entry:.2f}" if entry else "—"
            lp_str = f"${last_px:.2f}" if last_px else "—"
            detail_data = [
                ["Entry Price", ep_str, "Last Price", lp_str, "Reaffirmed", f"{reaffirms}x"],
            ]
            d_table = Table(detail_data, colWidths=[1.1*inch, 1.1*inch, 1.0*inch, 1.0*inch, 1.0*inch, 1.0*inch])
            d_table.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), CARD_BG),
                ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME",      (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTNAME",      (4, 0), (4, -1), "Helvetica-Bold"),
                ("TEXTCOLOR",     (0, 0), (0, -1), MID_TEXT),
                ("TEXTCOLOR",     (2, 0), (2, -1), MID_TEXT),
                ("TEXTCOLOR",     (4, 0), (4, -1), MID_TEXT),
                ("TEXTCOLOR",     (1, 0), (1, -1), LIGHT_TEXT),
                ("TEXTCOLOR",     (3, 0), (3, -1), LIGHT_TEXT),
                ("TEXTCOLOR",     (5, 0), (5, -1), LIGHT_TEXT),
                ("TOPPADDING",    (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ]))
            story.append(d_table)

            # thesis block
            thesis_text = initial_thesis
            if latest_thesis and latest_thesis != initial_thesis:
                thesis_text = f"<b>Initial:</b> {initial_thesis}<br/><b>Latest:</b> {latest_thesis}"
            thesis_data = [[Paragraph(thesis_text, ST["thesis"])]]
            t_table = Table(thesis_data, colWidths=[W])
            t_table.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#161f2e")),
                ("TOPPADDING",    (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING",   (0, 0), (-1, -1), 12),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
            ]))
            story.append(t_table)
            story.append(Spacer(1, 8))

    # ── Decision History ─────────────────────────────────────
    story.append(Paragraph("DECISION HISTORY", ST["section_header"]))

    if not decisions:
        story.append(Paragraph("No decisions recorded yet.", ST["value"]))
    else:
        hist_header = [["Timestamp", "Action", "NLV", "Holdings", "Reason"]]
        hist_rows = []
        for d in reversed(decisions):
            action_color = GREEN if d["action"] == "HOLD" else ACCENT if d["action"] == "REBALANCE" else RED
            hist_rows.append([
                Paragraph(d["ts"][:16], ST["decision_row"]),
                Paragraph(f"<b>{d['action']}</b>", ParagraphStyle("act",
                    fontName="Helvetica-Bold", fontSize=8, textColor=action_color)),
                Paragraph(f"${d['nlv']:,.0f}", ST["decision_row"]),
                Paragraph(d["holdings"], ST["decision_row"]),
                Paragraph(d["reason"][:120] + ("..." if len(d["reason"]) > 120 else ""), ST["decision_row"]),
            ])

        hist_data = hist_header + hist_rows
        hist_table = Table(
            hist_data,
            colWidths=[1.1*inch, 0.85*inch, 0.85*inch, 1.5*inch, 2.9*inch],
            repeatRows=1,
        )
        hist_table.setStyle(TableStyle([
            # header
            ("BACKGROUND",    (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR",     (0, 0), (-1, 0), WHITE),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 8),
            ("TOPPADDING",    (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            # body
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [CARD_BG, colors.HexColor("#253044")]),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE",      (0, 1), (-1, -1), 8),
            ("TOPPADDING",    (0, 1), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#374151")),
        ]))
        story.append(hist_table)

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width=W, color=MUTED, thickness=0.5))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Generated by Autotrader Report  ·  {generated_at}  ·  Memory: {MEMORY_PATH.name}",
        ST["footer"]
    ))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"Report saved: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate autotrader executive PDF report")
    parser.add_argument("--out", type=str, default=None, help="Output PDF path")
    parser.add_argument("--memory", type=str, default=None, help="Path to trading_memory.json")
    args = parser.parse_args()

    global MEMORY_PATH
    if args.memory:
        MEMORY_PATH = Path(args.memory)

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = REPORT_DIR / f"report_{ts}.pdf"

    memory = load_memory()
    build_report(memory, out_path)


if __name__ == "__main__":
    main()