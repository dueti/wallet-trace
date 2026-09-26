"""Key-free EVM history via public RPC only (slow but complete). Built for BSC where no free indexer API exists.

Phase 1  scan   : eth_getLogs for ERC20 Transfer events from/to the wallet over 5000-block windows
                  (bloXroute public node allows topic-only filters up to 5000 blocks; ~0.5 window/s).
Phase 2  decode : per tx -> receipt + tx + block time; token legs from Transfer events, native out = tx.value,
                  native in = WBNB Withdrawal events in the tx (router unwraps and forwards to the user).
Everything is cached under ~/.cache/wallet-trace/rpc/<chain>-<addr>/ and resumable.
Known gaps: plain BNB transfers (funding), failed txs, and native paid directly by a bonding-curve contract
(no WBNB event) are not seen; the report marks such sells with usd=0.
"""
import concurrent.futures as cf
import json
import os
import threading
import time
import urllib.error
import urllib.request

from common import EVM_CHAINS, UA, log

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # WETH9 Withdrawal(src, wad)
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"     # WETH9 Deposit(dst, wad)

SCAN_RPC = {"bsc": ["https://bsc.rpc.blxrbdn.com"]}
CALL_RPC = {"bsc": ["https://bsc-dataseed.bnbchain.org", "https://bsc-dataseed1.bnbchain.org", "https://bsc-dataseed2.bnbchain.org", "https://bsc-dataseed3.bnbchain.org",
                    "https://bsc-dataseed4.bnbchain.org", "https://bsc-dataseed1.defibit.io", "https://bsc-dataseed2.defibit.io", "https://bsc-dataseed1.ninicoin.io", "https://bsc-dataseed2.ninicoin.io"]}
WINDOW = 5000
CACHE = os.path.expanduser("~/.cache/wallet-trace/rpc")


class Node:
    def __init__(self, urls):
        self.urls = urls
        self.i = 0
        self.lock = threading.Lock()

    def call(self, method, params, retries=10, timeout=60):
        last = None
        for k in range(retries):
            with self.lock:
                url = self.urls[self.i % len(self.urls)]
                self.i += 1
            try:
                req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                             headers={"content-type": "application/json", "User-Agent": UA})
                j = json.load(urllib.request.urlopen(req, timeout=timeout))
                if "error" in j and j["error"]:
                    raise RuntimeError(str(j["error"])[:100])
                return j["result"]
            except Exception as e:
                last = e
                time.sleep(min(1.5 * (k + 1), 8))
        raise RuntimeError(f"rpc {method} failed: {last}")


def block_at(node, ts, head):
    """Binary search the first block with timestamp >= ts."""
    def bts(n):
        return int(node.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
    lo, hi = 1, head
    while hi - lo > 100:
        mid = (lo + hi) // 2
        if bts(mid) < ts:
            lo = mid
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------ phase 1
def scan(chain, address, start, end, cache_dir, threads=8, progress=True):
    node = Node(SCAN_RPC[chain])
    pad = "0x" + "0" * 24 + address[2:]
    os.makedirs(cache_dir, exist_ok=True)
    logs_path, done_path = os.path.join(cache_dir, "logs.jsonl"), os.path.join(cache_dir, "windows.done")
    done = set()
    if os.path.exists(done_path):
        done = {int(x) for x in open(done_path).read().split()}
    start -= start % WINDOW  # align windows so a re-run with a different --days reuses the cache
    wins = [w for w in range(start, end + 1, WINDOW) if w not in done]
    if progress:
        log(f"[scan] {chain} {address[:8]} blocks {start}-{end}: {len(wins)} windows to go ({len(done)} cached), ~{len(wins)/0.55/60:.0f} min")
    lock = threading.Lock()
    out, dn = open(logs_path, "a"), open(done_path, "a")
    cnt = [0]
    t0 = time.time()

    def work(w):
        hi = min(w + WINDOW - 1, end)
        a = node.call("eth_getLogs", [{"fromBlock": hex(w), "toBlock": hex(hi), "topics": [TRANSFER, pad]}])
        b = node.call("eth_getLogs", [{"fromBlock": hex(w), "toBlock": hex(hi), "topics": [TRANSFER, None, pad]}])
        with lock:
            for l in a + b:
                out.write(json.dumps({"tx": l["transactionHash"], "blk": int(l["blockNumber"], 16)}) + "\n")
            out.flush()
            dn.write(f"{w}\n")
            dn.flush()
            cnt[0] += 1
            if progress and cnt[0] % 100 == 0:
                log(f"[scan] {cnt[0]}/{len(wins)} windows, {time.time()-t0:.0f}s")

    with cf.ThreadPoolExecutor(threads) as ex:
        list(ex.map(work, wins))
    out.close()
    dn.close()
    txs = {}
    for line in open(logs_path):
        d = json.loads(line)
        txs[d["tx"]] = d["blk"]
    return txs


# ------------------------------------------------------------------ phase 2
def _word(data, i):
    return int(data[2 + 64 * i: 66 + 64 * i], 16) if len(data) >= 66 + 64 * i else 0


def _decode_string(hexdata):
    if not hexdata or hexdata == "0x":
        return None
    raw = bytes.fromhex(hexdata[2:])
    try:
        if len(raw) >= 64 and int.from_bytes(raw[:32], "big") == 32:
            n = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + n].decode("utf-8", "replace").strip("\x00")
        return raw[:32].rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        return None


def token_meta(node, addr, cache):
    if addr in cache:
        return cache[addr]
    dec, sym = 18, None
    try:
        r = node.call("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"], retries=2)
        if r and r != "0x":
            dec = int(r, 16)
    except Exception:
        pass
    try:
        r = node.call("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"], retries=2)
        sym = _decode_string(r)
    except Exception:
        pass
    cache[addr] = {"decimals": dec if 0 <= dec <= 36 else 18, "symbol": sym or addr[:8]}
    return cache[addr]


def decode(chain, address, txs, cache_dir, threads=6, progress=True):
    cfg = EVM_CHAINS[chain]
    node = Node(CALL_RPC[chain])
    wrapped = cfg["wrapped"]
    rc_dir = os.path.join(cache_dir, "receipts")
    os.makedirs(rc_dir, exist_ok=True)
    meta_path = os.path.join(cache_dir, "tokens.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    blk_path = os.path.join(cache_dir, "blocks.json")
    blk_ts = {int(k): v for k, v in (json.load(open(blk_path)) if os.path.exists(blk_path) else {}).items()}
    lock = threading.Lock()
    hashes = sorted(txs, key=lambda h: txs[h])
    t0 = time.time()
    cnt = [0]

    def fetch(h):
        p = os.path.join(rc_dir, h[2:6], h + ".json")
        if os.path.exists(p):
            return json.load(open(p))
        rc = None
        for _ in range(4):
            rc = node.call("eth_getTransactionReceipt", [h])
            if rc:
                break
        if not rc:
            raise RuntimeError(f"no receipt for {h}")
        tx = None
        for _ in range(4):
            tx = node.call("eth_getTransactionByHash", [h])
            if tx:
                break
        value = int(tx["value"], 16) if tx else 0
        d = {"hash": h, "blk": int(rc["blockNumber"], 16), "status": rc.get("status"), "from": rc["from"].lower(), "to": (rc.get("to") or "").lower(),
             "value": value, "no_tx": tx is None, "logs": [{"a": l["address"].lower(), "t": l["topics"], "d": l["data"]} for l in rc["logs"]]}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(d, open(p, "w"))
        with lock:
            cnt[0] += 1
            if progress and cnt[0] % 200 == 0:
                log(f"[decode] {cnt[0]} receipts fetched, {time.time()-t0:.0f}s")
        return d

    def safe(h):
        try:
            return fetch(h)
        except Exception as e:
            log(f"[decode] skip {h[:12]}: {str(e)[:60]}")
            return None

    with cf.ThreadPoolExecutor(threads) as ex:
        recs = [r for r in ex.map(safe, hashes) if r]
    # block timestamps
    need = sorted({r["blk"] for r in recs if r["blk"] not in blk_ts})
    if progress:
        log(f"[decode] {len(recs)} txs, {len(need)} block timestamps to fetch")

    def bts(n):
        return n, int(node.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)

    with cf.ThreadPoolExecutor(threads) as ex:
        for n, t in ex.map(bts, need):
            blk_ts[n] = t
    json.dump(blk_ts, open(blk_path, "w"))
    # token metadata
    toks = sorted({l["a"] for r in recs for l in r["logs"] if l["t"] and l["t"][0] == TRANSFER and len(l["t"]) == 3
                   and (l["t"][1][-40:] == address[2:] or l["t"][2][-40:] == address[2:])})
    with cf.ThreadPoolExecutor(threads) as ex:
        list(ex.map(lambda a: token_meta(node, a, meta), [a for a in toks if a not in meta]))
    json.dump(meta, open(meta_path, "w"))

    raw = {"token_transfers": [], "txs": [], "internal": []}
    unpriced = 0
    for r in recs:
        ts = blk_ts[r["blk"]]
        h = r["hash"]
        ok = r["status"] in ("0x1", 1, True, None)
        raw["txs"].append({"tx": h, "ts": ts, "from": r["from"], "to": r["to"], "value": r["value"] / 1e18, "ok": ok, "method": "", "nlogs": len(r["logs"])})
        native_in = 0.0
        for l in r["logs"]:
            t = l["t"]
            if not t:
                continue
            if t[0] == TRANSFER and len(t) == 3:
                frm, to = "0x" + t[1][-40:], "0x" + t[2][-40:]
                if frm != address and to != address:
                    continue
                m = token_meta(node, l["a"], meta)
                raw["token_transfers"].append({"tx": h, "ts": ts, "from": frm, "to": to, "token_addr": l["a"], "symbol": m["symbol"],
                                               "value": _word(l["d"], 0) / (10 ** m["decimals"])})
            elif t[0] == WITHDRAWAL and l["a"] == wrapped:
                native_in += _word(l["d"], 0) / 1e18
        if native_in > 0 and r["from"] == address:
            raw["internal"].append({"tx": h, "ts": ts, "from": r["to"], "to": address, "value": native_in})
        elif r["from"] == address and r["value"] == 0 and any(l["t"] and l["t"][0] == TRANSFER and l["t"][1][-40:] == address[2:] for l in r["logs"]) \
                and not any(l["t"] and l["t"][0] == TRANSFER and l["t"][2][-40:] == address[2:] for l in r["logs"]):
            unpriced += 1
    if progress:
        log(f"[decode] done: {len(raw['txs'])} txs, {len(raw['token_transfers'])} token legs, {len(raw['internal'])} native-in legs, {unpriced} sells without WBNB event (unpriced)")
    return raw


def fetch_raw(chain, address, since_ts=None, threads=8):
    if chain not in SCAN_RPC:
        raise RuntimeError(f"rpc scan not configured for {chain}")
    address = address.lower()
    node = Node(SCAN_RPC[chain])
    head = int(node.call("eth_blockNumber", []), 16)
    start = block_at(Node(CALL_RPC[chain]), since_ts, head) if since_ts else 1
    cache_dir = os.path.join(CACHE, f"{chain}-{address[:8]}")
    txs = scan(chain, address, start, head, cache_dir, threads=threads)
    log(f"[scan] {len(txs)} unique txs with token transfers")
    return decode(chain, address, txs, cache_dir), "rpc-scan"
