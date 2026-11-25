# SPX 尾盘观察台 · IBKR 只读版 v0.1

一台本地电脑上的单进程看板：Feed 异动流、Strike Leader、窗口内 GEX、Playbook。
运行时行情只放在内存，不建数据库，不保存逐笔行情，不连接夜航社或 Databento，不调用大模型。

**当前是可运行的第一版，真实 IBKR 连接尚未在本次交付中验证。**默认启动的是明确标注的假行情演示。
演示价格、成交、OI、模型状态都是合成数据，不能用于交易判断。

## 1. 先看界面

需要 Python 3.12（代码也兼容 3.11）。在解压后的本目录运行：

```bash
python -m venv .venv
```

Windows 激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux 激活虚拟环境：

```bash
source .venv/bin/activate
```

安装并运行：

```bash
python -m pip install -r requirements.txt
python -m observer.app --source demo
```

浏览器打开 **http://127.0.0.1:8765**。
假行情时钟固定从 2026-11-13 美东 15:50 开始向前运行，先合成 80 秒基线；与电脑当天日期无关。
过了演示日收盘，Playbook 会正常待命；重启即可重新演示。

## 2. 接 IBKR

在本机运行并登录 IB Gateway 或 TWS。程序不需要、也不会收集你的账户密码或 API 密钥。

在 Gateway / TWS 的 API 设置中：

1. 启用 Socket API 客户端（界面名称以当前 TWS/Gateway 为准）。
2. **勾选 Read-Only API**。本程序仅请求合约与行情；不会调用账户写接口。
3. 保持仅允许本地连接，确认 Socket 端口。
4. 确认账户具备 **SPX 指数实时行情及 SPXW 期权实时行情的 API 权限**。
   网页/手机能看实时，不足以证明 API 权限已具备。

常见默认端口（以你软件设置为准）：

| 登录入口 | 模拟账户端口 | 实盘账户端口 |
| --- | --- | --- |
| IB Gateway | 4002 | 4001 |
| TWS | 7497 | 7496 |

例如 IB Gateway 模拟登录：

```bash
python -m observer.app --source ibkr --ib-port 4002
```

例如 TWS 模拟登录：

```bash
python -m observer.app --source ibkr --ib-port 7497
```

端口只决定连到哪个本地 Gateway，不会让本程序具备交易行为。是否使用模拟账户、
模拟行情是否具备实时权限，要在自己的 IBKR 环境确认。

非正常交易时段，真实适配器保持待命，不拿延迟/昨日报价伪装成实时读数。
进入 RTH 后发现当日 SPXW 合约，验证每个具体合约后订阅；只有实时状态确认后才参与计算。
**本版不监控 GTH 或盘后。**半日市使用 XNYS 日历的提前收盘，固定 15:30 的 Playbook 窗口不会启动。

默认保留 20% 行情线，通常是 78 张期权＋1 条 SPX；VIX 默认关闭。
若你自己的 TWS 还开了很多行情，把 `IB_MARKET_LINES` 调小，例如 60。
窗口移位时旧合约最多保留 60 秒，期间宁可等待腾出额度，也不会超额订阅。
收到额度不足错误会缩小预算，连续三次不足后停止重试，等待你调整配置并重启。

### 环境变量

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `OBSERVER_SOURCE` | `demo` | `demo` 或 `ibkr` |
| `OBSERVER_PORT` | `8765` | 本地网页端口；仅监听 127.0.0.1 |
| `IB_HOST` | `127.0.0.1` | Gateway 地址 |
| `IB_PORT` | `4002` | Gateway/TWS 端口 |
| `IB_CLIENT_ID` | `71` | API 客户端 ID，避免与自己的其他程序冲突 |
| `IB_MARKET_LINES` | `100` | 你配置给本程序评估的总行情线；再保留其中 20% |
| `ENABLE_VIX` | `0` | `1` 时加订 VIX，占一条行情线 |
| `MODEL_RATE` | `0.04` | BSM 年化利率假设，不代表当前市场利率 |
| `MODEL_DIVIDEND` | `0.012` | BSM 年化股息率假设 |

CLI 参数可覆盖端口、数据源、客户端 ID，详见 `python -m observer.app --help`。

IBKR 官方 Python SDK 通过 `ibapi` 导入。本项目 requirements 提供 PyPI 可安装入口；
若你使用的最新 Gateway 对 SDK 版本有要求，安装 IBKR 官方配套 API 安装包中的 Python SDK。
请遵循官方说明，不凭不同券商的经验配置 IBKR。

## 3. 券商能力核对表

核对日期：2026-11-15。**“官方文档支持”与“本账户真实连接已验证”分开列出。**

| 核对项 | 官方文档与本版采用方式 | 本次验证范围 |
| --- | --- | --- |
| SPX / SPXW 实时覆盖及权限 | [行情订阅](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction)：SPX 指数与期权权限分别核对；请求实时，不静默使用延迟 | 未登录用户账户，权限未确认 |
| 并发订阅与请求限速 | [行情限制](https://www.interactivebrokers.com/docs/tws-api/doc/md/market-data-limitations)：配额受账户影响；本版默认 100 条评估值、保留 20%，出站请求统一限制 20 次/秒 | 假源模拟窗口单向移动 100 点，预算不超限；真实账户额度未确认 |
| 实时期权逐笔、主动方向 | [Tick-by-tick](https://www.interactivebrokers.com/docs/tws-api/doc/md/live-ticks)：期权没有实时逐笔接口；不调用该接口，不声明真实主动方向 | 用累计量增量替代，三个质量档位有离线检查 |
| 报价形式及频率 | [Streaming Data](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data)：聚合采样；报价不变化时可能不重复推送，不因单个 Bid/Ask 没变就判断线 | 同价连续成交、报价异步更新有离线检查 |
| Greeks / IV | [Options & Greeks](https://www.interactivebrokers.com/docs/tws-api/doc/md/option-greeks)：`reqMktData` 的模型计算，tick 13；通常需要标的与期权权限 | 过期/缺失时本地反推 IV，有数学参考检查；真实返回未验证 |
| OI | [Streaming Data](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data)：generic tick 101，Call tick 27 / Put tick 28 | 具体 SPXW 合约是否返回，未在账户验证；空值保留，不补 0 |
| OI 日期 | TWS 该 tick 不带可核验的业务日期；本版明确标“按上一交易日推定” | 推定日期来自交易日历，不声称实际日期被 API 证实 |
| SPX 实时指数点位 | 通过合约查询确认 SPX IND/CBOE，订阅 L1；[订阅权限说明](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/popular-market-data-subscriptions/introduction) | 未真实验证；本版缺现价就待命，没有加入 SPY/平价替代源 |
| 只读模式 | [IBKR API Release Notes](https://www.interactivebrokers.eu/cn/software/api_releasenotes_beta.php)：TWS/Gateway 可开启 Read-Only API；本程序不调用交易/账户写方法 | 离线请求白名单检查已做；GUI 勾选状态需使用者确认，API 不替你开关 |
| 合约发现 | [Option Chain](https://www.interactivebrokers.com/docs/tws-api/doc/contracts/option-chains)：`reqSecDefOptParams`＋`reqContractDetails`，确认 SPXW、日期、100 乘数与具体合约 | 请求形状与响应映射离线验证；真实合约解析未验证 |
| 重连与网关 | [官方 TWS API](https://www.interactivebrokers.com/docs/tws-api/)：本地 Socket；本版断开后退避重连，重建所有累计量基线 | 重连后首值作为基线的计算路径已检查；真实重连与定期登录未验证 |

## 4. 每个读数怎样理解

### Feed / Strike Leader

- 同一合约 10 秒内连续触发的类型合并到一行，金额与更新次数只累加一次。
- `VOL ×` 和 `PREM ×` 对比过去 1/3/5 分钟基准内的平均等长窗口；必须先取得至少 60 秒基线。
  基准期不含当前窗口；基准量不足时不计算夸张的无穷倍数。
- 大额主动买入估算按单次**累计量增量**≥50 张或估算权利金≥50,000 美元触发。
  “50 张的一次更新”不意味着市场上恰好只有一笔 50 张交易。
- `ASK BUY` 分母包括未知方向的观察量，状态条另报可判方向覆盖率。
- 质量 1.0 / 0.8 / 0.5 等是文档约定的输入质量档位，**没有经过概率校准**。
- 每次重新订阅、重连或累计量下降后，第一条累计量只建立基线。不会把全日累计量报成突发大单。
- “当日已观察”只包含本程序运行和订阅期间观察到的活动，不是交易所完整全天数据。
- 同一合约 Last 长时间不变可继续按本次订阅内的值做低一档估算；跨订阅的 Last 不继承。
- 只对 ATM/OTM、卖价 $0.05–$4.00 的合约报警。未满足条件时空白是正常状态。

### GEX / Skew / Charm

- GEX 只覆盖当前 ATM 订阅窗口：`gamma × OI × 100 × spot² × 0.01`，Call 取正、Put 取负。
  这是固定符号情景，不是真实做市商净仓位，也不是夜航社 Dealer GEX 的复制品。
- 覆盖率分母是当前应监控的合约数，未收到有效 Gamma/OI 的合约不会隐式记 0。
  少于 20% 覆盖时不输出总 GEX 与正负状态。
- OI 通常反映上一交易日；当日新开 0DTE 不会由这项输入完整捕获。
- IV 优先考虑输入时间：较新的有效报价可优先本地反推；券商模型超过 5 秒视为过期。
  本地反推会拒绝倒挂、宽价差、无套利边界之外或 Vega 太小的报价。
- RR25 用接近 25Δ 的 OTM 双侧合约；差距太远或覆盖不足时弃权。
- Charm 是“流逝时间导致 Delta 变化”的局部导数，换算成每分钟美元量，临近到期不保证线性。
- **原任务中 GEX 与 Charm 的持仓假设并不一致。**本版保留它们为两个明确标注的情景：
  GEX 用 Call 正/Put 负；Charm 默认两侧都假设做市商净空。不会声称它们来自同一本真实仓位账。
- Skew、Charm 没有单独面板，只在 Playbook 证据标签与 `/api/debug` 中出现。

### Playbook

- 仅 15:30 ET 至当日收盘输出规则倾向；休市、断线、延迟、现价过期、报价覆盖不足、
  队列丢失后的重建期统一 `STAND_BY`。
- 分差门槛 21，单独 Charm 20 分不会得到 CALL/PUT。
- C/P 偏向默认采用已观察成交量，不宣称 Call 成交就是买开，也不识别机构身份。
- “附近节点”本版明确为距现价不超过 10 SPX 点；突破/拒绝用最近 180 秒价格样本。
  这些是实现歧义的显式默认值，尚未证明具有预测优势。
- 拒绝形态额外使用 Charm 和 ATM 量加分，会重复使用部分已有证据；分数不能当独立证据的概率合成。
- 参考合约仅列同方向、带内成交量最大的有效报价合约，不构成买入建议。

所有阈值来自原任务的初值或上述显式默认，**不保证在其他时期或券商数据上有效**。
没有交易写权限，也不意味着信息正确率被保证。

## 5. 本版做了什么、没有做什么

已做：IBKR 单适配器、假行情、动态订阅、四块面板、短期内存、数据质量与待命状态、交易日历、
BSM/IV、OI 缺失降级、重连基线、额度退避、只读调试接口。

没有做：数据库、行情落盘、历史回放产品、回测、影子跟单、自动交易、其他券商适配器、
Premium/NET/HIRO 曲线、独立 Skew/Charm 面板、可选的 15:10 蝴蝶提示。

没有照抄截图中的“评分 100”：界面显示数据质量与 Playbook 规则分，两者都不叫胜率。
没有实现截图里额外的“IV 突刺共振”“节点处逆势大买”文字规则；本版顶部摘要来自实际已触发的 Feed。
没有在缺少标的行情时偷偷用 SPY 或其他标的替代。

真实 TWS/Gateway、实盘 SPXW 权限、真实重连、连续运行和尖峰吞吐量均尚未验证。
缺少 `ibapi` 或日历依赖时只显示原因并待命，不伪造实盘连接。

## 6. 检查

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

检查使用假行情、手算参考和 SDK 请求替身，不依赖账户或行情付费。
主要覆盖：期权定价参考值、IV 反推、Charm 时间符号、首次累计量、同价更新、跨订阅旧价隔离、
延迟/断线、量重置、队列丢弃、窗口移位、早收盘、缺 OI、单独 Charm 不能选边、API 只读路由。

测试不会证明信号能盈利，也不能替代本机真实连接核对。

## 7. 文件

```text
observer/app.py              本地网页与只读 API、计算循环
observer/ibkr.py             IBKR 合约与行情适配器
observer/engine.py           内存状态、异动、模型与 Playbook
observer/mathmodel.py        BSM、IV 反推、Charm 时间导数
observer/subscriptions.py    订阅预算和 60 秒退订宽限
observer/clock.py            交易日历、时区与半日市
observer/demo.py             明确标注的合成行情
observer/static/index.html  无构建步骤的单页界面
tests/test_observer.py       离线检查
```

这是编程项目交付文件，不是自动化交易指令。请始终在自己已确认的数据权限和网关设置下启动真实源。
