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
| Heatmap 更新时间（2026-10-13 开盘实测，见 logs/heatmap_open_probe_2026-10-13.log） | **只在常规时段更新，无盘前/盘后数据**。开盘前（实测 09:22–09:34 ET）与收盘后，snapshot 返回 **503 READ_MODEL_UNAVAILABLE**（不计费，客户端会重试约 30 秒），**不是**上一交易日的快照。当天第一个快照（`generated_at` = 09:30:00 ET，即按 5 分钟段开始时间标记）在 **09:34:40–09:35:10 ET 之间**首次可取；之后每 5 分钟一段（每天约 78 段）。所以 09:30 开盘那一刻拿不到当天数据，实盘从 09:35:25 开始拉取是最早可行的时间。开盘后 20 分钟内的旧日期快照不判定休市（`holiday_grace_min`，保留作保险）。收盘后快照不可用时，加速通道扫描改用 history 接口的收盘数据 |
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

观察：方向对但出场亏（10 笔峰值 ≥+35% 最终全亏，保本线跳空）；硬止损 23 次；午间开仓胜率 13%。文件：`backtests/*_v2.{csv,log}`。与本地 §6（远端节点 +$853，去掉最大一笔 +$55）标的与盯市粒度不同，不可直接比较。合并后的代码结果见 B2。

## B2. 合并后代码的回测（v3，2026-10-12）
17 只（archive_tickers），10-07~10-09；0DTE 否则 1DTE、挂牌行权价虚值 ≤0.5%、1 分钟盯市、δ≥0.15、09:45 后开仓；仅 Heatmap。消耗 183 次配额（Heatmap 走存档）。文件：`backtests/*_v3.{csv,log}`。
| 切片 | 笔数 | 合计 | 去掉最大一笔 | 胜率 | 方向正确率 | 盈亏比 |
|---|---|---|---|---|---|---|
| 全部 | 78 | +$82 | -$742 | 36% | 59% | 1.03 |
| 10-07 周三 | 24 | +$322 | +$124 | 42% | 67% | 1.66 |
| 10-08 周四（个股全为 1DTE） | 25 | +$267 | -$558 | 36% | 52% | 1.17 |
| 10-09 周五 | 29 | -$507 | -$635 | 31% | 59% | 0.46 |
| 0DTE / 1DTE | 57 / 21 | -$300 / +$382 | | 35% / 38% | | 0.81 / 1.26 |
| 早盘 / 午间 / 尾盘开仓 | 51 / 21 / 6 | +$770 / -$442 / -$245 | | 41% / 19% / 50% | | 1.46 / 0.51 / 0.42 |

- 出场：硬止损 42（平均 -38.0%，最差 -49.5%；5 分钟盯市时最差 -63%）、追踪止盈 23（平均 +52.5%）、保本 6、收盘强平 5、硬顶 2。
- 10-07：PLTR/MSTR/COIN/NFLX 无 0DTE/1DTE，按规则跳过；其余 13 只都有周三 0DTE。
- 结论：整体仍是盈亏平衡、靠单笔大赚；**午间开仓持续亏损**（两版回测一致）；硬止损占一半以上。

## C. 下一步候选
- 用合并后的代码 + 10-07~10-09 存档重跑（0DTE 否则 1DTE、虚值 ≤0.5%、1 分钟盯市），作为新基准。
- 0DTE 专用出场规则（分批止盈、放宽硬止损或改为现货跌破正节点止损）、午间不开新仓——一次只改一个变量。

## D. 实盘与云端定时任务（2026-10-12 建立）
- **实盘模式**：`python3 main.py --live --one-day`。A、B 两套独立模拟账户同时运行（`live_universe_mode="both"`）：
  - A（严格异动）= 成交量前 100 → 剔除指数/非白名单 ETF → 量能异动 且 PCR 极端（需求文档原文）；
  - B（宽松）= 成交量前 100 → 剔除指数/非白名单 ETF → 股价 ≥ $20；
  - 两者都**不限方向**（`pcr_direction_filter=False`，用户确认文档没有这条）；共用同一份 Heatmap（A 不额外耗配额）；Discord 标签 `🅰️【A·严格异动】` / `🅱️【B】`；A 交易明细 `*_A.csv`。
  - 实测：Heatmap 无盘前数据，首个快照 09:35 ET 才可用 → 实盘 09:35:25 开始拉取（修复了"开盘即判定休市退出"的 bug）。
  - 09:45 后开仓、15:30 后不开新仓、16:00 强平；同时持仓上限 8。午间：曾设 11:30–14:30 不开新仓，当天改回允许（`no_entry_windows=()`），由回测 big_s05_midday 对比决定。
  - 信号筛选：每轮按信号评分（0–1）从高到低开仓，评分 ≥ 0.5，每账户每天最多 15 笔（`signal_ranking`/`min_signal_score`/`max_trades_per_day`）。
  - 每小时 💓 心跳到 Discord。
- **Discord**：`notify.py`，Webhook 来自 `config.json` 的 `discord_webhook_url` 或环境变量 `DISCORD_WEBHOOK_URL`；`python3 main.py --notify-test` 测试。回测实验（`experiment.py`）跑完也会推送对比表。
- **云端环境变量**（claude.ai/code → 环境 → Environment variables）：`YHS_API_KEY`、`DISCORD_WEBHOOK_URL`。仓库里永远不放密钥。
- **云端定时任务（Routines，每个交易日，新会话）**：
  - `trig_01URbhAaykT4DAwC3zUVE5gk` 09:10 ET：拉最新分支 → `--check` → 后台启动 `--live --one-day` → 守到收盘 → 推送 `logs/live_*.log`、`live/trades_*.csv`。
  - `trig_019k7B2KSVPfARD5HnKhZyH5` 16:25 ET：`archive.py --universe heatmap --expiries near` + `--expiries nearest`（212 只）→ 推送存档 → Discord 🗄️ 通知。约 1,500 次/天。
  - 建议关闭 Mac 上旧的 17 只存档任务（已被云端覆盖）。
- **信号记录**（`signal_log.py`，0 配额）：212 只 × 10/7–10/9，开仓时段内新结构 1,461 个，30 分钟方向收益 +0.023% ± 0.017%（≈0）；午间信号方向并不差（+0.030% ± 0.014%），午间亏损可能来自期权 theta——待大样本回测单独验证。

## E. 大样本控制变量回测（212 只 × 10/7–10/9，2026-10-12）
| 实验 | 相对上一组的改动 | 笔数 | 合计 | 去最大一笔 | 胜率 | 盈亏比 |
|---|---|---|---|---|---|---|
| big_1dte | 对照：0DTE 否则 1DTE，先到先得，午间不开 | 108 | -$361 | -$1,339 | 32% | 0.92 |
| big_nearest | 0DTE 否则最近到期 | 92 | -$1,704 | -$2,682 | 29% | 0.70 |
| big_ranked15 | 评分排序 + 每天 15 笔 | 45 | +$240 | -$287 | 40% | 1.15 |
| big_ranked15_s05 | 评分 ≥ 0.5 | 45 | +$240 | -$287 | 40% | 1.15 |
| big_s05_midday | 午间也开仓（= 当前实盘规则） | 45 | +$232 | -$295 | 38% | 1.14 |

- 评分排序 + 15 笔上限是唯一明显改进；0.5 门槛与午间开仓在 15 笔上限下几乎不起作用（上限在早盘就用完）。
- big_nearest 内按评分三等分：低分组最差（胜率 23%），高分组总额为正但平均 ROI 为负 → 评分仅弱证据。
- 下一步候选：放开每日上限后单独测午间；"负节点在上方的加速形态"（如 IREN 10/16：现价 35.2，负节点 37、正节点 39）作为新候选规则加入信号记录；样本靠每日存档积累。

## F. 止损方式（2026-10-12 起实盘）
- 回测 stop_* 四组（同样 45 笔入场，只改止损）：期权 -35% +$240；标的破节点 0.2% +$856；**标的破节点 0.5% +$1,042（去最大一笔 +$516，胜率 56%，盈亏比 2.08）**；期权 -50% +$796。
- 实盘默认改为 `stop_mode="underlying"`、`underlying_stop_buffer_pct=0.005`、`disaster_stop_pct=-60`：标的反向穿越结构正 GEX 节点 0.5% 才止损，期权 -60% 兜底；保本/追踪/止盈不变。实盘按 5 分钟 Heatmap 现货检查（回测按 1 分钟）。
- 评分权重：去掉/反转「贴近节点」都更差（-$892 / -$651），保留原权重；评分 ≥0.8 两版都亏。

## G. 实盘实时报价（2026-10-13 接入，次日起生效）

夜航社新开放 `/v1/options/quote/{OCC}`（单个）与 `/v1/options/quotes?contracts=a,b,…`（≤50 个），返回最新 bid/ask/mid 与 `quoted_at`，**每个返回的报价 1 配额**；合约代码不能带 `O:` 前缀；只有最新报价、没有历史，所以只用于实盘，回测不变。

- `config.live_quotes=True`：实盘按 **ask 开仓**、按 **bid 盯市与平仓**（不再加 2% 滑点）；**bid 为 0（没有买盘）不开仓**；持仓中 bid 为 0 就按 0 计（不再用中间价估算，用户 2026-10-13 要求）；本轮无报价则沿用上次的真实报价。
- `config.require_live_quote=True`（用户 2026-10-13 要求"不要用估算"）：开仓时拿不到实时报价就**不开仓**，不再退回期权链中间价或公式估算；因此实盘所有持仓都按实时报价盯市，某一轮没报价时沿用上一次的真实报价。
- `main.QuoteBook`：每轮开始用批量接口一次拉齐 A、B 两个账户所有持仓合约；新开仓的合约单独拉一次；同一轮内走缓存。最多 8 笔持仓 × 2 个账户 × 78 轮，一天上限约 1,250 次，通常远低于此。
- 交易 CSV 新增 `price_source`（quote / chain / real_data / bs_fallback）、`entry_bid`、`entry_ask`、`entry_spread_pct`，用于 11 月评估时校准回测的 2% 滑点假设、并决定是否加"价差过大不开仓"的规则（目前只记录，不过滤）。
- 改动前后用 10/12 重放核对：回测结果完全一致（15 笔，+$220.66）。

## H. 2026-10-14 起实盘规则调整：离节点 ≤1%

依据：5 天回测（10/7–10/13，212 只，全部真实期权价格）：

| | 旧规则（距节点 ≤2.5%） | 新规则（距节点 ≤1%） |
|---|---|---|
| 合计 | +$1,262 | +$1,597（5d_dist10） |
| 去最大一笔 | +$735 | +$1,071 |
| 亏损天数 | 2/5 | 1/5（-$12） |

- `config.node_distance_max_pct=0.01`：现价距正 GEX 节点 ≤1% 才算结构成立（旧 2.5%）。其余规则不变（行权价仍按目标负节点、最多虚值 0.5%）。
- **注意（回测 bug 已修）**：早先的"严格平值"组（5d_atm、5d_atm_dist10、4d 之前无此组）把现价四舍五入当行权价，6 笔取到不存在的合约、被 BS 公式估价（`price_source=bs_fallback`），虚增约 +$300。现在 `strike_mode="atm"` 取 Heatmap 挂牌行权价中最近的一个；修正后"平值 + 距节点 ≤1%"（5d_new）只有 +$745，所以**没有**改用平值。
- 每日对比：`daily_compare.py` 在全部存档日上跑旧规则（daily_old）与新规则（daily_new），发 Discord；定时任务"夜航社 新旧规则每日回测对比"美东 17:35（存档完成后）运行，结果在 `backtests/daily_compare/<日期>.json`。

## I. 2026-10-15 起实盘：持仓名额"组合 A"；一个月观察期

依据：5 天回测（10/7–10/13）显示每天 15 笔名额常在中午前用完，下午信号（含尾盘反弹的 Call）开不了仓：

| | 现行（15 笔） | 上限 25 | 12:00 前 ≤10 | **组合 A（25 笔 + 12:00 前 ≤15）** | 组合 C（20 笔 + 12:00 前 ≤10） |
|---|---|---|---|---|---|
| 合计 | +$1,597 | +$2,373 | +$2,254 | **+$2,670** | +$2,416 |
| 盈亏比 | 1.96 | 2.13 | 2.95 | 2.35 | 2.71 |
| 亏损天数 | 1/5 | 1/5 | 0/5 | 1/5 | 0/5 |

- `config.max_trades_per_day=25`、`config.max_trades_before=("12:00", 15)`；同时持仓仍 8 笔；其余规则不变（距节点 ≤1%、目标节点行权价、实时报价、止损止盈）。
- "同方向最多 5 笔"（`max_same_direction`）回测更差，未采用。
- `daily_compare.py` 每天收盘后（定时任务美东 17:35）在全部存档日上跑 7 组：旧规则 / 10/14规则 / 实盘A / A+Flow加分 / 组合C / 午前≤10 / 上限25，发 Discord，结果在 `backtests/daily_compare/<日期>.json`。
- 用户决定：观察约一个月（约 11 月中）后再做最终决定。

## J. 2026-10-14 起：所有回测一律"真实回测"（硬规则）
用户要求：回测拿到的数据必须尽量和每日模拟交易一致；拿不到的数据用误差最小的估算。现在默认配置 / 参数即真实口径：
* 标的：`experiment.py --universe live`（默认）= 当天实盘 B 账户监控标的（logs/live_<日>.log 或 archive/universe/live_<日>.json），没有实盘日志的日子（10/7–10/9）退回 212 只
* Heatmap：标记 T 的 5 分钟段到 T+5 才用（replay_bar_delay_min=5，与实盘一致）
* 盯市：每 5 分钟一轮（intraday_mark_min=0，与实盘轮询一致）
* 成交价：历史 NBBO（chain-history 30m/1h，±60 分钟内最近一条）的真实买卖价差算单边滑点；当时 bid 为 0 不开仓；查不到报价时单边 2.7%（slippage_pct=0.027 ≈ 实盘实测价差中位 5.3% 的一半）
* daily_compare.py 全部组都按此口径跑；"实盘A" 一组直接与当天实盘逐笔对账（原"实盘复刻"组已并入）

校验（真实回测 vs 实盘）：10/14（10/14 规则）回测 +$2,233 vs 实盘 +$2,251，15 笔对上 12 笔；
10/13（旧规则）回测 −$601 vs 实盘 −$81（当天实盘还是估算价格，实盘数字本身偏乐观），15 笔对上 11 笔。

真实回测 10/7–10/14（6 天）结果：旧规则 −$4,022（90 笔）、10/14 规则 −$4,347（90 笔）、组合 A −$5,339（145 笔）；
只有 10/14 一天赚钱。之前（1 分钟盯市、212 只、提前 5 分钟用 Heatmap）的 +$1.5k~+2.7k 明显高估。

## K. 2026-10-16 起：隔夜期权模拟实盘（overnight_live.py，与日内实盘分开记账）
用户 10/15 定的规则（截图里别人的 MU / LITE / TSM 隔夜单）：
* 标的：212 只存档池 + 最近一个交易日的日内实盘名单
* 15:15 预扫（确定每只能过夜的到期：SPY/QQQ 用下一交易日，其余用下一个周五），15:44 正式扫描
* 结构：最近两个已可用快照都成立，局部节点（node_mode="local"）或全局节点任一即可；跳板买 Call、悬崖买 Put
* Flow 确认（必须）：今天该标的有同方向 Ask 侧（A/AA）单笔 ≥ $5 万 的大单
* 合约：第一档虚值（接近平值），1 张，按实时 ask 买，bid 为 0 / 无报价不开；每晚最多 10 笔，按大单金额排
* 次日 09:35 起按实时 bid 卖，每分钟重试，09:45 前全部平掉（无报价按 0 记）；夜里不止损
* 账本 live/overnight_<开仓日>.csv；开 / 平仓都发 Discord
* 定时任务：美东 15:05 `python3 overnight_live.py --open`，09:15 `python3 overnight_live.py --close`
