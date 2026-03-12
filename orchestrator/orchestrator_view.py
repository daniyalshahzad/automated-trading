import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider


# =====================================
# CONFIG
# =====================================

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")
BRIDGE_URL = "http://127.0.0.1:8787"

if not BRIDGE_TOKEN:
    raise RuntimeError("BRIDGE_TOKEN not found in ../.env")

HEADERS = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}

API_VERSION = "2025-11-15-preview"

RESEARCH_BASE = (
    "https://hubassist-us2.services.ai.azure.com/"
    "api/projects/proj-default/applications/autotrad-researcher/protocols/openai"
)

TRADER_BASE = (
    "https://hubassist-us2.services.ai.azure.com/"
    "api/projects/proj-default/applications/trader/protocols/openai"
)

MEMORY_PATH = Path(__file__).resolve().parent / "trading_memory.json"

# Hard rules for the trader
MAX_POSITIONS = 3
MAX_SINGLE_NAME_PCT = 0.80
MIN_NEW_POSITION_PCT = 0.10
ALLOW_FULL_CASH = True

# Exchange constraint
ALLOWED_EXCHANGE = "NASDAQ"
EXAMPLE_NYSE_EXCLUSIONS = "ORCL, JPM, BAC, WMT, BRK.B, GS, MS, XOM, CVX, JNJ, V, MA, HD, UNH"

# Memory limits — controls storage size and prompt bloat
MAX_DECISION_HISTORY = 10        # how many slim decision records to keep on disk
MAX_DECISIONS_IN_PROMPT = 5      # how many to include in the trader prompt
MAX_TARGET_PCT_HISTORY = 10      # max entries in conviction target_pct_history

# Swing trading horizon — all churn guidance is based on DAYS, not run count
# Script runs every 2 hours = ~12 runs/day, so days_held is the only meaningful signal
# These are GUIDELINES for the trader's judgment, not hard rules.
# A position working well at day 16 should stay. A broken thesis at day 2 can be exited.
SWING_TARGET_DAYS = 7            # target minimum holding period
SWING_MAX_DAYS = 14              # suggested outer horizon — not a hard exit trigger
DAYS_BEFORE_PROTECTED = 2        # below this, position is too new to have stickiness
DAYS_FOR_HIGH_STICKINESS = 4     # above this, prefer holding unless thesis has changed
DAYS_FOR_MAX_STICKINESS = 7      # at target horizon — thesis must be clearly broken to exit


# ============================================================
# AUTH / CLIENTS
# ============================================================

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(),
    "https://ai.azure.com/.default",
)

research_client = OpenAI(
    api_key=token_provider,
    base_url=RESEARCH_BASE,
    default_query={"api-version": API_VERSION},
)

trader_client = OpenAI(
    api_key=token_provider,
    base_url=TRADER_BASE,
    default_query={"api-version": API_VERSION},
)


# ============================================================
# HELPERS
# ============================================================

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_json_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ============================================================
# BROKER CALLS
# ============================================================

def get_account() -> Dict[str, Any]:
    r = requests.get(f"{BRIDGE_URL}/broker/account", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def get_positions() -> List[Dict[str, Any]]:
    r = requests.get(f"{BRIDGE_URL}/broker/positions", headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_quote(symbol: str) -> Dict[str, Any]:
    r = requests.get(
        f"{BRIDGE_URL}/v1/quote",
        headers=HEADERS,
        params={"symbols": symbol},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_live_price(symbol: str) -> Optional[float]:
    try:
        quotes = get_quote(symbol).get("quotes", [])
        if quotes:
            price = safe_float(quotes[0].get("price", 0.0))
            return price if price > 0 else None
    except Exception:
        pass
    return None


# ============================================================
# MEMORY
# ============================================================

def load_memory() -> Dict[str, Any]:
    if not MEMORY_PATH.exists():
        return {
            "portfolio_start_nlv": None,
            "last_run_at": None,
            "recent_decisions": [],   # slim records only — see slim_decision()
            "position_open_dates": {},
            "convictions": {},
        }
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        mem = json.load(f)
    mem.setdefault("convictions", {})
    mem.setdefault("recent_decisions", [])
    # remove old bloated last_decision key if present from earlier schema
    mem.pop("last_decision", None)
    return mem


def save_memory(memory: Dict[str, Any]) -> None:
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)


def slim_decision(
    decision: Dict[str, Any],
    nlv: float,
    timestamp: str,
) -> Dict[str, Any]:
    """
    Compact decision record stored in recent_decisions.
    Avoids bloating memory with full nested JSON per run.

    Stored format:
    {
        "ts": "2026-03-07T14:00Z",
        "action": "REBALANCE",
        "nlv": 102500.0,
        "holdings": "NVDA:50% AMZN:30% CASH:20%",
        "reason": "AI capex momentum intact, rotating out of MSFT on weak guidance"
    }
    """
    targets = decision.get("targets", [])
    holdings = " ".join(
        f"{t['symbol']}:{round(safe_float(t.get('target_pct', 0)) * 100)}%"
        for t in targets
    )
    return {
        "ts": timestamp,
        "action": decision.get("portfolio_action", "UNKNOWN"),
        "nlv": round(nlv, 2),
        "holdings": holdings,
        "reason": str(decision.get("reason", ""))[:200],  # cap reason length
    }


def recent_decisions_for_prompt(memory: Dict[str, Any]) -> str:
    """Compact multi-line digest of recent decisions for the trader prompt."""
    records = memory.get("recent_decisions", [])[-MAX_DECISIONS_IN_PROMPT:]
    if not records:
        return "No prior decisions recorded."
    lines = ["Recent decision history (most recent last):"]
    for r in records:
        lines.append(
            f"  {r['ts'][:16]} | {r['action']:10} | NLV=${r['nlv']:,.0f} "
            f"| {r['holdings']} | \"{r['reason']}\""
        )
    return "\n".join(lines)


def update_position_open_dates(
    memory: Dict[str, Any],
    current_positions: List[Dict[str, Any]],
) -> None:
    open_dates = memory.setdefault("position_open_dates", {})
    current_symbols = set()
    for pos in current_positions:
        symbol = str(pos.get("symbol", "")).upper().strip()
        qty = int(safe_float(pos.get("qty", 0)))
        if not symbol or qty <= 0:
            continue
        current_symbols.add(symbol)
        if symbol not in open_dates:
            open_dates[symbol] = now_utc_iso()
    for sym in list(open_dates.keys()):
        if sym not in current_symbols:
            del open_dates[sym]


def days_held_for_symbol(memory: Dict[str, Any], symbol: str) -> int:
    ts = memory.get("position_open_dates", {}).get(symbol)
    if not ts:
        return 0
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        return max(0, delta.days)
    except Exception:
        return 0


# ============================================================
# CONVICTION TRACKING
# ============================================================

def update_convictions(
    memory: Dict[str, Any],
    decision: Dict[str, Any],
    positions: List[Dict[str, Any]],
) -> None:
    """
    Updates conviction memory after each trader decision.
    Thesis is read directly from the trader's target objects — the trader
    owns the conviction reasoning, not the research agent.

    Conviction schema:
    {
        "symbol": "NVDA",
        "first_added": "2026-03-07T...",
        "last_reaffirmed": "2026-03-07T...",
        "reaffirm_count": 4,
        "entry_price": 875.00,
        "last_price": 910.00,
        "pnl_pct_since_conviction": 4.0,
        "initial_thesis": "Blackwell ramp accelerating, hyperscaler AI capex intact, breakout above resistance",
        "latest_thesis": "Thesis intact post-earnings, guidance raised, institutions adding",
        "current_target_pct": 0.50,
        "target_pct_history": [0.45, 0.45, 0.50, 0.50],  # capped at MAX_TARGET_PCT_HISTORY
    }
    """
    convictions: Dict[str, Any] = memory.setdefault("convictions", {})
    targets = decision.get("targets", [])
    position_map = {p["symbol"]: p for p in positions}

    active_symbols = {
        str(t.get("symbol", "")).upper().strip()
        for t in targets
        if str(t.get("symbol", "")).upper().strip() not in ("", "CASH")
    }

    now = now_utc_iso()

    for target in targets:
        symbol = str(target.get("symbol", "")).upper().strip()
        if not symbol or symbol == "CASH":
            continue

        target_pct = safe_float(target.get("target_pct", 0.0))

        # thesis written by the trader itself — grounded in the research it just read
        thesis = str(target.get("thesis", "")).strip()

        # live price: prefer position data, fall back to quote
        live_price: Optional[float] = None
        if symbol in position_map:
            live_price = safe_float(position_map[symbol].get("current_price", 0.0)) or None
        if not live_price:
            live_price = fetch_live_price(symbol)

        if symbol not in convictions:
            convictions[symbol] = {
                "symbol": symbol,
                "first_added": now,
                "last_reaffirmed": now,
                "reaffirm_count": 1,
                "entry_price": round(live_price, 4) if live_price else None,
                "last_price": round(live_price, 4) if live_price else None,
                "pnl_pct_since_conviction": 0.0,
                "initial_thesis": thesis,
                "latest_thesis": thesis,
                "current_target_pct": round(target_pct, 4),
                "target_pct_history": [round(target_pct, 4)],
            }
        else:
            c = convictions[symbol]
            c["last_reaffirmed"] = now
            c["reaffirm_count"] = c.get("reaffirm_count", 1) + 1
            c["current_target_pct"] = round(target_pct, 4)

            # cap history length
            history = c.get("target_pct_history", []) + [round(target_pct, 4)]
            c["target_pct_history"] = history[-MAX_TARGET_PCT_HISTORY:]

            # update price + P&L
            if live_price:
                c["last_price"] = round(live_price, 4)
                entry = safe_float(c.get("entry_price", 0.0))
                if entry > 0:
                    c["pnl_pct_since_conviction"] = round(
                        ((live_price - entry) / entry) * 100.0, 2
                    )

            # update latest thesis if the trader provided one this run
            if thesis:
                c["latest_thesis"] = thesis

    # drop symbols no longer targeted
    for sym in list(convictions.keys()):
        if sym not in active_symbols:
            del convictions[sym]


def days_since_conviction(conviction: Dict[str, Any]) -> int:
    """Returns how many calendar days since this conviction was first opened."""
    ts = conviction.get("first_added", "")
    if not ts:
        return 0
    try:
        opened = datetime.fromisoformat(ts)
        return max(0, (datetime.now(timezone.utc) - opened).days)
    except Exception:
        return 0


def stickiness_label(days: int) -> str:
    """
    Guidance tier based on days held.
    These bias the trader toward patience — they are NOT hard exit rules.
    Exit is always valid if the thesis is clearly broken, regardless of days held.
    Staying is always valid if the position is working, regardless of hitting SWING_MAX_DAYS.
    """
    if days < DAYS_BEFORE_PROTECTED:
        return f"NEW ({days}d) — bias toward patience, but exit is valid if thesis is clearly broken"
    elif days < DAYS_FOR_HIGH_STICKINESS:
        return f"EARLY ({days}d) — within swing window, prefer holding unless a catalyst has materially changed"
    elif days < DAYS_FOR_MAX_STICKINESS:
        return f"ESTABLISHED ({days}d) — approaching target horizon, lean strongly toward holding"
    elif days <= SWING_MAX_DAYS:
        return f"AT TARGET ({days}d) — in the ideal 7-14d swing zone, hold unless thesis is clearly broken"
    else:
        return f"EXTENDED ({days}d) — past suggested horizon, but stay if thesis is intact and position is working"


def conviction_summary_for_prompt(memory: Dict[str, Any]) -> str:
    """
    Rich conviction block injected into the trader prompt.
    Stickiness is driven entirely by DAYS held, not run count —
    because running every 2 hours makes run count meaningless for swing trading.
    """
    convictions = memory.get("convictions", {})
    if not convictions:
        return "No existing convictions — first run or portfolio was fully exited."

    lines = ["Current convictions (swing trading anti-churn reference):"]
    for sym, c in convictions.items():
        entry = c.get("entry_price")
        last = c.get("last_price")
        pnl = c.get("pnl_pct_since_conviction", 0.0)
        reaffirms = c.get("reaffirm_count", 1)
        since = c.get("first_added", "")[:10]
        pct = c.get("current_target_pct", 0.0)
        initial_thesis = c.get("initial_thesis", "n/a")
        latest_thesis = c.get("latest_thesis", "n/a")
        days = days_since_conviction(c)
        sticky = stickiness_label(days)

        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "n/a"
        entry_str = f"${entry:.2f}" if entry else "n/a"
        last_str = f"${last:.2f}" if last else "n/a"

        lines.append(f"\n  [{sym}] — opened {since} | {sticky}")
        lines.append(f"    Price  : entry={entry_str} → now={last_str} | P&L={pnl_str} | alloc={pct*100:.1f}%")
        lines.append(f"    Runs   : reaffirmed {reaffirms}x (~12 runs = 1 trading day)")
        lines.append(f"    Initial thesis : {initial_thesis}")
        if latest_thesis and latest_thesis != initial_thesis:
            lines.append(f"    Latest thesis  : {latest_thesis}")

    lines.append(f"""
SWING TRADING GUIDANCE (target range {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} days, script runs every 2 hours ~12x/day):
- This is a swing trading strategy. Bias decisions toward patience and holding within the {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} day range.
- These are guidelines for judgment, NOT hard rules. Use them to calibrate — not as exit triggers.
- Exit is always valid at any point if the thesis is clearly broken or a severe adverse catalyst hits.
- Staying is always valid beyond {SWING_MAX_DAYS} days if the position is working and the thesis is intact.
- Do NOT rotate based on intraday moves, same-run research updates, or minor sentiment shifts.
- The bar to exit rises with days held. The bar to stay falls if P&L is positive and thesis holds.
- Partial trimming is always preferred over full exit unless conviction is completely broken.
- When genuinely uncertain between holding and rotating: default to HOLD.""")

    return "\n".join(lines)


# ============================================================
# POSITION NORMALIZATION
# ============================================================

def extract_nlv_cash(account: Dict[str, Any]) -> Dict[str, float]:
    nlv = safe_float(
        account.get("nlv", account.get("NetLiquidation", account.get("net_liquidation", 0.0)))
    )
    cash = safe_float(
        account.get("cash", account.get("AvailableFunds", account.get("available_funds", 0.0)))
    )
    return {"nlv": nlv, "cash": cash}


def normalize_positions(
    raw_positions: List[Dict[str, Any]],
    nlv: float,
    memory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    normalized = []
    for pos in raw_positions:
        symbol = str(pos.get("symbol", "")).upper().strip()
        qty = int(safe_float(pos.get("qty", pos.get("position", 0))))
        if not symbol or qty <= 0:
            continue

        avg_price = safe_float(pos.get("avg_price", pos.get("averageCost", pos.get("avgCost", 0.0))))
        current_price = safe_float(pos.get("current_price", pos.get("market_price", pos.get("price", 0.0))))
        market_value = safe_float(pos.get("market_value", pos.get("marketValue", 0.0)))

        if current_price <= 0:
            fetched = fetch_live_price(symbol)
            if fetched:
                current_price = fetched

        if market_value <= 0 and current_price > 0:
            market_value = qty * current_price

        pnl_pct = 0.0
        unrealized_pnl_dollars = 0.0
        if avg_price > 0 and current_price > 0:
            pnl_pct = ((current_price - avg_price) / avg_price) * 100.0
            unrealized_pnl_dollars = (current_price - avg_price) * qty

        normalized.append({
            "symbol": symbol,
            "qty": qty,
            "avg_price": round(avg_price, 4),
            "current_price": round(current_price, 4),
            "market_value": round(market_value, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "unrealized_pnl_dollars": round(unrealized_pnl_dollars, 2),
            "portfolio_weight": round((market_value / nlv) if nlv > 0 else 0.0, 4),
            "days_held": days_held_for_symbol(memory, symbol),
        })
    return normalized


def portfolio_perf_snapshot(
    nlv: float,
    cash: float,
    positions: List[Dict[str, Any]],
    memory: Dict[str, Any],
) -> Dict[str, Any]:
    start_nlv = memory.get("portfolio_start_nlv")
    recent = memory.get("recent_decisions", [])
    last_nlv = safe_float(recent[-1].get("nlv", 0.0)) if recent else 0.0

    since_start_pct = None
    if start_nlv and safe_float(start_nlv) > 0:
        since_start_pct = round(((nlv - safe_float(start_nlv)) / safe_float(start_nlv)) * 100.0, 2)

    since_last_pct = None
    if last_nlv > 0:
        since_last_pct = round(((nlv - last_nlv) / last_nlv) * 100.0, 2)

    return {
        "nlv": round(nlv, 2),
        "cash": round(cash, 2),
        "cash_pct": round((cash / nlv) if nlv > 0 else 0.0, 4),
        "portfolio_return_since_start_pct": since_start_pct,
        "portfolio_return_since_last_decision_pct": since_last_pct,
        "total_unrealized_pnl_dollars": round(
            sum(p.get("unrealized_pnl_dollars", 0.0) for p in positions), 2
        ),
        "number_of_positions": len(positions),
    }


# ============================================================
# RESEARCH — free text, unconstrained
# ============================================================

def research_new_opportunities() -> str:
    """
    Returns a list of opportunity dicts:
    [
      {
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "thesis_summary": "~30 word summary",
        "catalysts": "...",
        "key_risk": "...",
        "confidence": "high|medium|low"
      },
      ...
    ]
    """
    prompt = f"""
You are a market research agent with web search enabled.

Find 3 to 5 US large-cap stock opportunities likely to perform well over the next 7-14 days.

Constraints:
- {ALLOWED_EXCHANGE}-listed stocks ONLY. Every symbol MUST trade on {ALLOWED_EXCHANGE}.
- Do NOT suggest NYSE-listed stocks (e.g. {EXAMPLE_NYSE_EXCLUSIONS}).
- If unsure of exchange, skip it and pick one you are certain is on {ALLOWED_EXCHANGE}.
- Prefer Nasdaq-100 names. Large, liquid stocks only. Tactical 7-14 day horizon.

For each candidate provide:
- Symbol and exchange confirmation (must say NASDAQ)
- Thesis: why should this move up in 7-14 days?
- Bullish points
- Bearish points / key risk
- Near-term catalyst(s)
- Confidence: high / medium / low

Start with a brief market summary.
"""
    response = research_client.responses.create(input=prompt)
    return response.output_text


def research_held_positions(positions: List[Dict[str, Any]]) -> str:
    if not positions:
        return "No currently held positions."

    symbols = ", ".join([p["symbol"] for p in positions])

    prompt = f"""
You are a market research agent with web search enabled.

Evaluate these currently held portfolio positions for the next 7-14 days: {symbols}

For each symbol provide:
- Keep / trim / exit leaning
- Updated thesis: what is the current conviction?
- Bullish points
- Bearish points / key risk
- Near-term catalyst(s)
- Confidence: high / medium / low

Be comparative and tactical.
"""
    response = research_client.responses.create(input=prompt)
    return response.output_text


# ============================================================
# TRADER
# ============================================================

def trader_decision(
    portfolio_state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    new_opps_research: str,
    held_positions_research: str,
    memory: Dict[str, Any],
) -> Dict[str, Any]:

    conviction_context = conviction_summary_for_prompt(memory)
    decision_history = recent_decisions_for_prompt(memory)

    prompt = f"""
You are an AI trader. Decide the TARGET PORTFOLIO for the next 7-14 days.

Return VALID JSON ONLY. No markdown fences. No commentary outside JSON.

Strategy: SWING TRADING. Suggested holding period {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} days per position.
This script runs every 2 hours (~12 runs per trading day). Always think in DAYS, not runs.

Core rules:
- Long only, whole shares at execution (do NOT compute qty here)
- target_pct values between 0 and 1
- Max positions: {MAX_POSITIONS}
- Max single-name allocation: {MAX_SINGLE_NAME_PCT}
- Minimum new position size: {MIN_NEW_POSITION_PCT}
- Full cash allowed: {ALLOW_FULL_CASH}
- CRITICAL: {ALLOWED_EXCHANGE}-listed symbols only. No NYSE stocks.

Swing trading guidance (bias, not hard rules):
- Bias strongly toward patience and holding within the {SWING_TARGET_DAYS}-{SWING_MAX_DAYS} day range.
- Exit is valid at ANY point — day 1, day 20 — if the thesis is clearly broken or a severe catalyst hits.
- Staying beyond {SWING_MAX_DAYS} days is valid if the position is working and thesis is intact.
- Do NOT rotate based on intraday moves, same-run research updates, or minor sentiment shifts.
- The bar to exit a position rises with days held. See CONVICTION HISTORY for per-symbol guidance.
- When genuinely uncertain between holding and rotating: default to HOLD.
- Partial trim is always preferred over full exit unless conviction is completely broken.

--- PORTFOLIO STATE ---
{json.dumps(portfolio_state, indent=2)}

--- CURRENT POSITIONS (live prices + unrealized P&L) ---
{json.dumps(positions, indent=2)}

--- CONVICTION HISTORY ---
{conviction_context}

--- RESEARCH: NEW OPPORTUNITIES ---
{new_opps_research}

--- RESEARCH: HELD POSITIONS REVIEW ---
{held_positions_research}

--- DECISION HISTORY ---
{decision_history}

Output schema:
{{
  "portfolio_action": "HOLD | REBALANCE | EXIT_TO_CASH",
  "targets": [
    {{
      "symbol": "NVDA",
      "target_pct": 0.50,
      "thesis": "1-2 sentence max. Your own words. Why hold this for the next 7-14 days based on the research above."
    }},
    {{
      "symbol": "AMZN",
      "target_pct": 0.30,
      "thesis": "AWS re-acceleration + advertising beat expected at next earnings in 10 days."
    }},
    {{
      "symbol": "CASH",
      "target_pct": 0.20,
      "thesis": ""
    }}
  ],
  "reason": "overall portfolio rationale, referencing conviction history"
}}

Rules:
- targets must sum to 1.0
- include CASH symbol if holding cash
- no duplicate symbols
- no tiny allocations
- use conviction history and days-held stickiness to resist rotation
- write a thesis per symbol that references the swing-trade rationale for the next 7-14 days
"""
    response = trader_client.responses.create(input=prompt)
    raw = response.output_text
    cleaned = extract_json_text(raw)
    parsed = json.loads(cleaned)
    parsed["_raw_text"] = raw
    return parsed


# ============================================================
# EXECUTION PREVIEW
# ============================================================

def clean_targets(targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for t in targets:
        symbol = str(t.get("symbol", "")).upper().strip()
        if not symbol or symbol in seen:
            continue
        pct = max(0.0, min(1.0, safe_float(t.get("target_pct", 0.0))))
        out.append({"symbol": symbol, "target_pct": pct})
        seen.add(symbol)
    total = sum(t["target_pct"] for t in out)
    if total > 0:
        out = [{"symbol": t["symbol"], "target_pct": t["target_pct"] / total} for t in out]
    return out


def current_position_map(positions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {p["symbol"]: p for p in positions}


def fetch_price_for_symbol(symbol: str, current_map: Dict[str, Dict[str, Any]]) -> float:
    if symbol in current_map and current_map[symbol].get("current_price", 0) > 0:
        return safe_float(current_map[symbol]["current_price"])
    return fetch_live_price(symbol) or 0.0


def build_execution_preview(
    portfolio_state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    decision: Dict[str, Any],
) -> List[Dict[str, Any]]:
    nlv = safe_float(portfolio_state["nlv"])
    current_map = current_position_map(positions)
    targets = clean_targets(decision.get("targets", []))

    cash_target_pct = next(
        (t["target_pct"] for t in targets if t["symbol"] == "CASH"), 0.0
    )
    all_symbols = set(current_map.keys()) | {
        t["symbol"] for t in targets if t["symbol"] != "CASH"
    }

    preview = []
    for symbol in sorted(all_symbols):
        target = next(
            (t for t in targets if t["symbol"] == symbol),
            {"symbol": symbol, "target_pct": 0.0},
        )
        target_pct = safe_float(target["target_pct"])
        price = fetch_price_for_symbol(symbol, current_map)
        current_qty = int(current_map.get(symbol, {}).get("qty", 0))
        current_value = safe_float(current_map.get(symbol, {}).get("market_value", 0.0))
        target_dollars = nlv * target_pct
        target_qty = int(math.floor(target_dollars / price)) if price > 0 else 0
        delta_qty = target_qty - current_qty

        preview.append({
            "symbol": symbol,
            "current_qty": current_qty,
            "current_value": round(current_value, 2),
            "price_used": round(price, 4),
            "target_pct": round(target_pct, 4),
            "target_dollars": round(target_dollars, 2),
            "target_qty": target_qty,
            "delta_qty": delta_qty,
            "suggested_action": "BUY" if delta_qty > 0 else "SELL" if delta_qty < 0 else "HOLD",
        })

    preview.append({
        "symbol": "CASH",
        "current_qty": None,
        "current_value": round(portfolio_state["cash"], 2),
        "price_used": None,
        "target_pct": round(cash_target_pct, 4),
        "target_dollars": round(nlv * cash_target_pct, 2),
        "target_qty": None,
        "delta_qty": None,
        "suggested_action": "HOLD",
    })
    return preview


# ============================================================
# MAIN
# ============================================================

def run_view() -> None:
    print("\n===================================================")
    print("ORCHESTRATOR VIEW — SINGLE RUN")
    print("===================================================")
    print("UTC Time:", now_utc_iso())

    memory = load_memory()

    # Step 1 — Account
    print("\nSTEP 1 — ACCOUNT")
    print("---------------------------------------------------")
    account = get_account()
    account_state = extract_nlv_cash(account)
    nlv = account_state["nlv"]
    cash = account_state["cash"]
    if memory.get("portfolio_start_nlv") is None and nlv > 0:
        memory["portfolio_start_nlv"] = nlv
    print(pretty(account_state))

    # Step 2 — Positions
    print("\nSTEP 2 — POSITIONS")
    print("---------------------------------------------------")
    raw_positions = get_positions()
    update_position_open_dates(memory, raw_positions)
    positions = normalize_positions(raw_positions, nlv, memory)
    print(pretty(positions))

    # Step 3 — Portfolio snapshot
    print("\nSTEP 3 — PORTFOLIO SNAPSHOT")
    print("---------------------------------------------------")
    portfolio_state = portfolio_perf_snapshot(nlv, cash, positions, memory)
    print(pretty(portfolio_state))

    # Step 4 — Memory & convictions
    print("\nSTEP 4 — MEMORY & CONVICTIONS")
    print("---------------------------------------------------")
    print(pretty({
        "portfolio_start_nlv": memory.get("portfolio_start_nlv"),
        "last_run_at": memory.get("last_run_at"),
        "recent_decisions": memory.get("recent_decisions", [])[-MAX_DECISIONS_IN_PROMPT:],
        "convictions": memory.get("convictions", {}),
    }))

    # Step 5 — Research: new opportunities
    print("\nSTEP 5 — RESEARCH: NEW OPPORTUNITIES")
    print("---------------------------------------------------")
    new_opps_research = research_new_opportunities()
    print(new_opps_research)

    # Step 6 — Research: held positions
    print("\nSTEP 6 — RESEARCH: HELD POSITIONS")
    print("---------------------------------------------------")
    held_positions_research = research_held_positions(positions)
    print(held_positions_research)

    # Step 7 — Trader decision
    print("\nSTEP 7 — TRADER DECISION")
    print("---------------------------------------------------")
    decision = trader_decision(
        portfolio_state=portfolio_state,
        positions=positions,
        new_opps_research=new_opps_research,
        held_positions_research=held_positions_research,
        memory=memory,
    )
    raw_text = decision.pop("_raw_text", "")
    print("Raw trader output:")
    print(raw_text)
    print("\nParsed:")
    print(pretty(decision))

    # Step 8 — Execution preview
    print("\nSTEP 8 — EXECUTION PREVIEW (NO ORDERS PLACED)")
    print("---------------------------------------------------")
    preview = build_execution_preview(portfolio_state, positions, decision)
    print(pretty(preview))

    # Step 9 — Update memory
    now = now_utc_iso()
    update_convictions(memory, decision, positions)

    slim = slim_decision(decision, nlv, now)
    memory.setdefault("recent_decisions", []).append(slim)
    memory["recent_decisions"] = memory["recent_decisions"][-MAX_DECISION_HISTORY:]
    memory["last_run_at"] = now
    save_memory(memory)

    print("\nSTEP 9 — UPDATED CONVICTIONS & DECISION LOG")
    print("---------------------------------------------------")
    print(pretty({
        "convictions": memory.get("convictions", {}),
        "recent_decisions": memory.get("recent_decisions", []),
    }))

    print("\n===================================================")
    print("END OF SINGLE RUN")
    print("===================================================\n")


if __name__ == "__main__":
    run_view()