#!/usr/bin/env python3
"""wallet-trace: fetch any wallet address's trading history and produce a playbook report.

  python3 wallet_trace.py <address> [--chain bsc|eth|base|arb|solana|auto] [--days 180] [--tz 8]
                          [--out ~/Desktop/wallet-trace] [--max-tx 3000] [--debank-dump file] [--no-fetch]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import EVM_CHAINS, is_evm_address, is_solana_address, rpc, etherscan_key_for, save_json, load_json, log  # noqa: E402
from analyze import analyze  # noqa: E402


def detect_evm_chains(address):
    active = []
    for name, cfg in EVM_CHAINS.items():
        try:
            n = int(rpc(cfg["rpc"], "eth_getTransactionCount", [address, "latest"], retries=2), 16)
        except Exception as e:
            log(f"[detect] {name}: {e}")
            n = -1
        log(f"[detect] {name}: tx count {n}")
        if n > 0:
            active.append((name, n))
    active.sort(key=lambda x: -x[1])
    return active


def out_dir(base, chain, address):
    return os.path.join(os.path.expanduser(base), f"{chain}-{address[:6]}-{address[-4:]}")


def run_chain(chain, address, args):
    d = out_dir(args.out, chain, address)
    tpath = os.path.join(d, "trades.json")
    data = None
    if args.no_fetch or (not args.refresh and load_json(tpath) and time.time() - load_json(tpath).get("fetched_at", 0) < 3600):
        data = load_json(tpath)
        if data:
            log(f"[{chain}] using cached trades.json ({d})")
    if data is None:
        since = int(time.time() - args.days * 86400) if args.days else None
        if args.debank_dump:
            from parse_debank import parse
            from datetime import datetime, timedelta, timezone
            tzinfo = timezone(timedelta(hours=args.tz))
            now = datetime.strptime(args.dump_time, "%Y-%m-%d %H:%M").replace(tzinfo=tzinfo) if args.dump_time else datetime.now(tzinfo)
            text = open(args.debank_dump).read()
            if text.lstrip().startswith('"'):
                text = json.loads(text[:text.rfind('"') + 1])
            data = parse(text, address, chain, args.tz, now)
        elif chain == "solana":
            from fetch_solana import fetch
            data = fetch(address, since, args.max_tx)
        else:
            from fetch_evm import fetch
            data = fetch(chain, address, since)
        save_json(tpath, data)
    summary, report = analyze(data, tz=args.tz)
    save_json(os.path.join(d, "summary.json"), summary)
    with open(os.path.join(d, "report.md"), "w") as f:
        f.write(report)
    return d, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address")
    ap.add_argument("--chain", default="auto")
    ap.add_argument("--days", type=int, default=180, help="look-back window (0 = all)")
    ap.add_argument("--tz", type=int, default=8, help="hours offset for time-of-day stats")
    ap.add_argument("--out", default="~/Desktop/wallet-trace")
    ap.add_argument("--max-tx", type=int, default=3000, help="solana: max signatures to parse")
    ap.add_argument("--debank-dump", default=None, help="DeBank history page text dump (fallback for bsc without API key)")
    ap.add_argument("--dump-time", default=None, help='"YYYY-MM-DD HH:MM" when the DeBank dump was taken')
    ap.add_argument("--no-fetch", action="store_true", help="re-analyze existing trades.json only")
    ap.add_argument("--refresh", action="store_true", help="ignore the 1h cache")
    ap.add_argument("--quiet", action="store_true", help="print only output paths")
    args = ap.parse_args()
    a = args.address.strip()

    if args.chain != "auto":
        chains = [args.chain]
    elif args.debank_dump:
        chains = ["bsc"]
    elif is_solana_address(a):
        chains = ["solana"]
    elif is_evm_address(a):
        chains = [c for c, _ in detect_evm_chains(a)] or ["bsc"]
    else:
        sys.exit("unrecognised address format")
    need = [c for c in chains if c != "solana" and not EVM_CHAINS[c]["blockscout"] and not etherscan_key_for(c)]
    if need and not args.debank_dump and not args.no_fetch:
        log(f"[!] {','.join(need)}: no free API (Etherscan free plan excludes bsc/base). Options:\n"
            f"    a) DeBank fallback: dump the history page and pass --debank-dump FILE (see SKILL.md)\n"
            f"    b) paid Etherscan plan: export ETHERSCAN_PAID=1\n"
            f"    continuing with the other chains: {[c for c in chains if c not in need]}")
        chains = [c for c in chains if c not in need]
    if not chains:
        sys.exit(2)
    results = []
    for ch in chains:
        try:
            d, rep = run_chain(ch, a.lower() if ch != "solana" else a, args)
            results.append((ch, d, rep))
        except Exception as e:
            log(f"[{ch}] failed: {e}")
    for ch, d, rep in results:
        print(f"\n===== {ch} -> {d}/report.md =====\n")
        if not args.quiet:
            print(rep)


if __name__ == "__main__":
    main()
