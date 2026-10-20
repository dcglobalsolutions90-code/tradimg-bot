"""Piggy Bank: late-bet near-certain strategy that runs next to kalshi_live.py.

Fires ~3:00 before each 15-min window closes, skips windows where sleeves A-F already bet,
buys the strongest legs priced >= 95c (>= 2 legs -> combo RFQ, exactly 1 -> single order),
and stops itself at the first loss. Places REAL orders: refuses to start unless KALSHI_LIVE=1.
"""
import sys, json, time, datetime, uuid, os
from pathlib import Path
D = Path(__file__).parent
sys.path.insert(0, str(D))
import env_setup
env_setup.ensure_credentials()
from kalshi_client import KalshiClient
import combo_new
import piggy_sizing
if os.environ.get("KALSHI_LIVE") != "1":
    sys.exit("piggy.py places real orders. Set KALSHI_LIVE=1 to run it (and make sure you want that).")
c = KalshiClient(key_id=os.environ["KALSHI_KEY_ID"], key_path=os.environ["KALSHI_KEY_PATH"], demo=os.environ.get("KALSHI_DEMO") == "1")
cc = dict(json.loads((D / "config.json").read_text())["combo_new"])
cc.update(stake_dollars=5.0, mode="live", quote_wait_seconds=15, max_price=0.99, price_above_fair=0.50, price_below_fair=0.20)
SER = ["KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M","KXXRP15M","KXBNB15M","KXHYPE15M","KXNEAR15M","KXZEC15M"]
def ts(x): return int(datetime.datetime.fromisoformat(x["close_time"].replace("Z","+00:00")).timestamp())
def mk():
    out=[]
    for s in SER:
        ms=[x for x in c.get_markets(s,status="open",limit=5)["markets"] if ts(x)>time.time()+20]
        if ms: out.append(min(ms,key=ts))
    return out
FRAC=float(os.environ.get("PIGGY_COMBO_FRAC", "0"))  # 0 = use piggy_sizing.stake(); e.g. 0.8 = 80% of combo-shard cash
def shard_cash(ix):
    b=c.get_balance(exchange_index=ix)
    return float(b.get("balance_dollars") or (b.get("balance",0)/100.0))
def P(*a): print(time.strftime("%H:%M:%S"),*a,flush=True)
PENDING=[]
def check_pending():
    live=open(D/"live_log.jsonl",encoding="utf-8",errors="ignore").read()
    for p in list(PENDING):
        if time.time()<p["close"]+120: continue
        if p["kind"]=="combo":
            import re
            for l in live.splitlines()[-3000:]:
                if '"combo_settled"' in l and p["key"] in l:
                    e=json.loads(l); P(f"RESULT {p['key']} win={e['win']} pnl={e['pnl']}"); PENDING.remove(p)
                    if not e["win"]: P("LOSS - STOPPING per instruction"); sys.exit(0)
                    break
        else:
            r=c.get_market(p["ticker"])["market"].get("result")
            if r in ("yes","no"):
                win=(r==p["side"]); P(f"RESULT single {p['ticker']} {p['side']} win={win}"); PENDING.remove(p)
                piggy_sizing.record_single(p["close"],p["ticker"],p["side"],win,(p.get("n",0)*(1-p.get("price",0)) if win else -p.get("n",0)*p.get("price",0)),p.get("stake"),p.get("price"))
                if not win: P("LOSS - STOPPING per instruction"); sys.exit(0)
def one_cycle(n):
    check_pending()
    ms=mk(); close_ts=ts(ms[0]); target=close_ts-180
    while target<time.time()+3: target+=900; close_ts+=900
    P(f"CYCLE {n} armed: fires {time.strftime('%H:%M:%S',time.localtime(target))} for window closing {time.strftime('%H:%M',time.localtime(close_ts))}")
    while time.time()<target: time.sleep(1)
    ms=mk(); close_ts=ts(ms[0]); sfx=ms[0]["ticker"].split("-")[1]
    stop_at=close_ts-60; tries=0; last_note=0
    def af_bet():
        return [x for x in open(D/"live_log.jsonl",encoding="utf-8",errors="ignore").readlines()[-600:] if '"order_placed"' in x and sfx in x]
    while True:
        tries+=1
        b=af_bet()
        if b: P(f"CYCLE {n} SKIP: A-F bet this window ({len(b)} orders)"); return
        ms=mk()
        if not ms or ts(ms[0])!=close_ts: P(f"CYCLE {n} window ended"); return
        cands=[]
        for m in ms:
            ya,na=float(m["yes_ask_dollars"]),float(m["no_ask_dollars"])
            side,p=("yes",ya) if ya>=na else ("no",na)
            cands.append((p,m["ticker"],side))
        cands.sort(reverse=True)
        legs=[dict(ticker=t,side=sd,price=p) for p,t,sd in cands[:4] if p>=0.95]
        left=close_ts-time.time()
        wait=(len(legs)==0) or (len(legs)==1 and left>195) or (len(legs)==1 and legs[0]["price"]>0.99)
        if wait:
            if left<90: P(f"CYCLE {n} SKIP: never reached a bet (best {cands[0][0]:.3f}, {len(legs)} legs >=95c)"); return
            if time.time()-last_note>=60: P(f"CYCLE {n} monitoring: best {cands[0][0]:.3f}, {len(legs)} legs >=95c, {int(left)}s left"); last_note=time.time()
            time.sleep(2); continue
        if len(legs)==1:
            l=legs[0]; m=next(x for x in ms if x["ticker"]==l["ticker"])
            cap=min(0.99,l["price"]+0.01); stake,info=piggy_sizing.stake(l["price"],shard_cash(m.get("exchange_index"))); cnt=max(1,int(stake//l["price"])); P(f"  single stake ${stake} {info}")
            px=cap if l["side"]=="yes" else 1-cap
            bs="bid" if l["side"]=="yes" else "ask"
            try:
                r=c.create_order(l["ticker"],bs,cnt,f"{px:.4f}",str(uuid.uuid4()),exchange_index=m.get("exchange_index"))
                o=r.get("order",r); oid=o.get("order_id") or o.get("id")
                time.sleep(4); o2=c.get_order(oid); o2=o2.get("order",o2)
                P(f"CYCLE {n} SINGLE {l['ticker']} {l['side']} n={cnt} status={o2.get('status')} fill={o2.get('fill_count_fp')} cost={o2.get('taker_fill_cost_dollars')}")
                if float(o2.get("fill_count_fp") or 0)>0: PENDING.append(dict(kind="single",ticker=l["ticker"],side=l["side"],close=close_ts,n=float(o2.get("fill_count_fp") or 0),price=l["price"],stake=stake))
                if o2.get("status") not in ("executed","filled"):
                    try: c.cancel_order(oid,market_ticker=l["ticker"],exchange_index=m.get("exchange_index")); P("  cancelled unfilled remainder")
                    except Exception as e: P("  cancel err",str(e)[:120])
            except Exception as e: P(f"CYCLE {n} SINGLE ERROR {str(e)[:200]}")
            return
        names=[(l["ticker"].split("-")[0][2:-3],l["side"],l["price"]) for l in legs]
        evs=[]
        def lg(e): evs.append(e); P("  EVENT",json.dumps(e,default=str)[:260])
        try:
            sc=shard_cash(int(cc.get("exchange_index",1)))
            q=min(0.99,__import__("math").prod(l["price"] for l in legs)+0.016*len(legs))
            cc["stake_dollars"],info=(round(FRAC*sc,2),dict(rule=f"{FRAC:.0%}_of_shard_cash")) if FRAC>0 else piggy_sizing.stake(q,max(0.0,sc-52))
            P(f"  sizing {info}")
            P(f"  combo stake ${cc['stake_dollars']} (shard cash ${sc:.2f}, $52 kept free for the bot's own combo)")
            r=combo_new._execute(c,cc,f"{close_ts}|manual5m{n}",legs,lg)
        except Exception as e:
            P(f"CYCLE {n} ERROR {str(e)[:200]}"); return
        if any(e.get("type")=="combo_filled" for e in evs): P(f"CYCLE {n} FILLED legs={names}"); PENDING.append(dict(kind="combo",key=f"{close_ts}|manual5m{n}",close=close_ts)); return
        if r!="retry": P(f"CYCLE {n} DONE no fill ({[e.get('type') for e in evs]})"); return
        if time.time()+25>stop_at: P(f"CYCLE {n} GAVE UP after {tries} tries, no fill"); return
        time.sleep(5)
n=0
while True:
    n+=1; one_cycle(n)
