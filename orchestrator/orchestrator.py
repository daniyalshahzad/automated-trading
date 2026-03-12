import json
import logging
import math
import os
import smtplib
from datetime import datetime, timezone, time as dtime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

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

API_VERSION   = os.getenv("AZURE_API_VERSION", "2025-11-15-preview")
RESEARCH_BASE = os.getenv("AZURE_RESEARCHER_URL")
TRADER_BASE   = os.getenv("AZURE_TRADER_URL")

if not RESEARCH_BASE or not TRADER_BASE:
    raise RuntimeError("AZURE_RESEARCHER_URL and AZURE_TRADER_URL must be set in .env")

MEMORY_PATH    = Path(__file__).resolve().parent / "trading_memory.json"
RESEARCH_DIR   = Path(__file__).resolve().parent / "research_logs"
RESEARCH_DIR.mkdir(exist_ok=True)
LOG_DIR        = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ──────────────────────────────────────────────────
log_file = LOG_DIR / f"orchestrator_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),          # also print to stdout
    ],
)
log = logging.getLogger("orchestrator")

# ── Market hours (NYSE/NASDAQ) ────────────────────────────────
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

# ── Notifications (email) ─────────────────────────────────────
# Set these in your .env or leave empty to disable notifications
NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO   = os.getenv("NOTIFY_EMAIL_TO", "")
NOTIFY_SMTP_HOST  = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
NOTIFY_SMTP_PORT  = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
NOTIFY_SMTP_USER  = os.getenv("NOTIFY_SMTP_USER", "")
NOTIFY_SMTP_PASS  = os.getenv("NOTIFY_SMTP_PASS", "")

# ── Force run (bypasses market hours guard — for testing) ─────
FORCE_RUN = os.getenv("FORCE_RUN", "false").lower() == "true"

# Hard rules for the trader
MAX_POSITIONS = 3
MAX_SINGLE_NAME_PCT = 0.80
MIN_NEW_POSITION_PCT = 0.10
ALLOW_FULL_CASH = True

# Order sizing — reserve a cash buffer to absorb market order slippage
# Target dollars are reduced by this factor before computing share qty
CASH_BUFFER_PCT = 0.02          # 2% buffer — reduces overshoot on market buys

# Minimum delta to actually place an order — suppresses noise rebalances
# If abs(delta_dollars) < NLV * MIN_REBALANCE_DELTA_PCT, treat as HOLD
MIN_REBALANCE_DELTA_PCT = 0.02  # ignore deltas smaller than 2% of NLV

# After placing orders, wait this many seconds before re-fetching actual positions
POST_TRADE_SETTLE_SECS = 5

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
# MARKET HOURS
# ============================================================

def is_market_open() -> bool:
    """Returns True if current time is within NYSE/NASDAQ trading hours."""
    now_et = datetime.now(MARKET_TZ)
    if now_et.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


# ============================================================
# NOTIFICATIONS
# ============================================================

def send_notification(subject: str, body: str) -> None:
    """Send an email alert. Silently skips if credentials are not configured."""
    if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS]):
        log.info("Notifications not configured — skipping.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = NOTIFY_EMAIL_FROM
        msg["To"] = NOTIFY_EMAIL_TO
        with smtplib.SMTP(NOTIFY_SMTP_HOST, NOTIFY_SMTP_PORT) as s:
            s.starttls()
            s.login(NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS)
            s.sendmail(NOTIFY_EMAIL_FROM, [NOTIFY_EMAIL_TO], msg.as_string())
        log.info(f"Notification sent: {subject}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")


# ============================================================
# ORDER EXECUTION
# ============================================================

def place_order(symbol: str, side: str, qty: int) -> Dict[str, Any]:
    """
    Places a single market order via the bridge.
    side must be "BUY" or "SELL".
    Uses /v1/orders/place with the bridge's OrderIntent schema.
    Requires ENABLE_TRADING=true in bridge .env.
    Returns the broker response dict.
    """
    payload = {
        "orders": [
            {
                "symbol": symbol.upper(),
                "side": side.lower(),   # bridge expects "buy" or "sell"
                "order_type": "MKT",
                "tif": "DAY",
                "quantity": qty,
            }
        ]
    }
    log.info(f"  Placing order: {side} {qty} {symbol}")
    r = requests.post(
        f"{BRIDGE_URL}/v1/orders/place",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    log.info(f"  Order response: {result}")
    return result


def wait_for_fills(order_ids: List[int], timeout_secs: int = 60) -> Dict[int, str]:
    """
    Poll bridge trades until all order IDs reach a terminal state.
    Returns a dict of {order_id: status}.
    Falls back gracefully if trades endpoint is unavailable.
    """
    import time
    TERMINAL = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
    deadline = time.time() + timeout_secs
    last_statuses: Dict[int, str] = {}

    while time.time() < deadline:
        trades = get_trades()
        statuses = {}
        for t in trades:
            oid = t.get("orderId") or t.get("order_id")
            status = t.get("status") or t.get("orderStatus", "")
            if oid is not None:
                statuses[int(oid)] = str(status)

        last_statuses = statuses
        pending = [oid for oid in order_ids if statuses.get(oid) not in TERMINAL]
        if not pending:
            log.info(f"  All {len(order_ids)} order(s) reached terminal state")
            return statuses

        log.info(f"  Waiting for fills — {len(pending)} order(s) still pending ...")
        time.sleep(3)

    log.warning(f"  Fill polling timed out after {timeout_secs}s — proceeding with re-fetch anyway")
    return last_statuses


def execute_orders(preview: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Executes orders from the execution preview.
    Sells first to free up cash, then buys.
    Before placing buys, re-fetches available cash to avoid insufficient-funds rejections.
    Skips CASH rows and HOLD rows.
    Returns list of order results with order IDs for fill polling.
    """
    import time

    sells = [p for p in preview if p["suggested_action"] == "SELL" and p["symbol"] != "CASH"]
    buys  = [p for p in preview if p["suggested_action"] == "BUY"  and p["symbol"] != "CASH"]

    results = []
    placed_order_ids: List[int] = []

    # --- Execute sells first ---
    sell_failures = 0
    for order in sells:
        symbol = order["symbol"]
        qty    = abs(order["delta_qty"])
        if qty <= 0:
            continue
        try:
            result = place_order(symbol, "SELL", qty)
            oid = _extract_order_id(result)
            if oid:
                placed_order_ids.append(oid)
            results.append({"symbol": symbol, "side": "SELL", "qty": qty, "result": result})
        except Exception as e:
            sell_failures += 1
            log.error(f"  Order failed for {symbol} SELL {qty}: {e}")
            results.append({"symbol": symbol, "side": "SELL", "qty": qty, "error": str(e)})

    # --- Wait briefly for sells to settle before checking cash ---
    if sells:
        log.info("  Waiting 3s for sell proceeds before placing buys ...")
        time.sleep(3)

    # --- Check available cash before buys ---
    if buys:
        try:
            post_sell_account = get_account()
            available_cash = safe_float(extract_nlv_cash(post_sell_account).get("cash", 0.0))
            log.info(f"  Available cash for buys: ${available_cash:,.2f}")
        except Exception as e:
            log.warning(f"  Could not fetch post-sell cash ({e}) — proceeding with buys cautiously")
            available_cash = float("inf")  # let bridge reject if insufficient

        # Sort buys largest first — prioritise highest-conviction allocations
        buys_sorted = sorted(buys, key=lambda p: p["target_dollars"], reverse=True)
        cash_remaining = available_cash

        for order in buys_sorted:
            symbol       = order["symbol"]
            qty          = abs(order["delta_qty"])
            price        = safe_float(order.get("price_used", 0.0))
            est_cost     = qty * price if price > 0 else 0.0

            if qty <= 0:
                continue

            if available_cash != float("inf") and est_cost > cash_remaining * 1.01:
                # 1% tolerance for price movement since preview
                log.warning(f"  Skipping BUY {qty} {symbol} — estimated cost ${est_cost:,.0f} "
                            f"exceeds remaining cash ${cash_remaining:,.0f}")
                results.append({"symbol": symbol, "side": "BUY", "qty": qty,
                                 "error": f"Insufficient cash (est ${est_cost:,.0f} > ${cash_remaining:,.0f})"})
                continue

            try:
                result = place_order(symbol, "BUY", qty)
                oid = _extract_order_id(result)
                if oid:
                    placed_order_ids.append(oid)
                cash_remaining -= est_cost
                results.append({"symbol": symbol, "side": "BUY", "qty": qty, "result": result})
            except Exception as e:
                log.error(f"  Order failed for {symbol} BUY {qty}: {e}")
                results.append({"symbol": symbol, "side": "BUY", "qty": qty, "error": str(e)})

    # --- Poll for fills ---
    if placed_order_ids:
        fill_statuses = wait_for_fills(placed_order_ids, timeout_secs=60)
        for r in results:
            oid = _extract_order_id(r.get("result", {}))
            if oid and oid in fill_statuses:
                r["fill_status"] = fill_statuses[oid]

    return results


def _extract_order_id(result: Any) -> Optional[int]:
    """Extract the first orderId from a bridge place_order response."""
    if not isinstance(result, dict):
        return None
    placed = result.get("placed", [])
    if placed and isinstance(placed, list):
        oid = placed[0].get("orderId")
        return int(oid) if oid is not None else None
    return None


# ============================================================
# BROKER CALLS
# ============================================================

def get_account() -> Dict[str, Any]:
    r = requests.get(f"{BRIDGE_URL}/broker/account", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def get_trades() -> List[Dict[str, Any]]:
    """Fetch recent trades/orders from the bridge for fill status polling."""
    try:
        r = requests.get(f"{BRIDGE_URL}/broker/trades", headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("trades", data.get("orders", []))
    except Exception as e:
        log.warning(f"  get_trades failed: {e}")
        return []


def resolve_symbol_exchange(symbol: str) -> Optional[str]:
    """
    Resolves the primary exchange for a symbol via the bridge contract endpoint.
    Returns the primaryExch string (e.g. 'NASDAQ', 'NYSE') or None on failure.
    """
    try:
        r = requests.get(
            f"{BRIDGE_URL}/v1/contract/resolve",
            headers=HEADERS,
            params={"symbol": symbol, "secType": "STK", "currency": "USD"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        # Bridge may return a list of contract candidates or a single object
        if isinstance(data, list) and data:
            return str(data[0].get("primaryExch", "")).upper().strip() or None
        if isinstance(data, dict):
            return str(data.get("primaryExch", "")).upper().strip() or None
    except Exception as e:
        log.warning(f"  resolve_symbol_exchange({symbol}) failed: {e}")
    return None


def get_positions() -> List[Dict[str, Any]]:
    r = requests.get(f"{BRIDGE_URL}/broker/positions", headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    # bridge returns {"positions": [...], "timestamp_utc": "..."}
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        positions = data.get("positions", [])
        # normalize field names: bridge uses "position" not "qty"
        out = []
        for p in positions:
            p = dict(p)
            if "position" in p and "qty" not in p:
                p["qty"] = p["position"]
            if "avgCost" in p and "avg_price" not in p:
                p["avg_price"] = p["avgCost"]
            out.append(p)
        return out
    return []


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

    # Only symbols that actually exist in the portfolio after execution
    # This prevents phantom convictions when a buy fails
    actual_symbols = set(position_map.keys())

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
        thesis = str(target.get("thesis", "")).strip()

        # live price and actual weight from real position data
        live_price: Optional[float] = None
        actual_weight: Optional[float] = None
        if symbol in position_map:
            live_price = safe_float(position_map[symbol].get("current_price", 0.0)) or None
            actual_weight = safe_float(position_map[symbol].get("portfolio_weight", 0.0)) or None
        if not live_price:
            live_price = fetch_live_price(symbol)

        # Use actual portfolio weight if available, fall back to intended target_pct
        # This ensures conviction reflects what's really in the account
        recorded_pct = round(actual_weight, 4) if actual_weight is not None else round(target_pct, 4)

        if symbol not in convictions:
            # Only create a NEW conviction if the position actually exists
            # Guards against buy failures creating phantom convictions
            if symbol not in actual_symbols:
                log.warning(f"  [convictions] Skipping new conviction for {symbol} — not found in actual positions (buy may have failed)")
                continue
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
                "current_target_pct": recorded_pct,
                "target_pct_history": [recorded_pct],
            }
        else:
            # Reaffirm existing conviction regardless — position existed before this run
            # (e.g. NVDA held from prior run, still in actual_positions)
            c = convictions[symbol]
            c["last_reaffirmed"] = now
            c["reaffirm_count"] = c.get("reaffirm_count", 1) + 1
            c["current_target_pct"] = recorded_pct

            history = c.get("target_pct_history", []) + [recorded_pct]
            c["target_pct_history"] = history[-MAX_TARGET_PCT_HISTORY:]

            if live_price:
                c["last_price"] = round(live_price, 4)
                entry = safe_float(c.get("entry_price", 0.0))
                if entry > 0:
                    c["pnl_pct_since_conviction"] = round(
                        ((live_price - entry) / entry) * 100.0, 2
                    )

            if thesis:
                c["latest_thesis"] = thesis

    # Drop convictions for symbols no longer in actual portfolio.
    # Exception: if a symbol is still in actual_positions despite not being targeted,
    # the sell likely failed. Keep the conviction alive but flag it as pending_exit
    # so the trader knows it's an unwanted position, not a reaffirmed one.
    for sym in list(convictions.keys()):
        if sym not in actual_symbols:
            log.info(f"  [convictions] Dropping {sym} — no longer in actual positions")
            del convictions[sym]

    # Mark positions that are in actual portfolio but NOT in targets as pending_exit
    # This happens when a sell order fails — position remains, conviction should too
    targeted_symbols = {
        str(t.get("symbol", "")).upper().strip()
        for t in targets
        if str(t.get("symbol", "")).upper().strip() not in ("", "CASH")
    }
    for sym in actual_symbols:
        if sym not in targeted_symbols:
            if sym not in convictions:
                # Position exists but no conviction — create one so trader knows about it
                p = position_map[sym]
                live_price = safe_float(p.get("current_price", 0.0)) or None
                actual_weight = safe_float(p.get("portfolio_weight", 0.0)) or None
                convictions[sym] = {
                    "symbol": sym,
                    "first_added": now,
                    "last_reaffirmed": now,
                    "reaffirm_count": 1,
                    "entry_price": round(live_price, 4) if live_price else None,
                    "last_price": round(live_price, 4) if live_price else None,
                    "pnl_pct_since_conviction": 0.0,
                    "initial_thesis": "Position retained after failed sell order.",
                    "latest_thesis": "Position retained after failed sell order.",
                    "current_target_pct": round(actual_weight, 4) if actual_weight else 0.0,
                    "target_pct_history": [round(actual_weight, 4) if actual_weight else 0.0],
                    "pending_exit": True,
                }
                log.warning(f"  [convictions] {sym} is in portfolio but not targeted — sell may have failed. Flagged pending_exit.")
            else:
                convictions[sym]["pending_exit"] = True
                log.warning(f"  [convictions] {sym} still held but not in targets — flagged pending_exit")
        else:
            # Clear pending_exit flag if symbol is now targeted again
            if sym in convictions:
                convictions[sym].pop("pending_exit", None)


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
        pending_exit = c.get("pending_exit", False)
        exit_flag = " ⚠️ PENDING EXIT (sell order failed — must exit this run)" if pending_exit else ""

        lines.append(f"\n  [{sym}]{exit_flag} — opened {since} | {sticky}")
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
        account.get("availableFunds",
        account.get("available_funds",
        account.get("cash",
        account.get("AvailableFunds",
        account.get("totalCashValue", 0.0)))))
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
# RESEARCH — 4-call pipeline per research type
# Call 1: first independent pass
# Call 2: second independent pass (different search path)
# Call 3: devil's advocate (bearish counter-arguments on both)
# Call 4: synthesis (balanced final view passed to trader)
# All 4 outputs saved to research_logs/ per run.
# ============================================================

def _call_research(prompt: str) -> str:
    response = research_client.responses.create(input=prompt)
    return response.output_text


def _synthesize(pass1: str, pass2: str, devil: str, context: str) -> str:
    prompt = f"""
You are a senior research analyst synthesizing three independent research reports into one balanced view.

{context}

--- REPORT 1 ---
{pass1}

--- REPORT 2 ---
{pass2}

--- DEVIL'S ADVOCATE ---
{devil}

Your task:
- For each symbol mentioned across the reports, write a balanced synthesis.
- Note where reports AGREE (higher conviction) and where they CONFLICT (flag explicitly).
- Give a net conviction: HIGH / MEDIUM / LOW / AVOID after weighing bull and bear cases.
- Be concise. One paragraph per symbol max.
- Do NOT pick sides — present the balanced picture so a trader can make an informed decision.
- Flag any symbol where the devil's advocate raised a material risk not addressed in Reports 1 or 2.
"""
    response = research_client.responses.create(input=prompt)
    return response.output_text


def save_research_log(run_ts: str, label: str, data: Dict[str, Any]) -> None:
    """Save research outputs to research_logs/YYYYMMDD_HHMMSS_<label>.json"""
    safe_ts = run_ts.replace(":", "").replace(".", "")[:15]
    path = RESEARCH_DIR / f"{safe_ts}_{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"  Research log saved: {path.name}")


def research_new_opportunities(run_ts: str) -> str:
    base_prompt = f"""
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

    log.info("  [Opportunities] Pass 1 ...")
    pass1 = _call_research(base_prompt)
    log.info("  [Opportunities] Pass 2 ...")
    pass2 = _call_research(base_prompt)

    devil_prompt = f"""
You are a contrarian research analyst with web search enabled.

Two research reports have identified the following NASDAQ stock opportunities.
Your job is to steelman the BEARISH case for every symbol mentioned.

Find what the reports missed:
- Weak technicals, broken chart structure, below key moving averages
- Analyst downgrades or price target cuts in the last 30 days
- Upcoming binary events (earnings, FDA, macro) that create downside risk
- Sector headwinds or macro risks not mentioned
- Valuation concerns
- Any recent negative news, lawsuits, regulatory risks

--- REPORT 1 ---
{pass1}

--- REPORT 2 ---
{pass2}

Be specific and cite real data where possible. Do not invent risks — only surface genuine concerns.
"""
    log.info("  [Opportunities] Devil's advocate ...")
    devil = _call_research(devil_prompt)

    log.info("  [Opportunities] Synthesis ...")
    synthesis = _synthesize(
        pass1, pass2, devil,
        context="These reports cover potential NEW stock opportunities for a swing trading portfolio (7-14 day horizon)."
    )

    save_research_log(run_ts, "opportunities", {
        "pass1": pass1,
        "pass2": pass2,
        "devil_advocate": devil,
        "synthesis": synthesis,
    })

    return synthesis


def research_held_positions(positions: List[Dict[str, Any]], run_ts: str) -> str:
    if not positions:
        return "No currently held positions."

    symbols = ", ".join([p["symbol"] for p in positions])
    pos_detail = json.dumps(positions, indent=2)

    base_prompt = f"""
You are a market research agent with web search enabled.

Evaluate these currently held portfolio positions for the next 7-14 days: {symbols}

Current position details:
{pos_detail}

For each symbol provide:
- Keep / trim / exit leaning
- Updated thesis: what is the current conviction?
- Bullish points
- Bearish points / key risk
- Near-term catalyst(s)
- Confidence: high / medium / low

Be comparative and tactical.
"""

    log.info("  [Held Positions] Pass 1 ...")
    pass1 = _call_research(base_prompt)
    log.info("  [Held Positions] Pass 2 ...")
    pass2 = _call_research(base_prompt)

    devil_prompt = f"""
You are a contrarian research analyst with web search enabled.

A swing trading portfolio currently holds: {symbols}

Two research reports have evaluated these positions.
Your job is to steelman the case for EXITING or TRIMMING each position.

Find what the reports may have missed or understated:
- Technical deterioration, broken support levels, below key moving averages
- Analyst downgrades or price target cuts in the last 30 days
- Upcoming binary events that create asymmetric downside
- Sector rotation risks or macro headwinds
- Any recent negative news

--- REPORT 1 ---
{pass1}

--- REPORT 2 ---
{pass2}

Be specific. Only surface genuine concerns backed by real data.
"""
    log.info("  [Held Positions] Devil's advocate ...")
    devil = _call_research(devil_prompt)

    log.info("  [Held Positions] Synthesis ...")
    synthesis = _synthesize(
        pass1, pass2, devil,
        context=f"These reports evaluate CURRENTLY HELD positions ({symbols}) in a swing trading portfolio."
    )

    save_research_log(run_ts, "held_positions", {
        "symbols": symbols,
        "pass1": pass1,
        "pass2": pass2,
        "devil_advocate": devil,
        "synthesis": synthesis,
    })

    return synthesis


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

def validate_decision(decision: Dict[str, Any], positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Hard validation of trader JSON output.
    Corrects what it can, raises on fatal issues.
    Logs a warning for every correction made so we can track model drift.
    """
    VALID_ACTIONS = {"HOLD", "REBALANCE", "EXIT_TO_CASH"}

    # 1 — portfolio_action
    action = str(decision.get("portfolio_action", "")).upper().strip()
    if action not in VALID_ACTIONS:
        log.warning(f"  [validate] Invalid portfolio_action '{action}' — defaulting to HOLD")
        decision["portfolio_action"] = "HOLD"
        action = "HOLD"

    targets = decision.get("targets", [])
    if not isinstance(targets, list):
        log.warning("  [validate] targets is not a list — resetting to empty")
        decision["targets"] = []
        targets = []

    # 2 — normalize and deduplicate symbols
    seen = set()
    clean = []
    for t in targets:
        sym = str(t.get("symbol", "")).upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        t["symbol"] = sym
        t["target_pct"] = max(0.0, min(1.0, safe_float(t.get("target_pct", 0.0))))
        clean.append(t)
    decision["targets"] = clean
    targets = clean

    # 3 — EXIT_TO_CASH: all non-cash targets must be zero
    if action == "EXIT_TO_CASH":
        for t in targets:
            if t["symbol"] != "CASH" and t["target_pct"] > 0:
                log.warning(f"  [validate] EXIT_TO_CASH but {t['symbol']} has target_pct={t['target_pct']} — zeroing")
                t["target_pct"] = 0.0

    # 2.5 — exchange validation: only NASDAQ symbols allowed
    # Only zero a target if we get a DEFINITIVE non-NASDAQ response from the bridge.
    # If the endpoint is unavailable or returns nothing, leave the target unchanged —
    # unknown is not the same as wrong. This prevents a bridge outage from liquidating the portfolio.
    validated_non_cash = []
    for t in [x for x in decision["targets"] if x["symbol"] != "CASH"]:
        sym = t["symbol"]
        if t["target_pct"] <= 0:
            validated_non_cash.append(t)
            continue
        exch = resolve_symbol_exchange(sym)
        if exch is None:
            # Could not resolve — bridge may be down or endpoint unsupported. Pass through.
            log.warning(f"  [validate] Could not resolve exchange for {sym} — passing through (bridge unavailable)")
        elif exch != ALLOWED_EXCHANGE:
            log.warning(f"  [validate] {sym} trades on {exch}, not {ALLOWED_EXCHANGE} — zeroing target")
            t["target_pct"] = 0.0
        else:
            log.info(f"  [validate] {sym} exchange confirmed: {exch}")
        validated_non_cash.append(t)
    decision["targets"] = validated_non_cash + [t for t in decision["targets"] if t["symbol"] == "CASH"]
    targets = decision["targets"]

    non_cash = [t for t in targets if t["symbol"] != "CASH"]
    cash_targets = [t for t in targets if t["symbol"] == "CASH"]
    if len(non_cash) > MAX_POSITIONS:
        log.warning(f"  [validate] {len(non_cash)} non-cash targets exceeds MAX_POSITIONS={MAX_POSITIONS} — trimming smallest")
        non_cash = sorted(non_cash, key=lambda t: t["target_pct"], reverse=True)[:MAX_POSITIONS]
        decision["targets"] = non_cash + cash_targets
        targets = decision["targets"]

    # 5 — max single-name allocation
    for t in non_cash:
        if t["target_pct"] > MAX_SINGLE_NAME_PCT:
            log.warning(f"  [validate] {t['symbol']} target_pct={t['target_pct']} > MAX_SINGLE_NAME_PCT={MAX_SINGLE_NAME_PCT} — capping")
            t["target_pct"] = MAX_SINGLE_NAME_PCT

    # 6 — min new position size
    current_symbols = {p["symbol"] for p in positions}
    for t in non_cash:
        if t["symbol"] not in current_symbols and 0 < t["target_pct"] < MIN_NEW_POSITION_PCT:
            log.warning(f"  [validate] New position {t['symbol']} target_pct={t['target_pct']} < MIN={MIN_NEW_POSITION_PCT} — zeroing")
            t["target_pct"] = 0.0

    # 7 — non-empty thesis for every non-cash target
    for t in non_cash:
        if not str(t.get("thesis", "")).strip():
            log.warning(f"  [validate] {t['symbol']} has empty thesis — filling placeholder")
            t["thesis"] = "No thesis provided."

    # 8 — targets must sum to ~1.0; if not, inject or adjust CASH
    total = sum(t["target_pct"] for t in decision["targets"])
    residual = round(1.0 - total, 6)
    if abs(residual) > 0.001:
        if cash_targets:
            cash_targets[0]["target_pct"] = max(0.0, cash_targets[0]["target_pct"] + residual)
            log.warning(f"  [validate] Targets summed to {total:.4f} — adjusted CASH by {residual:+.4f}")
        else:
            if residual > 0:
                decision["targets"].append({"symbol": "CASH", "target_pct": round(residual, 6), "thesis": ""})
                log.warning(f"  [validate] Targets summed to {total:.4f} — added CASH {residual:.4f}")

    return decision


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
        # Always apply cash buffer to target dollars before computing qty.
        # Floor division already rounds down, but the buffer gives extra room
        # for market order slippage. Applies to both buys and sells — on sells
        # it slightly undersells which is conservative and safe.
        target_dollars = nlv * target_pct * (1.0 - CASH_BUFFER_PCT)
        target_qty = int(math.floor(target_dollars / price)) if price > 0 else 0

        # Warn if floor division produced 0 shares despite a non-zero target
        # This means cash is too low to buy even 1 share — treated as HOLD
        if target_qty == 0 and target_pct > 0 and current_qty == 0 and price > 0:
            log.warning(f"  [{symbol}] target_qty=0 after floor (target=${target_dollars:.0f}, price=${price:.2f}) — insufficient cash for 1 share, treating as HOLD")

        delta_qty = target_qty - current_qty
        delta_dollars = abs(delta_qty * price) if price > 0 else 0

        # Suppress noise — don't place orders for tiny deltas
        # This handles the case where a stock "stays" in a REBALANCE but drifts by a few shares
        if delta_qty != 0 and delta_dollars < nlv * MIN_REBALANCE_DELTA_PCT:
            log.info(f"  [{symbol}] delta suppressed (${delta_dollars:.0f} < {MIN_REBALANCE_DELTA_PCT*100:.0f}% of NLV)")
            delta_qty = 0

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

def run() -> None:
    log.info("=" * 60)
    log.info("ORCHESTRATOR — SINGLE RUN")
    log.info("=" * 60)

    # ── Market hours guard ────────────────────────────────────
    if FORCE_RUN:
        log.info("FORCE_RUN=true — bypassing market hours guard.")
    elif not is_market_open():
        log.info("Market is closed. Exiting.")
        return

    memory = load_memory()

    # Step 1 — Account
    log.info("STEP 1 — ACCOUNT")
    account = get_account()
    account_state = extract_nlv_cash(account)
    nlv = account_state["nlv"]
    cash = account_state["cash"]
    # Set portfolio_start_nlv before snapshot so return % is correct even on first run
    if memory.get("portfolio_start_nlv") is None and nlv > 0:
        memory["portfolio_start_nlv"] = nlv
        save_memory(memory)   # persist immediately so a crash doesn't lose it
    log.info(f"  NLV={nlv:.2f}  Cash={cash:.2f}")

    # Step 2 — Positions
    log.info("STEP 2 — POSITIONS")
    raw_positions = get_positions()
    update_position_open_dates(memory, raw_positions)
    positions = normalize_positions(raw_positions, nlv, memory)
    for p in positions:
        log.info(f"  {p['symbol']:6}  qty={p['qty']}  price={p['current_price']}  "
                 f"pnl={p['unrealized_pnl_pct']:+.2f}%  (${p['unrealized_pnl_dollars']:+.2f})")

    # Step 3 — Portfolio snapshot
    log.info("STEP 3 — PORTFOLIO SNAPSHOT")
    portfolio_state = portfolio_perf_snapshot(nlv, cash, positions, memory)
    log.info(f"  {pretty(portfolio_state)}")

    # Step 4 — Memory & convictions
    log.info("STEP 4 — MEMORY & CONVICTIONS")
    log.info(f"  Active convictions: {list(memory.get('convictions', {}).keys())}")
    log.info(f"  Recent decisions  : {len(memory.get('recent_decisions', []))}")

    # Step 5 — Research: new opportunities (4-call pipeline)
    log.info("STEP 5 — RESEARCH: NEW OPPORTUNITIES (4-call pipeline)")
    run_ts = now_utc_iso()
    new_opps_research = research_new_opportunities(run_ts)
    log.info(f"  Synthesis complete ({len(new_opps_research)} chars)")

    # Step 6 — Research: held positions (4-call pipeline)
    log.info("STEP 6 — RESEARCH: HELD POSITIONS (4-call pipeline)")
    held_positions_research = research_held_positions(positions, run_ts)
    log.info(f"  Synthesis complete ({len(held_positions_research)} chars)")

    # Step 7 — Trader decision
    log.info("STEP 7 — TRADER DECISION")
    decision = trader_decision(
        portfolio_state=portfolio_state,
        positions=positions,
        new_opps_research=new_opps_research,
        held_positions_research=held_positions_research,
        memory=memory,
    )
    decision.pop("_raw_text", "")

    # Step 7b — Validate trader output (hard rules, not just prompt instructions)
    log.info("STEP 7b — VALIDATING DECISION")
    decision = validate_decision(decision, positions)
    action = decision.get("portfolio_action", "UNKNOWN")
    reason = decision.get("reason", "")
    log.info(f"  Action : {action}")
    log.info(f"  Reason : {reason}")
    for t in decision.get("targets", []):
        log.info(f"  Target : {t['symbol']} {t.get('target_pct', 0)*100:.1f}%  — {t.get('thesis', '')}")

    # Step 8 — Execution preview
    log.info("STEP 8 — EXECUTION PREVIEW")
    preview = build_execution_preview(portfolio_state, positions, decision)
    for p in preview:
        if p["suggested_action"] != "HOLD" or p["symbol"] == "CASH":
            log.info(f"  {p['suggested_action']:4}  {p['symbol']:6}  "
                     f"delta={p.get('delta_qty', 'n/a')}  "
                     f"target=${p['target_dollars']:.2f}")

    # Step 9 — Execute orders
    log.info("STEP 9 — EXECUTING ORDERS")
    order_results = execute_orders(preview)
    any_orders = len(order_results) > 0
    any_failures = any("error" in r for r in order_results)
    if order_results:
        log.info(f"  {len(order_results)} order(s) placed")
        for r in order_results:
            if "error" in r:
                log.error(f"  FAILED {r['side']} {r['qty']} {r['symbol']}: {r['error']}")
            else:
                log.info(f"  OK     {r['side']} {r['qty']} {r['symbol']}")
    else:
        log.info("  No orders needed (HOLD)")

    # Step 10 — Re-fetch actual positions AND account after execution
    # Fill polling already happened inside execute_orders via wait_for_fills.
    # We still wait a brief moment for the broker API to reflect settled state.
    log.info("STEP 10 — RE-FETCHING ACTUAL POSITIONS")
    if any_orders:
        import time
        log.info("  Waiting 3s for broker API to reflect settled positions ...")
        time.sleep(3)

    post_account = get_account()
    post_account_state = extract_nlv_cash(post_account)
    post_nlv  = post_account_state["nlv"]
    post_cash = post_account_state["cash"]
    post_raw_positions = get_positions()
    update_position_open_dates(memory, post_raw_positions)
    actual_positions = normalize_positions(post_raw_positions, post_nlv, memory)

    log.info(f"  Post-trade NLV={post_nlv:.2f}  Cash={post_cash:.2f}")
    if any_failures:
        log.warning("  Partial execution — memory will reflect actual holdings, not intended targets")
    for p in actual_positions:
        log.info(f"  ACTUAL  {p['symbol']:6}  qty={p['qty']}  price={p['current_price']}  "
                 f"weight={p['portfolio_weight']*100:.1f}%  "
                 f"pnl={p['unrealized_pnl_pct']:+.2f}%  (${p['unrealized_pnl_dollars']:+.2f})")

    # Step 11 — Update memory from actual holdings
    log.info("STEP 11 — UPDATING MEMORY")
    now = now_utc_iso()

    # Convictions built from actual positions, not intended targets
    # If a buy failed, that symbol won't be in actual_positions and won't get a conviction
    update_convictions(memory, decision, actual_positions)

    actual_holdings_str = " ".join(
        f"{p['symbol']}:{round(p['portfolio_weight']*100)}%"
        for p in actual_positions
    )
    # Add cash to holdings string if meaningful
    if post_cash > post_nlv * 0.01:
        cash_pct = round((post_cash / post_nlv) * 100)
        actual_holdings_str = (actual_holdings_str + f" CASH:{cash_pct}%").strip()
    if not actual_holdings_str:
        actual_holdings_str = "CASH:100%"

    slim = {
        "ts": now,
        "action": action,
        "nlv": round(post_nlv, 2),
        "holdings": actual_holdings_str,
        "intended_holdings": slim_decision(decision, nlv, now)["holdings"],
        "reason": str(reason)[:200],
        "execution_failures": any_failures,
    }
    memory.setdefault("recent_decisions", []).append(slim)
    memory["recent_decisions"] = memory["recent_decisions"][-MAX_DECISION_HISTORY:]
    memory["last_run_at"] = now
    save_memory(memory)
    log.info(f"  Memory saved from actual holdings. Convictions: {list(memory.get('convictions', {}).keys())}")

    # Step 12 — Notify on meaningful actions
    if action in ("REBALANCE", "EXIT_TO_CASH"):
        body = (
            f"Action   : {action}\n"
            f"Actual   : {actual_holdings_str}\n"
            f"Intended : {slim['intended_holdings']}\n"
            f"Reason   : {reason}\n\n"
            f"Orders placed: {len(order_results)}\n"
        )
        for r in order_results:
            if "error" in r:
                body += f"  FAILED {r['side']} {r['qty']} {r['symbol']}: {r['error']}\n"
            else:
                body += f"  OK     {r['side']} {r['qty']} {r['symbol']}\n"
        send_notification(f"[Autotrader] {action} — {actual_holdings_str}", body)

    log.info("=" * 60)
    log.info("END OF RUN")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Unhandled error in orchestrator run: {e}")
        send_notification(
            "[Autotrader] ERROR — run failed",
            f"Orchestrator crashed with error:\n\n{e}"
        )
        raise