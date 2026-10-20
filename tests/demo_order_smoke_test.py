"""One-off manual smoke test (BUILD_BRIEF.md test #3): connect to Kalshi, confirm
auth works, place one tiny real order priced far away from the market (so it
rests rather than fills), then cancel it.

Defaults to the demo environment. Hitting production requires the explicit
--prod flag -- this script never guesses which environment to use.

Intentionally separate from kalshi_live.py/the running paper dashboard: this
does not touch live_state.json/live_log.jsonl, and uses a 1-contract order at a
deliberately unmarketable price instead of the strategy's real sizing, so a
connectivity/auth test can't accidentally become a real-sized bet.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_client import KalshiClient, KalshiAPIError, KalshiNetworkError

SERIES_TO_TRY = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="hit production instead of demo (explicit, no default)")
    args = ap.parse_args()

    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_KEY_PATH")
    if not key_id or not key_path:
        print("KALSHI_KEY_ID / KALSHI_KEY_PATH not set in this environment.")
        return 1

    client = KalshiClient(key_id=key_id, key_path=key_path, demo=not args.prod)
    print(f"[demo-test] environment: {'PRODUCTION (real money)' if args.prod else 'demo'}")
    print(f"[demo-test] connecting to {client.base} ...")

    bal = client.get_balance()
    print(f"[demo-test] auth OK. balance: ${float(bal.get('balance_dollars', 0)):.2f}")

    # Prefer a market with plenty of time left and a healthy (non-degenerate) book,
    # not just the first one returned -- a market seconds from closing can show a
    # bid/ask of $1.00/$0.00 (already effectively decided) and will reject new orders.
    market = None
    for series in SERIES_TO_TRY:
        resp = client.get_markets(series, status="open", limit=10)
        candidates = [m for m in resp.get("markets", [])
                      if 0.05 <= float(m.get("yes_bid_dollars") or 0) <= 0.95
                      and 0.05 <= float(m.get("yes_ask_dollars") or 0) <= 0.95]
        if candidates:
            market = max(candidates, key=lambda m: m["close_time"])
            break
    if market is None:
        print("[demo-test] no healthy open market found in any tried series -- can't test order placement right now.")
        return 1

    ticker = market["ticker"]
    yes_bid = float(market.get("yes_bid_dollars") or 0)
    exchange_index = market.get("exchange_index")
    print(f"[demo-test] using market {ticker} (yes_bid=${yes_bid:.2f}, exchange_index={exchange_index})")

    # Deliberately unmarketable: well below the current bid, so this should rest, not fill.
    test_price = max(0.01, round(yes_bid * 0.2, 2)) if yes_bid > 0.05 else 0.01
    coid = f"smoketest-{int(time.time())}"
    print(f"[demo-test] placing 1-contract BUY YES limit @ ${test_price:.2f} (client_order_id={coid}) ...")
    try:
        order_resp = client.create_order(ticker, "bid", 1, test_price, coid, exchange_index=exchange_index)
    except KalshiAPIError as e:
        print(f"[demo-test] order REJECTED: {e}")
        return 1
    order_id = order_resp.get("order_id")
    fill_count = order_resp.get("fill_count")
    print(f"[demo-test] order placed: order_id={order_id} fill_count={fill_count} "
          f"remaining={order_resp.get('remaining_count')}")

    time.sleep(1)
    # get_order (single-id lookup) 404s for this account's exchange shard; the list
    # endpoint filtered by ticker + exchange_index is what actually works (confirmed live).
    found = next((o for o in client.get_orders(ticker=ticker, exchange_index=exchange_index, limit=50)
                  .get("orders", []) if o.get("order_id") == order_id), None)
    if found:
        print(f"[demo-test] order status: {found.get('status')} fill_count_fp={found.get('fill_count_fp')} "
              f"remaining_count_fp={found.get('remaining_count_fp')}")
    else:
        print("[demo-test] order not found in list (unexpected) -- proceeding to cancel anyway")

    print(f"[demo-test] cancelling order {order_id} ...")
    cancel_resp = client.cancel_order(order_id, market_ticker=ticker, exchange_index=exchange_index)
    print(f"[demo-test] cancel response: reduced_by={cancel_resp.get('reduced_by')} "
          f"ts_ms={cancel_resp.get('ts_ms')}")

    print("[demo-test] DONE: placed and cancelled one real demo order successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
