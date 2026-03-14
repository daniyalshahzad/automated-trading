"""
execution.py — Position normalization, execution preview, and order execution.
Sells first, re-fetches cash, then buys largest first.
"""
 
import math
import time
from typing import Any, Dict, List, Optional
 
from .config import CASH_BUFFER_PCT, MIN_REBALANCE_DELTA_PCT, log
from .broker import get_account, get_positions, get_quote, place_order, wait_for_fills
from .memory import safe_float, days_held
 
 
# ── Account helpers ───────────────────────────────────────────
 
def extract_nlv_cash(account: Dict) -> Dict:
    nlv  = safe_float(account.get("nlv") or account.get("NetLiquidation") or account.get("net_liquidation"))
    cash = safe_float(
        account.get("availableFunds") or account.get("available_funds") or
        account.get("AvailableFunds") or account.get("totalCashValue")
    )
    return {"nlv": nlv, "cash": cash}
 
 
# ── Position normalization ────────────────────────────────────
 
def normalize_positions(raw: List[Dict], nlv: float, memory: Dict) -> List[Dict]:
    """Enrich raw broker positions with live prices, P&L, weight, days held."""
    out = []
    for p in raw:
        sym = str(p.get("symbol", "")).upper().strip()
        qty = int(safe_float(p.get("qty", p.get("position", 0))))
        if not sym or qty <= 0:
            continue
 
        avg   = safe_float(p.get("avg_price") or p.get("avgCost") or p.get("averageCost"))
        price = safe_float(p.get("current_price") or p.get("market_price") or p.get("price"))
        mktval = safe_float(p.get("market_value") or p.get("marketValue"))
 
        if price <= 0:
            fetched = get_quote(sym)
            if fetched:
                price = fetched
 
        if mktval <= 0 and price > 0:
            mktval = qty * price
 
        pnl_pct     = ((price - avg) / avg * 100) if avg > 0 and price > 0 else 0.0
        pnl_dollars = ((price - avg) * qty)        if avg > 0 and price > 0 else 0.0
 
        out.append({
            "symbol":               sym,
            "qty":                  qty,
            "avg_price":            round(avg, 4),
            "current_price":        round(price, 4),
            "market_value":         round(mktval, 2),
            "unrealized_pnl_pct":   round(pnl_pct, 2),
            "unrealized_pnl_dollars": round(pnl_dollars, 2),
            "portfolio_weight":     round(mktval / nlv if nlv > 0 else 0.0, 4),
            "days_held":            days_held(memory, sym),
        })
    return out
 
 
def portfolio_snapshot(nlv: float, cash: float, positions: List[Dict], memory: Dict) -> Dict:
    start_nlv   = memory.get("portfolio_start_nlv")
    recent      = memory.get("recent_decisions", [])
    last_nlv    = safe_float(recent[-1].get("nlv", 0)) if recent else 0.0
 
    ret_start = round((nlv - safe_float(start_nlv)) / safe_float(start_nlv) * 100, 2) if start_nlv and safe_float(start_nlv) > 0 else None
    ret_last  = round((nlv - last_nlv) / last_nlv * 100, 2) if last_nlv > 0 else None
 
    return {
        "nlv":                                round(nlv, 2),
        "cash":                               round(cash, 2),
        "cash_pct":                           round(cash / nlv if nlv > 0 else 0.0, 4),
        "portfolio_return_since_start_pct":   ret_start,
        "portfolio_return_since_last_decision_pct": ret_last,
        "total_unrealized_pnl_dollars":       round(sum(p.get("unrealized_pnl_dollars", 0) for p in positions), 2),
        "number_of_positions":                len(positions),
    }
 
 
# ── Execution preview ─────────────────────────────────────────
 
def build_preview(portfolio_state: Dict, positions: List[Dict], decision: Dict) -> List[Dict]:
    """
    Compute what orders need to be placed to reach the target allocations.
    Suppresses deltas smaller than MIN_REBALANCE_DELTA_PCT of NLV.
    """
    nlv     = safe_float(portfolio_state["nlv"])
    targets = {t["symbol"]: safe_float(t.get("target_pct", 0)) for t in decision.get("targets", [])}
    pos_map = {p["symbol"]: p for p in positions}
 
    cash_target_pct = targets.get("CASH", 0.0)
    preview = []
 
    all_syms = set(targets) | {p["symbol"] for p in positions}
    all_syms.discard("CASH")
 
    for sym in all_syms:
        target_pct    = targets.get(sym, 0.0)
        target_dollars = nlv * target_pct * (1.0 - CASH_BUFFER_PCT)
        pos            = pos_map.get(sym, {})
        price          = safe_float(pos.get("current_price", 0))
        current_qty    = int(safe_float(pos.get("qty", 0)))
        current_value  = safe_float(pos.get("market_value", 0))
 
        if price <= 0:
            fetched = get_quote(sym)
            price = fetched if fetched else 0.0
 
        target_qty = int(target_dollars / price) if price > 0 else 0
        delta_qty  = target_qty - current_qty
        delta_dollars = delta_qty * price if price > 0 else 0.0
 
        # Suppress noise
        if abs(delta_dollars) < nlv * MIN_REBALANCE_DELTA_PCT:
            if delta_qty != 0:
                log.info(f"  [{sym}] delta suppressed (${delta_dollars:.0f} < {MIN_REBALANCE_DELTA_PCT*100:.0f}% of NLV)")
            delta_qty = 0
 
        if target_pct > 0 and target_qty == 0 and current_qty == 0:
            log.warning(f"  [{sym}] target_pct={target_pct:.1%} but qty=0 (price too high or NLV too low?)")
 
        action = "BUY" if delta_qty > 0 else "SELL" if delta_qty < 0 else "HOLD"
        preview.append({
            "symbol":           sym,
            "current_qty":      current_qty,
            "current_value":    round(current_value, 2),
            "price_used":       round(price, 4),
            "target_pct":       round(target_pct, 4),
            "target_dollars":   round(target_dollars, 2),
            "target_qty":       target_qty,
            "delta_qty":        delta_qty,
            "suggested_action": action,
        })
 
    preview.append({
        "symbol":           "CASH",
        "current_qty":      None,
        "current_value":    round(portfolio_state["cash"], 2),
        "price_used":       None,
        "target_pct":       round(cash_target_pct, 4),
        "target_dollars":   round(nlv * cash_target_pct, 2),
        "target_qty":       None,
        "delta_qty":        None,
        "suggested_action": "HOLD",
    })
    return preview
 
 
# ── Order execution ───────────────────────────────────────────
 
def _order_id(result: Any) -> Optional[int]:
    if not isinstance(result, dict):
        return None
    placed = result.get("placed", [])
    if placed and isinstance(placed, list):
        oid = placed[0].get("orderId")
        return int(oid) if oid is not None else None
    return None
 
 
def execute(preview: List[Dict]) -> List[Dict]:
    """
    Execute sells first, then re-fetch cash, then buys largest-first.
    Returns list of order results.
    """
    sells = [p for p in preview if p["suggested_action"] == "SELL" and p["symbol"] != "CASH"]
    buys  = [p for p in preview if p["suggested_action"] == "BUY"  and p["symbol"] != "CASH"]
 
    results       = []
    order_ids     = []
 
    # Sells
    for o in sells:
        qty = abs(o["delta_qty"])
        if qty <= 0:
            continue
        try:
            result = place_order(o["symbol"], "SELL", qty)
            oid = _order_id(result)
            if oid:
                order_ids.append(oid)
            results.append({"symbol": o["symbol"], "side": "SELL", "qty": qty, "result": result})
        except Exception as e:
            log.error(f"  SELL {qty} {o['symbol']} failed: {e}")
            results.append({"symbol": o["symbol"], "side": "SELL", "qty": qty, "error": str(e)})
 
    # Wait for sell proceeds
    if sells:
        log.info("  Waiting 3s for sell proceeds ...")
        time.sleep(3)
 
    # Re-fetch available cash
    available_cash = float("inf")
    if buys:
        try:
            acct = get_account()
            available_cash = safe_float(acct.get("availableFunds") or acct.get("totalCashValue"))
            log.info(f"  Available cash for buys: ${available_cash:,.2f}")
        except Exception as e:
            log.warning(f"  Could not fetch post-sell cash ({e}) — proceeding cautiously")
 
    # Buys (largest first)
    cash_remaining = available_cash
    for o in sorted(buys, key=lambda x: x["target_dollars"], reverse=True):
        qty      = abs(o["delta_qty"])
        price    = safe_float(o.get("price_used", 0))
        est_cost = qty * price if price > 0 else 0.0
 
        if qty <= 0:
            continue
 
        if available_cash != float("inf") and est_cost > cash_remaining * 1.01:
            log.warning(f"  Skipping BUY {qty} {o['symbol']} — est ${est_cost:,.0f} > cash ${cash_remaining:,.0f}")
            results.append({"symbol": o["symbol"], "side": "BUY", "qty": qty,
                             "error": f"Insufficient cash (est ${est_cost:,.0f})"})
            continue
 
        try:
            result = place_order(o["symbol"], "BUY", qty)
            oid = _order_id(result)
            if oid:
                order_ids.append(oid)
            cash_remaining -= est_cost
            results.append({"symbol": o["symbol"], "side": "BUY", "qty": qty, "result": result})
        except Exception as e:
            log.error(f"  BUY {qty} {o['symbol']} failed: {e}")
            results.append({"symbol": o["symbol"], "side": "BUY", "qty": qty, "error": str(e)})
 
    # Poll fills
    if order_ids:
        fills = wait_for_fills(order_ids)
        for r in results:
            oid = _order_id(r.get("result", {}))
            if oid and oid in fills:
                r["fill_status"] = fills[oid]
 
    return results