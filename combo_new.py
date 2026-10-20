"""combo-new: after each rule's 30-second decision window, take the legs A-F actually hold for that
window and, if there are at least min_legs, buy ONE combo (Kalshi multivariate market) of all of them
for a flat stake via Kalshi's RFQ flow. Runs on a side thread so A-F are never delayed.

RFQ mechanics used: makers quote yes_bid / no_bid (the prices THEY would pay). To end up holding YES
of the combo the requester takes the maker's NO bid: accepted_side="no", cost per contract = 1 - no_bid.
Two guards protect against a misread of that convention: the implied YES price must sit near the
product of the legs' own prices, and after acceptance the account must show a POSITIVE (YES) position
on the combo ticker, or combo-new switches itself off.
"""
import json, math, threading, time
from pathlib import Path

D = Path(__file__).parent
STATE = D / "combo_state.json"
HIST = D / "combo_history.jsonl"
_lock = threading.Lock()
_busy = set()


def _load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return dict(done=[], open={}, disabled=None)


def _save(st):
    STATE.write_text(json.dumps(st, default=str))


def _event_ticker(market_ticker):
    return market_ticker.rsplit("-", 1)[0]


PAPER_STATE = D / "state_kalshi_multi.json"
CONFIG = D / "config.json"


def _paper_groups():
    groups = {}
    try:
        ps = json.loads(PAPER_STATE.read_text())
    except (OSError, ValueError):
        return groups
    for ac in ps["accounts"].values():
        for p in ac["pos"]:
            groups.setdefault(f"{int(p['close'])}|{p['rule']}", {})[p["ticker"]] = dict(ticker=p["ticker"], side=p["side"], price=p["cost"])
    return groups


def _promote_to_live(log_event, why):
    c = json.loads(CONFIG.read_text())
    c["combo_new"]["mode"] = "live"
    CONFIG.write_text(json.dumps(c, indent=2))
    log_event(dict(type="combo_promoted", reason=why))


def tick(client, s, cfg, now, live_enabled, log_event, std_sleeves):
    cc = cfg.get("combo_new") or {}
    if not cc.get("enabled") or not live_enabled:
        return
    with _lock:
        st = _load()
        _settle(client, st, now, log_event, cc)
        if st.get("disabled"):
            _save(st)
            return
        if cc.get("mode") == "paper_test":
            if any(c.get("paper") for c in st["open"].values()) or _busy:
                _save(st)
                return  # one paper test at a time
            groups = _paper_groups()
        else:
            groups = {}
        for pos in (s["positions"].values() if cc.get("mode") != "paper_test" else ()):
            if not any(n in std_sleeves and c > 0 for n, c in pos["sleeve_contracts"].items()):
                continue
            key = f"{int(pos['close'])}|{pos['rule']}"
            groups.setdefault(key, {})[pos["ticker"]] = dict(
                ticker=pos["ticker"], side=pos["side"],
                price=sum(pos["sleeve_cost"].get(n, 0) for n in std_sleeves) / max(1e-9, sum(c for n, c in pos["sleeve_contracts"].items() if n in std_sleeves)))
        for key, legs in groups.items():
            close, rule = key.split("|")
            window_end = int(close) - 900 + (int(rule) + 0.5) * 60
            if key in st["done"] or key in _busy or now < window_end + 2 or now > int(close) - 60:
                continue
            st["done"].append(key)
            st["done"] = st["done"][-300:]
            if len(legs) < int(cc.get("min_legs", 2)):
                continue
            _busy.add(key)
            threading.Thread(target=_run, args=(client, cc, key, list(legs.values()), log_event), daemon=True).start()
        _save(st)


def _run(client, cc, key, legs, log_event):
    seen = set()
    thr = cc.get("confident_min_leg_price")
    if thr and all(l["price"] >= float(thr) for l in legs):
        try:
            b = client.get_balance(exchange_index=int(cc.get("exchange_index", 1)))
            cash = float(b.get("balance_dollars") or (b.get("balance", 0) / 100.0))
            big = round(cash * float(cc.get("confident_stake_pct", 0.30)), 2)
            if big > float(cc.get("stake_dollars", 5)):
                cc = dict(cc, stake_dollars=big)
                log_event(dict(type="combo_confident_stake", key=key, stake=big, shard_cash=cash, min_leg=round(min(l["price"] for l in legs), 4)))
        except Exception as e:
            log_event(dict(type="combo_error", key=key, error="confident sizing: " + str(e)[:200]))

    def quiet_log(e):
        t = e.get("type")
        if t in ("combo_shard_balance", "combo_skipped", "combo_no_quote", "combo_unfilled", "combo_accept_failed"):
            if t in seen:
                return
            seen.add(t)
        log_event(e)

    try:
        stop_at = int(key.split("|")[0]) - 60
        attempts = 0
        while True:
            attempts += 1
            if _execute(client, cc, key, legs, quiet_log) != "retry":
                break
            wait = float(cc.get("quote_wait_seconds", 15))
            if time.time() + wait + float(cc.get("retry_pause_seconds", 5)) > stop_at:
                log_event(dict(type="combo_gave_up", key=key, attempts=attempts))
                break
            time.sleep(float(cc.get("retry_pause_seconds", 5)))
    except Exception as e:  # never let combo-new take the main bot down
        log_event(dict(type="combo_error", key=key, error=str(e)[:300]))
    finally:
        _busy.discard(key)


def _execute(client, cc, key, legs, log_event):
    stake = float(cc.get("stake_dollars", 50))
    coll = cc.get("collection", "KXMVECROSSCATEGORY-R")
    fair = math.prod(l["price"] for l in legs)
    names = [l["ticker"].split("-")[0][2:-3] + " " + l["side"].upper() for l in legs]
    paper = cc.get("mode") == "paper_test"
    bal = client.get_balance(exchange_index=int(cc.get("exchange_index", 1)))
    shard_cash = float(bal.get("balance_dollars") or (bal.get("balance", 0) / 100.0))
    log_event(dict(type="combo_shard_balance", key=key, shard_cash=shard_cash, legs=names))
    if shard_cash < stake + 1 and not paper:
        log_event(dict(type="combo_skipped", key=key, legs=names, reason="no_funds_on_combo_shard", shard_cash=shard_cash, fair=round(fair, 4)))
        return
    mk = client._request("POST", f"/multivariate_event_collections/{coll}", body=dict(
        selected_markets=[dict(market_ticker=l["ticker"], event_ticker=_event_ticker(l["ticker"]), side=l["side"]) for l in legs]))
    combo = mk["market_ticker"]
    held = sum(float(p.get("position_fp", 0) or 0) for p in client.get_positions(ticker=combo).get("market_positions", []) if p.get("ticker") == combo)
    if held and not paper:
        with _lock:
            st = _load()
            if combo not in st["open"]:
                fills = client.get_fills(ticker=combo).get("fills", [])
                paid = sum(float(f.get("count_fp", 0) or 0) * float(f.get("yes_price_dollars", 0) or 0) for f in fills)
                fee = sum(float(f.get("fee_cost", 0) or 0) for f in fills)
                st["open"][combo] = dict(key=key, legs=names, combo=combo, fair=round(fair, 4), stake=stake, n=held,
                                         paid=round(paid, 4), fee=round(fee, 4), close=int(key.split("|")[0]))
                _save(st)
                log_event(dict(type="combo_filled", late=True, n=held, paid=round(paid, 4), fee=round(fee, 4), key=key, legs=names, combo=combo))
        return
    rfq = client._request("POST", "/communications/rfqs", body=dict(
        market_ticker=combo, target_cost_dollars=f"{stake:.2f}", rest_remainder=False))
    rfq_id = rfq.get("id") or rfq.get("rfq_id")
    uid = client._request("GET", f"/communications/rfqs/{rfq_id}").get("rfq", {}).get("creator_user_id")
    best, deadline = None, time.time() + float(cc.get("quote_wait_seconds", 15))
    while time.time() < deadline:
        time.sleep(1.0)
        qs = client._request("GET", "/communications/quotes", params=dict(rfq_id=rfq_id, rfq_creator_user_id=uid)).get("quotes", [])
        for q in qs:
            if q.get("status") not in (None, "open", "active"):
                continue
            nb = float(q.get("no_bid_dollars") or 0)
            if nb <= 0:
                continue
            yes_cost = 1.0 - nb
            if best is None or yes_cost < best[0]:
                best = (yes_cost, q)
        if best:
            break
    base = dict(key=key, legs=names, combo=combo, rfq_id=rfq_id, fair=round(fair, 4), stake=stake)
    if not best:
        log_event(dict(type="combo_no_quote", **base))
        _delete_rfq(client, rfq_id)
        return "retry"
    yes_cost, q = best
    base.update(quote_id=q.get("quote_id") or q.get("id"), yes_cost=round(yes_cost, 4),
                yes_bid=q.get("yes_bid_dollars"), no_bid=q.get("no_bid_dollars"))
    lo, hi = fair - float(cc.get("price_below_fair", 0.05)), min(float(cc.get("max_price", 0.95)), fair + float(cc.get("price_above_fair", 0.15)))
    if not (lo <= yes_cost <= hi):
        log_event(dict(type="combo_skipped", reason="quote_outside_band", band=[round(lo, 4), round(hi, 4)], **base))
        _delete_rfq(client, rfq_id)
        return "retry"
    if paper:
        _delete_rfq(client, rfq_id)
        n = stake / yes_cost
        with _lock:
            st = _load()
            st["open"][combo] = dict(base, n=round(n, 2), paid=stake, fee=0.0, close=int(key.split("|")[0]), paper=True)
            _save(st)
        log_event(dict(type="combo_paper_open", n=round(n, 2), **base))
        return
    if cc.get("mode", "live") != "live":
        log_event(dict(type="combo_quote_only", **base))
        _delete_rfq(client, rfq_id)
        return
    try:
        client._request("PUT", f"/communications/rfqs/{rfq_id}/quotes/{base['quote_id']}/accept", body=dict(accepted_side="no"))
    except Exception as e:
        log_event(dict(type="combo_accept_failed", error=str(e)[:200], **base))
        _delete_rfq(client, rfq_id)
        return "retry"
    log_event(dict(type="combo_accepted", **base))
    pos_n = 0.0
    for _ in range(15):
        time.sleep(2.0)
        mp = client.get_positions(ticker=combo).get("market_positions", [])
        pos_n = sum(float(p.get("position_fp", 0) or 0) for p in mp if p.get("ticker") == combo)
        if pos_n:
            break
    with _lock:
        st = _load()
        if pos_n < 0:
            st["disabled"] = f"accepted quote left a NO position ({pos_n}) on {combo}; side convention misread"
            log_event(dict(type="combo_disabled", reason=st["disabled"], **base))
        if pos_n:
            fills = client.get_fills(ticker=combo).get("fills", [])
            paid = sum(float(f.get("count_fp", 0) or 0) * float(f.get("yes_price_dollars" if pos_n > 0 else "no_price_dollars", 0) or 0) for f in fills)
            fee = sum(float(f.get("fee_cost", 0) or 0) for f in fills)
            st["open"][combo] = dict(base, n=pos_n, paid=round(paid, 4), fee=round(fee, 4), close=int(key.split("|")[0]))
            log_event(dict(type="combo_filled", n=pos_n, paid=round(paid, 4), fee=round(fee, 4), **base))
        else:
            log_event(dict(type="combo_unfilled", **base))
        _save(st)
    if not pos_n:
        _delete_rfq(client, rfq_id)
        return "retry"


def _delete_rfq(client, rfq_id):
    try:
        client._request("DELETE", f"/communications/rfqs/{rfq_id}")
    except Exception:
        pass


def _settle(client, st, now, log_event, cc):
    for combo, c in list(st["open"].items()):
        if now < c["close"] + 90:
            continue
        try:
            m = client.get_market(combo)["market"]
        except Exception:
            continue
        if m.get("result") not in ("yes", "no"):
            continue
        n = abs(c["n"])
        held_yes = c["n"] > 0
        win = (m["result"] == "yes") == held_yes
        pnl = (n - c["paid"] if win else -c["paid"]) - c["fee"]
        rec = dict(c, result=m["result"], win=win, pnl=round(pnl, 4), t=c["close"])
        with HIST.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        log_event(dict(type="combo_paper_settled" if c.get("paper") else "combo_settled", key=c["key"], legs=c["legs"], win=win, pnl=round(pnl, 4)))
        del st["open"][combo]
        if c.get("paper") and cc.get("mode") == "paper_test" and cc.get("promote_after_paper_pass", True):
            _promote_to_live(log_event, f"paper test passed: quote received at {c['yes_cost']} (fair {c['fair']}), combo {combo} settled {m['result']}")
