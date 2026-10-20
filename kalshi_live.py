"""kalshi_live.py: LIVE trading bot. Same signal, same six logic sets (A-F), same
bet sizes as the paper bot (reference/kalshi_multi_live_PAPER.py) -- see
signal_core.py, which both this file and the paper bot's math are built from.
This file adds everything the paper bot never needed: real order placement,
partial fills, reconciliation against the exchange, real settlement, real fees,
and a battery of safety limits. See README.md before running this.

SAFETY (do not weaken any of this without the user explicitly asking):
- Dry run unless BOTH --live is passed AND env var KALSHI_LIVE=1 is set.
- No API key loaded -> dry run only, regardless of flags.
- --demo selects Kalshi's demo environment (KALSHI_DEMO env var also works).
- config.json's size_multiplier (default 0.1) scales every bet; the bot never
  raises it itself.
- STOP file in this folder, or Ctrl+C, cancels open orders and exits.
- Reconciliation mismatch, repeated API errors, or a persistently stale price
  feed also cancel open orders and exit.
- A daily loss limit stops new orders (not settlement/cancellation) for the rest
  of the UTC day once tripped.
"""
import argparse
import datetime
import json
import os
import signal as signal_module
import sys
import time
import uuid
from pathlib import Path

import signal_core as sc
import env_setup
import combo_new
from kalshi_client import KalshiClient, KalshiAPIError, KalshiNetworkError, KalshiError, fmt_dollars

D = Path(__file__).parent
STATE_PATH = D / "live_state.json"
CONFIG_PATH = D / "config.json"
LOG_PATH = D / "live_log.jsonl"
HIST_PATH = D / "live_history.jsonl"
STOP_PATH = D / "STOP"
HEARTBEAT_PATH = D / "HEARTBEAT"

FEE_ESTIMATE = 0.02        # conservative pre-trade sizing estimate only; real P&L uses real fees from fills

# "C-loose": an EXTRA layer next to A-F (the original C is untouched). It copies the loose paper bot's C: minute-11 rule at 1.5 sigma, $100 tier,
# but runs on a dedicated $40 ledger, never bets more than 50% of that ledger at once, never draws more money from the main balance, and has no
# 1-contract minimum. signal_core.SLEEVES (the paper-bot parity list) is left alone.
# "A-skip": another EXTRA layer, same pattern as C-loose. It takes sleeve A's minute-4 signal when the move is WEAKER than A's 1.5 sigma threshold
# (1.0 <= |z| < 1.5), i.e. exactly the signals A skips, so it never overlaps A. Own $100 ledger (it stops if the ledger is gone), $10 flat stake
# (config extra_stake_dollars), never more than 50% of its ledger open at once, no 1-contract minimum.
SLEEVES = {**sc.SLEEVES,
           "C-loose": dict(rules=[11], stake=100.0, extra=True, start=40.0, min_sigma={11: 1.5}),
           "A-skip": dict(rules=[4], stake=10.0, extra=True, start=100.0, min_sigma={4: 1.0}, max_sigma={4: 1.5})}
LEDGER_START = {n: d.get("start", 1000.0) for n, d in SLEEVES.items()}
SETTLE_DELAY_S = 90        # spec PDF section 7: settle 90s after market close -- fixed, matches the paper bot

# The four constants below are safety/tolerance knobs, not signal logic -- they live in
# config.json (re-read every cycle) so they're visible and tunable without a code change.
# These are only the fallback defaults if a key is ever missing from config.json.
DEFAULT_MAX_CONSECUTIVE_ERRORS = 5
DEFAULT_MAX_STALE_STRIKES = 3
DEFAULT_SETTLEMENT_GRACE_SECONDS = 600
DEFAULT_CLOSE_SAFETY_MARGIN_S = 5
DEFAULT_RECONCILIATION_TOLERANCE = 0.5

iso = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Halt(Exception):
    """Raised to unwind the main loop and go straight to shutdown()."""


def log_event(rec):
    rec = dict(rec)
    rec.setdefault("ts", time.time())
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg


def utc_day():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def fresh_state():
    # Each sleeve's virtual bankroll stays $1000, same as the paper bot; only the
    # STAKE is scaled by size_multiplier (see process_rule_signal). That way, at
    # size_multiplier=1.0 the sizing math -- including the 50% open-bet cap --
    # reproduces the paper bot exactly, and at smaller multipliers the new
    # config.json hard limits (not this virtual cap) are what's actually binding.
    return dict(
        day=utc_day(), daily_realized_pnl=0.0, started_at=time.time(),
        sleeves={a: dict(seen=[], cash=LEDGER_START[a], start=LEDGER_START[a]) for a in SLEEVES},
        orders={}, positions={}, basis={}, cur={}, feed={}, addon_seen=[],
        consecutive_errors=0, stale_strikes=0, halted=False,
    )


def load_state():
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text())
        except Exception:
            s = fresh_state()
    else:
        s = fresh_state()
    for k, v in fresh_state().items():
        s.setdefault(k, v)
    for a in SLEEVES:
        s["sleeves"].setdefault(a, dict(seen=[], cash=LEDGER_START[a], start=LEDGER_START[a]))
    if s.get("day") != utc_day():
        s["day"] = utc_day()
        s["daily_realized_pnl"] = 0.0
    return s


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, default=str))


def sleeve_open_value(s, name):
    total = 0.0
    for pos in s["positions"].values():
        total += pos["sleeve_cost"].get(name, 0.0)
    for order in s["orders"].values():
        if order["status"] != "open":
            continue
        remaining = {k: order["requested"][k] - order.get("filled_alloc", {}).get(k, 0) for k in order["requested"]}
        total += remaining.get(name, 0) * order["price"]
    return total


def aggregate_open_dollars(s):
    total = sum(pos["sleeve_cost"].get(name, 0.0) for pos in s["positions"].values() for name in pos["sleeve_contracts"])
    for order in s["orders"].values():
        if order["status"] == "open":
            remaining_n = order["total_n"] - order.get("filled_n", 0)
            total += remaining_n * order["price"]
    return total


def market_open_dollars(s, ticker):
    total = sum(sum(pos["sleeve_cost"].values()) for pos in s["positions"].values() if pos["ticker"] == ticker)
    for order in s["orders"].values():
        if order["ticker"] == ticker and order["status"] == "open":
            remaining_n = order["total_n"] - order.get("filled_n", 0)
            total += remaining_n * order["price"]
    return total


def prorate_counts(requested, total_requested, amount):
    if amount <= 0 or total_requested <= 0:
        return {k: 0 for k in requested}
    raw = {k: requested[k] * amount / total_requested for k in requested}
    floor = {k: int(raw[k]) for k in raw}
    rem = amount - sum(floor.values())
    for k in sorted(requested, key=lambda k: raw[k] - floor[k], reverse=True)[:rem]:
        floor[k] += 1
    return floor


def _truncate_fractional_count(raw, order_id):
    """Truncate a fractional contract count to an int, logging (not silently
    dropping) any remainder. Kalshi supports fractional contracts (0.01
    granularity) on some markets; this bot's sleeve ledgers and position
    tracking are integer-contract-count based, so a fractional count here would
    otherwise be truncated with no record of the loss. Converts a silent
    data-loss risk into a visible, logged one rather than reworking the
    integer-based model -- fractional fills have not been observed live on
    these markets beyond sub-contract remainders within a single fill.
    """
    truncated = int(raw)
    if abs(raw - truncated) > 1e-9:
        log_event(dict(type="fractional_fill_count_truncated", order_id=order_id,
                        raw_fill_count_fp=raw, truncated_to=truncated))
    return truncated


def _parse_fill_count(resp, order_id):
    return _truncate_fractional_count(float(resp.get("fill_count_fp", "0") or 0), order_id)


def attribute_fill(s, order, total_filled, total_cost_dollars, total_fee_dollars):
    """Update sleeve ledgers and the shared position record for new fills on `order`.
    total_filled/total_cost_dollars/total_fee_dollars are CUMULATIVE for this order.
    """
    prev_filled = order.get("filled_n", 0)
    delta_n = total_filled - prev_filled
    if delta_n <= 0:
        return  # no new fill (or a stale/regressed read) -- nothing to attribute, leave filled_n as-is
    order["filled_n"] = total_filled
    delta_cost = total_cost_dollars - order.get("cum_cost", 0.0)
    delta_fee = total_fee_dollars - order.get("cum_fee", 0.0)
    order["cum_cost"], order["cum_fee"] = total_cost_dollars, total_fee_dollars
    prev_alloc = order.get("filled_alloc", {})
    new_alloc = prorate_counts(order["requested"], order["total_n"], total_filled)
    pos = s["positions"].setdefault(order["pos_key"], dict(
        ticker=order["ticker"], series=order["series"], rule=order["rule"], side=order["side"],
        close=order["close"], sleeve_contracts={}, sleeve_cost={}))
    for name in order["requested"]:
        d = new_alloc.get(name, 0) - prev_alloc.get(name, 0)
        if d <= 0:
            continue
        share_dollars = (delta_cost + delta_fee) * (d / delta_n)
        s["sleeves"][name]["cash"] -= share_dollars
        pos["sleeve_contracts"][name] = pos["sleeve_contracts"].get(name, 0) + d
        pos["sleeve_cost"][name] = pos["sleeve_cost"].get(name, 0.0) + share_dollars
        log_event(dict(type="fill", sleeve=name, ticker=order["ticker"], rule=order["rule"],
                        n=d, dollars=round(share_dollars, 4), order_id=order.get("order_id")))
    order["filled_alloc"] = new_alloc


def simulate_fill(s, requested, ticker, series, side, cost, rule, close_t, z):
    """Dry-run only: same instant-fill-at-displayed-price assumption the paper bot
    uses, purely so the status printout and history are meaningful while testing.
    Never used when live_enabled is true.
    """
    pos_key = f"{ticker}|{rule}|{uuid.uuid4().hex[:6]}"
    pos = dict(ticker=ticker, series=series, rule=rule, side=side, close=close_t,
               sleeve_contracts={}, sleeve_cost={})
    for name, n in requested.items():
        share = n * cost + FEE_ESTIMATE * n
        s["sleeves"][name]["cash"] -= share
        pos["sleeve_contracts"][name] = n
        pos["sleeve_cost"][name] = share
        log_event(dict(type="dry_run_fill", sleeve=name, ticker=ticker, rule=rule, n=n,
                        side=side, cost=cost, z=z))
    s["positions"][pos_key] = pos


def apply_hard_limits(s, cfg, series, ticker, cost, total_n, requested, real_value):
    """real_value is the account's portfolio VALUE (cash + open positions), not
    just liquid cash -- matches the paper bot's own "equity" concept for the
    overall exposure ceiling.
    """
    if total_n < 1:
        return 0, requested
    cap_by_order = int(cfg["max_order_size_dollars"] // cost) if cost > 0 else 0
    cap_by_market = int(max(0.0, cfg["max_market_open_dollars"] - market_open_dollars(s, ticker)) // cost) if cost > 0 else 0
    real_value = real_value if real_value is not None else 1000.0 * len(SLEEVES)
    cap_by_total = int(max(0.0, cfg["max_total_open_pct"] * real_value - aggregate_open_dollars(s)) // cost) if cost > 0 else 0
    capped = max(0, min(total_n, cap_by_order, cap_by_market, cap_by_total))
    if capped == total_n:
        return total_n, requested
    new_alloc = prorate_counts(requested, total_n, capped)
    return capped, {k: v for k, v in new_alloc.items() if v > 0}


def _resize_for_balance(client, ticker, exchange_index, requested, total_n, cost, fee_per_contract):
    """After an insufficient_balance rejection, re-check the real CURRENT shard
    balance (the balance used to size this order was a snapshot taken once at the
    start of the cycle, which can be stale if another order already spent it this
    same cycle -- confirmed live) and shrink the order to whatever that fresh
    balance can actually afford, keeping each sleeve's share proportional to its
    original request (same proration already used for partial fills).
    Returns (new_total_n, new_requested) or None if nothing is affordable -- and
    logs exactly which of those two things happened, rather than letting the
    caller's generic order_rejected log be the only trace of what was tried.
    """
    try:
        bal = client.get_balance(exchange_index=exchange_index)
    except (KalshiAPIError, KalshiNetworkError) as e:
        log_event(dict(type="resize_balance_check_failed", ticker=ticker, error=str(e)))
        return None
    fresh_cash = float(bal.get("balance_dollars", 0) or 0)
    affordable_n = int(fresh_cash // (cost + fee_per_contract))
    new_total_n = min(total_n, affordable_n)
    if new_total_n < 1:
        log_event(dict(type="resize_unaffordable", ticker=ticker, fresh_cash=fresh_cash,
                        affordable_n=affordable_n, original_total_n=total_n))
        return None
    new_alloc = {k: v for k, v in prorate_counts(requested, total_n, new_total_n).items() if v > 0}
    if not new_alloc:
        log_event(dict(type="resize_unaffordable", ticker=ticker, fresh_cash=fresh_cash,
                        affordable_n=affordable_n, original_total_n=total_n,
                        reason="proration left every sleeve at 0"))
        return None
    return sum(new_alloc.values()), new_alloc


def fetch_open_market(client, series, now):
    resp = client.get_markets(series, status="open", limit=10)
    mk = [m for m in resp.get("markets", []) if iso(m["open_time"]) <= now < iso(m["close_time"])]
    if not mk:
        return None
    return min(mk, key=lambda x: iso(x["close_time"]))


def _execute_order(client, s, cfg, series, ticker, rule, side, cost, close_t, live_enabled,
                    requested, total_n, exchange_index, price_str, z=None, is_addon=False):
    """Places (or simulates) a combined order for `requested` sleeves and tracks
    it, including the insufficient_balance resize-retry. Shared by a fresh signal
    entry and an add-on buy on an already-open position -- same order mechanics
    either way, only how `requested`/`total_n` were sized differs.
    """
    if not live_enabled:
        simulate_fill(s, requested, ticker, series, side, cost, rule, close_t, z)
        return 1

    coid = str(uuid.uuid4())
    book_side = "bid" if side == "yes" else "ask"
    # price_str is the exact string Kalshi quoted for this side (yes_ask_dollars for
    # a YES bet, yes_bid_dollars for a NO bet) -- never reformatted through float
    # rounding, which was rejecting orders outright on sub-penny-tick markets.
    price = price_str if price_str else fmt_dollars(cost if side == "yes" else (1 - cost))
    if cfg.get("cross_spread_first_order"):
        # fmt_dollars itself formats a float to 4 decimal places as a fallback --
        # do NOT round to 2 decimals here. Confirmed live earlier this session:
        # a 2-decimal-rounded price gets rejected outright on markets using
        # sub-penny ("tapered_deci_cent") ticks.
        worst_cost = min(sc.PRICE_HI, cost + float(cfg.get("cross_spread_buffer_dollars", 0.02)))
        price = fmt_dollars(worst_cost if side == "yes" else (1 - worst_cost))
    try:
        resp = client.create_order(ticker, book_side, total_n, price, coid, exchange_index=exchange_index)
    except KalshiAPIError as e:
        if e.code != "insufficient_balance":
            rej = dict(type="order_rejected", ticker=ticker, coid=coid, error=str(e))
            if cfg.get("log_full_error_body", True):
                rej.update(status=e.status, code=e.code, message=e.message, details=e.details,
                           sent=dict(book_side=book_side, count=total_n, price=price, exchange_index=exchange_index))
            log_event(rej)
            return 1
        resized = _resize_for_balance(client, ticker, exchange_index, requested, total_n, cost, FEE_ESTIMATE)
        if resized is None:
            log_event(dict(type="order_rejected", ticker=ticker, coid=coid, error=str(e)))
            return 1
        orig_total_n, total_n = total_n, resized[0]
        requested = resized[1]
        coid = str(uuid.uuid4())
        try:
            resp = client.create_order(ticker, book_side, total_n, price, coid, exchange_index=exchange_index)
            log_event(dict(type="order_resized_retry", ticker=ticker, coid=coid,
                            original_error=str(e), original_total_n=orig_total_n,
                            new_total_n=total_n, new_requested=requested))
        except KalshiAPIError as e2:
            log_event(dict(type="order_rejected", ticker=ticker, coid=coid, error=str(e2), after_resize=True))
            return 1

    order = dict(ticker=ticker, series=series, rule=rule, side=side, book_side=book_side, price=cost,
                 requested=requested, total_n=total_n, coid=coid, order_id=resp.get("order_id"),
                 exchange_index=exchange_index, is_addon=is_addon,
                 filled_n=0, cum_cost=0.0, cum_fee=0.0, filled_alloc={},
                 # No fixed decision-window deadline anymore (per explicit user direction):
                 # a resting order stays in and reprices as long as it's still within the
                 # profitable price bound. "deadline" here is purely the backstop display
                 # value for the dashboard -- the real exit trigger is _tick_resting_order.
                 deadline=close_t - cfg.get("close_safety_margin_seconds", DEFAULT_CLOSE_SAFETY_MARGIN_S),
                 close=close_t, status="open",
                 pos_key=f"{ticker}|{rule}")
    fc = int(float(resp.get("fill_count", "0") or 0))
    if fc > 0:
        if "average_fill_price" in resp:
            # average_fill_price is always YES-denominated (confirmed via Kalshi's
            # OpenAPI spec: the request's own `price` field is YES-denominated
            # regardless of book_side, and the response mirrors that convention) --
            # NOT the price of the side actually traded. Confirmed live: a "no"
            # side order's cost_paid got computed from the raw YES price (~0.10)
            # instead of the NO price actually paid (~0.90), inflating that
            # order's recorded P&L by roughly 9x. Convert for the side traded,
            # exactly like every other price in this codebase (see fmt_dollars
            # call above, compute_cost in signal_core.py).
            raw_price = float(resp["average_fill_price"])
            avg_price = raw_price if side == "yes" else (1 - raw_price)
        else:
            avg_price = cost  # already side-correct -- cost is sc.compute_cost(side, ...)
        avg_fee = float(resp.get("average_fee_paid", 0) or 0)
        attribute_fill(s, order, fc, fc * avg_price, fc * avg_fee)
    if order["filled_n"] >= order["total_n"]:
        order["status"] = "closed"
    s["orders"][coid] = order
    log_event(dict(type="order_placed", ticker=ticker, series=series, rule=rule, side=side,
                    book_side=book_side, price=cost, requested=requested, total_n=total_n,
                    order_id=order["order_id"], fill_count=fc, is_addon=is_addon))
    return 1


def process_addon_signal(client, s, cfg, series, ticker, close_t, rule, side, cost, z, live_enabled,
                          real_cash, real_value, exchange_index, price_str):
    """Every sleeve eligible for this rule already has a position on this
    ticker+rule (process_rule_signal only reaches here once that's true). Per
    explicit user request: if the signal is STILL firing (true by construction --
    this is only called while sc.rule_fires(...) holds for the current cycle) and
    the price has gotten CHEAPER than the position's own average entry cost, add
    to it once -- allocate addon_stake_pct_of_available of currently-available
    free liquid cash, split across every sleeve eligible for this rule
    proportionally to their original relative weight (same split as the initial
    entry). Only ever fires once per position, not every further price tick.

    Matches positions by (ticker, rule) rather than a direct dict key: live
    positions use the canonical pos_key "ticker|rule", but dry-run positions
    (simulate_fill) key themselves "ticker|rule|<random>" so repeated paper
    entries don't collide -- a direct lookup would silently never find a
    dry-run position. "Already added to this signal" is tracked separately in
    s["addon_seen"], the same bounded-list pattern each sleeve's own "seen" list
    already uses, rather than flagging one (possibly non-unique) position dict.
    """
    key = f"{ticker}|{rule}"
    if key in s["addon_seen"]:
        return 0
    matching = [pos for pos in s["positions"].values() if pos["ticker"] == ticker and pos["rule"] == rule]
    if not matching:
        return 0
    total_contracts = sum(sum(pos["sleeve_contracts"].values()) for pos in matching)
    if total_contracts <= 0:
        return 0
    # sleeve_cost bakes in the per-contract fee (both simulate_fill and
    # attribute_fill fold cost+fee together into share_dollars), but `cost` here
    # is fee-exclusive -- strip FEE_ESTIMATE back out so this is an apples-to-apples
    # comparison. Without it, an unchanged price looked artificially "cheaper"
    # than the fee-inclusive average and fired an add-on with no real improvement.
    avg_cost = sum(sum(pos["sleeve_cost"].values()) for pos in matching) / total_contracts - FEE_ESTIMATE
    if cost >= avg_cost - 1e-9:
        return 0  # price hasn't improved since entry -- nothing to add

    if s["daily_realized_pnl"] <= -cfg["daily_loss_limit_dollars"]:
        log_event(dict(type="signal_skipped", ticker=ticker, rule=rule, reason="daily_loss_limit", addon=True))
        return 0

    def mark_done():
        s["addon_seen"].append(key)
        s["addon_seen"] = s["addon_seen"][-200:]

    pct = cfg.get("addon_stake_pct_of_available")
    if not pct or real_cash is None:
        return 0
    eligible = [name for name, sleeve_def in SLEEVES.items() if rule in sleeve_def["rules"] and not sleeve_def.get("extra")]
    total_addon_dollars = float(real_cash) * float(pct)
    total_weight = sum(SLEEVES[name]["stake"] for name in eligible)
    requested = {}
    for name in eligible:
        sl = s["sleeves"][name]
        open_value = sleeve_open_value(s, name)
        equity = sl["cash"] + open_value
        stake = total_addon_dollars * (SLEEVES[name]["stake"] / total_weight) if total_weight > 0 else 0.0
        stake = min(stake, real_cash)  # never size a bet beyond actual liquid cash
        n = sc.compute_n(stake, sl["cash"], cost, FEE_ESTIMATE, open_value, equity)
        if n >= 1:
            requested[name] = n
    if not requested:
        mark_done()  # don't keep re-checking every cycle once genuinely unaffordable
        log_event(dict(type="signal_skipped", ticker=ticker, rule=rule, reason="open_cap_or_cash", addon=True))
        return 0

    total_n = sum(requested.values())
    total_n, requested = apply_hard_limits(s, cfg, series, ticker, cost, total_n, requested, real_value)
    if total_n < 1:
        mark_done()
        log_event(dict(type="signal_skipped", ticker=ticker, rule=rule, reason="hard_limit", addon=True))
        return 0

    mark_done()  # one add-on per position, win or lose below
    log_event(dict(type="addon_decision", sleeves=list(requested), ticker=ticker, series=series, rule=rule,
                    side=side, z=z, cost=cost, avg_cost=round(avg_cost, 4), requested_n=requested,
                    combined_n=total_n, live=live_enabled))
    return _execute_order(client, s, cfg, series, ticker, rule, side, cost, close_t, live_enabled,
                           requested, total_n, exchange_index, price_str, z=z, is_addon=True)


_recent_pnl_cache = {"t": 0.0, "window": None, "pnl": {}}


def sleeve_recent_pnl(window_hours):
    now = time.time()
    c = _recent_pnl_cache
    if c["window"] == window_hours and now - c["t"] < 60:
        return c["pnl"]
    cutoff, pnl = now - window_hours * 3600, {}
    if HIST_PATH.exists():
        with HIST_PATH.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= cutoff and r.get("sleeve"):
                    pnl[r["sleeve"]] = pnl.get(r["sleeve"], 0.0) + float(r.get("pnl", 0) or 0)
    c.update(t=now, window=window_hours, pnl=pnl)
    return pnl


def sleeve_throttle_factor(s, cfg, name, real_value):
    """Stake and open-cap multiplier for a sleeve, cut by its recent realized loss as a share of the account."""
    th = cfg.get("sleeve_throttle") or {}
    if not th.get("enabled") or SLEEVES[name].get("extra"):
        return 1.0
    base = real_value if real_value else sum(sl["cash"] + sleeve_open_value(s, n) for n, sl in s["sleeves"].items())
    if not base:
        return 1.0
    loss_pct = -sleeve_recent_pnl(float(th.get("window_hours", 24))).get(name, 0.0) / float(base)
    factor = 1.0
    for threshold, mult in sorted(th.get("steps", []), key=lambda st: st[0]):
        if loss_pct >= threshold:
            factor = float(mult)
    prev = s.setdefault("sleeve_throttle", {}).get(name, 1.0)
    if factor != prev:
        s["sleeve_throttle"][name] = factor
        log_event(dict(type="sleeve_throttle", sleeve=name, factor=factor, loss_pct=round(loss_pct, 4)))
    return factor


def process_rule_signal(client, s, cfg, series, ticker, open_t, close_t, rule, side, cost, z, live_enabled, real_cash, real_value, exchange_index, price_str,
                        std_fires=True, extra_fires=()):
    key = f"{ticker}|{rule}"
    firing = []
    for name, sleeve_def in SLEEVES.items():
        if rule not in sleeve_def["rules"]:
            continue
        # the extra layers (C-loose, A-skip) fire on their own thresholds; every other sleeve on the standard one
        if sleeve_def.get("extra"):
            if name not in extra_fires:
                continue
        elif not std_fires:
            continue
        else:
            # safe mode: no standard-sleeve entries above this price (a miss at 0.94+ costs 15x what a win pays)
            max_entry = cfg.get("safe_mode_max_entry_price")
            if max_entry and cost > float(max_entry):
                continue
        sl = s["sleeves"][name]
        if key in sl["seen"]:
            continue
        already_has = any(name in pos["sleeve_contracts"] for pos in s["positions"].values()
                           if pos["ticker"] == ticker and pos["rule"] == rule)
        if already_has:
            continue
        firing.append(name)
    if not firing:
        if not std_fires:
            return 0  # only the loose layer's threshold was met (and it already acted) -- no add-on for the standard sleeves
        return process_addon_signal(client, s, cfg, series, ticker, close_t, rule, side, cost, z,
                                     live_enabled, real_cash, real_value, exchange_index, price_str)
    for name in firing:
        s["sleeves"][name]["seen"].append(key)
        s["sleeves"][name]["seen"] = s["sleeves"][name]["seen"][-200:]

    if s["daily_realized_pnl"] <= -cfg["daily_loss_limit_dollars"]:
        log_event(dict(type="signal_skipped", ticker=ticker, rule=rule, reason="daily_loss_limit"))
        return 0

    requested = {}
    total_stake_pct = cfg.get("total_stake_pct_of_balance")
    flat_stake = cfg.get("flat_stake_dollars")
    if total_stake_pct:
        # Portfolio VALUE (cash + open positions), not just liquid cash -- matches
        # the paper bot's own "equity" concept, so the stake tracks the account's
        # true size rather than shrinking every time money is tied up in a resting
        # bet. This is the TOTAL across every sleeve firing on this one signal,
        # not a per-sleeve amount (explicit request: "5% of the overall balance,
        # A through F divvy up that 5%") -- split proportionally to each firing
        # sleeve's original relative weight (SLEEVES' tiered stake amounts,
        # e.g. B/C/F at $100 vs A/D/E at $40 -- a 2.5x ratio), scaled to fit inside
        # the 5% total rather than each sleeve getting its own independent 5%.
        balance_base = real_value if real_value is not None else sum(s["sleeves"][n]["cash"] for n in firing)
        # the dedicated C-loose ledger is set aside: it is not part of the base the other sleeves are sized from
        cl_equity = sum(s["sleeves"][n]["cash"] + sleeve_open_value(s, n) for n, d_ in SLEEVES.items() if d_.get("extra"))
        total_stake_dollars = max(0.0, float(balance_base) - cl_equity) * float(total_stake_pct)
        # Shrink rather than skip when many coins fire together: each signal may use at most
        # 1/divisor of the room left under max_total_open_pct, so every paper signal still gets a bet.
        divisor = cfg.get("exposure_room_divisor")
        if divisor:
            room = max(0.0, cfg["max_total_open_pct"] * float(balance_base) - aggregate_open_dollars(s))
            total_stake_dollars = min(total_stake_dollars, room / float(divisor))
        total_weight = sum(SLEEVES[name]["stake"] for name in firing if not SLEEVES[name].get("extra"))
    for name in firing:
        sl = s["sleeves"][name]
        open_value = sleeve_open_value(s, name)
        equity = sl["cash"] + open_value
        is_extra = bool(SLEEVES[name].get("extra"))
        if is_extra:
            # its own tier (config extra_stake_dollars can override); compute_n then caps it at 50% of the dedicated ledger and the ledger's cash
            stake = float(cfg.get("extra_stake_dollars", {}).get(name, SLEEVES[name]["stake"]))
        elif total_stake_pct:
            stake = total_stake_dollars * (SLEEVES[name]["stake"] / total_weight) if total_weight > 0 else 0.0
        elif flat_stake:
            stake = float(flat_stake)
        else:
            stake = SLEEVES[name]["stake"] * cfg["size_multiplier"]
        # Sleeve-specific fixed targets override normal percentage sizing only
        # when spendable cash covers the target and estimated execution costs.
        # Otherwise leave the existing percentage allocation in place.
        fixed_target = cfg.get("sleeve_fixed_stake_dollars", {}).get(name)
        if not is_extra and fixed_target is not None and float(fixed_target) > 0:
            fixed_target = float(fixed_target)
            available_cash = min(sl["cash"], real_cash) if real_cash is not None else sl["cash"]
            fixed_n = int(fixed_target // cost) if cost > 0 else 0
            limit_cost = min(sc.PRICE_HI, cost + float(cfg.get("cross_spread_buffer_dollars", 0.02))) if cfg.get("cross_spread_first_order") else cost
            required_cash = max(fixed_target, fixed_n * (limit_cost + FEE_ESTIMATE))
            if fixed_n > 0 and available_cash >= required_cash:
                stake = fixed_target
            else:
                log_event(dict(type="fixed_stake_fallback", sleeve=name, ticker=ticker, rule=rule,
                               target=fixed_target, available_cash=available_cash,
                               fallback_stake=stake, reason="insufficient_cash"))
        if real_cash is not None:
            stake = min(stake, real_cash)  # never size a bet beyond actual liquid cash
        throttle = sleeve_throttle_factor(s, cfg, name, real_value)
        stake *= throttle
        n = sc.compute_n(stake, sl["cash"], cost, FEE_ESTIMATE, open_value, equity, open_cap=sc.OPEN_CAP * throttle)
        # min_one_contract: when the sized stake or the open-bet cap leaves 0 contracts,
        # still buy 1, as long as real cash covers that contract plus its fee. Not for C-loose: its ledger is strict.
        if (not is_extra and n < 1 and cfg.get("min_one_contract") and cost > 0 and sl["cash"] >= cost + FEE_ESTIMATE
                and (real_cash is None or real_cash >= cost + FEE_ESTIMATE)):
            n = 1
            log_event(dict(type="min_one_contract", sleeve=name, ticker=ticker, rule=rule, cost=cost))
        if n < 1:
            log_event(dict(type="signal_skipped", sleeve=name, ticker=ticker, rule=rule,
                            reason="open_cap_or_cash", z=z, cost=cost))
            continue
        requested[name] = n
    if not requested:
        return 0
    requested_pre_limits = dict(requested)

    total_n = sum(requested.values())
    total_n, requested = apply_hard_limits(s, cfg, series, ticker, cost, total_n, requested, real_value)
    std_pre_limits = {k: v for k, v in requested_pre_limits.items() if not SLEEVES[k].get("extra")}
    if (total_n < 1 and cfg.get("min_one_contract") and std_pre_limits and cost > 0
            and (real_cash is None or real_cash >= cost + FEE_ESTIMATE)):
        top = max(std_pre_limits, key=lambda n_: SLEEVES[n_]["stake"])
        # A one-contract minimum must not bypass the account exposure ceiling.
        total_n, requested = apply_hard_limits(s, cfg, series, ticker, cost, 1, {top: 1}, real_value)
        if total_n:
            log_event(dict(type="min_one_contract", sleeve=top, ticker=ticker, rule=rule, cost=cost, after="hard_limit"))
    if total_n < 1:
        log_event(dict(type="signal_skipped", ticker=ticker, rule=rule, reason="hard_limit"))
        return 0

    log_event(dict(type="decision", sleeves=list(requested), ticker=ticker, series=series, rule=rule,
                    side=side, z=z, cost=cost, requested_n=requested, combined_n=total_n, live=live_enabled))
    return _execute_order(client, s, cfg, series, ticker, rule, side, cost, close_t, live_enabled,
                           requested, total_n, exchange_index, price_str, z=z)


def _find_order(client, order):
    """GET /portfolio/orders/{id} 404s for accounts whose orders live on a
    non-default exchange shard (confirmed live against production); the list
    endpoint filtered by ticker + exchange_index is what actually works.
    """
    resp = client.get_orders(ticker=order["ticker"], exchange_index=order.get("exchange_index"), limit=50)
    for o in resp.get("orders", []):
        if o.get("order_id") == order["order_id"]:
            return o
    return None


def _tick_resting_order(client, s, cfg, order, now):
    """What to do with a still-open resting order, per explicit user direction:
    no fixed time limit on the decision window anymore. Stay in it -- repricing
    toward the market as needed -- for as long as the current price is still
    within signal_core.price_ok, the same 3-97 cent bound that made the bet
    worth taking to begin with. Only give up (return a reason to cancel) when
    the price has moved outside that bound, or the market itself is about to
    close. Returns a cancel-reason string, or None to keep resting.
    """
    close_safety_margin = cfg.get("close_safety_margin_seconds", DEFAULT_CLOSE_SAFETY_MARGIN_S)
    if now >= order["close"] - close_safety_margin:
        return "market closing"
    try:
        m = client.get_market(order["ticker"])["market"]
    except (KalshiAPIError, KalshiNetworkError):
        return None  # transient read failure -- try again next cycle, don't give up on a blip
    bid = float(m.get("yes_bid_dollars") or 0)
    ask = float(m.get("yes_ask_dollars") or 0)
    if not (bid and ask):
        return None
    new_cost = sc.compute_cost(order["side"], bid, ask)
    if not sc.price_ok(new_cost):
        return f"price moved to {new_cost:.3f}, outside the profitable 3-97 cent bound"
    if new_cost > order["price"] + 1e-9:
        new_submitted_price = new_cost if order["side"] == "yes" else (1 - new_cost)
        try:
            client.amend_order(order["order_id"], order["ticker"], order["book_side"], new_submitted_price,
                                order["total_n"], exchange_index=order.get("exchange_index"))
            log_event(dict(type="order_repriced", order_id=order["order_id"], ticker=order["ticker"],
                            old_cost=order["price"], new_cost=new_cost))
            order["price"] = new_cost
            # A reprice can itself be marketable enough to fill immediately (confirmed
            # live: repricing toward the market crossed the book and filled on the
            # spot) -- re-check and attribute right away, or reconcile() sees a fill
            # this cycle never recorded and halts on what isn't really a mismatch.
            try:
                resp = _find_order(client, order)
                if resp is not None:
                    attribute_fill(
                        s, order, _parse_fill_count(resp, order["order_id"]),
                        float(resp.get("taker_fill_cost_dollars", 0) or 0) + float(resp.get("maker_fill_cost_dollars", 0) or 0),
                        float(resp.get("taker_fees_dollars", 0) or 0) + float(resp.get("maker_fees_dollars", 0) or 0))
                    if resp.get("status") in ("canceled", "executed") or order["filled_n"] >= order["total_n"]:
                        order["status"] = "closed"
                        log_event(dict(type="order_closed", order_id=order["order_id"], ticker=order["ticker"],
                                        filled_n=order["filled_n"], total_n=order["total_n"],
                                        exchange_status=resp.get("status")))
            except KalshiAPIError:
                pass
        except KalshiAPIError as e:
            log_event(dict(type="reprice_error", order_id=order["order_id"], ticker=order["ticker"], error=str(e)))
    return None


def poll_open_orders(client, s, cfg, now, live_enabled):
    if not live_enabled:
        return
    for coid, order in list(s["orders"].items()):
        if order["status"] != "open":
            continue
        try:
            resp = _find_order(client, order)
        except KalshiAPIError as e:
            log_event(dict(type="get_order_error", order_id=order["order_id"], error=str(e)))
            continue
        if resp is None:
            log_event(dict(type="get_order_error", order_id=order["order_id"], error="order not found in list"))
            continue
        total_filled = _parse_fill_count(resp, order["order_id"])
        total_cost = float(resp.get("taker_fill_cost_dollars", 0) or 0) + float(resp.get("maker_fill_cost_dollars", 0) or 0)
        total_fee = float(resp.get("taker_fees_dollars", 0) or 0) + float(resp.get("maker_fees_dollars", 0) or 0)
        attribute_fill(s, order, total_filled, total_cost, total_fee)
        if resp.get("status") in ("canceled", "executed") or order["filled_n"] >= order["total_n"]:
            order["status"] = "closed"
            log_event(dict(type="order_closed", order_id=order["order_id"], ticker=order["ticker"],
                            filled_n=order["filled_n"], total_n=order["total_n"], exchange_status=resp.get("status")))
            continue
        cancel_reason = _tick_resting_order(client, s, cfg, order, now)
        if cancel_reason is None:
            continue
        try:
            cresp = client.cancel_order(order["order_id"], market_ticker=order["ticker"],
                                         exchange_index=order.get("exchange_index"))
            log_event(dict(type="order_cancelled", order_id=order["order_id"], ticker=order["ticker"],
                            reason=cancel_reason, reduced_by=cresp.get("reduced_by")))
            # A fill can land in the gap between the poll above and this cancel;
            # true up against the exchange once more before closing the order out.
            try:
                resp2 = _find_order(client, order)
                if resp2 is not None:
                    attribute_fill(
                        s, order, _parse_fill_count(resp2, order["order_id"]),
                        float(resp2.get("taker_fill_cost_dollars", 0) or 0) + float(resp2.get("maker_fill_cost_dollars", 0) or 0),
                        float(resp2.get("taker_fees_dollars", 0) or 0) + float(resp2.get("maker_fees_dollars", 0) or 0))
            except KalshiAPIError:
                pass
        except KalshiAPIError as e:
            log_event(dict(type="cancel_error", order_id=order["order_id"], error=str(e)))
        order["status"] = "closed"


def settle(client, s, now):
    for key, pos in list(s["positions"].items()):
        if now < pos["close"] + SETTLE_DELAY_S:
            continue
        try:
            m = client.get_market(pos["ticker"])["market"]
        except (KalshiAPIError, KalshiNetworkError):
            continue
        if m.get("result") not in ("yes", "no"):
            continue
        win = m["result"] == pos["side"]
        for name, n in pos["sleeve_contracts"].items():
            payout = n * 1.0 if win else 0.0
            cost_paid = pos["sleeve_cost"].get(name, 0.0)
            pnl = payout - cost_paid
            s["sleeves"][name]["cash"] += payout
            s["daily_realized_pnl"] += pnl
            rec = dict(sleeve=name, series=pos["series"], ticker=pos["ticker"], side=pos["side"], n=n,
                        cost_paid=round(cost_paid, 4), pnl=round(pnl, 4), win=win, res=m["result"],
                        rule=pos["rule"], t=pos["close"])
            with HIST_PATH.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            log_event(dict(type="settled", **rec))
        del s["positions"][key]


def _auto_resolve_mismatch(client, s, ticker):
    """Per explicit user request ("the bot should never go down") after this
    exact race hit multiple times live: a reprice/amend fills an order
    instantly, but the bot's own tracking lags behind it, so reconcile() sees a
    mismatch that isn't really one -- the fill is real and already
    attributed-but-stale, not unexplained. Before halting, re-derive every one
    of the bot's own orders on this ticker from Kalshi's OWN FILLS LEDGER
    (GET /portfolio/fills) right now and attribute whatever it finds.

    Fills, not the orders-list endpoint's fill_count_fp snapshot, are the
    source of truth here: confirmed live that fill_count_fp can still read
    stale (e.g. 39.12 when the real total was already 48) even on an immediate
    re-check after a reprice, while /portfolio/fills for that same order_id
    already reflected the true total -- each fill there is the actual trade
    execution record, not a snapshot that can lag behind it.

    If a real position is NOT explained by any of the bot's own orders' fills
    (e.g. a stray manual trade, or a genuine bug), this changes nothing and
    reconcile() still halts -- this only auto-resolves the lag, it doesn't
    paper over a real anomaly.
    """
    for order in s["orders"].values():
        if order.get("ticker") != ticker:
            continue
        try:
            fills = client.get_fills(order_id=order["order_id"]).get("fills", [])
        except (KalshiAPIError, KalshiNetworkError):
            continue
        if not fills:
            continue
        raw_total = sum(float(f.get("count_fp", 0) or 0) for f in fills)
        total_filled = _truncate_fractional_count(raw_total, order["order_id"])
        price_key = "yes_price_dollars" if order["side"] == "yes" else "no_price_dollars"
        total_cost = sum(float(f.get("count_fp", 0) or 0) * float(f.get(price_key, 0) or 0) for f in fills)
        total_fee = sum(float(f.get("fee_cost", 0) or 0) for f in fills)
        attribute_fill(s, order, total_filled, total_cost, total_fee)
        if order["filled_n"] >= order["total_n"]:
            order["status"] = "closed"


def _compute_expected_positions(s):
    expected = {}
    close_time_by_ticker = {}
    for pos in s["positions"].values():
        n = sum(pos["sleeve_contracts"].values())
        signed = n if pos["side"] == "yes" else -n
        expected[pos["ticker"]] = expected.get(pos["ticker"], 0) + signed
        close_time_by_ticker[pos["ticker"]] = pos["close"]
    return expected, close_time_by_ticker


def reconcile(client, s, cfg, live_enabled, now):
    if not live_enabled:
        return
    tolerance = cfg.get("reconciliation_tolerance_contracts", DEFAULT_RECONCILIATION_TOLERANCE)
    grace_seconds = cfg.get("settlement_grace_seconds", DEFAULT_SETTLEMENT_GRACE_SECONDS)
    try:
        real_positions = client.get_positions()["market_positions"]
    except (KalshiAPIError, KalshiNetworkError) as e:
        raise Halt(f"reconciliation fetch failed: {e}")
    expected, close_time_by_ticker = _compute_expected_positions(s)
    real = {p["ticker"]: float(p.get("position_fp", 0) or 0) for p in real_positions}
    # Untracked positions in markets outside the bot's own series (manual combos, sports, etc.)
    # can never be the bot's, so they must not halt it; strays inside its own series still do.
    bot_series = set(sc.SERIES)
    real = {t: n for t, n in real.items() if t in expected or t.split("-")[0] in bot_series}
    for ticker in set(expected) | set(real):
        exp_n = expected.get(ticker, 0.0)
        real_n = real.get(ticker, 0.0)
        if abs(real_n - exp_n) <= tolerance:
            continue
        # A ticker we already track, whose market has already closed, can briefly
        # vanish from the exchange's "unsettled positions" view before its public
        # result is queryable (confirmed live: a market dropped out of unsettled
        # positions while GET /markets/{ticker} still returned no result for a
        # while after). That's settle()'s job to clean up once the result shows
        # up, not a real mismatch -- skip it here rather than halt on a race.
        close_time = close_time_by_ticker.get(ticker)
        if close_time is not None and close_time <= now < close_time + grace_seconds:
            continue

        _auto_resolve_mismatch(client, s, ticker)
        expected, close_time_by_ticker = _compute_expected_positions(s)
        new_exp_n = expected.get(ticker, 0.0)
        if abs(real_n - new_exp_n) <= tolerance:
            log_event(dict(type="reconciliation_auto_resolved", ticker=ticker,
                            was_expected=exp_n, now_expected=new_exp_n, real=real_n))
            continue
        # The user places manual bets in the bot's own markets: a position the bot has no record of and
        # none of its own orders explain is external, not a bot bug -- note it once and keep trading.
        if abs(new_exp_n) <= tolerance and not any(o.get("ticker") == ticker for o in s["orders"].values()):
            ext = s.setdefault("external_positions", {})
            if ext.get(ticker) != real_n:
                ext[ticker] = real_n
                log_event(dict(type="external_position_ignored", ticker=ticker, real=real_n))
            continue

        raise Halt(f"reconciliation mismatch on {ticker}: bot expects {new_exp_n}, exchange shows {real_n}")


def status_line(s, real_cash, real_value):
    sleeve_vals = []
    for name, sl in s["sleeves"].items():
        open_v = sleeve_open_value(s, name)
        sleeve_vals.append(f"{name} ${sl['cash'] + open_v:,.2f}")
    if real_cash is not None:
        tc, tv = s.get("tradeable_cash"), s.get("tradeable_value")
        shard_part = f" shard_cash=${tc:,.2f} shard_value=${tv:,.2f}" if tc is not None else ""
        bal = f"cash=${real_cash:,.2f} value=${real_value:,.2f}{shard_part}"
    else:
        bal = "n/a (dry run)"
    print(f"[live] {bal} open=${aggregate_open_dollars(s):,.2f} "
          f"today={s['daily_realized_pnl']:+.2f} | " + " | ".join(sleeve_vals), flush=True)


def shutdown(client, s, live_enabled, reason):
    log_event(dict(type="shutdown", reason=reason))
    print(f"[live] shutting down: {reason}", flush=True)
    if live_enabled:
        try:
            resp = client.cancel_all_orders()
            log_event(dict(type="cancel_all", reason=reason, resp=resp))
        except KalshiError as e:
            log_event(dict(type="cancel_all_failed", error=str(e)))
    save_state(s)


def fetch_balance_pair(client, exchange_index=None):
    bal = client.get_balance(exchange_index=exchange_index)
    cash = float(bal.get("balance_dollars") or (bal.get("balance", 0) / 100.0))
    # portfolio_value is the value held in OPEN POSITIONS only (confirmed live: it
    # read 0 with zero open positions while cash was $50.99) -- total value/equity
    # is cash plus that, not portfolio_value alone.
    value = cash + float(bal.get("portfolio_value", 0) or 0) / 100.0
    return cash, value


def run_cycle(client, s, cfg, now, live_enabled):
    real_cash = real_value = None       # aggregate, across every shard -- for display only
    tradeable_cash = tradeable_value = None  # the shard these markets actually trade on -- for sizing
    if live_enabled:
        try:
            real_cash, real_value = fetch_balance_pair(client)
        except (KalshiAPIError, KalshiNetworkError) as e:
            raise Halt(f"could not fetch balance: {e}")
    s["real_cash"], s["real_value"] = real_cash, real_value

    # Order matters here, confirmed live both ways:
    # 1. poll FIRST: a fill that just happened needs attributing into our own
    #    positions before anything else looks at them, or reconcile() sees a real
    #    position it doesn't recognize yet and halts on a timing race, not a
    #    genuine mismatch.
    # 2. settle AFTER poll, BEFORE reconcile: a position that was only just
    #    attributed by the poll above, but whose market already closed+90s ago,
    #    needs to be settled (cleared out) in this SAME cycle -- otherwise
    #    reconcile compares it against the exchange's real (already-settled, so
    #    now empty) view and halts on a position that's simply late to be cleared
    #    from our own side, one cycle before settle() would have gotten to it.
    poll_open_orders(client, s, cfg, now, live_enabled)
    settle(client, s, now)
    reconcile(client, s, cfg, live_enabled, now)
    try:
        combo_new.tick(client, s, cfg, now, live_enabled, log_event,
                       {n for n, d in SLEEVES.items() if not d.get("extra")})
    except Exception as e:
        log_event(dict(type="combo_error", error=str(e)[:300]))

    orders_this_cycle = 0
    checked_count = 0
    stale_count = 0
    s.setdefault("feed", {})
    for series, prod in sc.SERIES.items():
        if not cfg["enabled_series"].get(series, True):
            continue
        try:
            mk = fetch_open_market(client, series, now)
            if mk is None:
                continue
            t = mk["ticker"]
            o, c = iso(mk["open_time"]), iso(mk["close_time"])
            if mk.get("floor_strike") is None:
                continue
            K = float(mk["floor_strike"])
            # Funds are sharded per exchange_index (confirmed live: an account can
            # show ~$50 aggregate while the shard these crypto markets actually
            # trade on has under $1, causing real "insufficient_balance" order
            # rejections). Fetch that shard's real spendable balance once per
            # cycle, the first time we see it, and use it for all sizing/limits.
            if live_enabled and tradeable_cash is None and mk.get("exchange_index") is not None:
                try:
                    tradeable_cash, tradeable_value = fetch_balance_pair(client, mk["exchange_index"])
                    s["tradeable_cash"], s["tradeable_value"] = tradeable_cash, tradeable_value
                except (KalshiAPIError, KalshiNetworkError) as e:
                    raise Halt(f"could not fetch shard balance: {e}")
            rows = sc.candles(prod, now=now)
            checked_count += 1
            # "Stale" means the feed has stopped ADVANCING, not that the latest
            # 1-minute candle is older than now (1-minute bars always lag now by
            # up to ~2 minutes; that's normal, not an outage).
            latest_ts = rows[-1][0] if rows else None
            feed = s["feed"].setdefault(series, {"last_ts": None, "since": now})
            if latest_ts != feed["last_ts"]:
                feed["last_ts"], feed["since"] = latest_ts, now
            if latest_ts is None or (now - feed["since"]) > cfg["stale_feed_seconds"]:
                stale_count += 1
                log_event(dict(type="stale_feed", series=series, seconds=round(now - feed["since"], 1)))
                continue
            spot = rows[-1][4]
            minute = (now - o) / 60
            if t not in s["basis"]:
                s["basis"][t] = sc.compute_basis(rows, o, K)
            sigma = sc.compute_sigma(rows)
            rem = sc.compute_rem(c, now)
            z = sc.compute_z(spot, s["basis"][t], K, sigma, rem)
            bid = float(mk.get("yes_bid_dollars") or 0)
            ask = float(mk.get("yes_ask_dollars") or 0)
            s["cur"][series] = dict(ticker=t, minute=round(minute, 2), z=round(z, 2), bid=bid, ask=ask, spot=spot, strike=K)
            if not (bid and ask) or sigma <= 0:
                continue
            side = sc.decide_side(z)
            cost = sc.compute_cost(side, bid, ask)
            # The exact string Kalshi quoted, never float-rounded -- some markets
            # use sub-penny ("tapered_deci_cent") ticks where a rounded price gets
            # rejected outright (confirmed live). Used for the actual order price;
            # `cost` (float) still drives thresholds/sizing math.
            price_str = mk.get("yes_ask_dollars") if side == "yes" else mk.get("yes_bid_dollars")
            if not sc.price_ok(cost):
                continue
            for rule in sc.RULES:
                std_fires = sc.rule_fires(minute, z, rule)
                extra_fires = set()
                for xn, xd in SLEEVES.items():
                    if not xd.get("extra") or rule not in xd["rules"]:
                        continue
                    x_on = cfg.get("c_loose_enabled", True) if xn == "C-loose" else cfg.get("a_skip_enabled", True)
                    lo, hi = xd["min_sigma"].get(rule), xd.get("max_sigma", {}).get(rule)
                    if x_on and lo is not None and sc.rule_fires(minute, z, rule, min_sigma=lo) and (hi is None or abs(z) < hi):
                        extra_fires.add(xn)
                if not (std_fires or extra_fires):
                    continue
                # Blocked local hours (3-4 AM / 7-8 AM lost money live): no new bets
                # unless the signal is in the historically loss-free zone.
                if time.localtime(now).tm_hour in cfg.get("blocked_hours", []) and abs(z) < float(cfg.get("blocked_hours_min_sigma", 2.5)):
                    log_event(dict(type="signal_skipped", ticker=t, rule=rule, z=round(z, 2), reason="blocked_hour"))
                    continue
                if orders_this_cycle >= cfg["max_orders_per_cycle"]:
                    log_event(dict(type="signal_skipped", ticker=t, rule=rule, reason="max_orders_per_cycle"))
                    continue
                orders_this_cycle += process_rule_signal(
                    client, s, cfg, series, t, o, c, rule, side, cost, z, live_enabled,
                    tradeable_cash if tradeable_cash is not None else real_cash,
                    tradeable_value if tradeable_value is not None else real_value,
                    mk.get("exchange_index"), price_str, std_fires=std_fires, extra_fires=extra_fires)
        except (KalshiAPIError, KalshiNetworkError) as e:
            s["consecutive_errors"] = s.get("consecutive_errors", 0) + 1
            log_event(dict(type="api_error", series=series, error=str(e)))
            max_errors = cfg.get("max_consecutive_errors", DEFAULT_MAX_CONSECUTIVE_ERRORS)
            if s["consecutive_errors"] >= max_errors:
                raise Halt(f"{max_errors} consecutive API errors, last: {e}")
            continue
        else:
            s["consecutive_errors"] = 0

    if checked_count > 0 and stale_count == checked_count:
        s["stale_strikes"] = s.get("stale_strikes", 0) + 1
    else:
        s["stale_strikes"] = 0
    max_stale = cfg.get("max_stale_strikes", DEFAULT_MAX_STALE_STRIKES)
    if s.get("stale_strikes", 0) >= max_stale:
        raise Halt(f"every price feed stale for {max_stale} consecutive cycles")

    s["basis"] = {k: v for k, v in s["basis"].items() if k in {c["ticker"] for c in s["cur"].values()}}
    status_line(s, real_cash, real_value)


def build_client(args):
    env_setup.ensure_credentials()
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_KEY_PATH")
    demo = args.demo or os.environ.get("KALSHI_DEMO") == "1"
    has_key = bool(key_id and key_path and Path(key_path).exists())
    live_enabled = args.live and os.environ.get("KALSHI_LIVE") == "1" and has_key
    if args.live and not has_key:
        print("[live] --live was passed but no usable API key was found "
              "(KALSHI_KEY_ID / KALSHI_KEY_PATH) -- forcing dry run.", flush=True)
    if args.live and has_key and os.environ.get("KALSHI_LIVE") != "1":
        print("[live] --live was passed but KALSHI_LIVE=1 is not set -- forcing dry run.", flush=True)
    client = KalshiClient(key_id=key_id if has_key else None, key_path=key_path if has_key else None, demo=demo)
    return client, live_enabled, demo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="allow real orders (also requires env KALSHI_LIVE=1)")
    ap.add_argument("--demo", action="store_true", help="use Kalshi's demo environment")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit (for testing)")
    ap.add_argument("--interval", type=float, default=12.0)
    args = ap.parse_args()

    client, live_enabled, demo = build_client(args)
    s = load_state()
    cfg = load_config()
    s["live_mode"], s["demo_mode"] = live_enabled, demo  # persisted so the dashboard reflects real status
    print(f"[live] mode={'LIVE' if live_enabled else 'DRY RUN'} env={'demo' if demo else 'PROD'} "
          f"size_multiplier={cfg['size_multiplier']}", flush=True)
    log_event(dict(type="start", live=live_enabled, demo=demo, size_multiplier=cfg["size_multiplier"]))

    stop_requested = {"flag": False}

    def _sigint(signum, frame):
        stop_requested["flag"] = True
    signal_module.signal(signal_module.SIGINT, _sigint)

    try:
        while True:
            if STOP_PATH.exists():
                shutdown(client, s, live_enabled, "STOP file present")
                return
            if stop_requested["flag"]:
                shutdown(client, s, live_enabled, "Ctrl+C")
                return
            # Written at the top of every loop iteration, before any network call
            # that could block -- an external watchdog checks this file's age to
            # detect a silent stall (no exception raised, just stuck on I/O) that
            # auto_restart_on_halt cannot catch, since that only fires on an
            # actual Halt exception. Confirmed live: the process can block on a
            # network call indefinitely without ever raising, with near-zero CPU
            # use, for 30-90+ minutes at a time.
            try:
                HEARTBEAT_PATH.write_text(str(time.time()))
            except OSError:
                pass
            cfg = load_config()
            now = time.time()
            try:
                run_cycle(client, s, cfg, now, live_enabled)
            except Halt as h:
                msg = str(h)
                tries = s.get("halt_retries", 0) + 1
                s["halt_retries"] = tries
                if (cfg.get("auto_restart_on_halt") and tries <= cfg.get("max_halt_retries", 5)
                        and "401" not in msg and "Unauthorized" not in msg):
                    log_event(dict(type="halt_retry", reason=msg, attempt=tries))
                    print(f"[live] halt condition, retrying ({tries}/{cfg.get('max_halt_retries', 5)}): {msg}", flush=True)
                    save_state(s)
                    time.sleep(cfg.get("halt_retry_seconds", 15))
                    continue
                shutdown(client, s, live_enabled, msg)
                return
            s["halt_retries"] = 0
            save_state(s)
            if args.once:
                return
            time.sleep(cfg.get("cycle_interval_seconds", args.interval))
    except KeyboardInterrupt:
        shutdown(client, s, live_enabled, "Ctrl+C")


if __name__ == "__main__":
    main()
