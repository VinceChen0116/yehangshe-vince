# 项目守则（夜航社 Dealer GEX 模拟盘）

每次开始工作前先读这里。用户（Vince）用中文沟通，回复一律用中文。

## 交易原则（用户定，永远有效）
* **宁缺毋滥。** 信号条件不完整、不确定就不开仓：没有大正节点承接不做、路径走不动不做、方向不明不做。
  任何"兜底""退而求其次"的开仓逻辑都不要加；不确定时宁可不开，并把疑问告诉用户。
* 悬崖 / 跳板的重点是**超大正节点**：天花板 / 踏板和目标都必须是相对其他节点足够大的正节点（见 `baseline.BASELINE_NOTES`）。
* 日内规则以 `docs/AGENT_REPRODUCTION_BLUEPRINT_HEATMAP_AND_TPSL.md` + `strategy_core_spec.py` 为 baseline（实现在 `baseline.py`，
  说明在 `docs/BASELINE_IMPLEMENTATION.md`）。文档没写死的地方先问用户，不要自己发挥。
* 隔夜单（`overnight_live.py`）规则独立，未经用户明确要求不要改。

## 回测原则
* 回测必须尽量复刻实盘：真实期权价格（真实 IV + 真实标的价、真实成交）、真实买卖价差、Heatmap 晚 5 分钟可用、与实盘同样的盯仓频率。
  不能用估算价格充数；数据不可得时用误差最小的估计并说明。
* 只在用户要求时跑回测（配额有限）。

## 配额与安全
* 夜航社 API 每月 10 万次配额，注意节省。
* 密钥只在环境变量 `YHS_API_KEY` / `DISCORD_WEBHOOK_URL`；不要提交 `config.json`，不要在任何输出、提交或消息里打印密钥。
* 不使用 Discord user token / self-bot。

## 时间口径
* Heatmap 快照标记 T 表示 T..T+5 这一段，里面的现价是 T+5 前后的价格，T+5 之后才拿得到。和用户说时间时用"数据实际时刻"。
