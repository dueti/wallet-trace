"""Price helpers: CoinGecko daily native price history (cached), DexScreener token metadata."""
import bisect
import os
import time
import urllib.parse

from common import http_json, load_json, save_json, log

CACHE_DIR = os.path.expanduser("~/.cache/wallet-trace")


class NativePrice:
    """Daily USD price series for a CoinGecko id, nearest-day lookup."""

    def __init__(self, cg_id):
        self.cg_id = cg_id
        path = os.path.join(CACHE_DIR, f"cg_{cg_id}.json")
        cached = load_json(path)
        if cached and time.time() - cached.get("fetched_at", 0) < 6 * 3600:
            pts = cached["prices"]
        else:
            pts = None
            for days in (365, 90):
                try:
                    r = http_json(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days={days}&interval=daily", retries=3)
                    pts = r.get("prices") or []
                    if pts:
                        break
                except Exception as e:
                    log(f"[prices] coingecko {cg_id} days={days} failed: {e}")
            if pts:
                save_json(path, {"fetched_at": time.time(), "prices": pts})
            elif cached:
                pts = cached["prices"]
            else:
                pts = []
        self.ts = [p[0] / 1000 for p in pts]
        self.px = [p[1] for p in pts]

    def at(self, ts):
        if not self.ts:
            return 0.0
        i = bisect.bisect_left(self.ts, ts)
        if i <= 0:
            return self.px[0]
        if i >= len(self.ts):
            return self.px[-1]
        # nearest
        return self.px[i] if abs(self.ts[i] - ts) < abs(self.ts[i - 1] - ts) else self.px[i - 1]


def looks_like_address(a):
    if a.startswith("0x"):
        return len(a) == 42
    return 32 <= len(a) <= 44 and a.isascii() and a.isalnum()


def _pair_info(p):
    bt = p.get("baseToken", {})
    return {
        "symbol": bt.get("symbol"), "name": bt.get("name"), "address": bt.get("address"),
        "price": float(p.get("priceUsd") or 0),
        "mcap": p.get("marketCap") or p.get("fdv"),
        "liq": (p.get("liquidity") or {}).get("usd"),
        "created": (p.get("pairCreatedAt") or 0) / 1000,
        "url": p.get("url"), "_vol": (p.get("volume") or {}).get("h24") or 0,
    }


def dexscreener_search(chain, symbol):
    """Fallback when we only know a symbol (DeBank dump): best pair on chain by 24h volume, exact symbol match."""
    try:
        r = http_json("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(symbol), retries=2)
    except Exception as e:
        log(f"[prices] dexscreener search {symbol!r} failed: {e}")
        return None
    pairs = [p for p in (r.get("pairs") or []) if p.get("chainId") == chain and (p.get("baseToken") or {}).get("symbol") == symbol]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("volume") or {}).get("h24") or 0)
    info = _pair_info(best)
    info["by_symbol"] = True
    return info


def dexscreener_tokens(chain, addrs):
    """chain: dexscreener chain id (bsc/ethereum/base/arbitrum/solana). Returns {addr: info}."""
    out = {}
    addrs = [a for a in dict.fromkeys(addrs) if a and looks_like_address(a)]
    for i in range(0, len(addrs), 30):
        batch = addrs[i:i + 30]
        try:
            pairs = http_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{','.join(batch)}", retries=3)
        except Exception as e:
            log(f"[prices] dexscreener batch failed: {e}")
            continue
        if not isinstance(pairs, list):
            continue
        for p in pairs:
            a = (p.get("baseToken") or {}).get("address", "")
            key = a if chain == "solana" else a.lower()
            info = _pair_info(p)
            cur = out.get(key)
            # keep the pair with the highest 24h volume (avoid fake pools)
            if cur is None or info["_vol"] > cur["_vol"]:
                out[key] = info
        time.sleep(0.25)
    return out
