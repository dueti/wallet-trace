"""Fallback for chains without a free API (BSC): parse a DeBank history page dump into trades.json.

How to make the dump (Claude Browser pane or any browser):
  1. open https://debank.com/profile/<addr>/history?chain=bsc
  2. click "Load More" until far enough back (JS snippet in SKILL.md)
  3. save document.querySelector('main').innerText to a file
Then: python3 parse_debank.py dump.txt --address <addr> --chain bsc --tz 8 --now "2026-09-24 15:00" > trades.json
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from common import make_trade, make_transfer

QUOTE = {"USDT", "USDC", "BUSD", "DAI", "BNB", "WBNB", "ETH", "WETH", "USD1", "FDUSD"}
NATIVE = {"BNB", "WBNB", "ETH", "WETH"}


def parse(text, address, chain, tz, now):
    lines = [l.strip() for l in text.split("\n")]
    try:
        i = next(k for k, l in enumerate(lines) if l.startswith("Filter by token")) + 1
    except StopIteration:
        i = 0
    ts_re = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}|.*\bago)$")
    tzinfo = timezone(timedelta(hours=tz))

    def to_ts(s):
        if s.endswith("ago"):
            h = re.search(r"(\d+)\s*hrs?", s)
            m = re.search(r"(\d+)\s*mins?", s)
            d = re.search(r"(\d+)\s*days?", s)
            return int((now - timedelta(days=int(d.group(1)) if d else 0, hours=int(h.group(1)) if h else 0, minutes=int(m.group(1)) if m else 0)).timestamp())
        return int(datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=tzinfo).timestamp())

    recs, cur = [], None
    while i < len(lines):
        l = lines[i]
        if ts_re.match(l) and i + 2 < len(lines):
            cur = {"ts": to_ts(l), "hash": lines[i + 1], "action": lines[i + 2], "legs": [], "cp": None}
            recs.append(cur)
            i += 3
            continue
        if cur is None or l in ("Load More", "History", ""):
            i += 1
            continue
        if l == "Gas Fee":
            i += 2
            continue
        m = re.match(r"^([+-])([\d,\.]+)$", l)
        if m:
            try:
                amt = float(m.group(2).replace(",", ""))
            except ValueError:
                amt = 0.0
            tok = lines[i + 1] if i + 1 < len(lines) else "?"
            val = lines[i + 2] if i + 2 < len(lines) and lines[i + 2].startswith("(") else ""
            mm = re.search(r"\$([\d,\.]+)", val)
            usd = float(mm.group(1).replace(",", "")) if mm else 0.0
            cur["legs"].append((m.group(1), amt, tok, usd))
            i += 3 if val else 2
            continue
        if cur["cp"] is None and not cur["legs"]:
            cur["cp"] = l
        i += 1

    trades, transfers = [], []
    for r in recs:
        outs = [l for l in r["legs"] if l[0] == "-"]
        ins = [l for l in r["legs"] if l[0] == "+"]
        act = r["action"]
        if act.startswith("Approve"):
            continue
        if act in ("Send", "Receive") or act.startswith(("Deposit to", "Withdraw from")):
            for l in r["legs"]:
                kind = "cex" if act.startswith(("Deposit", "Withdraw")) else act.lower()
                transfers.append(make_transfer(r["ts"], r["hash"], "out" if l[0] == "-" else "in", l[2], l[1], l[3] if l[2] in QUOTE else 0.0, r["cp"] or "", kind))
            continue
        if not outs and ins:
            for l in ins:  # airdrop spam labelled Swap/airdrop/batchTransfer...
                transfers.append(make_transfer(r["ts"], r["hash"], "in", l[2], l[1], l[3] if l[2] in QUOTE else 0.0, r["cp"] or "", "receive"))
            continue
        if not outs or not ins:
            continue
        out_q = [l for l in outs if l[2] in QUOTE]
        in_q = [l for l in ins if l[2] in QUOTE]
        out_t = [l for l in outs if l[2] not in QUOTE]
        in_t = [l for l in ins if l[2] not in QUOTE]
        if out_q and in_t:
            tgt = max(in_t, key=lambda l: l[3])
            trades.append(make_trade(r["ts"], r["hash"], "BUY", tgt[2], tgt[2], tgt[1], out_q[0][2], sum(l[1] for l in out_q), sum(l[3] for l in out_q)))
        elif in_q and out_t:
            src = max(out_t, key=lambda l: l[3])
            trades.append(make_trade(r["ts"], r["hash"], "SELL", src[2], src[2], src[1], in_q[0][2], sum(l[1] for l in in_q), sum(l[3] for l in in_q)))
        elif out_t and in_t:
            trades.append(make_trade(r["ts"], r["hash"], "SWAP", f"{out_t[0][2]}->{in_t[0][2]}", out_t[0][2], out_t[0][1], in_t[0][2], in_t[0][1], max(out_t[0][3], in_t[0][3]), "token-token"))
        elif out_q and in_q:
            trades.append(make_trade(r["ts"], r["hash"], "QUOTE_SWAP", f"{out_q[0][2]}->{in_q[0][2]}", out_q[0][2], out_q[0][1], in_q[0][2], in_q[0][1], out_q[0][3]))
    return {"chain": chain, "address": address.lower(), "source": "debank-dump", "fetched_at": int(now.timestamp()),
            "native": "BNB" if chain == "bsc" else "ETH", "trades": sorted(trades, key=lambda t: t["ts"]),
            "transfers": sorted(transfers, key=lambda t: t["ts"]), "raw_counts": {"records": len(recs)},
            "note": "DeBank token symbols are used as token_addr (no contract address in the page dump); same-symbol tokens may be merged."}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--address", required=True)
    ap.add_argument("--chain", default="bsc")
    ap.add_argument("--tz", type=int, default=8, help="timezone of the browser that made the dump (hours)")
    ap.add_argument("--now", default=None, help='"YYYY-MM-DD HH:MM" when the dump was taken (for "x hrs ago" rows)')
    a = ap.parse_args()
    tzinfo = timezone(timedelta(hours=a.tz))
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M").replace(tzinfo=tzinfo) if a.now else datetime.now(tzinfo)
    text = open(a.dump).read()
    if text.lstrip().startswith('"'):  # a JSON-encoded string (javascript_tool result)
        text = json.loads(text[:text.rfind('"') + 1])
    json.dump(parse(text, a.address, a.chain, a.tz, now), sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
