"""
main.py — Orchestrator run loop.
 
Steps:
  1  Account — fetch NLV and cash
  2  Positions — fetch and normalize with live prices
  3  Snapshot — portfolio performance state
  4  Memory — load convictions and decision history
  5  Research — new opportunities (4-call pipeline)
  6  Research — held positions (4-call pipeline)
  7  Decision — trader agent decides target portfolio
  7b Validate — hard rules enforced
  8  Preview — compute orders needed
  9  Execute — sells first, then buys
  10 Re-fetch — actual positions after fills
  11 Memory — update convictions and decision record
  12 Notify — email on REBALANCE or EXIT_TO_CASH
"""
 
import json
import time
from datetime import datetime, timezone
 
from .config import FORCE_RUN, POST_TRADE_SETTLE_SECS, MARKET_TZ, MARKET_OPEN, MARKET_CLOSE, log
from .broker import get_account, get_positions
from .memory import (
    load, save, update_open_dates, record_decision,
    update_convictions, safe_float,
)
from .execution import extract_nlv_cash, normalize_positions, portfolio_snapshot, build_preview, execute
from .research import research_opportunities, research_held_positions
from .trader import decide, validate
from .notify import send
 
 
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
 
 
def is_market_open() -> bool:
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() < MARKET_CLOSE
 
 
def run() -> None:
    log.info("=" * 60)
    log.info("ORCHESTRATOR RUN START")
    log.info("=" * 60)
 
    if FORCE_RUN:
        log.info("FORCE_RUN=true — bypassing market hours guard")
    elif not is_market_open():
        log.info("Market is closed. Exiting.")
        return
 
    memory = load()
 
    # ── Step 1 — Account ──────────────────────────────────────
    log.info("STEP 1 — ACCOUNT")
    account = get_account()
    state   = extract_nlv_cash(account)
    nlv, cash = state["nlv"], state["cash"]
 
    if memory.get("portfolio_start_nlv") is None and nlv > 0:
        memory["portfolio_start_nlv"] = nlv
        save(memory)
    log.info(f"  NLV={nlv:.2f}  Cash={cash:.2f}")
 
    # ── Step 2 — Positions ────────────────────────────────────
    log.info("STEP 2 — POSITIONS")
    raw_positions = get_positions()
    update_open_dates(memory, raw_positions)
    positions = normalize_positions(raw_positions, nlv, memory)
    for p in positions:
        log.info(f"  {p['symbol']:6}  qty={p['qty']}  price={p['current_price']}  "
                 f"pnl={p['unrealized_pnl_pct']:+.2f}%  (${p['unrealized_pnl_dollars']:+.2f})")
 
    # ── Step 3 — Portfolio snapshot ───────────────────────────
    log.info("STEP 3 — PORTFOLIO SNAPSHOT")
    port_state = portfolio_snapshot(nlv, cash, positions, memory)
    log.info(f"  {json.dumps(port_state, indent=2)}")
 
    # ── Step 4 — Memory ───────────────────────────────────────
    log.info("STEP 4 — MEMORY")
    log.info(f"  Convictions: {list(memory.get('convictions', {}).keys())}")
    log.info(f"  Decisions:   {len(memory.get('recent_decisions', []))}")
 
    # ── Step 5 — Research: new opportunities ──────────────────
    log.info("STEP 5 — RESEARCH: NEW OPPORTUNITIES")
    run_ts   = now_utc()
    new_opps = research_opportunities(run_ts)
    log.info(f"  Synthesis complete ({len(new_opps)} chars)")
 
    # ── Step 6 — Research: held positions ────────────────────
    log.info("STEP 6 — RESEARCH: HELD POSITIONS")
    held_review = research_held_positions(positions, run_ts)
    log.info(f"  Synthesis complete ({len(held_review)} chars)")
 
    # ── Step 7 — Trader decision ──────────────────────────────
    log.info("STEP 7 — TRADER DECISION")
    decision = decide(port_state, positions, new_opps, held_review, memory)
    decision.pop("_raw_text", None)
 
    # ── Step 7b — Validate ────────────────────────────────────
    log.info("STEP 7b — VALIDATING DECISION")
    decision = validate(decision, positions)
    action   = decision.get("portfolio_action", "UNKNOWN")
    reason   = decision.get("reason", "")
    log.info(f"  Action: {action}")
    log.info(f"  Reason: {reason}")
    for t in decision.get("targets", []):
        log.info(f"  Target: {t['symbol']} {t.get('target_pct', 0)*100:.1f}%  — {t.get('thesis', '')}")
 
    # ── Step 8 — Execution preview ────────────────────────────
    log.info("STEP 8 — EXECUTION PREVIEW")
    preview = build_preview(port_state, positions, decision)
    for p in preview:
        if p["suggested_action"] != "HOLD" or p["symbol"] == "CASH":
            log.info(f"  {p['suggested_action']:4}  {p['symbol']:6}  "
                     f"delta={p.get('delta_qty', 'n/a')}  target=${p['target_dollars']:.2f}")
 
    # ── Step 9 — Execute orders ───────────────────────────────
    log.info("STEP 9 — EXECUTING ORDERS")
    order_results = execute(preview)
    any_failures  = any("error" in r for r in order_results)
    if order_results:
        log.info(f"  {len(order_results)} order(s) placed")
        for r in order_results:
            if "error" in r:
                log.error(f"  FAILED {r['side']} {r['qty']} {r['symbol']}: {r['error']}")
            else:
                log.info(f"  OK     {r['side']} {r['qty']} {r['symbol']}")
    else:
        log.info("  No orders needed (HOLD)")
 
    # ── Step 10 — Re-fetch actual positions ───────────────────
    log.info("STEP 10 — RE-FETCHING ACTUAL POSITIONS")
    if order_results:
        log.info(f"  Waiting {POST_TRADE_SETTLE_SECS}s for broker to settle ...")
        time.sleep(POST_TRADE_SETTLE_SECS)
 
    post_account  = get_account()
    post_state    = extract_nlv_cash(post_account)
    post_nlv      = post_state["nlv"]
    post_cash     = post_state["cash"]
    post_raw      = get_positions()
    update_open_dates(memory, post_raw)
    actual        = normalize_positions(post_raw, post_nlv, memory)
 
    log.info(f"  Post-trade NLV={post_nlv:.2f}  Cash={post_cash:.2f}")
    for p in actual:
        log.info(f"  ACTUAL  {p['symbol']:6}  qty={p['qty']}  price={p['current_price']}  "
                 f"weight={p['portfolio_weight']*100:.1f}%  pnl={p['unrealized_pnl_pct']:+.2f}%")
 
    # ── Step 11 — Update memory ───────────────────────────────
    log.info("STEP 11 — UPDATING MEMORY")
    update_convictions(memory, decision, actual)
 
    # Build actual holdings string
    actual_str = " ".join(f"{p['symbol']}:{round(p['portfolio_weight']*100)}%" for p in actual)
    if post_cash > post_nlv * 0.01:
        actual_str = (actual_str + f" CASH:{round(post_cash / post_nlv * 100)}%").strip()
    if not actual_str:
        actual_str = "CASH:100%"
 
    intended_str = " ".join(
        f"{t['symbol']}:{round(safe_float(t.get('target_pct', 0)) * 100)}%"
        for t in decision.get("targets", [])
    )
 
    record_decision(memory, action, post_nlv, actual_str, intended_str, reason, any_failures)
    save(memory)
    log.info(f"  Memory saved. Convictions: {list(memory.get('convictions', {}).keys())}")
 
    # ── Step 12 — Notify ──────────────────────────────────────
    if action in ("REBALANCE", "EXIT_TO_CASH"):
        body = (
            f"Action  : {action}\n"
            f"Actual  : {actual_str}\n"
            f"Intended: {intended_str}\n"
            f"Reason  : {reason}\n\n"
        )
        for r in order_results:
            tag = "FAILED" if "error" in r else "OK"
            body += f"  {tag} {r['side']} {r['qty']} {r['symbol']}\n"
        send(f"[Autotrader] {action} — {actual_str}", body)
 
    log.info("=" * 60)
    log.info("ORCHESTRATOR RUN END")
    log.info("=" * 60)
 
 
if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception(f"Unhandled error: {e}")
        send("[Autotrader] ERROR — run failed", f"Orchestrator crashed:\n\n{e}")
        raise