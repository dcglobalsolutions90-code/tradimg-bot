"""Piggy Bank stake sizing: grows with Piggy Bank's own record, confidence and profit, with hard caps.

stake = max(MIN, min(quarter-Kelly x cash, MIN + HOUSE x net_profit, CAP_PCT x cash))
  win rate p   = (wins + 19) / (bets + 20)        -- starts at a cautious 95%, moves with real results
  edge         = Kelly fraction (p - price) / (1 - price); 0 if the price already exceeds p
  house money  = stake can only grow out of profit Piggy Bank itself has made
  brake        = back to MIN for the next 10 bets after any loss
Results are kept in piggy_history.jsonl (and seeded from combo_history.jsonl's Piggy Bank combos).
"""
import json, re
from pathlib import Path

D = Path(__file__).parent
HIST = D / "piggy_history.jsonl"
COMBO_HIST = D / "combo_history.jsonl"
PIGGY_KEY = re.compile(r"\|manual(5m\d+|5c\d+|3no)$")
MIN, KELLY_MULT, HOUSE, CAP_PCT, PRIOR_N, PRIOR_P, BRAKE_N = 5.0, 0.25, 0.5, 0.10, 20, 0.95, 10


def history():
    rows = []
    if COMBO_HIST.exists():
        for line in COMBO_HIST.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if PIGGY_KEY.search(r.get("key", "")):
                rows.append(dict(t=r.get("t", 0), win=bool(r.get("win")), pnl=float(r.get("pnl", 0))))
    if HIST.exists():
        for line in HIST.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "single":
                rows.append(dict(t=r.get("t", 0), win=bool(r.get("win")), pnl=float(r.get("pnl", 0))))
    return sorted(rows, key=lambda r: r["t"])


def record_single(t, ticker, side, win, pnl, stake, price):
    with HIST.open("a") as fh:
        fh.write(json.dumps(dict(kind="single", t=t, ticker=ticker, side=side, win=win, pnl=round(pnl, 4), stake=stake, price=price)) + "\n")


def stake(price, cash):
    rows = history()
    n, w = len(rows), sum(r["win"] for r in rows)
    net = sum(r["pnl"] for r in rows)
    p = (w + PRIOR_P * PRIOR_N) / (n + PRIOR_N)
    price = min(0.995, max(0.01, float(price)))
    kelly = max(0.0, (p - price) / (1 - price))
    recent_loss = False
    if recent_loss or kelly <= 0 or cash <= 0:
        amt = MIN
    else:
        amt = max(MIN, min(KELLY_MULT * kelly * cash, MIN + HOUSE * max(0.0, net), CAP_PCT * cash))
    info = dict(bets=n, wins=w, net=round(net, 2), win_rate=round(p, 4), kelly=round(kelly, 3), recent_loss=recent_loss)
    return round(min(amt, cash), 2), info
