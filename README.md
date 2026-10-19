# 夜航社 Dealer GEX 异动期权捕捉与模拟复盘系统

纯 Python 3.10+ 标准库实现，零第三方依赖。模拟盘，不下真实订单。

## 快速开始

```bash
# 1. 配置 API Key（在 https://yehangshe.com/app/account/api 生成）
export YHS_API_KEY="sk_live_xxx"          # Windows PowerShell: $env:YHS_API_KEY="sk_live_xxx"

# 2. 免费验证 Key 与剩余配额（health + discover，0 配额）
python main.py --check

# 3. 本地模拟回放（仿夜航社服务器，0 配额，约 2 秒跑完 78 个 5 分钟步进）
python main.py --replay

# 4. 历史回放（真实数据；Heatmap 历史仅保留最近 3 个交易日）
python main.py --replay --source history --date 2026-10-09 --tickers NVDA,TSLA,AAPL

# 5. 实盘盘中监控（建议 09:20 ET 前启动，可常驻）
python main.py --notify-test                 # 先测试 Discord Webhook（config.json 的 discord_webhook_url）
caffeinate -i python3 main.py --live --log-file logs/live.log   # macOS：caffeinate 防止休眠；开仓/平仓/收盘复盘推送到 Discord

# 6. 盘前研究工作流：活跃榜 → Heatmap 关键节点 → Flow 确认，输出 long/short/range 观察名单
python main.py --premarket
python main.py --premarket --replay-audit yhs_data/premarket/<run_id>   # 用审计目录离线重放（0 配额，调参用）
python -m unittest test_premarket -v                                   # 离线测试（0 配额）
```

也可以用 `--config my.json` 覆盖任意配置项（见 `config.example.json`，字段与 `SystemConfig` 同名）。

## 与需求文档不同之处（按真实 API 调整）

需求文档中的端点名与夜航社 OpenAPI 不一致，代码按真实接口实现：

| 用途 | 需求文档写法 | 真实端点（已采用） |
|---|---|---|
| 阶段1 榜单 | `/options/volume-top-150-with-oi?limit=100` | `GET /v1/market/options-volume-rank`（固定 Top150，不接受 limit，本地截取前 100） |
| 阶段2 Heatmap | `/heatmap/snapshot?ticker=` | `GET /v1/derived/heatmap/{ticker}/snapshot` |
| 阶段3 Flow | `/flow/trades?ticker=&min_premium=` | `GET /v1/flow/trades?tickers=&min_premium_usd=` |
| 阶段4 期权链 | — | `GET /v1/options/chain-snapshot/{ticker}?expiration=` |
| 鉴权 | — | `Authorization: Bearer <key>` |

**榜单不含股价**：$20 门槛改为在每只标的首次拉取 Heatmap 时用 `spot_usd` 惰性校验（反正要拉，零额外配额），低于门槛立即移出标的池。

## 我做的几个解读（可在 config 中改）

- **初筛逻辑**：量能异动（Vol/OI ≥ 0.65 或 OI 增幅 ≥ 5%）**且** PCR 极端，两者都满足才入池（`require_volume_anomaly` / `require_pcr_extreme`）。（PCR 偏向只作标签，不限制做多/做空；`pcr_direction_filter` 默认关闭。）实盘默认 A、B 两套账户同时运行（`live_universe_mode="both"`）：A 严格按上述初筛，B 去掉量能异动与 PCR 条件；Discord 消息分别带 `🅰️【A·严格异动】` / `🅱️【B】`。
- **到期日**：有 0DTE 用 0DTE，否则 1DTE（交易日口径，周五→周一算 1DTE）；都没有则该标的不交易（`expiry_max_trading_dte`）。
- **买入行权价**：目标仍是负 GEX 节点（`strike_mode="target_node"`），但最多虚值 0.5%（`max_otm_pct`）：在 Heatmap 当前到期的挂牌行权价中，选虚值不超过 0.5% 且离节点最近的一个（不取整，不会造出不存在的合约）。真实 |delta| < 0.15（`min_entry_delta`）或期权价 < $0.05（`min_option_price`）不开仓。改为 `"atm"` 则买平值。
- **开盘观察期**：09:45 ET 前不开新仓（`no_new_entries_before`）。
- **止损**：标的反向穿越结构的正 GEX 节点（看涨地板/看跌天花板）再多 0.5% 才止损，期权另设 -60% 灾难止损（`stop_mode="underlying"`、`underlying_stop_buffer_pct`、`disaster_stop_pct`）；+35% 保本、+60% 追踪（回撤 20 个百分点）、+150% 止盈不变。设 `stop_mode="option"` 回到期权 -35% 止损。
- **信号筛选**：每轮先收集所有信号，按信号评分（0–1：加速燃料 30%、地板厚度 20%、贴近正节点 20%、目标可达 20%、节点新鲜 10%）从高到低填空位；评分 ≥ 0.5 才开仓，每个账户每天最多 15 笔（`signal_ranking`、`max_trades_per_day`、`min_signal_score`、`score_weights`）。评分尚未经数据验证。
- **历史回放**：Heatmap 本地存档优先（`archive.py`，见 HANDOFF.md §7）；持仓在 5 分钟快照之间逐分钟盯市（`intraday_mark_min=1`，greeks 用 1m 粒度，0 额外配额）。
- **Flow**：Flow API 修复中，默认跳过阶段 3（`require_flow_confirm=false`）。
- **Greeks**：期权链返回 delta/gamma 时用真实值，否则用文档默认 δ=0.45、γ=0.04（`use_chain_greeks`）。高价股用固定 γ=0.04 会明显高估凸性，建议保留真实值。
- **追踪止盈"回撤 20%"**：解读为从峰值 ROI 回撤 20 个**百分点**（峰值 +80% → +60% 平仓）。
- **午间 12 分钟**：Heatmap 是 5 分钟固定采样，轮询对齐到采样网格，"≥12 分钟"实际落在每 15 分钟一次。**持仓标的始终 5 分钟盯市**，风控优先。
- **Flow 重查节流**：Flow 未确认后同一标的 10 分钟内不重查（小于 15 分钟窗口，不会漏单）。模拟回放中 Flow 调用从 153 次降到 96 次，信号不变。
- **节点疲劳**：现货进入正节点 ±0.4% 记一次测试，须离开 ≥0.8% 后再回来才算新一次；>3 次放弃。

## 需要你首次实盘时确认的地方

OpenAPI 只公开了 Heatmap 的精确 schema。**成交量榜单、Flow 成交行、期权链行**的字段名未公开，代码用多候选字段名容错解析（如 `premium_usd`/`premium`、`side`、`open_interest.total.current` 等）。若某字段没解析到，日志会打印该行的实际字段名。建议第一次运行 `--live` 前先盯一下日志的阶段1输出；有不匹配的告诉我字段名即可调整。

Flow 的 `sides=ask` / `rights=C` 枚举值同样未公开：若服务端返回 400（免费），自动改为客户端过滤，不影响结果。

## 配额预算

| 项目 | 估算 |
|---|---|
| 每标的每日 Heatmap | 约 54 次（早盘 24 + 午间 12 + 尾盘 18） |
| 40 只 × 21 个交易日 | 约 45,000 次 / 月（2026-10-19 起由 60 只降到 40 只；持仓另外每分钟盯一次实时报价） |
| Flow + 期权链 + 榜单 | 预留 15% |

启动时用免费的 `discover` 读取剩余配额，按"剩余配额 ÷ 本月剩余交易日"自动压缩当日标的池；剩余 < 5,000 时池子再减半；剩余 ≤ 1,500 时本地熔断，清平模拟持仓并导出。

## 盘前研究工作流（`--premarket`）

单次运行、不轮询、不建定时任务。约 `4 × 候选数 + 3 × 入围数` 次计费调用（候选 ≤40、入围 10–15）。

1. **启动**：`discover`（免费）读取配额、Heatmap working_set、能力可用性；免费 `openapi.json` 核查命令参数；NYSE 日历（含假日/提前收盘）确定目标交易日与上一完整交易日。
2. **候选池**：成交量榜前 30 + OI 变化显著 10（OI 基数 ≥5 万），与 working_set 取交集，上限 40；分普通股票 / 指数ETF / 其他ETF / 杠杆反向ETF。榜单若不是上一完整交易日，优先用本地归档 `yhs_data/rank_archive/`，都没有则标 `reference_only`。OI `change_pct` 单位用 `change/previous` 核实；相对成交量仅在本地归档 ≥5 日时计算。
3. **价格 / Heatmap**：只有 60 秒内的 NBBO 才算可信现价；其次是本时段内的已完成 5 分钟 K 线（降级）；否则 `quote_unavailable`，位置以昨收为参考。从不使用 Heatmap 内部 `spot_usd`。删除早于目标交易日的到期列；ATR14 用常规时段日线计算。
4. **Wall**：按到期取最强正节点，近端（0–7 DTE）优先取前 3，加上现价最近的正节点；锚点只取显著节点（≥25% 近端最强正节点）。记录前收 → 昨收 → 现价相对锚点的位置、上下方下一显著正节点、同价位正负冲突。
5. **Flow**：`ticker_overview`（上一完整交易日，1d / 7d，`single_leg_only=true`）+ `flow.trades` 第一页样本 + 关键合约 `contract_history`；计算 `direction_balance` 并降级（组合占比、单笔主导、深度 ITM 远期）。**任一 flow.* 返回 403 时整组 Flow 能力停用，`flow_support=unknown`，状态最多到 `candidate`。**
6. **分类**：A reclaim_long / B loss_short / C、D 负节点加速 / E failed_breakout（仅开盘后可判）/ F 正节点区间。
7. **开盘确认**：09:35 / 09:40 / 09:45 后重新运行即可，自动检查已完成 5 分钟 K 线（2 根在容差带外 + 回测守住）与 `flow.ticker_bucket`（`bucket_start_micros` 为 UTC epoch 微秒）。

输出：`reports/premarket_<目标日>_<运行时刻>.md`（中文报告，末尾显示 `quota.monthly_remaining`），`yhs_data/premarket/<run_id>/audit.json` + `responses/`（全部业务响应、参数、内容时间、候选清单；不含 Key）。全部阈值是 `SystemConfig` 里 `pm_*` 开头的待验证参数，可用 `--config` 覆盖。

## 文件

| 文件 | 内容 |
|---|---|
| `config.py` | `SystemConfig` 全部参数、ETF 白名单/指数黑名单、美东时间工具（不依赖 tzdata，Windows 可直接跑） |
| `yhs_client.py` | 50 RPM 滑动窗口、1.2s 串行间隔、并发 ≤3、202 免费退避重试、429/503 处理、配额头熔断 |
| `detector.py` | 阶段1 标的池、阶段2 跳板/悬崖/疲劳、阶段3 Flow 确认、`evaluate_full_signal` |
| `paper_trader.py` | 开仓定价、Delta-Gamma 盯市、两阶出场状态机、统计、`export_trades_csv`、`SessionEngine` |
| `mock_replay.py` | 本地仿 YHS 服务器 + 10 个剧本，覆盖每条逻辑分支 |
| `main.py` | `--live` / `--replay` / `--check` / `--premarket` 入口、预算守卫、历史回放 |
| `premarket_workflow.py` | 盘前研究工作流：NYSE 日历、候选池、价格可信度、Wall/负节点分析、setup 分类、Flow 复核、开盘确认、审计重放 |
| `premarket_report.py` | 中文 Markdown 报告与审计 JSON |
| `test_premarket.py` | 盘前工作流离线测试（假客户端，覆盖 A/B/C/E/F、Flow 403 降级、开盘确认） |

输出：`trades_history.csv`（实盘按日追加），`yhs_data/universe_YYYY-MM-DD.json`（当日标的池缓存，重启不重复扣费）。

## 局限

- 节假日：不内置交易日历；当 Heatmap 的 `session_date_et` 不是今天时判定休市并跳过（耗 1 次）。
- 历史回放没有历史期权链快照，开仓价用 BS（IV 默认 45%）换算。
- Delta-Gamma 近似未计入 Theta 与 IV 变化，日内持仓误差可接受，持仓越久越偏乐观。
