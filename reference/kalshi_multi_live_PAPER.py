"""kalshi_multi_live: PAPER-trading bot for the 15-minute crypto 'price up?' markets on Kalshi/Robinhood, all coins at once, one shared fake bankroll.
Rule (price vs strike only, NEVER the market's odds): at minute 4 of each market, if the coin is at least 1.5 sigma above/below the strike, buy the side it is already on.
Costs follow Robinhood: flat $0.02 per contract, no trades at 97c+/3c-. Prices from Kalshi's public feed, coin prices from Coinbase. No account, no keys, no real orders.
Settings: kalshi_multi_params.json (re-read each cycle).
"""
import json, math, time, datetime, urllib.request
from pathlib import Path

D = Path(__file__).parent
STATE, PARAMS, HIST = D / "state_kalshi_multi.json", D / "kalshi_multi_params.json", D / "kalshi_multi_history.jsonl"
BASE = "https://api.elections.kalshi.com/trade-api/v2"; UA = {"User-Agent": "Mozilla/5.0"}
SERIES = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD", "KXDOGE15M": "DOGE-USD", "KXXRP15M": "XRP-USD", "KXBNB15M": "BNB-USD", "KXHYPE15M": "HYPE-USD", "KXNEAR15M": "NEAR-USD", "KXZEC15M": "ZEC-USD"}
R3 = [(4, 1.5), (11, 2.0)]                              # the minute-3 rule (1.25 sigma) was dropped: -$198 over 24 bets                  # (decision minute, minimum sigma from strike)
ACCOUNTS = {                                            # each account has its OWN $1,000 fake bankroll
    "A  Min 4, 1.5 sigma, $40/bet": dict(rules=[(4, 1.5)], stake=40.0),
    "B  Min 4, 1.5 sigma, $100/bet": dict(rules=[(4, 1.5)], stake=100.0),
    "C  Min 11, 2 sigma, $100/bet": dict(rules=[(11, 2.0)], stake=100.0),
    "D  2 rules (min 4 + min 11), $40/bet": dict(rules=R3, stake=40.0),
    "E  2 rules (min 4 + min 11), $40/bet": dict(rules=R3, stake=40.0),
    "F  2 rules (min 4 + min 11), $100/bet": dict(rules=R3, stake=100.0),
}
DEFAULT = dict(bankroll=1000.0, fee=0.02, lo=0.03, hi=0.97, open_cap=0.5, enabled={})
Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20))
iso = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def params():
    p = dict(DEFAULT)
    try: p.update(json.loads(PARAMS.read_text()))
    except Exception: PARAMS.write_text(json.dumps(DEFAULT, indent=1))
    return p


def log(s, m): s["log"].append(f"{time.strftime('%m-%d %H:%M:%S')} {m}"); s["log"] = s["log"][-200:]


def candles(prod, n=70):
    end = int(time.time()) // 60 * 60 + 60; st = end - n * 60; f = lambda x: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(x))
    return sorted(get(f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=60&start={f(st)}&end={f(end)}"))


def settle(s, P):
    for an, ac in s["accounts"].items():
        for p in list(ac["pos"]):
            if time.time() < p["close"] + 90: continue
            try: m = get(f"{BASE}/markets/{p['ticker']}")["market"]
            except Exception: continue
            if m.get("result") not in ("yes", "no"): continue
            win = (m["result"] == "yes") == (p["side"] == "yes"); pnl = (p["n"] * (1 - p["cost"]) if win else -p["n"] * p["cost"]) - p["fee"]
            ac["cash"] += p["n"] * (1.0 if win else 0.0)
            rec = dict(account=an, series=p["series"], ticker=p["ticker"], side=p["side"], n=p["n"], cost=p["cost"], fee=p["fee"], pnl=pnl, win=win, res=m["result"], z=p["z"], rule=p.get("rule"), t=p["close"], run=s["run"])
            ac["closed"].append(rec); ac["pos"].remove(p)
            with HIST.open("a") as fh: fh.write(json.dumps(rec) + chr(10))
            log(s, f"[{an[:1]}][{p['series'][2:-3]}] SETTLED {p['side'].upper()} x{p['n']} -> {m['result'].upper()} {'WIN' if win else 'LOSS'} ${pnl:+.2f}")


def step(s, P):
    settle(s, P); now = time.time(); cur = {}
    for series, prod in SERIES.items():
        if P["enabled"].get(series, True) is False: continue
        try:
            mk = [m for m in get(f"{BASE}/markets?series_ticker={series}&status=open&limit=10").get("markets", []) if iso(m["open_time"]) <= now < iso(m["close_time"])]
            if not mk: continue
            m = min(mk, key=lambda x: iso(x["close_time"])); o, c = iso(m["open_time"]), iso(m["close_time"]); t = m["ticker"]
            if m.get("floor_strike") is None: continue                          # strike not published yet in the first seconds of a market
            K = float(m["floor_strike"])
            rows = candles(prod); spot = rows[-1][4]; minute = (now - o) / 60
            if t not in s["basis"]:
                r0 = next((r for r in rows if r[0] == int(o) - 60), None); s["basis"][t] = (K - (r0[3] + r0[4]) / 2) if r0 else 0.0
            ch = [rows[i][4] - rows[i - 1][4] for i in range(max(1, len(rows) - 60), len(rows))]; mu = sum(ch) / len(ch); sig = (sum((x - mu) ** 2 for x in ch) / len(ch)) ** 0.5
            bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
            rem = max((c - now) / 60 - 0.5, 0.25); z = (spot + s["basis"][t] - K) / max(sig * math.sqrt(rem), 1e-12)
            cur[series] = dict(ticker=t, minute=round(minute, 2), z=round(z, 2), bid=bid, ask=ask, spot=spot, strike=K)
            if not (bid and ask) or sig <= 0: continue
            side = "yes" if z > 0 else "no"; cost = ask if side == "yes" else 1 - bid
            if not (P["lo"] <= cost <= P["hi"]): continue
            for an, ac in s["accounts"].items():
                for (m_, zt) in ACCOUNTS[an]["rules"]:
                    key = f"{t}|{m_}"
                    if not (m_ <= minute < m_ + 0.5) or abs(z) < zt or key in ac["seen"] or any(p["ticker"] == t and p.get("rule") == m_ for p in ac["pos"]): continue
                    ac["seen"].append(key); ac["seen"] = ac["seen"][-200:]
                    openc = sum(p["n"] * p["cost"] for p in ac["pos"]); eq = ac["cash"] + openc
                    stake = min(ACCOUNTS[an]["stake"], max(0.0, P["open_cap"] * eq - openc))
                    n = int(min(stake // cost, ac["cash"] // (cost + P["fee"])))
                    if n < 1: log(s, f"[{an[:1]}][{series[2:-3]}] signal skipped: open-bet cap or cash"); continue
                    fe = P["fee"] * n; ac["cash"] -= n * cost + fe
                    ac["pos"].append(dict(series=series, ticker=t, side=side, n=n, cost=cost, fee=fe, close=c, z=round(z, 2), rule=m_))
                    log(s, f"[{an[:1]}][{series[2:-3]}] BUY {side.upper()} x{n} @ {cost:.2f} (min {minute:.1f}, {z:+.2f} sigma, ${n * cost:.0f})")
        except Exception as e: log(s, f"[{series}] error: {str(e)[:80]}")
    s["cur"] = cur; s["upd"] = int(now); s["basis"] = {k: v for k, v in s["basis"].items() if k in {c['ticker'] for c in cur.values()}}


def fresh(P, run):
    return dict(accounts={an: dict(cash=P["bankroll"], start=P["bankroll"], pos=[], closed=[], seen=[], rules=ACCOUNTS[an]["rules"], stake=ACCOUNTS[an]["stake"]) for an in ACCOUNTS},
                log=[f"fresh start: run {run}, six accounts with $1,000 each"], cur={}, basis={}, upd=0, run=run)


def main():
    P = params()
    s = json.loads(STATE.read_text()) if STATE.exists() else None
    if not s or "accounts" not in s: s = fresh(P, 2)
    for an in ACCOUNTS: s["accounts"].setdefault(an, dict(cash=P["bankroll"], start=P["bankroll"], pos=[], closed=[], seen=[], rules=ACCOUNTS[an]["rules"], stake=ACCOUNTS[an]["stake"]))
    while True:
        P = params()
        try: step(s, P)
        except Exception as e: log(s, f"cycle error: {str(e)[:100]}")
        STATE.write_text(json.dumps(s))
        print("[multi] " + " | ".join(f"{an[:1]} ${ac['cash'] + sum(p['n'] * p['cost'] for p in ac['pos']):,.0f}" for an, ac in s["accounts"].items()), flush=True)
        time.sleep(12)


if __name__ == "__main__":
    main()
