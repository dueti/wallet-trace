# wallet-trace

给一个链上钱包地址，复盘这个人是怎么交易的：拉全部买卖记录，剔掉刷量/挂机单，按币拆成一个个"轮次"，算出仓位分档、持仓时长、止盈止损习惯、补仓和复做、卖飞与躲过、活跃时段、资金进出，最后生成一份 `report.md`。配合 Claude Code / Codex 使用时，AI 会按固定框架把这些事实讲成"他的出手路线"。

适合：学习聪明钱/群友的打法、核实某个"大神"到底赚没赚、看自己钱包的坏习惯。

支持链：BSC、Ethereum、Base、Arbitrum、Solana。纯 Python 标准库，无第三方依赖。

## 安装

这是一个 Claude Code / Codex skill，也可以脱离 AI 当纯命令行工具用。

```bash
# Claude Code
git clone https://github.com/dueti/wallet-trace ~/.claude/skills/wallet-trace
# Codex
git clone https://github.com/dueti/wallet-trace ~/.codex/skills/wallet-trace
```

装完在 Claude Code / Codex 里丢一个地址说「看看他的操作 / 复盘一下这个钱包」就会触发。

## 使用

```bash
python3 scripts/wallet_trace.py 0x...            # 自动识别链（逐条链查交易数）
python3 scripts/wallet_trace.py <solana地址>      # base58 地址自动走 Solana
python3 scripts/wallet_trace.py 0x... --chain eth --days 365
python3 scripts/wallet_trace.py 0x... --no-fetch  # 只重算，不重新抓
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--chain` | auto | `bsc` / `eth` / `base` / `arb` / `solana` |
| `--days` | 180 | 回看天数，0 = 全部 |
| `--tz` | 8 | 时段统计用的时区（小时偏移） |
| `--out` | `~/Desktop/wallet-trace` | 输出根目录 |
| `--max-tx` | 3000 | Solana 最多解析多少笔 |
| `--refresh` | | 忽略 1 小时缓存重新抓 |
| `--debank-dump FILE` | | BSC 兜底：用 DeBank 页面导出文本代替 API |

输出在 `~/Desktop/wallet-trace/<链>-<地址前6>-<后4>/`：

- `trades.json` 统一格式的逐笔买卖 + 转账
- `summary.json` 全部统计数字 + 中文事实句
- `report.md` 给人看的报告（要点、刷量识别、逐币轮次表、资金流、时段、逐笔明细）

整个目录随时可删，重跑即重建。`~/.cache/wallet-trace/` 是价格和 Solana 交易缓存，也可删。

## 数据源

| 链 | 数据源 |
|---|---|
| eth / arb | Etherscan V2（免费 key）；没 key 自动走 Blockscout 公共 API |
| base | Blockscout 公共 API（Etherscan 免费版不覆盖 base，脚本会自动回退） |
| bsc | **没有免费 API**：Etherscan 免费版不覆盖，BscScan 网页有人机验证。走下面的 DeBank 兜底，或付费版 Etherscan + `ETHERSCAN_PAID=1` |
| solana | 公共 RPC（慢，每笔约 0.35s；设 `SOLANA_RPC_URL` 用 Helius 等私有节点会快很多） |

Etherscan key 放任一处：环境变量 `ETHERSCAN_API_KEY`、文件 `~/.config/wallet-trace/etherscan.key`、macOS 钥匙串 service `etherscan-api`。

价格：主币（BNB/ETH/SOL）按 CoinGecko 当日价折算成美元；代币现价、市值、开池时间来自 DexScreener（按 24h 成交量选池，避开假池）。

### BSC 的 DeBank 兜底

1. 浏览器打开 `https://debank.com/profile/<addr>/history?chain=bsc`
2. DevTools Console 里反复执行，把历史翻到足够久（匿名大约能翻 1200 条，约 2 到 3 个月）：
   ```js
   const sleep=ms=>new Promise(r=>setTimeout(r,ms));
   const btn=()=>[...document.querySelectorAll('div,button,span')].find(e=>e.children.length===0&&e.textContent.trim()==='Load More');
   for(let i=0;i<22;i++){const b=btn(); if(!b) break; b.click(); await sleep(1300);}
   ```
3. `copy(document.querySelector('main').innerText)` 粘贴存成 `dump.txt`
4. `python3 scripts/wallet_trace.py <addr> --chain bsc --debank-dump dump.txt --dump-time "2026-01-01 12:00"`

DeBank 只给币名不给合约地址，同名币会被合并，报告里会标注。

## 报告里有什么

- **刷量识别**：同一个币买卖同量、几分钟内来回、成对 ≥8 笔且占该币交易 ≥60%，判为刷积分/刷量（典型是币安 Alpha），单独列出并从统计里剔除。很多"活跃地址"扣掉之后只剩十几笔真实出手。
- **轮次**：从建仓到仓位归零算一轮。每轮给投入、收回、盈亏、持仓时长、首次卖出时间、低于均价的补仓次数；仓位被转走而非卖出的轮次标"转走"，不计胜负。
- **统计**：胜率、总盈亏、利润集中度（最大一笔占盈利的比例）、单轮投入中位/p90/最大、探路单占比、同时在场的最大敞口、持仓时长分布、赢单/亏单的中位涨跌幅、亏单割得多快、复做同一币几轮、卖后又涨超 1 倍（卖飞）和卖后跌超 70%（躲过）、进场距开池多久、活跃时段和周节奏、大额资金进出。

## 已知限制

- 主币按当日价而非成交时刻价折算，单笔误差可到几个百分点。
- 一笔到账多个币时，取该钱包后来卖过的那个为目标币，其余当灰尘。
- 跨链桥、借贷、LP、NFT 不识别，只会落到转账或 token 互换里。
- Solana 公共 RPC 有速率限制，几千笔要跑几分钟；交易会缓存在本地，重跑是增量。

## License

MIT
