"""memory.py — Trading memory: load, save, convictions, position open dates."""
 
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
 
from .config import (
    MEMORY_PATH, MAX_DECISION_HISTORY, MAX_DECISIONS_IN_PROMPT,
    MAX_TARGET_PCT_HISTORY, SWING_TARGET_DAYS, SWING_MAX_DAYS,
    DAYS_BEFORE_PROTECTED, DAYS_FOR_HIGH_STICKINESS, DAYS_FOR_MAX_STICKINESS,
    log,
)
from .broker import get_quote
 
 
# ── Helpers ───────────────────────────────────────────────────
 
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
 
 
def safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default
 
 
# ── Load / save ───────────────────────────────────────────────
 
def load() -> Dict:
    if not MEMORY_PATH.exists():
        return {
            "portfolio_start_nlv": None,
            "last_run_at":         None,
            "recent_decisions":    [],
            "position_open_dates": {},
            "convictions":         {},
        }
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        mem = json.load(f)
    mem.setdefault("convictions", {})
    mem.setdefault("recent_decisions", [])
    mem.pop("last_decision", None)  # remove old schema key
    return mem
 
 
def save(memory: Dict) -> None:
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)
 
 
# ── Position open dates ───────────────────────────────────────
 
def update_open_dates(memory: Dict, positions: List[Dict]) -> None:
    """Track when each position was first opened."""
    open_dates = memory.setdefault("position_open_dates", {})
    current = set()
    for p in positions:
        sym = str(p.get("symbol", "")).upper().strip()
        qty = int(safe_float(p.get("qty", p.get("position", 0))))
        if sym and qty > 0:
            current.add(sym)
            open_dates.setdefault(sym, now_utc())
    for sym in list(open_dates):
        if sym not in current:
            del open_dates[sym]
 
 
def days_held(memory: Dict, symbol: str) -> int:
    ts = memory.get("position_open_dates", {}).get(symbol)
    if not ts:
        return 0
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days)
    except Exception:
        return 0
 
 
# ── Convictions ───────────────────────────────────────────────
 
def _stickiness(days: int) -> str:
    if days < DAYS_BEFORE_PROTECTED:
        return f"NEW ({days}d) — bias toward patience, exit only if thesis clearly broken"
    elif days < DAYS_FOR_HIGH_STICKINESS:
        return f"EARLY ({days}d) — within swing window, prefer holding"
    elif days < DAYS_FOR_MAX_STICKINESS:
        return f"ESTABLISHED ({days}d) — approaching target, lean strongly toward holding"
    elif days <= SWING_MAX_DAYS:
        return f"AT TARGET ({days}d) — ideal swing zone, hold unless thesis broken"
    else:
        return f"EXTENDED ({days}d) — past horizon, stay if thesis intact and working"
 
 
def _days_since(ts: str) -> int:
    if not ts:
        return 0
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days)
    except Exception:
        return 0
 
 
def update_convictions(memory: Dict, decision: Dict, actual_positions: List[Dict]) -> None:
    """
    Update convictions from actual post-trade positions.
    Only creates new convictions for symbols that actually filled.
    """
    convictions = memory.setdefault("convictions", {})
    targets     = decision.get("targets", [])
    pos_map     = {p["symbol"]: p for p in actual_positions}
    actual_syms = set(pos_map)
    now         = now_utc()
 
    for t in targets:
        sym = str(t.get("symbol", "")).upper().strip()
        if not sym or sym == "CASH":
            continue
 
        target_pct = safe_float(t.get("target_pct", 0))
        thesis     = str(t.get("thesis", "")).strip()
 
        # Use actual portfolio weight if position exists
        if sym in pos_map:
            actual_weight = safe_float(pos_map[sym].get("portfolio_weight", target_pct))
            live_price    = safe_float(pos_map[sym].get("current_price", 0)) or None
        else:
            actual_weight = target_pct
            live_price    = None
 
        if not live_price:
            live_price = get_quote(sym)
 
        recorded_pct = round(actual_weight, 4)
 
        if sym not in convictions:
            if sym not in actual_syms:
                log.warning(f"  [convictions] Skipping {sym} — not in actual positions (buy failed?)")
                continue
            convictions[sym] = {
                "symbol":                   sym,
                "first_added":              now,
                "last_reaffirmed":          now,
                "reaffirm_count":           1,
                "entry_price":              round(live_price, 4) if live_price else None,
                "last_price":               round(live_price, 4) if live_price else None,
                "pnl_pct_since_conviction": 0.0,
                "initial_thesis":           thesis,
                "latest_thesis":            thesis,
                "current_target_pct":       recorded_pct,
                "target_pct_history":       [recorded_pct],
            }
        else:
            c = convictions[sym]
            c["last_reaffirmed"]  = now
            c["reaffirm_count"]   = c.get("reaffirm_count", 1) + 1
            if thesis:
                c["latest_thesis"] = thesis
            if live_price:
                c["last_price"] = round(live_price, 4)
                entry = safe_float(c.get("entry_price", 0))
                if entry > 0:
                    c["pnl_pct_since_conviction"] = round((live_price - entry) / entry * 100, 2)
            c["current_target_pct"] = recorded_pct
            history = c.setdefault("target_pct_history", [])
            history.append(recorded_pct)
            c["target_pct_history"] = history[-MAX_TARGET_PCT_HISTORY:]
            c.pop("pending_exit", None)
 
    # Drop convictions no longer held
    active = {str(t.get("symbol", "")).upper() for t in targets if t.get("symbol") != "CASH"}
    for sym in list(convictions):
        if sym not in actual_syms:
            log.info(f"  [convictions] Dropping {sym} — no longer held")
            del convictions[sym]
        elif sym not in active:
            convictions[sym]["pending_exit"] = True
            log.warning(f"  [convictions] {sym} held but not in targets — flagged pending_exit")
 
 
def convictions_for_prompt(memory: Dict) -> str:
    convictions = memory.get("convictions", {})
    if not convictions:
        return "No existing convictions — first run or portfolio fully exited."
 
    lines = ["Current convictions (swing trading anti-churn reference):"]
    for sym, c in convictions.items():
        days    = _days_since(c.get("first_added", ""))
        sticky  = _stickiness(days)
        pnl     = c.get("pnl_pct_since_conviction", 0.0)
        entry   = c.get("entry_price")
        last    = c.get("last_price")
        pct     = c.get("current_target_pct", 0.0)
        pending = " ⚠️ PENDING EXIT (sell failed — must exit)" if c.get("pending_exit") else ""
 
        lines.append(f"\n  [{sym}]{pending} — opened {c.get('first_added','')[:10]} | {sticky}")
        lines.append(f"    Price: entry=${entry:.2f} → now=${last:.2f} | P&L={pnl:+.2f}% | alloc={pct*100:.1f}%")
        lines.append(f"    Reaffirmed {c.get('reaffirm_count', 1)}x")
        lines.append(f"    Initial thesis: {c.get('initial_thesis', 'n/a')}")
        if c.get("latest_thesis") and c.get("latest_thesis") != c.get("initial_thesis"):
            lines.append(f"    Latest thesis:  {c.get('latest_thesis')}")
 
    lines.append(f"""
SWING TRADING GUIDANCE (target {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} days, runs every 2h ~12x/day):
- Bias toward patience. Exit only if thesis clearly broken or severe catalyst hits.
- Do NOT rotate on intraday moves or minor sentiment shifts.
- Partial trim preferred over full exit unless conviction completely broken.
- When uncertain: default to HOLD.""")
 
    return "\n".join(lines)
 
 
def decisions_for_prompt(memory: Dict) -> str:
    records = memory.get("recent_decisions", [])[-MAX_DECISIONS_IN_PROMPT:]
    if not records:
        return "No prior decisions recorded."
    lines = ["Recent decisions (most recent last):"]
    for r in records:
        lines.append(
            f"  {r['ts'][:16]} | {r['action']:10} | NLV=${r['nlv']:,.0f} "
            f"| {r['holdings']} | \"{r['reason']}\""
        )
    return "\n".join(lines)
 
 
def record_decision(memory: Dict, action: str, post_nlv: float, actual_holdings: str,
                    intended_holdings: str, reason: str, any_failures: bool) -> None:
    """Append a slim decision record to memory."""
    slim = {
        "ts":                  now_utc(),
        "action":              action,
        "nlv":                 round(post_nlv, 2),
        "holdings":            actual_holdings,
        "intended_holdings":   intended_holdings,
        "reason":              str(reason)[:200],
        "execution_failures":  any_failures,
    }
    memory.setdefault("recent_decisions", []).append(slim)
    memory["recent_decisions"] = memory["recent_decisions"][-MAX_DECISION_HISTORY:]
    memory["last_run_at"] = now_utc()