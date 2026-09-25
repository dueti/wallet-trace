---
name: wallet-trace
description: >-
  链上钱包操作复盘：给一个钱包地址（EVM 0x… 或 Solana），拉全部买卖记录，自动剔除刷量/挂机单，
  按币拆成轮次算盈亏、持仓时长、仓位分档、止盈止损习惯、补仓、复做、时段、资金进出、卖飞/躲过，
  输出 report.md，再由 Claude 归纳成"这个人的出手路线/交易思路"。当用户给出地址并说"看看他的操作/出手路线/
  学习一下/复盘一下这个钱包/他是怎么做的/聪明钱分析"时触发。支持 bsc / eth / base / arb / solana。
---

# wallet-trace 钱包操作复盘

一句话：**先跑脚本拿到事实（轮次表 + 要点），再用固定框架把事实讲成"他的打法"。不要凭印象编。**

## 用法

```bash
python3 ~/.claude/skills/wallet-trace/scripts/wallet_trace.py <address>            # 自动识别链
python3 ~/.claude/skills/wallet-trace/scripts/wallet_trace.py <address> --chain bsc --days 120
python3 ~/.claude/skills/wallet-trace/scripts/wallet_trace.py <address> --no-fetch  # 只重算，不重新抓
```

参数：`--chain auto|bsc|eth|base|arb|solana`、`--days 180`（回看天数，0=全部）、`--tz 8`（时段统计用的时区）、
`--out ~/Desktop/wallet-trace`、`--max-tx 3000`（Solana 上限）、`--refresh`（忽略 1 小时缓存）、`--quiet`。

输出目录 `~/Desktop/wallet-trace/<chain>-<addr前6>-<后4>/`：`trades.json`（统一格式的逐笔）、`summary.json`（数字 + facts）、`report.md`（给人看）。
脚本会把 report.md 打到 stdout，直接读它写结论。整个目录可随时删，重跑即重建；`~/.cache/wallet-trace/` 是价格和 Solana 交易缓存，也可删。

## 数据源与 key（按优先级）

| 链 | 数据源 |
|---|---|
| eth / arb | Etherscan V2（免费 key，已验证）；没 key 走 Blockscout 公共 API（已验证） |
| base | Blockscout 公共 API（已验证，无 key）。Etherscan 免费版不覆盖 base |
| bsc | 没有免费索引 API（Etherscan 免费版不覆盖，Moralis 已转付费）。**`--scan`：纯公共 RPC 扫日志，无 key、全量、可断点续跑，约 1 小时/70 天**；急用就走 DeBank 兜底（只有最近约 1000 条）；有付费 key（Moralis / Etherscan 付费版）则直连 |
| solana | 公共 RPC（已验证，慢：每笔 0.35s；设 `SOLANA_RPC_URL` 用 Helius 等更快） |

key 放三处任一：环境变量 `ETHERSCAN_API_KEY`、文件 `~/.config/wallet-trace/etherscan.key`、钥匙串 service `etherscan-api`。
付费 key 可选：Moralis 放 `MORALIS_API_KEY` 或 `~/.config/wallet-trace/moralis.key`；Etherscan 付费版加 `ETHERSCAN_PAID=1`。

### BSC 无 key 全量：`--scan`（已验证）

```bash
python3 scripts/wallet_trace.py <addr> --chain bsc --scan --days 80
```
原理：bloXroute 公共节点允许按 topic 过滤、一次 5000 块的 `eth_getLogs`，扫钱包作为 from/to 的所有 ERC20 Transfer，
再逐笔拉 receipt 还原：token 腿来自 Transfer 事件，付出的 BNB 来自 tx.value，收到的 BNB 来自同一笔里 WBNB 的 Withdrawal 事件
（路由先 unwrap 再转给用户，和 DeBank 显示的到账只差路由手续费）。BSC 现在约 19 万块/天，每窗口约 2 秒，70 天约 1 小时；
中间断了重跑同一命令会接着扫（缓存在 `~/.cache/wallet-trace/rpc/`）。看不到的：纯 BNB 转账（充提）、失败交易、
bonding curve 合约直接付 BNB 的卖出（会标 `unpriced`，usd=0）。适合挂后台跑，跑完再 `--no-fetch` 重算。

### BSC 急用时的 DeBank 兜底（已验证）

1. 浏览器打开 `https://debank.com/profile/<addr>/history?chain=bsc`（Claude Code 用 Browser pane，人工用 DevTools Console 也一样）。
2. 在控制台反复执行下面这段点 Load More（Claude 的 javascript_tool 每次 ≤22 下，否则 45s 超时）：
   ```js
   const sleep=ms=>new Promise(r=>setTimeout(r,ms));
   const btn=()=>[...document.querySelectorAll('div,button,span')].find(e=>e.children.length===0&&e.textContent.trim()==='Load More');
   let n=0; for(let i=0;i<22;i++){const b=btn(); if(!b) break; b.click(); n++; await sleep(1300);}
   const t=document.querySelector('main').innerText; const d=[...t.matchAll(/\d{4}\/\d{2}\/\d{2}/g)];
   ({n, last:d[d.length-1]?.[0], len:t.length})
   ```
   匿名访问大约翻到 1200 条就停了（约 2–3 个月），再点没用。
3. 取正文存成文件：`copy(document.querySelector('main').innerText)` 粘贴到 dump.txt；Claude 环境下用 `.slice(0,66000)` / `.slice(66000)` 分两次返回（超长结果会被存成 JSON 字符串文件，脚本能直接吃），再拼成一个文件。
4. 跑：`wallet_trace.py <addr> --chain bsc --debank-dump dump.txt --dump-time "YYYY-MM-DD HH:MM"`（dump-time 用来换算"x hrs ago"）。
   注意 DeBank 只给币名不给合约，同名币会被合并；报告里会标注。实测 Claude Browser pane 里 DeBank 显示的时间比北京时间快 1 小时，`--tz 9` 才对得上链上时间。

## 报告怎么读、怎么讲

脚本已经做了：剔除刷量（同币买卖同量、几分钟内来回、成对 ≥8 且占该币交易 ≥60%，典型是币安 Alpha 刷分）；
按"仓位归零"拆轮次；每轮算投入/收回/盈亏/持仓时长/首卖时间/补仓次数；DexScreener 拿现价算"卖后至今"和"进场距开池"。

把 `## 要点` 的 facts 讲成人话，按这个框架组织（每条都要落到数字，缺就说缺）：

1. **先说这是不是判断性交易**：刷量占比多少、真实出手多少个币、总盈亏。很多"活跃地址"扣掉刷分后只剩十几笔。
2. **仓位两档还是一档**：探路单尺寸、主仓尺寸、最大敞口；利润是否集中在一两笔重仓（top_win_share > 60% 就是"小单买信息、一次重仓吃饭"）。
3. **节奏**：持仓中位、1h/24h 内了结比例；是否过夜拿亏损单。
4. **止盈止损习惯**：赢单中位涨幅（不贪/贪）、亏单中位跌幅和最差一笔、亏单多快割；有没有补仓摊低的习惯及其结果。
5. **分批和复做**：每轮几笔进出；同一币做几轮、后面几轮是否还赚。
6. **卖飞 vs 躲过**：卖后又涨的和卖后归零的各几个，说明他偏向"接受错过"还是"拿到头"。
7. **入场时点**：距开池多久进（狙击新币 vs 二波/老币）。
8. **时段和资金流**：活跃时段（换算成用户时区）、钱从哪来回哪去（进出 CEX 的节奏 = 落袋习惯）。
9. **最后一段给"可学的"和"不可学的"**：哪些是纪律（可复制），哪些是运气或信息优势（不可复制）。

写法：中文口语，结论先行，不要报告体；先给一句总判断，再一个轮次小表，再 5–8 条打法要点，末尾说数据局限
（回看范围、同名币合并、时区、主币按当日价折算）。用户如果只是随口问"他最近干嘛"，给三句话就够，别甩全表。

## 已知限制

- 回看范围：DeBank 兜底 ≈ 最近 1200 条；Blockscout/Etherscan 受 `--days` 控制；Solana 受 `--max-tx`。
- 主币计价用 CoinGecko 当日收盘价，不是成交时刻价，单笔误差可到几个百分点。
- 多币种同笔（一笔买入到账两个币）取"该钱包后来卖过的那个"为目标币，剩下的当灰尘。
- 跨链桥、借贷、LP、NFT 一律不识别，只会落到转账或 SWAP 里。
- 同一 symbol 不同合约在 API 路径下按合约区分，DeBank 路径下会合并。

## 文件

```
~/.claude/skills/wallet-trace/
  SKILL.md
  scripts/wallet_trace.py   入口：识别链 → 抓取 → 分析 → 写报告
  scripts/fetch_evm.py      Etherscan V2 / Blockscout 抓取 + 交易重建
  scripts/fetch_solana.py   Solana RPC 抓取 + 交易重建（带本地缓存）
  scripts/parse_debank.py   DeBank 页面文本 → trades.json
  scripts/analyze.py        刷量识别、轮次、统计、facts、report.md
  scripts/prices.py         CoinGecko 主币价、DexScreener 代币信息
  scripts/common.py         链配置、HTTP/RPC、统一 schema
```
纯标准库，无第三方依赖。
