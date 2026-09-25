"""Shared helpers: HTTP/RPC with retry, chain config, trade schema."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) wallet-trace/1.0"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_json(url, data=None, headers=None, retries=4, timeout=60, backoff=1.5):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503, 504, 403):
                time.sleep(backoff * (2 ** i))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(backoff * (2 ** i))
    raise RuntimeError(f"http failed after {retries} tries: {url[:120]} -> {last}")


def rpc(url, method, params, retries=5):
    r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, retries=retries)
    if "error" in r and r["error"]:
        raise RuntimeError(f"rpc {method} error: {r['error']}")
    return r.get("result")


# ---------------------------------------------------------------- chains
EVM_CHAINS = {
    "bsc": {
        "id": 56, "native": "BNB", "cg": "binancecoin", "dexscreener": "bsc",
        "wrapped": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "stables": {
            "0x55d398326f99059ff775485246999027b3197955": "USDT",
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
            "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
            "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": "DAI",
        },
        "rpc": "https://bsc-rpc.publicnode.com",
        "blockscout": None,
        "explorer": "https://bscscan.com",
    },
    "eth": {
        "id": 1, "native": "ETH", "cg": "ethereum", "dexscreener": "ethereum",
        "wrapped": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "stables": {
            "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
            "0xa0b86991c6218b36c1d19d4a2e8eb0ce3606eb48": "USDC",
            "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
        },
        "rpc": "https://ethereum-rpc.publicnode.com",
        "blockscout": "https://eth.blockscout.com",
        "explorer": "https://etherscan.io",
    },
    "base": {
        "id": 8453, "native": "ETH", "cg": "ethereum", "dexscreener": "base",
        "wrapped": "0x4200000000000000000000000000000000000006",
        "stables": {
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
            "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT",
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
        },
        "rpc": "https://base-rpc.publicnode.com",
        "blockscout": "https://base.blockscout.com",
        "explorer": "https://basescan.org",
    },
    "arb": {
        "id": 42161, "native": "ETH", "cg": "ethereum", "dexscreener": "arbitrum",
        "wrapped": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "stables": {
            "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": "USDT",
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
            "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": "USDC.e",
        },
        "rpc": "https://arbitrum-one-rpc.publicnode.com",
        "blockscout": "https://arbitrum.blockscout.com",
        "explorer": "https://arbiscan.io",
    },
}

SOL_STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCdmyANKMeNz": "USDT",
}
WSOL = "So11111111111111111111111111111111111111112"


# Etherscan V2 free plan (verified 2026-09-25): eth/arb OK, bsc/base return "Free API access is not supported for this chain".
ETHERSCAN_FREE_UNSUPPORTED = {"bsc", "base"}


def etherscan_key_for(chain):
    """Key usable for this chain: paid plan (ETHERSCAN_PAID=1) or chain is in the free tier."""
    k = etherscan_key()
    if not k:
        return None
    if chain in ETHERSCAN_FREE_UNSUPPORTED and os.environ.get("ETHERSCAN_PAID") != "1":
        return None
    return k


def etherscan_key():
    k = os.environ.get("ETHERSCAN_API_KEY")
    if k:
        return k.strip()
    p = os.path.expanduser("~/.config/wallet-trace/etherscan.key")
    if os.path.exists(p):
        return open(p).read().strip()
    try:
        import subprocess
        out = subprocess.run(["security", "find-generic-password", "-s", "etherscan-api", "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def solana_rpc_url():
    return os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def is_evm_address(a):
    return a.startswith("0x") and len(a) == 42 and all(c in "0123456789abcdefABCDEF" for c in a[2:])


def is_solana_address(a):
    if a.startswith("0x") or not (32 <= len(a) <= 44):
        return False
    return all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in a)


# ---------------------------------------------------------------- trade schema
def make_trade(ts, tx, side, token, token_addr, qty, quote, quote_amt, usd, note=""):
    """side: BUY / SELL / SWAP (token->token) / QUOTE_SWAP (stable<->native)."""
    return {"ts": int(ts), "tx": tx, "side": side, "token": token, "token_addr": token_addr,
            "qty": float(qty), "quote": quote, "quote_amt": float(quote_amt), "usd": float(usd), "note": note}


def make_transfer(ts, tx, direction, token, qty, usd, counterparty, kind=""):
    return {"ts": int(ts), "tx": tx, "dir": direction, "token": token, "qty": float(qty),
            "usd": float(usd), "counterparty": counterparty, "kind": kind}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default
