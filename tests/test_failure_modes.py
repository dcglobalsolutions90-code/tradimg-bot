"""BUILD_BRIEF.md test #4: simulate a network outage, a partial fill, a rejected
order, and a reconciliation mismatch; confirm the bot stops (or degrades) safely.
Uses tests/fakes.py's FakeClient -- no real network calls.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kalshi_live as kl
from kalshi_client import KalshiAPIError, KalshiNetworkError
from fakes import FakeClient


def _redirect_files(tmp_dir):
    kl.LOG_PATH = tmp_dir / "live_log.jsonl"
    kl.HIST_PATH = tmp_dir / "live_history.jsonl"
    kl.STATE_PATH = tmp_dir / "live_state.json"


def test_network_outage_halts_after_consecutive_errors(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.get_markets_error = KalshiNetworkError("simulated DNS outage")
    s = kl.fresh_state()
    cfg = kl.load_config()
    try:
        kl.run_cycle(client, s, cfg, time.time(), live_enabled=True)
        assert False, "expected run_cycle to raise Halt on repeated network errors"
    except kl.Halt as h:
        assert "consecutive API errors" in str(h)
    assert s["consecutive_errors"] >= kl.DEFAULT_MAX_CONSECUTIVE_ERRORS


def test_rejected_order_is_logged_and_does_not_crash(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_error = KalshiAPIError(400, "insufficient_balance", "not enough buying power")
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    placed = kl.process_rule_signal(
        client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60, close_t=now + 600,
        rule=4, side="yes", cost=0.82, z=2.0, live_enabled=True, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    assert placed == 1  # a decision was made (and attempted)
    assert s["orders"] == {}, "a rejected order must not be tracked as open"
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "order_rejected"' in l for l in log_lines)


def test_insufficient_balance_resizes_and_retries_successfully(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_errors = [KalshiAPIError(400, "insufficient_balance", "insufficient balance")]
    client.balance_dollars = 50.0  # fresh shard balance the resize check will see
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    placed = kl.process_rule_signal(
        client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60, close_t=now + 600,
        rule=4, side="yes", cost=0.82, z=2.0, live_enabled=True, real_cash=1000.0, real_value=1000.0,
        exchange_index=0, price_str=None)
    assert placed == 1
    assert len(s["orders"]) == 1, "the resized retry should succeed and be tracked as an open order"
    order = next(iter(s["orders"].values()))
    # $50 / (0.82 + 0.02 fee) = 59 contracts affordable -- strictly smaller than the
    # original request (sized off real_value=1000, unconstrained by this fresh balance).
    assert order["total_n"] == 59
    assert sum(order["requested"].values()) == 59
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "order_resized_retry"' in l and '"new_total_n": 59' in l for l in log_lines)
    assert not any('"type": "order_rejected"' in l for l in log_lines)


def test_insufficient_balance_resize_still_unaffordable_is_skipped(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_error = KalshiAPIError(400, "insufficient_balance", "insufficient balance")
    client.balance_dollars = 0.50  # fresh balance can't afford even 1 contract at this price
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    placed = kl.process_rule_signal(
        client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60, close_t=now + 600,
        rule=4, side="yes", cost=0.82, z=2.0, live_enabled=True, real_cash=1000.0, real_value=1000.0,
        exchange_index=0, price_str=None)
    assert placed == 1
    assert s["orders"] == {}, "nothing affordable even after resize -- must not track a phantom order"
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "order_rejected"' in l for l in log_lines)


def test_other_rejection_reasons_are_not_resized(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_error = KalshiAPIError(400, "price_outside_bounds", "price too far from market")
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    placed = kl.process_rule_signal(
        client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60, close_t=now + 600,
        rule=4, side="yes", cost=0.82, z=2.0, live_enabled=True, real_cash=1000.0, real_value=1000.0,
        exchange_index=0, price_str=None)
    assert placed == 1
    assert s["orders"] == {}
    assert len(client.created_orders) == 1, "a non-balance rejection must not trigger a resize retry"
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "order_rejected"' in l for l in log_lines)
    assert not any('"type": "order_resized_retry"' in l for l in log_lines)


def test_no_side_immediate_fill_uses_correct_price_not_raw_yes_price(tmp_path):
    """average_fill_price in CreateOrder's response is always YES-denominated
    (confirmed via Kalshi's OpenAPI spec -- the request's own `price` field is
    YES-denominated regardless of book_side, and the response mirrors that --
    plus real fill data showing yes_price_dollars/no_price_dollars as
    complements) regardless of which side was actually traded. A real bug used
    it raw for a "no" side order, computing cost_paid from the YES price (~0.10)
    instead of the NO price actually paid (~0.90), inflating that trade's
    recorded P&L by roughly 9x. Confirmed live via Kalshi's own settled-position
    records disagreeing with the bot's own tracked P&L for the same trade.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_response = {
        "order_id": "order-1", "client_order_id": "c1",
        "fill_count": "10.00", "remaining_count": "0.00",
        "average_fill_price": "0.10",  # YES-denominated, exactly as Kalshi actually returns
        "average_fee_paid": "0.02", "ts_ms": 0,
    }
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    kl.process_rule_signal(client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60,
                            close_t=now + 600, rule=4, side="no", cost=0.90, z=2.0, live_enabled=True,
                            real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    order = next(iter(s["orders"].values()))
    assert order["filled_n"] == 10
    # Correct: 10 contracts at the NO price actually paid (1 - 0.10 yes price) = 9.00,
    # plus 10 * 0.02 fee = 0.20. The pre-fix bug would have recorded cum_cost as
    # 10 * 0.10 = 1.00 instead -- the raw, un-converted YES price.
    assert abs(order["cum_cost"] - 9.00) < 1e-6
    assert abs(order["cum_fee"] - 0.20) < 1e-6


def test_partial_fill_is_attributed_and_remainder_cancelled_at_deadline(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_response = {
        "order_id": "order-1", "client_order_id": "c1",
        "fill_count": "4.00", "remaining_count": "6.00",
        "average_fill_price": "0.82", "average_fee_paid": "0.02", "ts_ms": 0,
    }
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    open_t = now - 4 * 60
    kl.process_rule_signal(client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=open_t,
                            close_t=now + 600, rule=4, side="yes", cost=0.82, z=2.0,
                            live_enabled=True, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    assert len(s["orders"]) == 1
    order = next(iter(s["orders"].values()))
    assert order["filled_n"] == 4
    assert order["status"] == "open"  # 4 of 10 filled, still resting
    pos = s["positions"][order["pos_key"]]
    assert sum(pos["sleeve_contracts"].values()) == 4

    # Simulate the exchange filling 2 more contracts on the next poll.
    client.get_orders_responses = [{
        "order_id": "order-1", "status": "resting", "fill_count_fp": "6.00",
        "taker_fill_cost_dollars": "4.92", "maker_fill_cost_dollars": "0",
        "taker_fees_dollars": "0.12", "maker_fees_dollars": "0",
    }]
    kl.poll_open_orders(client, s, cfg, now, live_enabled=True)
    assert order["filled_n"] == 6
    assert sum(s["positions"][order["pos_key"]]["sleeve_contracts"].values()) == 6

    # Now simulate the decision window having ended: next poll should cancel the rest.
    client.get_orders_responses = [{
        "order_id": "order-1", "status": "resting", "fill_count_fp": "6.00",
        "taker_fill_cost_dollars": "4.92", "maker_fill_cost_dollars": "0",
        "taker_fees_dollars": "0.12", "maker_fees_dollars": "0",
    }]
    past_deadline = order["deadline"] + 5
    kl.poll_open_orders(client, s, cfg, past_deadline, live_enabled=True)
    assert order["status"] == "closed"
    assert "order-1" in client.cancelled_orders


def test_reconciliation_mismatch_halts(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    s["positions"]["KXBTC15M-TEST|4"] = dict(
        ticker="KXBTC15M-TEST", series="KXBTC15M", rule=4, side="yes", close=time.time() + 3600,
        sleeve_contracts={"A": 5}, sleeve_cost={"A": 4.1})
    # Bot thinks it holds 5 YES contracts; exchange says 2. Market not closed yet
    # (close is in the future), so the settlement-lag grace period doesn't apply --
    # this must still halt immediately.
    client.positions_response = {"market_positions": [{"ticker": "KXBTC15M-TEST", "position_fp": "2.00"}]}
    try:
        kl.reconcile(client, s, kl.load_config(), live_enabled=True, now=time.time())
        assert False, "expected a Halt on position mismatch"
    except kl.Halt as h:
        assert "KXBTC15M-TEST" in str(h)


def test_reconciliation_catches_untracked_real_position(tmp_path):
    """A real position the bot has no record of at all (e.g. placed outside the
    bot, by hand, or by a diagnostic script) must still halt -- not just a
    tracked position that disagrees in size. Confirmed live: this direction was
    missing and let a real untracked fill through silently.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()  # bot thinks it holds nothing anywhere
    client.positions_response = {"market_positions": [{"ticker": "KXBTC15M-SURPRISE", "position_fp": "1.00"}]}
    try:
        kl.reconcile(client, s, kl.load_config(), live_enabled=True, now=time.time())
        assert False, "expected a Halt on an untracked real position"
    except kl.Halt as h:
        assert "KXBTC15M-SURPRISE" in str(h)


def test_reconciliation_auto_resolves_own_orders_missed_fill(tmp_path):
    """Per explicit user request ("the bot should never go down"): if a mismatch
    is fully explained by the bot's OWN order having filled before its tracking
    caught up (the exact race confirmed live three times -- a reprice instantly
    fills the order, but attribution lags by a cycle or two), reconcile() must
    catch up right now and keep running, not halt.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_response = {
        "order_id": "order-1", "client_order_id": "c1",
        "fill_count": "0.00", "remaining_count": "7.00", "ts_ms": 0,
    }
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    kl.process_rule_signal(client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60,
                            close_t=now + 600, rule=4, side="yes", cost=0.82, z=2.0, live_enabled=True,
                            real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    order = next(iter(s["orders"].values()))
    assert order["filled_n"] == 0  # tracking hasn't caught up yet

    # The order actually filled completely on the exchange already -- reflected
    # in the fills ledger (the authoritative source _auto_resolve_mismatch uses)
    # even though the orders-list snapshot (not queried here at all) might not be.
    client.get_fills_response = {"fills": [{
        "count_fp": f"{float(order['total_n']):.2f}", "yes_price_dollars": "0.82", "no_price_dollars": "0.18",
        "fee_cost": f"{0.02 * order['total_n']:.4f}",
    }]}
    client.positions_response = {"market_positions": [
        {"ticker": "KXBTC15M-TEST", "position_fp": f"{float(order['total_n']):.2f}"}]}

    kl.reconcile(client, s, cfg, live_enabled=True, now=now)  # must NOT raise

    assert order["filled_n"] == order["total_n"]
    log_lines = kl.LOG_PATH.read_text().splitlines()
    assert any('"type": "reconciliation_auto_resolved"' in l for l in log_lines)


def test_reconciliation_grace_period_for_just_closed_market(tmp_path):
    """A tracked position whose market has already closed can briefly vanish from
    the exchange's unsettled-positions view before settle() has had a chance to
    see a result and clear it out on our side (confirmed live: a market dropped
    out of unsettled positions while its public result was still unqueryable for
    a while after). That must NOT halt the bot -- it's settle()'s job to catch up.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    now = time.time()
    s["positions"]["KXBTC15M-TEST|4"] = dict(
        ticker="KXBTC15M-TEST", series="KXBTC15M", rule=4, side="yes", close=now - 200,
        sleeve_contracts={"A": 5}, sleeve_cost={"A": 4.1})
    # Market closed 200s ago; exchange no longer lists it as an open position at all.
    client.positions_response = {"market_positions": []}
    kl.reconcile(client, s, kl.load_config(), live_enabled=True, now=now)  # must NOT raise


def test_reconciliation_still_halts_after_grace_period_expires(tmp_path):
    """If a tracked position is STILL missing from the exchange's unsettled view
    long after its market closed (well past any normal settlement lag), that's a
    genuine problem again, not lag -- the grace period must not mask it forever.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    s = kl.fresh_state()
    now = time.time()
    s["positions"]["KXBTC15M-TEST|4"] = dict(
        ticker="KXBTC15M-TEST", series="KXBTC15M", rule=4, side="yes",
        close=now - (kl.DEFAULT_SETTLEMENT_GRACE_SECONDS + 60),
        sleeve_contracts={"A": 5}, sleeve_cost={"A": 4.1})
    client.positions_response = {"market_positions": []}
    try:
        kl.reconcile(client, s, kl.load_config(), live_enabled=True, now=now)
        assert False, "expected a Halt once the grace period has expired"
    except kl.Halt as h:
        assert "KXBTC15M-TEST" in str(h)


def test_reprice_follows_market_within_bound(tmp_path):
    """Per explicit user direction: a resting order should track the market
    within the decision window (pay a slightly worse price for a better fill
    rate), as long as the new price stays inside the existing 3-97 cent bound
    that made the bet worth taking in the first place.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    order = dict(ticker="KXBTC15M-TEST", side="yes", book_side="bid", price=0.80,
                 total_n=10, filled_n=0, order_id="order-1", exchange_index=2, close=time.time() + 3600)
    client.get_market_response = {"market": {"yes_bid_dollars": "0.8400", "yes_ask_dollars": "0.8500"}}
    kl._tick_resting_order(client, kl.fresh_state(), kl.load_config(), order, time.time())
    assert len(client.amended_orders) == 1
    assert client.amended_orders[0]["price"] == 0.85
    assert order["price"] == 0.85


def test_reprice_stops_at_price_bound(tmp_path):
    """The chase must not cross outside signal_core's price_ok bound -- if the
    market has moved past the point where the bet is still worth taking, stop
    chasing rather than pay into a price the bot would never have entered at.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    order = dict(ticker="KXBTC15M-TEST", side="yes", book_side="bid", price=0.90,
                 total_n=10, filled_n=0, order_id="order-1", exchange_index=2, close=time.time() + 3600)
    client.get_market_response = {"market": {"yes_bid_dollars": "0.9800", "yes_ask_dollars": "0.9900"}}
    kl._tick_resting_order(client, kl.fresh_state(), kl.load_config(), order, time.time())
    assert len(client.amended_orders) == 0
    assert order["price"] == 0.90  # unchanged -- did not chase past the 0.97 bound


def test_reprice_does_nothing_when_market_has_not_moved_against_us(tmp_path):
    _redirect_files(tmp_path)
    client = FakeClient()
    order = dict(ticker="KXBTC15M-TEST", side="yes", book_side="bid", price=0.85,
                 total_n=10, filled_n=0, order_id="order-1", exchange_index=2, close=time.time() + 3600)
    client.get_market_response = {"market": {"yes_bid_dollars": "0.80", "yes_ask_dollars": "0.82"}}
    kl._tick_resting_order(client, kl.fresh_state(), kl.load_config(), order, time.time())
    assert len(client.amended_orders) == 0
    assert order["price"] == 0.85


def test_reprice_that_immediately_fills_gets_attributed_not_missed(tmp_path):
    """Confirmed live: repricing toward the market can itself be marketable enough
    to cross the book and fill on the spot. If that fill isn't attributed in the
    same cycle, reconcile() sees a real fill the bot never recorded and halts on
    what looks like a mismatch but is really just a bug in the reprice path.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_response = {
        "order_id": "order-1", "client_order_id": "c1",
        "fill_count": "0.00", "remaining_count": "10.00",
        "ts_ms": 0,
    }
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    kl.process_rule_signal(client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60,
                            close_t=now + 600, rule=4, side="yes", cost=0.82, z=2.0,
                            live_enabled=True, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    order = next(iter(s["orders"].values()))
    assert order["price"] == 0.82

    # Market has moved up to 0.90 -- triggers a reprice. The FIRST get_orders call
    # (the normal top-of-cycle poll) shows still fully unfilled and resting; the
    # SECOND (the post-amend re-check inside the reprice path) shows the amend
    # itself crossed the book and filled completely.
    client.get_market_response = {"market": {"yes_bid_dollars": "0.89", "yes_ask_dollars": "0.90"}}
    client.get_orders_responses = [
        {"order_id": "order-1", "status": "resting", "fill_count_fp": "0.00",
         "taker_fill_cost_dollars": "0", "maker_fill_cost_dollars": "0",
         "taker_fees_dollars": "0", "maker_fees_dollars": "0"},
        {"order_id": "order-1", "status": "executed", "fill_count_fp": "10.00",
         "taker_fill_cost_dollars": "9.00", "maker_fill_cost_dollars": "0",
         "taker_fees_dollars": "0.20", "maker_fees_dollars": "0"},
    ]
    kl.poll_open_orders(client, s, cfg, now, live_enabled=True)

    assert len(client.amended_orders) == 1, "should have repriced toward the new market price"
    assert order["status"] == "closed", "the fill caused by the reprice must be recognized and close the order"
    assert order["filled_n"] == 10
    pos = s["positions"][order["pos_key"]]
    assert sum(pos["sleeve_contracts"].values()) == 10, "the reprice-caused fill must be attributed to positions"


def test_order_stays_open_well_past_old_fixed_window_if_still_profitable(tmp_path):
    """Locks in the removal of the old fixed 30s decision-window cancel: an order
    must stay open far past that mark as long as the price is still within the
    profitable bound and the market hasn't closed -- per explicit user direction
    to commit to a placed bet rather than time out of it.
    """
    _redirect_files(tmp_path)
    client = FakeClient()
    client.create_order_response = {
        "order_id": "order-1", "client_order_id": "c1",
        "fill_count": "4.00", "remaining_count": "6.00",
        "average_fill_price": "0.82", "average_fee_paid": "0.02", "ts_ms": 0,
    }
    s = kl.fresh_state()
    cfg = kl.load_config()
    now = time.time()
    kl.process_rule_signal(client, s, cfg, series="KXBTC15M", ticker="KXBTC15M-TEST", open_t=now - 4 * 60,
                            close_t=now + 600, rule=4, side="yes", cost=0.82, z=2.0,
                            live_enabled=True, real_cash=1000.0, real_value=1000.0, exchange_index=0, price_str=None)
    order = next(iter(s["orders"].values()))
    old_fixed_deadline = now - 4 * 60 + (4 + 0.5) * 60  # the old window-end formula, well in the past below
    well_past_old_window = old_fixed_deadline + 120
    client.get_market_response = {"market": {"yes_bid_dollars": "0.81", "yes_ask_dollars": "0.82"}}
    client.get_orders_responses = [{
        "order_id": "order-1", "status": "resting", "fill_count_fp": "4.00",
        "taker_fill_cost_dollars": "3.28", "maker_fill_cost_dollars": "0",
        "taker_fees_dollars": "0.08", "maker_fees_dollars": "0",
    }]
    kl.poll_open_orders(client, s, cfg, well_past_old_window, live_enabled=True)
    assert order["status"] == "open", "should still be resting -- still profitable, market not closed"
    assert "order-1" not in client.cancelled_orders


if __name__ == "__main__":
    import tempfile
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                t(Path(td))
                print(f"PASS {t.__name__}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {t.__name__}: {e}")
            except Exception:
                failed += 1
                print(f"ERROR {t.__name__}")
                traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
