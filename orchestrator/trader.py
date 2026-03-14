"""
trader.py — Trader prompt, decision parsing, and hard validation.
 
The trader receives portfolio state + research and returns a JSON decision.
validate_decision() enforces hard rules regardless of what the LLM returns.
"""
 
import json
import math
from typing import Any, Dict, List, Optional
 
from .config import (
    MAX_POSITIONS, MAX_SINGLE_NAME_PCT, MIN_NEW_POSITION_PCT,
    ALLOW_FULL_CASH, ALLOWED_EXCHANGE, SWING_TARGET_DAYS, SWING_MAX_DAYS,
    log,
)
from .llm import call_trader
from .broker import resolve_exchange
from .memory import convictions_for_prompt, decisions_for_prompt
 
 
def safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default
 
 
def _parse_json(text: str) -> Dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)
 
 
def decide(portfolio_state: Dict, positions: List[Dict],
           new_opps: str, held_review: str, memory: Dict) -> Dict:
    """Send state + research to trader agent and return parsed JSON decision."""
 
    conviction_context = convictions_for_prompt(memory)
    decision_history   = decisions_for_prompt(memory)
 
    prompt = f"""
You are an AI trader. Decide the TARGET PORTFOLIO for the next 7-14 days.
 
Return VALID JSON ONLY. No markdown fences. No commentary outside JSON.
 
Strategy: SWING TRADING. Suggested holding period {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} days.
This script runs every 2 hours (~12 runs/day). Think in DAYS, not runs.
 
Rules:
- Long only, whole shares at execution
- target_pct values between 0 and 1, must sum to 1.0
- Max positions: {MAX_POSITIONS}
- Max single-name allocation: {MAX_SINGLE_NAME_PCT}
- Minimum new position size: {MIN_NEW_POSITION_PCT}
- Full cash allowed: {ALLOW_FULL_CASH}
- {ALLOWED_EXCHANGE}-listed symbols only. No NYSE stocks.
- CRITICAL: every currently held position MUST appear in targets, even if unchanged.
  Omitting a held symbol causes incorrect cash calculation.
 
Swing trading bias (not hard rules):
- Bias toward patience and holding within the {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} day range.
- Exit is valid any time if thesis is clearly broken or a severe catalyst hits.
- Do NOT rotate on intraday moves, same-run updates, or minor sentiment shifts.
- Partial trim preferred over full exit. When uncertain: default to HOLD.
 
--- PORTFOLIO STATE ---
{json.dumps(portfolio_state, indent=2)}
 
--- CURRENT POSITIONS ---
{json.dumps(positions, indent=2)}
 
--- CONVICTION HISTORY ---
{conviction_context}
 
--- RESEARCH: NEW OPPORTUNITIES ---
{new_opps}
 
--- RESEARCH: HELD POSITIONS REVIEW ---
{held_review}
 
--- DECISION HISTORY ---
{decision_history}
 
Output schema:
{{
  "portfolio_action": "HOLD | REBALANCE | EXIT_TO_CASH",
  "targets": [
    {{"symbol": "NVDA", "target_pct": 0.40, "thesis": "1-2 sentence rationale"}},
    {{"symbol": "CASH", "target_pct": 0.60, "thesis": ""}}
  ],
  "reason": "overall rationale referencing conviction history"
}}
"""
 
    raw = call_trader(prompt)
    parsed = _parse_json(raw)
    parsed["_raw_text"] = raw
    return parsed
 
 
def validate(decision: Dict, positions: List[Dict]) -> Dict:
    """
    Enforce hard rules on the trader's decision.
    Corrects what it can and logs every correction.
    """
    VALID_ACTIONS = {"HOLD", "REBALANCE", "EXIT_TO_CASH"}
 
    # 1 — portfolio_action
    action = str(decision.get("portfolio_action", "")).upper().strip()
    if action not in VALID_ACTIONS:
        log.warning(f"  [validate] Invalid action '{action}' — defaulting to HOLD")
        decision["portfolio_action"] = "HOLD"
 
    # 2 — targets must be a list
    targets = decision.get("targets", [])
    if not isinstance(targets, list):
        log.warning("  [validate] targets is not a list — resetting")
        decision["targets"] = []
        targets = []
 
    # 3 — deduplicate and normalize
    seen, clean = set(), []
    for t in targets:
        sym = str(t.get("symbol", "")).upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        t["symbol"]     = sym
        t["target_pct"] = max(0.0, min(1.0, safe_float(t.get("target_pct", 0.0))))
        clean.append(t)
    decision["targets"] = clean
    targets = clean
 
    # 4 — EXIT_TO_CASH: zero all non-cash targets
    if decision["portfolio_action"] == "EXIT_TO_CASH":
        for t in targets:
            if t["symbol"] != "CASH" and t["target_pct"] > 0:
                log.warning(f"  [validate] EXIT_TO_CASH — zeroing {t['symbol']}")
                t["target_pct"] = 0.0
 
    # 5 — Exchange validation (only zero on definitive non-NASDAQ response)
    non_cash = [t for t in targets if t["symbol"] != "CASH"]
    for t in non_cash:
        if t["target_pct"] <= 0:
            continue
        exch = resolve_exchange(t["symbol"])
        if exch is None:
            log.warning(f"  [validate] Could not resolve exchange for {t['symbol']} — passing through")
        elif exch != ALLOWED_EXCHANGE:
            log.warning(f"  [validate] {t['symbol']} is on {exch}, not {ALLOWED_EXCHANGE} — zeroing")
            t["target_pct"] = 0.0
        else:
            log.info(f"  [validate] {t['symbol']} exchange confirmed: {exch}")
 
    # 6 — Inject missing held positions (prevents cash accumulation bug)
    target_syms = {t["symbol"] for t in targets}
    for p in positions:
        sym = p["symbol"]
        if sym == "CASH" or sym in target_syms:
            continue
        weight = safe_float(p.get("portfolio_weight", 0.0))
        if weight > 0:
            log.warning(f"  [validate] {sym} held at {weight:.1%} but missing from targets — injecting as HOLD")
            targets.append({"symbol": sym, "target_pct": weight, "thesis": "Auto-injected: held but omitted."})
 
    # 7 — Rebuild non_cash after injections
    non_cash     = [t for t in targets if t["symbol"] != "CASH"]
    cash_targets = [t for t in targets if t["symbol"] == "CASH"]
 
    # 8 — Max positions
    if len(non_cash) > MAX_POSITIONS:
        log.warning(f"  [validate] {len(non_cash)} positions > MAX {MAX_POSITIONS} — trimming smallest")
        non_cash = sorted(non_cash, key=lambda t: t["target_pct"], reverse=True)[:MAX_POSITIONS]
 
    # 9 — Max single-name allocation
    for t in non_cash:
        if t["target_pct"] > MAX_SINGLE_NAME_PCT:
            log.warning(f"  [validate] {t['symbol']} capped at {MAX_SINGLE_NAME_PCT}")
            t["target_pct"] = MAX_SINGLE_NAME_PCT
 
    # 10 — Min new position size
    current_syms = {p["symbol"] for p in positions}
    for t in non_cash:
        if t["symbol"] not in current_syms and 0 < t["target_pct"] < MIN_NEW_POSITION_PCT:
            log.warning(f"  [validate] {t['symbol']} new position too small — zeroing")
            t["target_pct"] = 0.0
 
    # 11 — Non-empty thesis
    for t in non_cash:
        if not str(t.get("thesis", "")).strip():
            t["thesis"] = "No thesis provided."
 
    # 12 — Force targets to sum to 1.0 via CASH adjustment
    decision["targets"] = non_cash + cash_targets
    total    = sum(t["target_pct"] for t in decision["targets"])
    residual = round(1.0 - total, 6)
    if abs(residual) > 0.001:
        if cash_targets:
            cash_targets[0]["target_pct"] = max(0.0, cash_targets[0]["target_pct"] + residual)
            log.warning(f"  [validate] Targets summed to {total:.4f} — adjusted CASH by {residual:+.4f}")
        else:
            decision["targets"].append({"symbol": "CASH", "target_pct": max(0.0, residual), "thesis": ""})
            log.warning(f"  [validate] Added CASH target for residual {residual:.4f}")
 
    return decision