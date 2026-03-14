"""
research.py — 4-call research pipeline.
 
Each pipeline: Pass 1 → Pass 2 → Devil's Advocate → Synthesis
Outputs saved to research_logs/ per run.
"""
 
import json
from pathlib import Path
from typing import List, Dict
 
from .config import RESEARCH_DIR, ALLOWED_EXCHANGE, log
from .llm import call_researcher
 
 
def _save(run_ts: str, label: str, data: dict) -> None:
    safe_ts = run_ts.replace(":", "").replace(".", "")[:15]
    path = RESEARCH_DIR / f"{safe_ts}_{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"  Research log saved: {path.name}")
 
 
def _synthesize(pass1: str, pass2: str, devil: str, context: str) -> str:
    prompt = f"""
You are a senior research analyst synthesizing three independent reports into one balanced view.
 
{context}
 
--- REPORT 1 ---
{pass1}
 
--- REPORT 2 ---
{pass2}
 
--- DEVIL'S ADVOCATE ---
{devil}
 
For each symbol: note where reports agree (higher conviction) and conflict (flag explicitly).
Give net conviction: HIGH / MEDIUM / LOW / AVOID.
One paragraph per symbol max. Do NOT pick sides — present the balanced picture.
"""
    return call_researcher(prompt)
 
 
def research_opportunities(run_ts: str) -> str:
    """Research new NASDAQ stock opportunities for the next 7-14 days."""
    base = f"""
You are a market research agent with web search enabled.
 
Find 3-5 US large-cap {ALLOWED_EXCHANGE}-listed stock opportunities likely to perform well in the next 7-14 days.
Only suggest stocks you are certain trade on {ALLOWED_EXCHANGE}. Skip any NYSE-listed stocks.
 
For each candidate: symbol, exchange confirmation, 7-14 day thesis, bullish points, bearish risks, near-term catalysts, confidence level.
Start with a brief market summary.
"""
 
    log.info("  [Opportunities] Pass 1 ...")
    pass1 = call_researcher(base)
    log.info("  [Opportunities] Pass 2 ...")
    pass2 = call_researcher(base)
 
    devil = f"""
You are a contrarian analyst with web search enabled. Steelman the BEARISH case for every symbol in these reports.
 
Find: weak technicals, analyst downgrades/PT cuts in last 30 days, upcoming binary event risks, sector headwinds, valuation concerns, negative news.
 
--- REPORT 1 ---
{pass1}
 
--- REPORT 2 ---
{pass2}
 
Only surface genuine concerns backed by real data.
"""
    log.info("  [Opportunities] Devil's advocate ...")
    devil_out = call_researcher(devil)
 
    log.info("  [Opportunities] Synthesis ...")
    synthesis = _synthesize(
        pass1, pass2, devil_out,
        context=f"Potential NEW {ALLOWED_EXCHANGE} opportunities for a 7-14 day swing trading portfolio."
    )
 
    _save(run_ts, "opportunities", {"pass1": pass1, "pass2": pass2, "devil_advocate": devil_out, "synthesis": synthesis})
    return synthesis
 
 
def research_held_positions(positions: List[Dict], run_ts: str) -> str:
    """Evaluate currently held positions for the next 7-14 days."""
    if not positions:
        return "No currently held positions."
 
    symbols    = ", ".join(p["symbol"] for p in positions)
    pos_detail = json.dumps(positions, indent=2)
 
    base = f"""
You are a market research agent with web search enabled.
 
Evaluate these held positions for the next 7-14 days: {symbols}
 
{pos_detail}
 
For each: keep/trim/exit leaning, updated thesis, bullish points, bearish risks, near-term catalysts, confidence.
"""
 
    log.info("  [Held Positions] Pass 1 ...")
    pass1 = call_researcher(base)
    log.info("  [Held Positions] Pass 2 ...")
    pass2 = call_researcher(base)
 
    devil = f"""
You are a contrarian analyst with web search enabled. Steelman the case for EXITING or TRIMMING each of: {symbols}
 
Find: technical deterioration, analyst downgrades/PT cuts, binary event downside risks, sector rotation risks, negative news.
 
--- REPORT 1 ---
{pass1}
 
--- REPORT 2 ---
{pass2}
 
Only surface genuine concerns backed by real data.
"""
    log.info("  [Held Positions] Devil's advocate ...")
    devil_out = call_researcher(devil)
 
    log.info("  [Held Positions] Synthesis ...")
    synthesis = _synthesize(
        pass1, pass2, devil_out,
        context=f"Currently held positions ({symbols}) in a 7-14 day swing trading portfolio."
    )
 
    _save(run_ts, "held_positions", {"symbols": symbols, "pass1": pass1, "pass2": pass2, "devil_advocate": devil_out, "synthesis": synthesis})
    return synthesis