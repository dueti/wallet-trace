"""Fetch EVM wallet history and rebuild trades.

Sources (in order):
  1. Etherscan V2 (ETHERSCAN_API_KEY; free plan = eth/arb only, bsc/base need paid + ETHERSCAN_PAID=1)
  2. Blockscout public API (no key; eth/base/arb — NOT bsc)
Returns the unified trades.json structure used by analyze.py.
"""
import time
from collections import defaultdict

from common import EVM_CHAINS, http_json, etherscan_key_for, make_trade, make_transfer, log
from prices import NativePrice


# ------------------------------------------------------------------ raw fetch
def _etherscan(chain, address, key, since_ts):
    cfg = EVM_CHAINS[chain]
    base = f"https://api.etherscan.io/v2/api?chainid={cfg['id']}&apikey={key}&address={address}"
    raw = {"token_transfers": [], "txs": [], "internal": []}

    def pull(action, dest, conv):
        start = 0
        while True:
            url = f"{base}&module=account&action={action}&startblock={start}&endblock=99999999&page=1&offset=10000&sort=asc"
            r = http_json(url, retries=4)
            res = r.get("result")
            if r.get("status") != "1":
                msg = str(r.get("message", "")) + " " + str(res)
                if "No transactions found" in msg or "No records" in msg or res == []:
                    return
                raise RuntimeError(f"etherscan {action}: {r.get('message')} {msg[:200]}")
            for x in res:
                dest.append(conv(x))
            if len(res) < 10000:
                return
            start = int(res[-1]["blockNumber"]) + 1
            time.sleep(0.25)

    pull("tokentx", raw["token_transfers"], lambda x: {
        "tx": x["hash"], "ts": int(x["timeStamp"]), "from": x["from"].lower(), "to": x["to"].lower(),
        "token_addr": x["contractAddress"].lower(), "symbol": x.get("tokenSymbol") or "?",
        "value": int(x["value"]) / (10 ** int(x.get("tokenDecimal") or 18))})
    time.sleep(0.25)
    pull("txlist", raw["txs"], lambda x: {
        "tx": x["hash"], "ts": int(x["timeStamp"]), "from": x["from"].lower(), "to": (x["to"] or "").lower(),
        "value": int(x["value"]) / 1e18, "ok": x.get("isError") == "0", "method": (x.get("functionName") or "")[:40]})
    time.sleep(0.25)
    pull("txlistinternal", raw["internal"], lambda x: {
        "tx": x["hash"], "ts": int(x["timeStamp"]), "from": x["from"].lower(), "to": (x["to"] or "").lower(),
        "value": int(x["value"]) / 1e18})
    if since_ts:
        for k in raw:
            raw[k] = [x for x in raw[k] if x["ts"] >= since_ts]
    return raw


def _iso_ts(s):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def _blockscout(chain, address, since_ts, max_pages=200):
    cfg = EVM_CHAINS[chain]
    bs = cfg["blockscout"]
    raw = {"token_transfers": [], "txs": [], "internal": []}

    def pull(path, dest, conv):
        params = None
        for _ in range(max_pages):
            url = f"{bs}/api/v2/addresses/{address}/{path}"
            if params:
                url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
            r = http_json(url, retries=4)
            items = r.get("items") or []
            stop = False
            for x in items:
                c = conv(x)
                if since_ts and c["ts"] < since_ts:
                    stop = True
                    break
                dest.append(c)
            params = r.get("next_page_params")
            if stop or not params:
                return
            time.sleep(0.2)

    pull("token-transfers", raw["token_transfers"], lambda x: {
        "tx": x["transaction_hash"], "ts": _iso_ts(x["timestamp"]), "from": x["from"]["hash"].lower(), "to": x["to"]["hash"].lower(),
        "token_addr": (x["token"].get("address_hash") or x["token"].get("address") or "").lower(), "symbol": x["token"].get("symbol") or "?",
        "value": int((x.get("total") or {}).get("value") or 0) / (10 ** int(x["token"].get("decimals") or 18))})
    pull("transactions", raw["txs"], lambda x: {
        "tx": x["hash"], "ts": _iso_ts(x["timestamp"]), "from": x["from"]["hash"].lower(), "to": ((x.get("to") or {}).get("hash") or "").lower(),
        "value": int(x.get("value") or 0) / 1e18, "ok": x.get("result") == "success" or x.get("status") == "ok",
        "method": (x.get("method") or "")[:40]})
    pull("internal-transactions", raw["internal"], lambda x: {
        "tx": x["transaction_hash"], "ts": _iso_ts(x["timestamp"]), "from": (x.get("from") or {}).get("hash", "").lower(),
        "to": (x.get("to") or {}).get("hash", "").lower(), "value": int(x.get("value") or 0) / 1e18})
    return raw


def fetch_raw(chain, address, since_ts=None):
    address = address.lower()
    key = etherscan_key_for(chain)
    if key:
        log(f"[evm] {chain}: etherscan v2")
        try:
            return _etherscan(chain, address, key, since_ts), "etherscan"
        except RuntimeError as e:
            if "not supported for this chain" not in str(e) or not EVM_CHAINS[chain]["blockscout"]:
                raise
            log(f"[evm] {chain}: etherscan refused ({e}); falling back to blockscout")
    if EVM_CHAINS[chain]["blockscout"]:
        log(f"[evm] {chain}: blockscout (no usable etherscan key for this chain)")
        return _blockscout(chain, address, since_ts), "blockscout"
    raise RuntimeError(f"{chain}: no data source. Etherscan free plan does not cover {chain}; use the DeBank fallback (--debank-dump) or a paid key with ETHERSCAN_PAID=1.")


# ------------------------------------------------------------------ rebuild trades
def build_trades(chain, address, raw):
    cfg = EVM_CHAINS[chain]
    address = address.lower()
    native = cfg["native"]
    wrapped = cfg["wrapped"]
    stables = cfg["stables"]
    px = NativePrice(cfg["cg"])

    by_tx = defaultdict(lambda: {"ts": 0, "legs": [], "sender": None, "ok": True, "method": ""})
    for t in raw["token_transfers"]:
        e = by_tx[t["tx"]]
        e["ts"] = t["ts"]
        if t["from"] == address and t["value"] > 0:
            e["legs"].append(("-", t["token_addr"], t["symbol"], t["value"], t["to"]))
        if t["to"] == address and t["value"] > 0:
            e["legs"].append(("+", t["token_addr"], t["symbol"], t["value"], t["from"]))
    for t in raw["txs"]:
        e = by_tx[t["tx"]]
        e["ts"] = t["ts"]
        e["ok"] = t.get("ok", True)
        e["method"] = t.get("method", "")
        if t["from"] == address:
            e["sender"] = address
            if t["value"] > 0:
                e["legs"].append(("-", "NATIVE", native, t["value"], t["to"]))
        elif t["to"] == address and t["value"] > 0:
            e["legs"].append(("+", "NATIVE", native, t["value"], t["from"]))
    for t in raw["internal"]:
        e = by_tx[t["tx"]]
        e["ts"] = e["ts"] or t["ts"]
        if t["to"] == address and t["value"] > 0:
            e["legs"].append(("+", "NATIVE", native, t["value"], t["from"]))
        elif t["from"] == address and t["value"] > 0:
            e["legs"].append(("-", "NATIVE", native, t["value"], t["to"]))

    def is_quote(a):
        return a == "NATIVE" or a == wrapped or a in stables

    def usd_of(a, qty, ts):
        if a in stables:
            return qty
        return qty * px.at(ts)

    # first pass: which non-quote tokens does this wallet actively send out (sold) -> "real" tokens
    sold_tokens = defaultdict(int)
    for h, e in by_tx.items():
        if e["sender"] == address:
            for d, a, s, q, cp in e["legs"]:
                if d == "-" and not is_quote(a):
                    sold_tokens[a] += 1

    trades, transfers = [], []
    for h, e in sorted(by_tx.items(), key=lambda kv: kv[1]["ts"]):
        if not e["ok"] or not e["legs"]:
            continue
        ts = e["ts"]
        outs = [l for l in e["legs"] if l[0] == "-"]
        ins = [l for l in e["legs"] if l[0] == "+"]
        # merge same asset legs
        def merge(legs):
            m = {}
            for d, a, s, q, cp in legs:
                if a in m:
                    m[a] = (d, a, s, m[a][3] + q, cp)
                else:
                    m[a] = (d, a, s, q, cp)
            return list(m.values())
        outs, ins = merge(outs), merge(ins)
        out_q = [l for l in outs if is_quote(l[1])]
        in_q = [l for l in ins if is_quote(l[1])]
        out_t = [l for l in outs if not is_quote(l[1])]
        in_t = [l for l in ins if not is_quote(l[1])]
        out_q_usd = sum(usd_of(l[1], l[3], ts) for l in out_q)
        in_q_usd = sum(usd_of(l[1], l[3], ts) for l in in_q)
        # net native across legs (a buy with BNB may also refund dust)
        if e["sender"] != address:
            # passive: someone sent us something (airdrop / transfer in)
            for l in ins:
                usd = usd_of(l[1], l[3], ts) if is_quote(l[1]) else 0.0
                transfers.append(make_transfer(ts, h, "in", l[2], l[3], usd, l[4], "receive"))
            continue
        if out_t and in_t:
            # token -> token swap; value via quote legs if any
            usd = max(out_q_usd, in_q_usd)
            trades.append(make_trade(ts, h, "SWAP", f"{out_t[0][2]}->{in_t[0][2]}", out_t[0][1], out_t[0][3], in_t[0][2], in_t[0][3], usd, "token-token"))
            continue
        if out_q and in_t:
            # BUY: pick the token most likely to be the real target
            cand = sorted(in_t, key=lambda l: (sold_tokens.get(l[1], 0), l[3]), reverse=True)
            tgt = cand[0]
            qsym = out_q[0][2] if len(out_q) == 1 else "MIXED"
            usd = out_q_usd - in_q_usd  # refund of quote dust
            trades.append(make_trade(ts, h, "BUY", tgt[2], tgt[1], tgt[3], qsym, sum(l[3] for l in out_q), max(usd, 0), ""))
            continue
        if in_q and out_t:
            src = sorted(out_t, key=lambda l: l[3], reverse=True)[0]
            qsym = in_q[0][2] if len(in_q) == 1 else "MIXED"
            usd = in_q_usd - out_q_usd
            trades.append(make_trade(ts, h, "SELL", src[2], src[1], src[3], qsym, sum(l[3] for l in in_q), max(usd, 0), ""))
            continue
        if out_q and in_q:
            trades.append(make_trade(ts, h, "QUOTE_SWAP", f"{out_q[0][2]}->{in_q[0][2]}", out_q[0][1], out_q[0][3], in_q[0][2], in_q[0][3], out_q_usd, ""))
            continue
        if outs and not ins:
            for l in outs:
                usd = usd_of(l[1], l[3], ts) if is_quote(l[1]) else 0.0
                transfers.append(make_transfer(ts, h, "out", l[2], l[3], usd, l[4], "send"))
            continue
        if ins and not outs:
            # we initiated and only received: claim / unwrap / withdraw
            for l in ins:
                usd = usd_of(l[1], l[3], ts) if is_quote(l[1]) else 0.0
                transfers.append(make_transfer(ts, h, "in", l[2], l[3], usd, l[4], e["method"] or "claim"))
    return trades, transfers


def fetch(chain, address, since_ts=None):
    raw, source = fetch_raw(chain, address, since_ts)
    trades, transfers = build_trades(chain, address, raw)
    n = sum(len(v) for v in raw.values())
    log(f"[evm] {chain}: raw records {n}, trades {len(trades)}, transfers {len(transfers)}")
    return {"chain": chain, "address": address, "source": source, "fetched_at": int(time.time()),
            "native": EVM_CHAINS[chain]["native"], "trades": trades, "transfers": transfers,
            "raw_counts": {k: len(v) for k, v in raw.items()}}
