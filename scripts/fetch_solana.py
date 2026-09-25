"""Fetch a Solana wallet's history via JSON-RPC (public endpoint OK, throttled) and rebuild trades."""
import os
import time
from collections import defaultdict

from common import SOL_STABLES, WSOL, rpc, solana_rpc_url, make_trade, make_transfer, load_json, save_json, log
from prices import NativePrice, dexscreener_tokens

CACHE_DIR = os.path.expanduser("~/.cache/wallet-trace/sol_tx")


def _get_tx(url, sig):
    p = os.path.join(CACHE_DIR, sig[:2], sig + ".json")
    c = load_json(p)
    if c is not None:
        return c
    for i in range(6):
        try:
            r = rpc(url, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], retries=1)
            save_json(p, r or {})
            return r
        except Exception as e:
            time.sleep(0.6 * (2 ** i))
    log(f"[sol] give up {sig[:12]}")
    return None


def fetch(address, since_ts=None, max_tx=3000):
    url = solana_rpc_url()
    public = "mainnet-beta.solana.com" in url
    delay = 0.35 if public else 0.05
    sigs, before = [], None
    while len(sigs) < max_tx:
        params = {"limit": 1000}
        if before:
            params["before"] = before
        batch = rpc(url, "getSignaturesForAddress", [address, params])
        if not batch:
            break
        for s in batch:
            if since_ts and (s.get("blockTime") or 0) < since_ts:
                batch = None
                break
            if not s.get("err"):
                sigs.append(s)
        if batch is None or len(batch) < 1000:
            break
        before = sigs[-1]["signature"] if sigs else None
        if not before:
            break
        time.sleep(delay)
    sigs = sigs[:max_tx]
    log(f"[sol] {len(sigs)} signatures, fetching txs (delay {delay}s)...")

    px = NativePrice("solana")
    events = []  # (ts, sig, sol_delta, {mint: delta})
    for i, s in enumerate(sigs):
        tx = _get_tx(url, s["signature"])
        if not public:
            time.sleep(delay)
        else:
            time.sleep(delay)
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            continue
        if i and i % 100 == 0:
            log(f"[sol] {i}/{len(sigs)}")
        meta, msg = tx["meta"], tx["transaction"]["message"]
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
        if address not in keys:
            continue
        idx = keys.index(address)
        sol_delta = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9
        if idx == 0:
            sol_delta += meta.get("fee", 0) / 1e9  # ignore fee for direction purposes
        tok = defaultdict(float)
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == address:
                tok[b["mint"]] -= float(b["uiTokenAmount"].get("uiAmount") or 0)
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == address:
                tok[b["mint"]] += float(b["uiTokenAmount"].get("uiAmount") or 0)
        if WSOL in tok:
            sol_delta += tok.pop(WSOL)
        signer = keys[0]
        events.append((tx.get("blockTime") or s.get("blockTime") or 0, s["signature"], sol_delta, dict(tok), signer))

    mints = sorted({m for _, _, _, t, _ in events for m in t if m not in SOL_STABLES})
    meta_tok = dexscreener_tokens("solana", mints)

    def sym(m):
        if m in SOL_STABLES:
            return SOL_STABLES[m]
        info = meta_tok.get(m)
        return (info.get("symbol") if info else None) or m[:6]

    trades, transfers = [], []
    for ts, sig, sol_d, tok, signer in sorted(events):
        stable_d = sum(v for m, v in tok.items() if m in SOL_STABLES)
        toks = {m: v for m, v in tok.items() if m not in SOL_STABLES and abs(v) > 0}
        quote_out = 0.0
        quote_in = 0.0
        qsym = "SOL"
        if sol_d < -0.0005:
            quote_out += -sol_d * px.at(ts)
        elif sol_d > 0.0005:
            quote_in += sol_d * px.at(ts)
        if stable_d < -0.01:
            quote_out += -stable_d
            qsym = "USD"
        elif stable_d > 0.01:
            quote_in += stable_d
            qsym = "USD"
        gained = {m: v for m, v in toks.items() if v > 0}
        lost = {m: -v for m, v in toks.items() if v < 0}
        if signer != address:
            for m, v in gained.items():
                transfers.append(make_transfer(ts, sig, "in", sym(m), v, 0.0, signer, "receive"))
            if quote_in > 0.5:
                transfers.append(make_transfer(ts, sig, "in", qsym, quote_in, quote_in, signer, "receive"))
            continue
        if gained and lost:
            m_out, m_in = max(lost, key=lost.get), max(gained, key=gained.get)
            trades.append(make_trade(ts, sig, "SWAP", f"{sym(m_out)}->{sym(m_in)}", m_out, lost[m_out], sym(m_in), gained[m_in], max(quote_out, quote_in), "token-token"))
        elif gained and quote_out > 0.05:
            m = max(gained, key=gained.get)
            trades.append(make_trade(ts, sig, "BUY", sym(m), m, gained[m], qsym, quote_out, max(quote_out - quote_in, 0)))
        elif lost and quote_in > 0.05:
            m = max(lost, key=lost.get)
            trades.append(make_trade(ts, sig, "SELL", sym(m), m, lost[m], qsym, quote_in, max(quote_in - quote_out, 0)))
        elif lost:
            for m, v in lost.items():
                transfers.append(make_transfer(ts, sig, "out", sym(m), v, 0.0, "", "send"))
        elif gained:
            for m, v in gained.items():
                transfers.append(make_transfer(ts, sig, "in", sym(m), v, 0.0, "", "claim"))
        elif quote_out > 1:
            transfers.append(make_transfer(ts, sig, "out", qsym, quote_out, quote_out, "", "send"))
        elif quote_in > 1:
            transfers.append(make_transfer(ts, sig, "in", qsym, quote_in, quote_in, "", "receive"))
    log(f"[sol] trades {len(trades)}, transfers {len(transfers)}")
    return {"chain": "solana", "address": address, "source": "solana-rpc", "fetched_at": int(time.time()),
            "native": "SOL", "trades": trades, "transfers": transfers, "token_meta": meta_tok,
            "raw_counts": {"signatures": len(sigs), "parsed": len(events)}}
