# 交接文档：夜航社 (YHS) Dealer GEX 期权系统

> 截至 2026-10-11（周日）晚。写给接手的 Claude session：读完本文即可继续工作，不需要原始对话。
> **用户偏好：所有回复用中文。**

---

## 1. 项目是什么

- 需求来源：用户的规格文档「基于heatmap构造一套可迭代的自动化期权系统-中文版.txt」（原件在用户的 Downloads 里）。要点：
  - 基于夜航社 API（https://docs.yehangshe.com/api ，OpenAPI：`https://api.yehangshe.com/v1/openapi.json`，免费）；
  - **每月 10 万次配额**，只有 HTTP 200 扣 1 次；202/4xx/5xx/health/discover 免费；限频 50 RPM、间隔 ≥1.2s、并发 ≤3；
  - **只用 Python 标准库**（不能 pip 装任何东西）；
  - 4 个阶段：①成交量 Top100 标的池过滤（ETF 白名单 SPY/QQQ/TLT/SOXL/TQQQ、股价 ≥$20、Vol/OI、PCR）；②Heatmap 跳板/悬崖结构识别 + 节点疲劳过滤；③Flow 大单确认（≥$50k Ask 侧、当日量 ≥ OI）；④模拟盘：-35% 止损、+35% 保本、+60% 追踪（回撤 20 个百分点）、+150% 硬顶、16:00 强平，导出 `trades_history.csv`；
  - 模块：`config.py` / `yhs_client.py` / `detector.py` / `paper_trader.py` / `mock_replay.py` / `main.py`。
- 代码最初由另一个 Claude 会话生成（zip：`yhs_gex_system 2.zip`），本 session 负责运行、修 bug、接真实数据、回测和建存档。

## 2. 位置与环境

- 项目目录：`/Users/vince/Documents/yhs_gex_system/yhs_gex_system/`（外层 `Documents/yhs_gex_system/` 还有个解压残留的 `__MACOSX`，可删）。
- **不是 git 仓库**（截至本文写作时）。
- macOS，用户时区 **PDT**（美东 = 太平洋 +3 小时）。
- **机器上只有 Python 3.7**（Anaconda 3.7.4 / 系统 3.7.3），规格要求 3.10+。已做兼容（见 §4），用 `python3` 运行即可。
- **API Key 在 `config.json` 的 `api_key` 字段**（`sk_live_...`，权限 600）。`SystemConfig.load()` 在没传 `--config` 时会自动读取项目目录下的 `config.json`。**绝不能提交到 git / 不能出现在聊天里。** 用户的 Key 曾在聊天中明文出现过，已建议用户考虑在夜航社后台重新生成。

## 3. 已验证的 API 事实（很重要，多数是踩坑得来的）

| 端点 | 结论 |
|---|---|
| `/v1/discover`、`/v1/health` | 免费；`--check` 用它们验证 Key 与配额 |
| `/v1/derived/heatmap/{t}/snapshot` | 返回 `ticker, generated_at, session_date_et, spot_usd, expirations[], cells[{strike_usd, expiration, net_dealer_gex_usd}]`；`expirations` 列出所有可用到期日（SPY/QQQ 每天都有到期日） |
| `/v1/derived/heatmap/{t}/history` | **服务端只保留最近 3 个交易日**。参数 `expiration`(必填)、`from`、`to`、`limit`(24)、`cursor`。**`from`/`to` 只传日期会返回空集，必须传带时区的完整时间**（如 `2026-10-09T09:30:00-04:00`）。每页 24 行，全天 78 个 5 分钟快照≈4 页，最新的在前。**0DTE 全天都有节点，只有收盘前最后一个快照（15:55）cells 为空** |
| `/v1/flow/trades` 等 Flow 端点 | **当前账户 403 `FLOW_ACCESS_RESTRICTED`**。用户说 Flow API 正在"回炉重修"，尚未修好 |
| `/v1/options/contract-intraday/{OCC}` | 合约代码**不能带 `O:` 前缀**（如 `NVDA261016P00220000`）。1 分钟 bar（OHLC、IV、Bid/Ask/Mid 各侧成交量与权利金）。**只有最近一个交易日，且只返回最新 150 根（约收盘前 2.5 小时）**，无分页、`limit` 参数会 400。用户说该数据 15 分钟刷新一次 |
| `/v1/options/contract-greeks-series/{OCC}` | 参数 `from`、`to`(日期)、`interval`(1m/5m/10m/15m/30m/1h)。返回 delta/gamma/vega/theta/charm/`implied_vol_pct`(实为小数，如 0.31)/`underlying_price_usd`，`snapshot_at` 是**不带时区的美东时间**。**`interval=1m` 一次请求返回全天 391 点，配额与 5m 相同**。**历史可回溯 9 个月以上**（2026-01-09 也能拿到）。**09:30 那个点是坏数据**（IV 仅 6%~12%、delta≈0），必须丢弃。用"BS + 该接口的真实 IV + 真实标的价"算出的期权价与真实成交价误差约 1–2 美分（已抽样 19 个时点验证） |
| 503 `READ_MODEL_UNAVAILABLE` "This key recently failed to materialize" | 通常是请求了不存在的合约，重试无意义 |
| 周末请求 | 会带 `notice`：数据可能过期（data_freshness 很大），对回放上一交易日没有影响 |

## 4. 本 session 做过的全部代码改动

1. **Python 3.7 兼容**：`config.py setup_logging` 去掉 `basicConfig(force=True)`，改为手动清理 root handlers。
2. **自动读取 config.json**：`SystemConfig.load()` 未指定路径时读项目目录 `config.json`。
3. **历史回放 Heatmap 时间参数**：`from`/`to` 改为带时区的 09:30–16:00 ET（现在实现在 `archive.py fetch_heatmap_rows`）。
4. **到期日**：曾错误地把周五改成取下周五，**已撤回**。`main.py nearest_friday` = 回放日所在周的周五（周五当天即 0DTE）。
5. **仅 Heatmap 模式**：`config.require_flow_confirm`（默认 True；**用户的 `config.json` 里设为 false**）。False 时跳过阶段3，结构成立即开仓。
6. **Flow 权限错误不再重试**：`detector.py` 收到 401/403 后置 `flow_disabled=True`，本会话不再请求 Flow，并提示切换仅 Heatmap 模式。
7. **历史回放用真实期权价格**（`config.use_real_option_pricing=True`）：`main.py HistoryClient.option_price()` 两层价格源——5 分钟内有 contract-intraday 真实成交 bar 就用成交价；否则用 contract-greeks-series 的真实 IV + 标的价算 BS。`PaperTrader.price_provider` / `delta_provider` 注入；`price_source="real_data"`。开仓时当刻无报价则按 5 分钟内下一个可用报价成交。回放日不是最近交易日时，contract-intraday 发现无数据后不再请求。
8. **行权价不再取整**：`paper_trader.open_position` 在 `strike_mode="target_node"` 时直接用 Heatmap 节点行权价（之前把 COIN 192.5 取整成不存在的 192）。
9. **`yhs_client.py`**：503 且消息含 "recently failed" 时直接报错不重试。
10. **丢弃 09:30 greeks 坏点**；`config.min_option_price=0.05`（期权价低于此值不开仓）。
11. **1 分钟盯市**：`config.greeks_interval="1m"`、`config.intraday_mark_min=1`。历史回放在两个 5 分钟 Heatmap 快照之间逐分钟检查持仓的止损/保本/追踪（0 额外配额）。开仓信号仍按 5 分钟。
12. **开仓过滤**：`config.min_entry_delta=0.15`（真实 |delta| 低于此值不开仓，0=关闭）；`config.no_new_entries_before="09:45"`。**注意：这两个现在是默认开启的，但回测显示效果不明（见 §6），用户尚未决定保留还是关掉。**
13. **每日存档**：新增 `archive.py`（见 §7）；`main.py HistoryClient.load_heatmap` 改为通过 `archive.get_heatmap_rows()` **本地存档优先**，没有才调 API 并顺手存档。
14. `config.py` 新增 `archive_tickers`（17 只）、`archive_max_dte=7`。

> mock 回放（`python3 main.py --replay`）每次改动后都跑过，能正常完成。注意：mock 数据是写死的剧本（如 NVDA 现货 186），**它的盈亏没有参考意义**，只验证流程。

### 已知小问题（未修）
- `main.py` 遇到 API 错误时退出码仍为 0。
- 历史回放的交易 CSV 是追加写入（`append=True`），重跑前要先删/改名旧文件。
- 补存过去日期时，个股"当天周三到期的 0DTE"会漏存（如 NVDA 10-07 当日到期）；每天收盘后正常运行不受影响。
- Flow 恢复后，原四阶段流程还没在真实数据上验证过。

## 5. 常用命令

```bash
cd /Users/vince/Documents/yhs_gex_system/yhs_gex_system
python3 main.py --check                      # 免费：验证 Key 与配额
python3 main.py --replay                     # mock 回放（0 配额，只验证流程）
python3 main.py --replay --source history --date 2026-10-09 \
  --tickers SPY,QQQ,TQQQ,SOXL,NVDA,TSLA,AAPL,AMD,META,MSFT,AMZN,GOOGL,AVGO,PLTR,MSTR,COIN,NFLX \
  --csv trades_history_real.csv              # 真实数据历史回放
python3 archive.py                           # 存档最近收盘日 + 补齐近 3 个交易日缺口
python3 archive.py --dry-run                 # 只看计划
```

## 6. 回测结果演进（17 只标的，仅 Heatmap 模式，每笔 1 张；周四 10-08 用 1DTE，周五 10-09 用 0DTE）

| 版本 | 笔数 | 合计盈亏 | 去掉最大一笔 | 胜率 | 方向正确率 | 盈亏比 | 硬止损平均/最差 |
|---|---|---|---|---|---|---|---|
| 5 分钟盯市（`trades_history_real_5min.csv`） | 55 | +$853 | +$55 | 32.7% | 50.9% | 1.59 | -44.3% / -63.2% |
| 1 分钟盯市（`trades_history_real_1min.csv`） | 55 | +$803 | +$317 | 32.7% | 63.6% | 1.67 | -41.1% / -63.8% |
| 1 分钟 + Delta≥0.15 + 09:45 后开仓（`trades_history_real.csv`） | 49 | +$142 | -$208 | 30.6% | 59.2% | 1.12 | -38.7% / -52.0% |

结论与观察：
- 收益高度依赖少数 +150% 大单；样本只有两天，**噪音大于信号，暂不能下结论**。
- 0DTE 跳空：1 分钟内就能击穿 -35% 止损；周五止损多发生在开仓后 10–20 分钟。
- "买负 GEX 节点行权价"在 0DTE 上常选到深度虚值（delta≈0.001 的彩票合约）。
- 规则之间路径依赖强（每标的每日最多 2 笔、30 分钟冷却、最多同时持仓 5 笔），改一条规则后续开仓全变；09:45 限制只是把"开盘一次性开满 5 仓"推迟到 09:45。
- 早期结论"Delta-Gamma 估算定价"严重失真（如 NVDA 220P 估算 +30.9%，真实 +0.3%）——已全面改用真实期权数据定价。

## 7. 每日存档（已上线）

- `archive.py`：存每只标的**未来 7 天内全部到期日 + 下一个周五**的 Heatmap 全天快照 → `yhs_data/archive/heatmap/{日期}/{标的}/{到期日}.json`；完成标记 `_manifest.json`；当天成交量榜 → `yhs_data/archive/universe/{日期}.json`。幂等；默认补齐最近 3 个交易日缺口；复用 `premarket_workflow.py` 里的 NYSE 交易日历（含节假日）。
- **已存档：2026-10-07（周三）、10-08（周四）、10-09（周五）**，17 只标的全部完成（每个到期日 77–78 个快照），消耗 879 次配额（≈290 次/天，≈6% 月配额/月），3 天 18 MB。
- **期权价格不需要存档**（greeks-series 可回溯 9 个月以上）。
- **定时任务**：Claude 桌面 app 的 scheduled task `yhs-heatmap-daily-archive`（标题"【每日收盘】夜航社 Heatmap 存档"），cron `30 13 * * 1-5`（本地 PDT 13:30 = 美东 16:30），首次运行 2026-10-12 周一。**只在 Claude app 开着时运行**（错过会在下次启动时补跑）；首次可能需要批准工具权限（可在侧边栏点 "Run now" 预批准）。任务文件：`/Users/vince/.claude/scheduled-tasks/yhs-heatmap-daily-archive/SKILL.md`。

## 8. 其他会话加入的文件（不是本 session 写的，未改动）

`premarket_workflow.py`、`premarket_report.py`、`test_premarket.py`、`reports/`、`logs/premarket.log`、`yhs_data/premarket/`、`yhs_data/rank_archive/`：盘前研究工作流（活跃榜 → Heatmap 关键节点 → Flow 确认，输出 long/short/range 观察名单，只出报告不下单），`main.py --premarket` 入口和 README 相应章节也来自那个会话。

## 9. 配额

截至本文：服务端剩余 **97,628 / 100,000**（本月）。

## 10. 下一步（用户尚未选择）

1. 用周三~周五 3 天存档，**一次只改一个变量**做参数对比，并放宽 `max_open_positions` 以减少路径依赖。候选变量：
   - 到期日至少 1DTE/2DTE（避开 0DTE）；
   - `strike_mode: atm`（平值）替代买远端节点；
   - 保本线从 0% 改为锁定 +10%；
   - 是否保留 `min_entry_delta` / `no_new_entries_before`。
2. 等存档积累几周后再做有统计意义的回测。
3. `--live` 实盘模式接真实期权价格：greeks-series 每持仓每分钟 1 次配额（5 仓全天≈2000 次/天，太贵）；更省的方案是持仓期间拉 `/v1/stocks/quote/{ticker}`（未测刷新频率）用 Delta/Gamma/Theta 估算。
4. Flow 修好后：把 `config.json` 的 `require_flow_confirm` 改回 true，验证完整四阶段流程。contract-intraday 的 Ask 侧逐分钟权利金可作为 Flow 的粗略替代，但只覆盖最后 150 分钟。

---

# 附录：云端 session 进展（2026-10-12，分支 `claude/yehangshe-mcp-backtest-8u54ni`）

> 云端仓库：`VinceChen0116/yehangshe-vince`。**本地代码（`yhs_gex_system_2_1.zip`）已与云端改动三方合并，仓库即为唯一最新版本**；本地目录请以仓库为准（`config.json` 不在仓库里，需自行保留）。
> 本地 §4 全部改动（1 分钟盯市、`min_entry_delta`、09:45、`archive.py` 与 10-07~10-09 存档、503 不重试、09:30 坏点等）都在仓库里。

## A. 云端新增（在本地版本之上）
1. Flow 默认关闭：`require_flow_confirm=False`、`pm_require_flow_for_confirm=False`（Flow API 修复中）。
2. **到期日：有 0DTE 用 0DTE，否则 1DTE**（用户 10-12 明确要求；交易日口径，周五→周一算 1DTE，含节假日）；都没有则该标的不交易。`config.expiry_max_trading_dte=1`，在 `detector.parse_heatmap` 实现；历史回放依次尝试回放日、下一个交易日两个到期（经 `archive.get_heatmap_rows`，存档优先；查询结果为空也会存档，下次不再扣配额）。**取代 `nearest_friday`**。注：NVDA 等个股有周一/三/五到期，所以周三也有 0DTE。
3. NYSE 日历从 `premarket_workflow.py` 移到 `config.py`（`premarket_workflow` 仍可导入同名函数，`archive.py` 改为从 `config` 导入），新增 `config.trading_dte()`。
4. **行权价**（合并本地"不取整"与云端"限制虚值"）：目标仍是负 GEX 节点，但在 Heatmap 当前到期的挂牌行权价（`HeatmapSnapshot.strikes`）中，选虚值 ≤ `max_otm_pct`(0.5%) 且离节点最近的一个；挂牌价不取整。没有挂牌列表时才按步长取整并退回上限内。
5. 合并时的取舍：开仓时间用本地 **09:45**；最低价格用本地 `min_option_price=0.05` + `min_entry_delta=0.15`（删去云端的 `min_entry_price_usd=0.20`）。
6. mock 回放的近端到期改为回放当天（0DTE）。
7. 新增 `test_trading_rules.py`（10 个测试，含 192.5 不取整）；`python3 -m unittest test_trading_rules test_premarket` 全部通过；vermin 确认运行时兼容 Python 3.7。
8. 云端容器的 API Key 在本地 `config.json`（gitignore，权限 600）。

## B. 云端回测（合并前的云端代码：5 分钟盯市、无 delta 过滤、09:40、$0.20 门槛、虚值 ≤0.5% 按步长取整）
15 只：SPY,QQQ,TLT,SOXL,TQQQ,NVDA,TSLA,AAPL,AMD,META,MSFT,AMZN,GOOGL,PLTR,AVGO；仅 Heatmap。
| 日期 | 笔数 | 合计 | 胜率 | 方向正确率 | 平均峰值 |
|---|---|---|---|---|---|
| 10-08 | 22 | -$456 | 32% | 64% | +32.8% |
| 10-09 | 25 | -$986 | 20% | 64% | +37.6% |
| 合计 | 47 | -$1,442 | 26% | 64% | +35.4% |

观察：方向对但出场亏（10 笔峰值 ≥+35% 最终全亏，保本线跳空）；硬止损 23 次；午间开仓胜率 13%。文件：`backtests/*_v2.{csv,log}`。与本地 §6（远端节点 +$853，去掉最大一笔 +$55）标的与盯市粒度不同，不可直接比较。**合并后的代码尚未重跑回测。**

## C. 下一步候选
- 用合并后的代码 + 10-07~10-09 存档重跑（0DTE 否则 1DTE、虚值 ≤0.5%、1 分钟盯市），作为新基准。
- 0DTE 专用出场规则（分批止盈、放宽硬止损或改为现货跌破正节点止损）、午间不开新仓——一次只改一个变量。
