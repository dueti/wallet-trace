"""Turn trades.json into summary.json + report.md (facts for Claude to narrate the trader's playbook)."""
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from prices import dexscreener_tokens, dexscreener_search, looks_like_address
from common import EVM_CHAINS, log


def fmt_ts(ts, tz):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=tz))).strftime("%m-%d %H:%M")


def fmt_day(ts, tz):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=tz))).strftime("%Y-%m-%d")


def med(xs, d=0.0):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else d


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


# ------------------------------------------------------------------ wash / farming detection
def detect_wash(trades, window_s=900, tol=0.08, min_pairs=8, min_share=0.6):
    """Tokens where most activity is buy->sell round-trips of the same qty within minutes."""
    by_tok = defaultdict(list)
    for t in trades:
        by_tok[t["token_addr"]].append(t)
    wash = {}
    for addr, ts in by_tok.items():
        ts.sort(key=lambda x: x["ts"])
        pairs, i = [], 0
        while i < len(ts) - 1:
            a, b = ts[i], ts[i + 1]
            if a["side"] == "BUY" and b["side"] == "SELL" and b["ts"] - a["ts"] <= window_s and a["qty"] and abs(b["qty"] / a["qty"] - 1) <= tol:
                pairs.append((a, b))
                i += 2
            else:
                i += 1
        if len(pairs) >= min_pairs and 2 * len(pairs) >= min_share * len(ts):
            days = Counter(fmt_day(a["ts"], 0) for a, _ in pairs)
            wash[addr] = {
                "token": ts[0]["token"], "pairs": len(pairs), "trades": len(ts),
                "volume": sum(a["usd"] + b["usd"] for a, b in pairs),
                "net": sum(b["usd"] - a["usd"] for a, b in pairs),
                "size_med": med([a["usd"] for a, _ in pairs]),
                "gap_med_min": med([(b["ts"] - a["ts"]) / 60 for a, b in pairs]),
                "days": len(days), "per_day_med": med(list(days.values())),
                "first": min(a["ts"] for a, _ in pairs), "last": max(b["ts"] for _, b in pairs),
            }
    return wash


# ------------------------------------------------------------------ rounds
def build_rounds(trades, meta, transfers=None):
    by_tok = defaultdict(list)
    for t in trades:
        by_tok[t["token_addr"]].append(t)
    # token transfers OUT reduce the position without proceeds (moved to another wallet / bridge / CEX)
    sym_to_addr = {}
    for t in trades:
        sym_to_addr.setdefault(t["token"], t["token_addr"])
    for x in transfers or []:
        if x["dir"] == "out" and x["qty"] > 0:
            addr = x["token"] if x["token"] in by_tok else sym_to_addr.get(x["token"])
            if addr in by_tok:
                by_tok[addr].append({"ts": x["ts"], "side": "OUT", "token": x["token"], "token_addr": addr, "qty": x["qty"], "usd": 0.0, "tx": x["tx"]})
    rounds, sold_unbought = [], []
    for addr, ts in by_tok.items():
        ts.sort(key=lambda x: x["ts"])
        cur = None
        for t in ts:
            if t["side"] == "OUT":
                if cur is None:
                    continue
                cur["pos"] -= t["qty"]
                cur["moved_out"] = cur.get("moved_out", 0.0) + t["qty"]
                cur["last_ts"] = t["ts"]
                if cur["pos"] <= cur["max_pos"] * 0.02:
                    cur["pos"] = 0.0
                    cur["closed_by"] = "out"
                    cur = None
                continue
            if t["side"] == "BUY":
                if cur is None:
                    cur = {"token": t["token"], "addr": addr, "first_buy": t["ts"], "last_ts": t["ts"], "first_sell": None,
                           "buys": [], "sells": [], "pos": 0.0, "max_pos": 0.0, "adds_below_avg": 0, "bought_qty": 0.0,
                           "sold_qty": 0.0, "invested": 0.0, "returned": 0.0}
                    rounds.append(cur)
                if cur["bought_qty"] and t["qty"] and cur["invested"] / cur["bought_qty"] > t["usd"] / t["qty"]:
                    cur["adds_below_avg"] += 1
                cur["buys"].append(t)
                cur["pos"] += t["qty"]
                cur["max_pos"] = max(cur["max_pos"], cur["pos"])
                cur["bought_qty"] += t["qty"]
                cur["invested"] += t["usd"]
                cur["last_ts"] = t["ts"]
            else:  # SELL
                if cur is None:
                    sold_unbought.append(t)
                    continue
                if cur["first_sell"] is None:
                    cur["first_sell"] = t["ts"]
                cur["sells"].append(t)
                cur["pos"] -= t["qty"]
                cur["sold_qty"] += t["qty"]
                cur["returned"] += t["usd"]
                cur["last_ts"] = t["ts"]
                if cur["pos"] <= cur["max_pos"] * 0.02:
                    cur["pos"] = 0.0
                    cur = None
    now_meta = meta or {}
    out = []
    for r in rounds:
        info = now_meta.get(r["addr"]) or {}
        cur_px = info.get("price") or 0.0
        left_qty = max(r["pos"], 0.0)
        mtm = left_qty * cur_px
        moved = r.get("closed_by") == "out"
        closed = r["pos"] <= 0 and not moved
        entry_px = r["invested"] / r["bought_qty"] if r["bought_qty"] else 0
        exit_px = r["returned"] / r["sold_qty"] if r["sold_qty"] else 0
        pnl = r["returned"] + mtm - r["invested"]
        if moved:
            sold_share = (r["sold_qty"] / r["bought_qty"]) if r["bought_qty"] else 0.0
            pnl = r["returned"] - r["invested"] * sold_share  # only the part actually sold
        end_ts = r["last_ts"]
        out.append({
            "token": r["token"], "addr": r["addr"], "closed": closed, "moved": moved, "moved_out": r.get("moved_out", 0.0),
            "first_buy": r["first_buy"], "first_sell": r["first_sell"], "end": end_ts,
            "n_buys": len(r["buys"]), "n_sells": len(r["sells"]), "adds_below_avg": r["adds_below_avg"],
            "invested": r["invested"], "returned": r["returned"], "mtm": mtm, "left_qty": left_qty,
            "pnl": pnl, "pnl_pct": (pct(r["returned"] + mtm, r["invested"]) if r["invested"] else 0.0) if not moved else None,
            "hold_h": (end_ts - r["first_buy"]) / 3600, "to_first_sell_h": ((r["first_sell"] or end_ts) - r["first_buy"]) / 3600,
            "entry_px": entry_px, "exit_px": exit_px, "cur_px": cur_px,
            "since_exit_pct": pct(cur_px, exit_px) if (exit_px and cur_px) else None,
            "buy_after_launch_h": ((r["first_buy"] - info["created"]) / 3600) if info.get("created") else None,
            "mcap_now": info.get("mcap"), "url": info.get("url"),
        })
    out.sort(key=lambda r: r["first_buy"])
    return out, sold_unbought


# ------------------------------------------------------------------ stats
def stats(rounds, trades, transfers, tz):
    closed = [r for r in rounds if r["closed"]]
    openr = [r for r in rounds if not r["closed"] and not r.get("moved")]
    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] <= 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = sum(r["pnl"] for r in losses)
    inv = [r["invested"] for r in rounds if r["invested"] > 0]
    inv_sorted = sorted(inv)
    p90 = inv_sorted[int(len(inv_sorted) * 0.9) - 1] if len(inv_sorted) >= 2 else (inv_sorted[-1] if inv_sorted else 0)
    probe_cut = p90 * 0.25
    probes = [x for x in inv if x <= probe_cut]
    # peak concurrent exposure
    ev = []
    for r in rounds:
        ev.append((r["first_buy"], r["invested"]))
        ev.append((r["end"] + 1 if r["closed"] else 2 ** 40, -r["invested"]))
    ev.sort()
    cur = peak = 0.0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    hours = Counter(datetime.fromtimestamp(t["ts"], timezone(timedelta(hours=tz))).hour for t in trades)
    wdays = Counter(datetime.fromtimestamp(t["ts"], timezone(timedelta(hours=tz))).weekday() for t in trades)
    weeks = Counter()
    for t in trades:
        d = datetime.fromtimestamp(t["ts"], timezone(timedelta(hours=tz))).date()
        weeks[(d - timedelta(days=d.weekday())).isoformat()] += 1
    days = {fmt_day(t["ts"], tz) for t in trades}
    span_days = ((max(t["ts"] for t in trades) - min(t["ts"] for t in trades)) / 86400) if trades else 0
    big_tr = sorted([x for x in transfers if x["usd"] >= 50], key=lambda x: -x["usd"])
    return {
        "rounds": len(rounds), "closed": len(closed), "open": len(openr),
        "wins": len(wins), "losses": len(losses), "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "pnl_closed": sum(r["pnl"] for r in closed), "pnl_open_mtm": sum(r["pnl"] for r in openr),
        "gross_win": gross_win, "gross_loss": gross_loss,
        "top_win": max((r["pnl"] for r in wins), default=0.0), "top_win_token": max(wins, key=lambda r: r["pnl"])["token"] if wins else None,
        "top_win_share": (max((r["pnl"] for r in wins), default=0.0) / gross_win * 100) if gross_win else 0.0,
        "worst_loss": min((r["pnl"] for r in losses), default=0.0), "worst_loss_token": min(losses, key=lambda r: r["pnl"])["token"] if losses else None,
        "worst_loss_pct": min((r["pnl_pct"] for r in losses), default=0.0),
        "win_pct_med": med([r["pnl_pct"] for r in wins]), "loss_pct_med": med([r["pnl_pct"] for r in losses]),
        "invested_med": med(inv), "invested_mean": statistics.mean(inv) if inv else 0.0, "invested_p90": p90, "invested_max": max(inv, default=0.0),
        "probe_cut": probe_cut, "probe_share": (len(probes) / len(inv) * 100) if inv else 0.0,
        "peak_exposure": peak,
        "hold_med_h": med([r["hold_h"] for r in closed]), "hold_med_win_h": med([r["hold_h"] for r in wins]), "hold_med_loss_h": med([r["hold_h"] for r in losses]),
        "to_first_sell_med_h": med([r["to_first_sell_h"] for r in closed]),
        "share_lt_1h": (sum(r["hold_h"] < 1 for r in closed) / len(closed) * 100) if closed else 0.0,
        "share_lt_24h": (sum(r["hold_h"] < 24 for r in closed) / len(closed) * 100) if closed else 0.0,
        "share_gt_3d": (sum(r["hold_h"] > 72 for r in closed) / len(closed) * 100) if closed else 0.0,
        "avg_buys": statistics.mean([r["n_buys"] for r in rounds]) if rounds else 0.0,
        "avg_sells": statistics.mean([r["n_sells"] for r in rounds]) if rounds else 0.0,
        "rounds_avg_down": sum(r["adds_below_avg"] > 0 for r in rounds),
        "losses_cut_lt_1h": sum(r["hold_h"] < 1 for r in losses), "losses_held_gt_2d": sum(r["hold_h"] > 48 for r in losses),
        "repeat_tokens": Counter(r["addr"] for r in rounds),
        "missed": [r for r in closed if r["since_exit_pct"] is not None and r["since_exit_pct"] > 100],
        "dodged": [r for r in closed if r["since_exit_pct"] is not None and r["since_exit_pct"] < -70],
        "hours": dict(sorted(hours.items())), "weekdays": dict(sorted(wdays.items())), "weeks": dict(sorted(weeks.items())),
        "active_days": len(days), "span_days": span_days,
        "flows_in": sum(x["usd"] for x in big_tr if x["dir"] == "in"), "flows_out": sum(x["usd"] for x in big_tr if x["dir"] == "out"),
        "big_transfers": big_tr[:20],
    }


# ------------------------------------------------------------------ facts (Chinese, for narration)
def facts(S, W, rounds, tz):
    f = []
    if W:
        wv = sum(w["volume"] for w in W.values())
        wt = sum(w["trades"] for w in W.values())
        names = "、".join(w["token"] for w in W.values())
        f.append(f"刷量/挂机识别：{names} 共 {wt} 笔、成交 {wv:,.0f}U、净损益 {sum(w['net'] for w in W.values()):+,.0f}U，"
                 f"典型单笔 {med([w['size_med'] for w in W.values()]):,.0f}U、买卖间隔中位 {med([w['gap_med_min'] for w in W.values()]):.1f} 分钟、每天约 {med([w['per_day_med'] for w in W.values()]):.0f} 笔。这是刷积分/刷量，不是判断性交易，已从下面统计剔除。")
    if not rounds:
        f.append("剔除刷量后没有真实的买卖轮次。")
        return f
    f.append(f"真实交易：{S['rounds']} 个轮次（{S['closed']} 已平、{S['open']} 持有中、{S['rounds']-S['closed']-S['open']} 转走），{S['wins']} 胜 {S['losses']} 负，胜率 {S['win_rate']:.0f}%，已平仓盈亏 {S['pnl_closed']:+,.0f}U"
             + (f"，持仓浮盈亏 {S['pnl_open_mtm']:+,.0f}U" if S['open'] else "") + "。")
    if S["wins"]:
        f.append(f"利润集中度：最大一笔 {S['top_win_token']} 赚 {S['top_win']:,.0f}U，占全部盈利的 {S['top_win_share']:.0f}%。"
                 + ("利润基本靠一两笔重仓，其余是小单试错。" if S['top_win_share'] > 60 else "盈利分布比较均匀。"))
    f.append(f"仓位：单轮投入中位 {S['invested_med']:,.0f}U、p90 {S['invested_p90']:,.0f}U、最大 {S['invested_max']:,.0f}U；"
             f"{S['probe_share']:.0f}% 的轮次是 ≤{S['probe_cut']:,.0f}U 的探路单；同时在场的最大敞口约 {S['peak_exposure']:,.0f}U。")
    f.append(f"节奏：平仓轮次持仓中位 {S['hold_med_h']:.1f} 小时（首买到首卖中位 {S['to_first_sell_med_h']:.1f} 小时），"
             f"{S['share_lt_1h']:.0f}% 在 1 小时内了结、{S['share_lt_24h']:.0f}% 在 24 小时内、{S['share_gt_3d']:.0f}% 超过 3 天。")
    if S["wins"] or S["losses"]:
        f.append(f"止盈止损：赢单中位 +{S['win_pct_med']:.0f}%、持仓中位 {S['hold_med_win_h']:.1f}h；亏单中位 {S['loss_pct_med']:.0f}%、持仓中位 {S['hold_med_loss_h']:.1f}h；"
                 f"最差一笔 {S['worst_loss_token']} {S['worst_loss']:,.0f}U（{S['worst_loss_pct']:.0f}%）。亏单 1 小时内割掉 {S['losses_cut_lt_1h']} 笔、拿超 2 天 {S['losses_held_gt_2d']} 笔。")
    f.append(f"分批与补仓：平均每轮 {S['avg_buys']:.1f} 笔买入、{S['avg_sells']:.1f} 笔卖出；{S['rounds_avg_down']} 个轮次有低于均价的补仓（摊低成本）。")
    rep = [(a, n) for a, n in S["repeat_tokens"].items() if n > 1]
    if rep:
        names = "、".join(f"{next(r['token'] for r in rounds if r['addr']==a)}×{n}" for a, n in sorted(rep, key=lambda x: -x[1])[:5])
        f.append(f"复做同一个币：{names}。")
    def dedupe(rs):
        seen = {}
        for r in rs:
            seen[r["addr"]] = r  # keep the latest round per token
        return list(seen.values())
    if S["missed"]:
        f.append("卖飞（卖出后又涨超 1 倍）：" + "、".join(f"{r['token']} 卖后 +{r['since_exit_pct']:.0f}%" for r in dedupe(S['missed'])[:6]) + "。")
    if S["dodged"]:
        f.append("躲过（卖出后跌超 70%）：" + "、".join(f"{r['token']} 卖后 {r['since_exit_pct']:.0f}%" for r in dedupe(S['dodged'])[:6]) + "。")
    moved = [r for r in rounds if r.get("moved_out")]
    if moved:
        f.append("有仓位被转走而不是卖出（可能转去别的钱包/交易所）：" + "、".join(r["token"] for r in moved[:6]) + "，这些轮次的盈亏看不到。")
    launch = [r["buy_after_launch_h"] for r in rounds if r["buy_after_launch_h"] is not None]
    if launch:
        f.append(f"入场时点：距池子创建中位 {med(launch):.0f} 小时（{sum(h<24 for h in launch)}/{len(launch)} 在 24h 内进场）。")
    if S["hours"]:
        top = sorted(S["hours"].items(), key=lambda x: -x[1])[:4]
        f.append(f"时段（UTC{tz:+d}）：最活跃 " + "、".join(f"{h}点({n})" for h, n in top) + f"；{S['active_days']} 个活跃日 / {S['span_days']:.0f} 天。")
    if S["weeks"]:
        f.append("周节奏：" + "、".join(f"{k[5:]}:{v}" for k, v in S["weeks"].items()) + "。")
    if S["flows_in"] or S["flows_out"]:
        f.append(f"资金流（≥50U 的稳定币/主币转账）：转入 {S['flows_in']:,.0f}U、转出 {S['flows_out']:,.0f}U。")
    return f


# ------------------------------------------------------------------ report
def render(data, S, W, rounds, unb, F, tz):
    a = data["address"]
    ch = data["chain"]
    lines = [f"# 钱包操作复盘 {ch} {a[:6]}…{a[-4:]}", "",
             f"- 地址：`{a}`  链：{ch}  数据源：{data.get('source')}  抓取：{fmt_day(data.get('fetched_at', 0), tz)}",
             f"- 记录：买卖 {sum(t['side'] in ('BUY','SELL') for t in data['trades'])} 笔，token 互换 {sum(t['side']=='SWAP' for t in data['trades'])}，主币/稳定币互换 {sum(t['side']=='QUOTE_SWAP' for t in data['trades'])}，转账 {len(data['transfers'])}",
             f"- 时区：UTC{tz:+d}；金额单位 U（稳定币按 1，主币按 CoinGecko 当日价）", ""]
    lines += ["## 要点", ""] + [f"- {x}" for x in F] + [""]
    if W:
        lines += ["## 刷量 / 挂机（已剔除）", "", "| 币 | 配对 | 笔数 | 成交U | 净损益U | 单笔U | 间隔min | 天数 | 每天笔 | 区间 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for w in sorted(W.values(), key=lambda w: -w["volume"]):
            lines.append(f"| {w['token']} | {w['pairs']} | {w['trades']} | {w['volume']:,.0f} | {w['net']:+,.0f} | {w['size_med']:,.0f} | {w['gap_med_min']:.1f} | {w['days']} | {w['per_day_med']:.0f} | {fmt_day(w['first'],tz)}~{fmt_day(w['last'],tz)} |")
        lines.append("")
    lines += ["## 逐币轮次", "", "| 首买 | 币 | 买/卖笔 | 投入U | 收回U | 盈亏U | 盈亏% | 持仓h | 首卖h | 补仓 | 状态 | 卖后至今 | 进场距开池h |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rounds:
        st = "已平" if r["closed"] else ("转走" if r.get("moved") else f"持有 {r['left_qty']:,.0f}（{r['mtm']:,.0f}U）")
        if r.get("moved_out") and not r.get("moved"):
            st += f" 转走{r['moved_out']:,.0f}"
        se = f"{r['since_exit_pct']:+.0f}%" if r["since_exit_pct"] is not None else "-"
        la = f"{r['buy_after_launch_h']:.0f}" if r["buy_after_launch_h"] is not None else "-"
        pp = f"{r['pnl_pct']:+.0f}%" if r['pnl_pct'] is not None else "-"
        lines.append(f"| {fmt_ts(r['first_buy'],tz)} | {r['token'][:14]} | {r['n_buys']}/{r['n_sells']} | {r['invested']:,.0f} | {r['returned']:,.0f} | {r['pnl']:+,.0f} | {pp} | {r['hold_h']:.1f} | {r['to_first_sell_h']:.1f} | {r['adds_below_avg']} | {st} | {se} | {la} |")
    lines.append("")
    if unb:
        lines += ["## 卖出但没买过（空投/转入后卖出）", ""] + [f"- {fmt_ts(t['ts'],tz)} {t['token']} 卖出 {t['usd']:,.0f}U" for t in unb[:15]] + [""]
    if S.get("big_transfers"):
        lines += ["## 大额资金流（≥50U）", "", "| 时间 | 方向 | 币 | 金额U | 对手方 | 类型 |", "|---|---|---|---|---|---|"]
        for x in sorted(S["big_transfers"], key=lambda x: x["ts"]):
            lines.append(f"| {fmt_ts(x['ts'],tz)} | {'转入' if x['dir']=='in' else '转出'} | {x['token']} | {x['usd']:,.0f} | {x['counterparty'][:10]} | {x['kind']} |")
        lines.append("")
    if S.get("hours"):
        lines += ["## 时段分布（小时→笔数）", "", " ".join(f"{h}:{n}" for h, n in S["hours"].items()), ""]
    lines += ["## 逐笔明细（真实买卖，最近 80 笔）", "", "| 时间 | 方向 | 币 | 金额U | 数量 | 单价 |", "|---|---|---|---|---|---|"]
    real = [t for t in data["trades"] if t["side"] in ("BUY", "SELL") and t["token_addr"] not in W]
    for t in sorted(real, key=lambda t: -t["ts"])[:80]:
        px = t["usd"] / t["qty"] if t["qty"] else 0
        lines.append(f"| {fmt_ts(t['ts'],tz)} | {'买' if t['side']=='BUY' else '卖'} | {t['token'][:14]} | {t['usd']:,.0f} | {t['qty']:,.0f} | {px:.6g} |")
    lines.append("")
    return "\n".join(lines)


def analyze(data, tz=8, token_meta=None):
    trades = [t for t in data["trades"] if t["side"] in ("BUY", "SELL")]
    if token_meta is None:
        token_meta = data.get("token_meta")
    if token_meta is None:
        ch = data["chain"]
        ds = "solana" if ch == "solana" else EVM_CHAINS.get(ch, {}).get("dexscreener", ch)
        ids = sorted({t["token_addr"] for t in trades})
        try:
            token_meta = dexscreener_tokens(ds, ids)
        except Exception as e:
            log(f"[analyze] dexscreener failed: {e}")
            token_meta = {}
        # symbol-only ids (DeBank dump): search by symbol, only for tokens the wallet actually bought
        bought = {t["token_addr"] for t in trades if t["side"] == "BUY"}
        for i in ids:
            if i in bought and i not in token_meta and not looks_like_address(i):
                info = dexscreener_search(ds, i)
                if info:
                    token_meta[i] = info
    W = detect_wash(trades)
    real = [t for t in trades if t["token_addr"] not in W]
    rounds, unb = build_rounds(real, token_meta, data.get("transfers", []))
    S = stats(rounds, real, data.get("transfers", []), tz) if real else stats([], [], data.get("transfers", []), tz)
    F = facts(S, W, rounds, tz)
    report = render(data, S, W, rounds, unb, F, tz)
    summary = {k: v for k, v in S.items() if k not in ("big_transfers", "missed", "dodged", "repeat_tokens")}
    summary.update({"wash": W, "rounds": rounds, "facts": F, "address": data["address"], "chain": data["chain"]})
    return summary, report
