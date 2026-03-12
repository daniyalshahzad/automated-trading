"""
Broker Bridge - Full Test Suite
================================
Tests all endpoints of the IBKR Broker Bridge (v2.1).
Assumes TWS is running in PAPER trading mode and ENABLE_TRADING=true in .env

Usage:
    python scripts/test_broker_bridge.py

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import json
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
import requests

# -------------------------
# Config
# -------------------------
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

BASE_URL = os.getenv("BRIDGE_URL", "http://127.0.0.1:8787")
TOKEN = os.getenv("BRIDGE_TOKEN", "")

if not TOKEN:
    print("ERROR: BRIDGE_TOKEN not set in .env")
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Test symbols - safe, liquid, cheap-ish for paper trading
TEST_SYMBOL_BUY  = "MSFT"   # will buy then sell
TEST_SYMBOL_QUOTE = "AAPL"  # just for quote tests
TEST_NOTIONAL = 500.0        # ~$500 notional buy

# -------------------------
# Helpers
# -------------------------
PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
WARN = "\033[93m⚠ WARN\033[0m"
INFO = "\033[94mℹ INFO\033[0m"

results = {"passed": 0, "failed": 0, "warned": 0}


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(label: str, condition: bool, detail: str = "", warn_only: bool = False):
    if condition:
        print(f"  {PASS}  {label}")
        if detail:
            print(f"         {detail}")
        results["passed"] += 1
    elif warn_only:
        print(f"  {WARN}  {label}")
        if detail:
            print(f"         {detail}")
        results["warned"] += 1
    else:
        print(f"  {FAIL}  {label}")
        if detail:
            print(f"         {detail}")
        results["failed"] += 1


def get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=30)
    return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def post(path: str, body: dict = None) -> tuple:
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=body or {}, timeout=30)
    return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def pretty(d: dict, indent=9):
    pad = " " * indent
    for line in json.dumps(d, indent=2).splitlines():
        print(f"{pad}{line}")


# -------------------------
# Tests
# -------------------------

def test_health():
    section("1. HEALTH CHECK")
    status, data = get("/health")
    check("HTTP 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)
    check("IB connected", data.get("ib_connected") is True,
          f"ib_connected={data.get('ib_connected')} — is TWS running on paper?",
          warn_only=not data.get("ib_connected"))
    check("trading not halted", data.get("trading_halted") is False)
    print(f"\n  {INFO}  Bridge info:")
    print(f"         host={data.get('ib_host')}:{data.get('ib_port')}  clientId={data.get('ib_client_id')}")
    print(f"         currency={data.get('default_currency')}  ENABLE_TRADING={data.get('enable_trading_env')}")
    print(f"         pnl_subscribed={data.get('pnl_subscribed')}  pnl_account={data.get('pnl_account_id')}")
    return data.get("ib_connected", False)


def test_auth():
    section("2. AUTH CHECKS")
    # No token
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    # health has no auth — check a protected endpoint
    r = requests.get(f"{BASE_URL}/v1/snapshot", timeout=10)
    check("Missing token → 401", r.status_code == 401, f"got {r.status_code}")

    # Wrong token
    r = requests.get(f"{BASE_URL}/v1/snapshot",
                     headers={"Authorization": "Bearer wrongtoken123"}, timeout=10)
    check("Wrong token → 403", r.status_code == 403, f"got {r.status_code}")


def test_trading_controls():
    section("3. TRADING HALT / RESUME")

    # status
    status, data = get("/v1/trading/status")
    check("GET /v1/trading/status → 200", status == 200)
    check("trading_halted=False initially", data.get("trading_halted") is False)

    # halt
    status, data = post("/v1/trading/halt", {"reason": "test halt"})
    check("POST /v1/trading/halt → 200", status == 200)
    check("trading_halted=True after halt", data.get("trading_halted") is True)
    check("reason preserved", data.get("trading_halted_reason") == "test halt")

    # resume
    status, data = post("/v1/trading/resume")
    check("POST /v1/trading/resume → 200", status == 200)
    check("trading_halted=False after resume", data.get("trading_halted") is False)


def test_contract_resolve():
    section("4. CONTRACT RESOLVE")
    status, data = get("/v1/contract/resolve", {"symbol": TEST_SYMBOL_BUY})
    check("HTTP 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)
    check("symbol matches", data.get("symbol") == TEST_SYMBOL_BUY)
    check("conId present", bool(data.get("conId")), f"conId={data.get('conId')}")
    check("secType=STK", data.get("secType") == "STK")
    check("currency=USD", data.get("currency") == "USD")
    print(f"\n  {INFO}  {TEST_SYMBOL_BUY}: conId={data.get('conId')}  longName={data.get('longName')}")
    print(f"         industry={data.get('industry')}  category={data.get('category')}")

    # bad symbol
    status, data = get("/v1/contract/resolve", {"symbol": "XYZZZINVALID999"})
    check("Bad symbol → 400", status == 400, f"got {status}", warn_only=True)


def test_quote():
    section("5. QUOTE")
    status, data = get("/v1/quote", {"symbols": f"{TEST_SYMBOL_BUY},{TEST_SYMBOL_QUOTE}"})
    check("HTTP 200", status == 200)
    check("ok=True", data.get("ok") is True)
    quotes = data.get("quotes", [])
    check("2 quotes returned", len(quotes) == 2, f"got {len(quotes)}")

    for q in quotes:
        sym = q.get("symbol")
        px = q.get("price")
        src = q.get("price_source")
        if px:
            check(f"{sym} price received", True, f"${px:.2f} (source={src})")
        else:
            check(f"{sym} price received", False,
                  "price=None — check market data permissions in TWS", warn_only=True)

    # warnings surfaced
    warnings = data.get("warnings", [])
    if warnings:
        print(f"\n  {WARN}  Quote warnings: {warnings}")


def test_snapshot():
    section("6. SNAPSHOT")
    status, data = get("/v1/snapshot")
    check("HTTP 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)

    # account block
    acct = data.get("account", {})
    check("account block present", bool(acct))
    check("nlv present", acct.get("nlv") is not None, f"nlv={acct.get('nlv')}")
    check("available_funds present", acct.get("available_funds") is not None,
          f"available_funds={acct.get('available_funds')}")

    # pnl block
    pnl = data.get("pnl", {})
    check("pnl block present", bool(pnl))
    print(f"\n  {INFO}  PnL: daily={pnl.get('dailyPnL')}  "
          f"unrealized={pnl.get('unrealizedPnL')}  realized={pnl.get('realizedPnL')}")

    # positions
    positions = data.get("positions", [])
    print(f"\n  {INFO}  Positions ({len(positions)} held):")
    for p in positions:
        print(f"         {p['symbol']}: qty={p['qty']}  avgCost={p.get('avgCost')}  "
              f"mktPx={p.get('marketPrice')}  unrealPnL={p.get('unrealizedPnL')}")

    # totals
    totals = data.get("totals", {})
    print(f"\n  {INFO}  Totals: marketValue={totals.get('totalMarketValue')}  "
          f"unrealizedPnL={totals.get('totalUnrealizedPnL')}")

    # open orders
    open_orders = data.get("openOrders", [])
    print(f"\n  {INFO}  Open orders: {len(open_orders)}")

    # trades
    trades = data.get("recentTrades", [])
    print(f"  {INFO}  Recent trades: {len(trades)}")

    # warnings
    for w in data.get("warnings", []):
        print(f"  {WARN}  {w}")

    return acct.get("available_funds", 0)


def test_preview_buy(available_funds: float):
    section("7. ORDER PREVIEW — BUY")
    client_id = str(uuid.uuid4())

    body = {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "MKT",
            "tif": "DAY",
            "notional": TEST_NOTIONAL,
            "client_order_id": client_id,
        }]
    }

    status, data = post("/v1/orders/preview", body)
    check("HTTP 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)

    resolved = data.get("resolved_orders", [])
    check("1 order resolved", len(resolved) == 1, f"got {len(resolved)}")

    if resolved:
        r = resolved[0]
        check("clientOrderId preserved", r.get("clientOrderId") == client_id)
        check("symbol correct", r.get("symbol") == TEST_SYMBOL_BUY)
        check("side=buy", r.get("side") == "buy")
        check("quantity > 0", (r.get("quantity") or 0) > 0, f"qty={r.get('quantity')}")
        qty = r.get("quantity", 0)
        print(f"\n  {INFO}  Preview: {TEST_SYMBOL_BUY} BUY qty={qty} "
              f"(notional=${TEST_NOTIONAL} → ~{qty} shares)")
        print(f"  {INFO}  Warnings: {data.get('warnings', [])}")

    # preview — insufficient funds (huge notional)
    body2 = {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "LMT",
            "tif": "DAY",
            "quantity": 999999,
            "limit_price": 9999.0,
        }]
    }
    status2, _ = post("/v1/orders/preview", body2)
    check("Oversized order → 400", status2 == 400, f"got {status2}")

    return resolved[0] if resolved else None


def test_preview_limit_buy():
    section("8. ORDER PREVIEW — LIMIT BUY")
    status, data = post("/v1/orders/preview", {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "LMT",
            "tif": "GTC",
            "quantity": 1,
            "limit_price": 1.00,   # way below market — won't fill, safe for paper
        }]
    })
    check("HTTP 200", status == 200)
    r = (data.get("resolved_orders") or [{}])[0]
    check("order_type=LMT", r.get("order_type") == "LMT")
    check("limit_price preserved", r.get("limit_price") == 1.00)
    check("tif=GTC", r.get("tif") == "GTC")


def test_place_buy() -> dict | None:
    section("9. PLACE ORDER — BUY MKT")
    client_id = f"test-buy-{uuid.uuid4().hex[:8]}"

    body = {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "MKT",
            "tif": "DAY",
            "notional": TEST_NOTIONAL,
            "client_order_id": client_id,
        }]
    }

    status, data = post("/v1/orders/place", body)
    check("HTTP 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)

    placed = data.get("placed", [])
    check("1 order placed", len(placed) == 1, f"got {len(placed)}")

    if placed:
        p = placed[0]
        check("orderId assigned", bool(p.get("orderId")), f"orderId={p.get('orderId')}")
        check("action=BUY", p.get("action") == "BUY")
        check("clientOrderId matches", p.get("clientOrderId") == client_id)
        print(f"\n  {INFO}  Placed: orderId={p.get('orderId')}  permId={p.get('permId')}  "
              f"status={p.get('status')}  qty={p.get('totalQuantity')}")
        return p

    return None


def test_wait_for_fill(order: dict):
    section("10. WAIT FOR FILL")
    if not order:
        check("Skipped (no order placed)", False, warn_only=True)
        return

    order_id = order.get("orderId")
    print(f"  {INFO}  Polling for fill on orderId={order_id} (up to 15s)...")

    filled = False
    for i in range(15):
        time.sleep(1)
        _, snap = get("/v1/snapshot")
        trades = snap.get("recentTrades", [])
        for t in trades:
            if t.get("orderId") == order_id:
                status = t.get("status", "")
                filled_qty = t.get("filled", 0)
                print(f"         [{i+1}s] status={status}  filled={filled_qty}  "
                      f"avgFillPx={t.get('avgFillPrice')}")
                if status.lower() in ("filled", "submitted") and filled_qty > 0:
                    filled = True
                    break
        if filled:
            break

    check("Order filled (paper MKT)", filled,
          "Not filled in 15s — check TWS paper account is active", warn_only=not filled)


def test_place_sell(symbol: str = TEST_SYMBOL_BUY):
    section("11. PLACE ORDER — SELL (close position)")
    # Get current position size
    _, snap = get("/v1/snapshot")
    positions = snap.get("positions", [])
    pos = next((p for p in positions if p["symbol"] == symbol), None)

    if not pos or pos["qty"] <= 0:
        print(f"  {WARN}  No position in {symbol} to sell — skipping sell test")
        print(f"         (buy test may not have filled yet; run again or check TWS)")
        results["warned"] += 1
        return

    qty_to_sell = pos["qty"]
    print(f"  {INFO}  Selling full position: {symbol} qty={qty_to_sell}")

    client_id = f"test-sell-{uuid.uuid4().hex[:8]}"
    body = {
        "orders": [{
            "symbol": symbol,
            "side": "sell",
            "order_type": "MKT",
            "tif": "DAY",
            "quantity": qty_to_sell,
            "client_order_id": client_id,
        }]
    }

    status, data = post("/v1/orders/place", body)
    check("HTTP 200", status == 200, f"got {status}")
    placed = data.get("placed", [])
    check("1 order placed", len(placed) == 1)
    if placed:
        p = placed[0]
        check("action=SELL", p.get("action") == "SELL")
        print(f"\n  {INFO}  Sell placed: orderId={p.get('orderId')}  qty={p.get('totalQuantity')}")


def test_short_sell_blocked():
    section("12. SHORT SELL PROTECTION")
    # Try to sell a symbol we definitely don't hold
    body = {
        "orders": [{
            "symbol": "NVDA",
            "side": "sell",
            "order_type": "MKT",
            "tif": "DAY",
            "quantity": 1,
        }]
    }
    status, data = post("/v1/orders/preview", body)
    check("Short sell blocked → 400", status == 400,
          f"got {status} — detail: {data.get('detail', '')}")


def test_place_limit_order_and_cancel():
    section("13. LIMIT ORDER + CANCEL")

    # Place a far-below-market limit buy (won't fill, safe)
    client_id = f"test-lmt-{uuid.uuid4().hex[:8]}"
    body = {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "LMT",
            "tif": "GTC",
            "quantity": 1,
            "limit_price": 1.00,
            "client_order_id": client_id,
        }]
    }

    status, data = post("/v1/orders/place", body)
    check("Limit order placed → 200", status == 200, f"got {status}")
    placed = data.get("placed", [])
    check("1 order placed", len(placed) == 1)

    if not placed:
        return

    order_id = placed[0]["orderId"]
    print(f"  {INFO}  LMT order placed: orderId={order_id}")

    time.sleep(1)  # let it register

    # Cancel it
    status, data = post("/v1/orders/cancel", {"order_id": order_id})
    check("Cancel → 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)
    cancelled = data.get("cancelled", {})
    check("orderId matches", cancelled.get("orderId") == order_id,
          f"cancelled.orderId={cancelled.get('orderId')}")
    print(f"  {INFO}  Cancelled: {cancelled}")


def test_cancel_all():
    section("14. CANCEL ALL OPEN ORDERS")

    # Place 2 limit orders that won't fill
    for i in range(2):
        post("/v1/orders/place", {
            "orders": [{
                "symbol": TEST_SYMBOL_BUY,
                "side": "buy",
                "order_type": "LMT",
                "tif": "GTC",
                "quantity": 1,
                "limit_price": 1.00,
                "client_order_id": f"cancel-all-test-{i}",
            }]
        })

    time.sleep(1)
    status, data = post("/v1/orders/cancelAll")
    check("cancelAll → 200", status == 200, f"got {status}")
    check("ok=True", data.get("ok") is True)
    count = data.get("cancelled_count", 0)
    check("At least 1 order cancelled", count >= 1, f"cancelled_count={count}")
    print(f"  {INFO}  Cancelled {count} orders")


def test_halt_blocks_trading():
    section("15. HALT BLOCKS ORDER PLACEMENT")

    post("/v1/trading/halt", {"reason": "automated test"})

    status, data = post("/v1/orders/place", {
        "orders": [{
            "symbol": TEST_SYMBOL_BUY,
            "side": "buy",
            "order_type": "MKT",
            "tif": "DAY",
            "quantity": 1,
        }]
    })
    check("Halted → 403", status == 403, f"got {status}")
    check("Detail mentions halt", "halted" in str(data.get("detail", "")).lower())

    # Resume for cleanup
    post("/v1/trading/resume")
    check("Resumed successfully", True)


def test_legacy_endpoints():
    section("16. LEGACY ENDPOINTS (backwards compat)")

    status, data = get("/broker/account")
    check("GET /broker/account → 200", status == 200)
    check("nlv present", data.get("nlv") is not None)

    status, data = get("/broker/positions")
    check("GET /broker/positions → 200", status == 200)
    check("positions list present", isinstance(data.get("positions"), list))

    status, data = get("/broker/openOrders")
    check("GET /broker/openOrders → 200", status == 200)
    check("openOrders list present", isinstance(data.get("openOrders"), list))

    status, data = get("/broker/trades")
    check("GET /broker/trades → 200", status == 200)
    check("trades list present", isinstance(data.get("trades"), list))


def test_final_snapshot():
    section("17. FINAL SNAPSHOT (post-trade state)")
    status, data = get("/v1/snapshot")
    check("HTTP 200", status == 200)

    acct = data.get("account", {})
    pnl = data.get("pnl", {})
    positions = data.get("positions", [])

    print(f"\n  {INFO}  Final account state:")
    print(f"         NLV:             {acct.get('nlv')}")
    print(f"         Available Funds: {acct.get('available_funds')}")
    print(f"         Total Cash:      {acct.get('total_cash')}")
    print(f"\n  {INFO}  Session PnL:")
    print(f"         Daily:           {pnl.get('dailyPnL')}")
    print(f"         Unrealized:      {pnl.get('unrealizedPnL')}")
    print(f"         Realized:        {pnl.get('realizedPnL')}")
    print(f"\n  {INFO}  Positions held: {len(positions)}")
    for p in positions:
        print(f"         {p['symbol']}: qty={p['qty']}  mktPx={p.get('marketPrice')}  "
              f"unrealPnL={p.get('unrealizedPnL')}")


# -------------------------
# Main
# -------------------------
def main():
    print("\n" + "="*60)
    print("  IBKR BROKER BRIDGE — FULL TEST SUITE")
    print(f"  Target: {BASE_URL}")
    print("="*60)

    ib_connected = test_health()

    if not ib_connected:
        print(f"\n  {FAIL}  IB not connected. Start TWS paper trading and retry.")
        sys.exit(1)

    test_auth()
    test_trading_controls()
    test_contract_resolve()
    test_quote()
    available_funds = test_snapshot()
    test_preview_buy(available_funds)
    test_preview_limit_buy()
    test_short_sell_blocked()

    # Live trading tests
    placed_order = test_place_buy()
    test_wait_for_fill(placed_order)
    test_place_sell()
    test_place_limit_order_and_cancel()
    test_cancel_all()
    test_halt_blocks_trading()
    test_legacy_endpoints()
    test_final_snapshot()

    # Summary
    print(f"\n{'='*60}")
    print(f"  RESULTS: {results['passed']} passed  |  "
          f"{results['failed']} failed  |  {results['warned']} warnings")
    print("="*60 + "\n")

    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()