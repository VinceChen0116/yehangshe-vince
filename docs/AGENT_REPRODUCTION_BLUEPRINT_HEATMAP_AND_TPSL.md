# Dealer Heatmap 三层信号判断权重 & 双轨动态棘轮止盈止损算法 — Agent 零依赖复现蓝图

> **用途说明**：本文档将系统的两大核心量化模块——**① Heatmap 结构识别与三层确认评分权重**、**② 期权 + 现货双轨动态棘轮止盈止损算法**——完全解耦为无任何外部依赖的数学公式、参数字典与状态机伪代码。
> 你可以将本文档或配套单文件参考实现 [`strategy_core_spec.py`](../strategy_core_spec.py) 直接发给任意 AI 编程 Agent（Claude Code / Cursor / Codex / Antigravity），即可在数分钟内 1:1 复现。

---

## 模块一：Heatmap 做市商三层结构判断与评分权重 (`HeatmapJudgmentEngine`)

### 1.1 输入数据契约 (Input Schema)
每个标的每次轮询输入一个快照对象 `snapshot` 及日内历史序列：
- `ticker`: 标的代码（如 `"NVDA"`, `"GOOGL"`, `"SPX"`）
- `spot`: 当前标的现货价格 $S_t$（优先取自 Heatmap 返回的 `spot_usd`）
- `cells`: GEX 节点数组，每个元素包含：
  - `expiration`: 到期日 `"YYYY-MM-DD"`
  - `strike_usd`: 行权价 $K$
  - `net_dealer_gex_usd`: 做市商净 Gamma 敞口 $G_K$（美元，正值 = 做市商 Long Gamma 阻力/支撑墙；负值 = 做市商 Short Gamma 加速磁吸/深渊）
- **日内时序上下文（由快照序列按真实时间戳差 $\Delta t$ 维护）**：
  - `open_spot`: 当日 `09:30 ET` 开盘首帧现货锚点 $S_{\text{open}}$
  - `session_vwap_proxy`: 当日已采集帧现货均值 $\overline{S}$
  - `intraday_ret`: $\frac{S_t - S_{\text{open}}}{S_{\text{open}}}$
  - `tick_ret_5m`: 最近 $\Delta t \ge 180\text{s}$ 的 5 分钟现货涨跌幅
  - `ret_15m`: 最近 $660\text{s} \le \Delta t \le 1500\text{s}$ 的 15 分钟现货涨跌幅

---

### 1.2 基础阈值与显著节点门槛
1. **动态最小显著节点 GEX 门槛 ($G_{\min}$)**：
   $$G_{\min} = \begin{cases} \$2,500,000, & \text{if } \text{ticker} \in \{\text{SPX, SPXW, NDX, QQQ}\} \\ \max\big(\$800,000,\ \min(\$1,500,000,\ S_t \times 8000)\big), & \text{otherwise (个股)} \end{cases}$$
2. **到期日分层 (`struct_exps`)**：
   - **盘中 (`< 15:00 ET`)**：取当日 `0DTE`（若无则取最近一期周度期权）作为 `T1` 主力结构场；
   - **尾盘隔夜窗口 (`15:00 - 15:55 ET`)**：底层做市商力场 `struct_exps` **同时合并当日 `0DTE` 与最近周五到期日**（防止周五尾盘丢失 0DTE 主力墙体），但期权建仓合约 `primary_exp` 选用 $\text{DTE} \ge 3$ 的跨周期权。
3. **Top-10 截断感知基线 (`t1_pos_trunc_floor` / `t1_neg_trunc_floor`)**：
   - 因 API 每个到期日最多返回 Top 10 正节点 + Top 10 负节点，“未返回”不代表 $G_K = 0$，而是 $|G_K| \le \text{第 10 名幅值}$。
   - 若正/负节点数 $\ge 8$，记录最小正节点 $G_{\text{trunc}}^+$ 与最小负节点绝对值 $G_{\text{trunc}}^-$。

---

### 1.3 第一层 (Layer 1)：局部 Gamma 环境分类 (`local_gamma_regime`)
截取现价 $S_t \pm 3.0\%$ 内的节点，取绝对值最大的 **Top-6 节点**，计算正 GEX 绝对值占比：
$$\text{pos\_abs\_share} = \frac{\sum_{i \in \text{Top6}, G_i > 0} G_i}{\sum_{i \in \text{Top6}} |G_i| + 10^{-6}}$$
并定义双向缓冲条件 `has_bidirectional_pos_buffer`：现价上方（$>1.001 S_t$）和下方（$<0.999 S_t$）的正 GEX 均占 `Top6` 总量的 $\ge 15\%$。

按以下优先级依次判定 `local_gamma_regime`：

| 优先级 | 环境枚举值 (`local_gamma_regime`) | 精确触发条件 | 物理含义 |
| :---: | :--- | :--- | :--- |
| **1** | `SPEED_BUMP_MUD` | $S_t \pm 1.2\%$ 内存在 $\ge 3$ 个正节点（$G_i \ge 0.55 G_{\min}$），且上下均有正节点夹击，且 $\text{pos\_abs\_share} \ge 0.72$ | 密集正值减速带泥潭（严禁追突破） |
| **2** | `DISORDERED_WHIPSAW` | $S_t \pm 2.5\%$ 内 $\ge 5$ 个显著节点且正负号交替次数 $\ge 4$ 且最大节点占比 $< 20\%$，或墙体 30m 发生反复横跳 (`WHIPSAW`) | 正负交错无序洗盘区 |
| **3** | `STACKED_FLIP_ZONE` | 局部最大正墙 $K^+$ 与最大负墙 $K^-$ 间距 $\le 1.2\% S_t$，且现价距二者中点 $\le 0.8\% S_t$，且二者幅值均 $\ge 0.8 G_{\min}$ | 正负紧挨的 Gamma 极性翻转区 |
| **4** | `TREND_DRIVER` | $\text{pos\_abs\_share} \le 0.45$（即负 GEX 占比 $\ge 55\%$），或墙体发生同向迁移 (`MIGRATING_UP` / `MIGRATING_DOWN`) | 负 Gamma 主导的顺势趋势加速场 |
| **5** | `RANGE_BUFFER` | $\text{pos\_abs\_share} \ge 0.65$ **且满足 `has_bidirectional_pos_buffer`**（上下双向正墙包夹） | 正 Gamma 均值回归缓冲场 |
| **6** | `NEUTRAL` | 其余过渡状态 | 中性平衡场 |

---

### 1.4 第二层 (Layer 2)：节点 15m 动态演化 (`node_growth_state`) 与 30m 墙体迁移 (`wall_migration`)
1. **核心节点 15 分钟生长率 ($g_{15m}$)**：
   对当前起作用的核心节点 $K_{\text{acting}}$，对比 10~25 分钟前（$\Delta t \in [540\text{s}, 1600\text{s}]$）的同节点幅值 $|G_{\text{prev}}|$：
   $$g_{15m} = \frac{|G_{\text{curr}}| - |G_{\text{prev}}|}{|G_{\text{prev}}| + 10^{-6}}$$
   - `GROWING`：$g_{15m} \ge +8\%$（做市商被新单迫使持续加厚墙体/磁吸）
   - `DECAYING`：$g_{15m} \le -10\%$（节点正在平仓衰减/失守）
   - `NEW`：日内满 3 帧后首次挤入 Top-10 榜单的新生节点
   - `FLAT`：$-10\% < g_{15m} < +8\%$
2. **30 分钟墙体迁移 (`wall_migration`)**：
   对比 10~35 分钟前（$\Delta t \in [600\text{s}, 2100\text{s}]$）的主力正墙/天花板/地板行权价：
   - `MIGRATING_UP`：支撑地板 $K_{\text{floor}}$ 或主正墙 $K^+$ 上移（或上方天花板消失），且 $S_t > 1.001 S_{\text{ref}}$
   - `MIGRATING_DOWN`：上方天花板 $K_{\text{ceil}}$ 或主正墙 $K^+$ 下移，且 $S_t < 0.999 S_{\text{ref}}$
   - `WHIPSAW`：最近 6 帧内主正墙 $K^+$ 发生 $\ge 2$ 次幅度 $>0.3\% S_t$ 的正反向反复跳动
   - `STABLE`：墙体位置稳定

---

### 1.5 第三层 (Layer 3)：五大结构原型几何条件与价格反应触发器 (`reaction_trigger_type`)

#### 全局前置门控（日内衰竭拦截 `Exhaustion Filter`）
- **空头深跌末端拦截 (`is_bearish_exhausted`)**：当 `intraday_ret <= -6.5%` 且（$S_t > S_{\text{day\_low}} \times 1.0025$ 或 `ret_15m > -0.25%`）时，禁止新开看跌结构。
- **多头暴涨末端拦截 (`is_bullish_exhausted`)**：当 `intraday_ret >= +6.5%` 且（$S_t < S_{\text{day\_high}} \times 0.9975$ 或 `ret_15m < +0.25%`）时，禁止新开看涨结构。

#### 五大原型判定表

| 原型名称 | 方向 | 几何与力场条件 | 价格反应触发器条件 (`Layer 3 Price Trigger`) | 输出 `reaction_trigger_type` |
| :--- | :---: | :--- | :--- | :--- |
| **1. `PINNING_MAGNET`**<br>(SPX 0DTE 钉盘) | `PINNING` | 仅限 `SPX/SPXW` 盘中；最大正墙 $G^+ \ge G_{\min}$ 且 $\frac{|S_t - K^+|}{S_t} \le 0.40\%$ | 现货紧贴锚点未突破 | `PINNING_HOLD` |
| **2. `CLIFF_STRUCTURE`**<br>(做市商破位悬崖) | `BEARISH` | 上方正天花板 $K_{\text{ceil}} > S_t$ ($G_{\text{ceil}} \ge G_{\min}$)，下方负深渊 $K_{\text{chasm}} < S_t$；<br>距天花板 $0.05\% \le d_{\text{ceil}} \le 1.8\%$（强空头水下放宽至 $2.7\%$）；<br>下方空间 $d_{\text{target}} \ge 1.0\%$（指数 $0.4\%$）；<br>结构盈亏比 $R:R = \frac{d_{\text{target}}}{\max(0.8\%, \min(d_{\text{ceil}}, 1.2\%)+0.3\%)} \ge 1.35$；<br>下方 $0.45\%$ 内无强正地板拦截 | 非向上冲刺（非 `tick_ret > +0.25% & ret_15m > +0.35%`）；且满足：<br>① 受阻回落：`tick_ret <= +0.05%` 且 (`ret_15m <= +0.05%` 或 $S_t \le \text{VWAP}$ 或 `MIGRATING_DOWN`)；或<br>② 顺势反抽受阻：`intraday_ret <= -1.2%` 且 $S_t \le 0.995\text{VWAP}$ 且 `tick_ret <= +0.25%` | $d_{\text{ceil}} \le 0.6\%$ 为 `REJECTION_DROP`；<br>否则为 `TREND_PULLBACK_REJECT` |
| **3. `SPRINGBOARD_STRUCTURE`**<br>(做市商支撑跳板) | `BULLISH` | 下方正踏板 $K_{\text{floor}} \le S_t$ ($G_{\text{floor}} \ge G_{\min}$ 且 $G_{\text{floor}} > 1.2 G_{\text{ceil}}$)，上方负靶心 $K_{\text{accel}} > S_t$；<br>距踏板 $0.25\% \le d_{\text{floor}} \le 1.8\%$；上方空间 $\ge 1.0\%$；$R:R \ge 1.35$；上方 $0.45\%$ 内无强天花板封堵 | 非接飞刀（要求日内帧数 $\ge 3$，且 `intraday_ret >= +0.05%` 或 `MIGRATING_UP`）；且满足企稳回升（`tick_ret >= -0.05%` 且 `ret_15m >= -0.05%`）；若 $d_{\text{floor}} \le 0.6\%$ 要求节点 `GROWING/NEW` 或已放量反弹 | $d_{\text{floor}} \le 0.6\%$ 为 `REJECTION_BOUNCE`；<br>否则为 `TREND_PULLBACK_HOLD` |
| **4. `SHORT_GAMMA_VACUUM_SQUEEZE`**<br>(短Gamma逼空真空加速) | `BULLISH` | `local_gamma_regime != RANGE_BUFFER`；上方主力 Call 墙 $K_{\text{cw}} > S_t$，$G_{\text{cw}} \ge \max(G_{\min}, 2.0 G_{\text{trunc}}^+)$，集中度 $\ge 18\%$；<br>$(S_t, K_{\text{cw}})$ 之间无 $>0.25 G_{\text{cw}}$ 的中间正墙；跑道空间 $1.2\% \le d_{\text{runway}} \le 5.5\%$ | `intraday_ret >= +0.15%` 且 `tick_ret >= 0` 且 `ret_15m >= +0.08%` 且 $S_t \ge \text{VWAP}$ | `EFFECTIVE_BREAKOUT` |
| **5. `SHORT_GAMMA_AVALANCHE`**<br>(全域负Gamma雪崩加速) | `BEARISH` | `local_gamma_regime != RANGE_BUFFER`；`t1_net_gex < -$1.5M` 或现价下方 $[-4\%, +0.3\%]$ 局部净 GEX $\le -G_{\min}$；<br>现价附近 $[-1.2\%, +0.1\%]$ 支撑净和 $\le 0$；下方 $0.85\% \sim 4.5\%$ 存在深负靶心 $G_{\text{chasm}} \le -\max(0.75 G_{\min}, 1.5 G_{\text{trunc}}^-)$ 且无中间正地板 | `intraday_ret <= -0.40%` 且 $S_t \le \text{VWAP}$ 且 (`ret_15m <= -0.15%` 或深跌趋势延续) | `EFFECTIVE_BREAKDOWN` |

#### 中间关卡修正 (`Intermediate Gatekeeper Calibration`)
在现价 $S_t$ 与目标行权价 $K_{\text{target}}$ 之间，若存在反向强正 GEX 节点（$G_{\text{gk}} \ge \max(0.85 G_{\min}, 0.45 |G_{\text{acting}}|)$）：
- 若最近关卡距现价 $< 0.40\%$（指数 $< 0.20\%$）：**直接拦截否决该信号**（无合理位移空间）；
- 若最近关卡距现价 $\ge 1.0\%$（指数 $\ge 0.40\%$）：**将第一目标位 `target_strike` 下调/上修至该中间关卡行权价**。

---

### 1.6 三层综合确认度评分公式 (`structure_confirmation_score` $\in [0, 100]$)

一旦上述任一结构原型通过几何与价格反应校验，授予基础分 **`Base = 58.0`**，随后叠加以下 4 个维度的精确加减分权重：

$$\text{Score} = \text{clip}_{[0, 100]}\Big(58.0 + W_{\text{node}} + W_{\text{regime}} + W_{\text{migration}} + W_{\text{expiry}} + W_{\text{mtf}}\Big)$$

| 评分维度 | 条件分支 | 权重分值 | 量化逻辑说明 |
| :--- | :--- | :---: | :--- |
| **1. 节点 15m 演化 ($W_{\text{node}}$)** | `node_growth_state == "GROWING"` ($\ge +8\%/15\text{m}$) | **`+12.0`** | 做市商核心墙体/磁吸正在持续增强 |
| | `node_growth_state == "NEW"` | **`+8.0`** | 盘中新涌现的机构大单节点 |
| | `node_growth_state == "FLAT"` | **`+4.0`** | 节点存量稳固 |
| | `DECAYING` ($\le -10\%/15\text{m}$) 且处于近墙博弈 (`REJECTION_*`) | **`-16.0`** | 紧贴墙体博弈但墙体正在快速撤单/被打穿，重罚 |
| | `DECAYING` 但已处于顺势脱离趋势 (`TREND_PULLBACK_*` / `EFFECTIVE_*`) | **`-2.0`** | 现货远离墙体导致 $|S-K|$ 增大引起的自然 BS Gamma 回落，仅微扣 |
| **2. 局部 Gamma 环境 ($W_{\text{regime}}$)** | `local_gamma_regime in ("SPEED_BUMP_MUD", "DISORDERED_WHIPSAW")` | **`-22.0`** | 减速带泥潭或无序洗盘区，一票否决级重罚 |
| | `TREND_DRIVER` 且结构为 `SQUEEZE / AVALANCHE` 或触发器为 `TREND_PULLBACK_*` | **`+10.0`** | 负 Gamma 趋势驱动场与顺势突破/回踩结构完美契合 |
| | `RANGE_BUFFER` 且结构为 `SPRINGBOARD / CLIFF / PINNING` | **`+8.0`** | 正 Gamma 缓冲场与边界反转/钉盘结构契合 |
| **3. 30m 墙体迁移 ($W_{\text{migration}}$)** | 多头结构 + `MIGRATING_UP` 或 空头结构 + `MIGRATING_DOWN` | **`+10.0`** | 做市商防线重心顺势推移 |
| | 多头结构 + `MIGRATING_DOWN` 或 空头结构 + `MIGRATING_UP` | **`-12.0`** | 墙体迁移方向与信号方向背离 |
| **4. 跨周期共振/打架 ($W_{\text{expiry}}$)** | 次近到期日同ストライク同符号共振 (`same_sign_confluences`) | **`+6.0 / 个`** (上限 `+12.0`) | 多周期期权链在同一点位同向叠加 |
| | 近端与次近端在核心行权价正负异号冲突 (`has_expiry_conflict`) | **`-14.0`** | 0DTE 与周度期权在同一点位多空互搏 |
| **5. 多周期动量共振 ($W_{\text{mtf}}$)** | 多头 (`tick_ret_5m > +0.05%` & `ret_15m > +0.15%`) 或 空头 (`< -0.05%` & `< -0.15%`) | **`+8.0`** | 5m 与 15m 价格动量同向共振 |

- **放行门槛**：$\text{Score} \ge 55.0$ 判定为 `is_confirmed = True`（低于 55 分直接丢弃）。
- **动态仓位权重 (`position_size_weight`)**：
  - $\text{Score} \ge 75$：**`1.15x`** 标准仓位
  - $55 \le \text{Score} < 75$：**`0.85x`** 标准仓位

---

## 模块二：双轨动态棘轮止盈止损与时序截断算法 (`OptionExitEngine`)

### 2.1 状态变量定义
每笔持仓在每个行情切片（Tick / 1m Bar）维护以下核心变量：
- $P_0$: 期权入场成交价 (`entry_option_price`)
- $P_t$: 当前期权最新价 (`current_option_price`，优先取自 OPRA 实时期权链)
- $R_t = \frac{P_t - P_0}{P_0} \times 100\%$: 当前期权浮动收益率 (`unrealized_roi_pct`)
- $M = \max_{\tau \le t} R_\tau$: 持仓期间达到的历史最高期权收益率 (`peak_roi_pct` / `MFE`)
- $S_0, S_t, S_{\text{peak}}$: 标的入场现货价、当前现货价、达到峰值时的最优现货价
- $SL_{\text{init}}, SL_t$: 初始结构现货止损价、当前动态抬升后的现货止损价

---

### 2.2 轨道一：期权连续分段线性移动止盈棘轮 $L(M)$ (`Continuous Option Ratchet`)

根据持仓历史最高浮盈 $M$（单位：`%`），实时计算**软止损底线 `soft_sl`** 与**连续移动止盈锁定线 `trail_floor` $L(M)$**（棘轮只升不降）：

$$(\text{soft\_sl},\ L(M)) = \begin{cases}
(-35.0\%,\ -\infty), & M < 6.0\% & \text{【初始保护区】硬止损 -35\%} \\
(-12.0\%,\ -\infty), & 6.0\% \le M < 10.0\% & \text{【一阶软止损】曾涨超 +6\%，最大亏损收紧至 -12\%} \\
(-5.0\%,\ 3.0\% + 0.65(M - 10\%)), & 10.0\% \le M < 20.0\% & \text{【二阶保本启动】锁定 +3.0\% \sim +9.5\%} \\
(0.0\%,\ 9.5\% + 0.80(M - 20\%)), & 20.0\% \le M < 40.0\% & \text{【三阶趋势锁利】锁定 +9.5\% \sim +25.5\%} \\
(0.0\%,\ 25.5\% + 0.86(M - 40\%)), & 40.0\% \le M < 80.0\% & \text{【四阶主升锁利】锁定 +25.5\% \sim +59.9\%} \\
(0.0\%,\ 59.9\% + 0.90(M - 80\%)), & M \ge 80.0\% & \text{【五阶暴利锁利】每多涨 1\% 锁定 0.90\% 利润}
\end{cases}$$

- **早盘快刀加成 (`is_opening_scalp`, `09:30 - 10:00 ET`)**：若早盘入场且 $M \ge 22.0\%$，$L(M) = \max(L(M),\ 0.85 M)$。
- **分批止盈 (`SCALE_OUT_50`)**：当 $R_t \ge +80.0\%$ 且持仓张数 $\ge 2$ 且尚未减仓时，立即平掉 **50% 仓位**落袋为安，剩余 50% 作为 Runner 由 $L(M)$ 继续追踪，直至触及 **`+250.0%` 终极硬顶 (`HARD_TP_CAP`)** 或跌破 $L(M)$。

---

### 2.3 轨道二：现货五阶止损抬升棘轮 (`5-Tier Spot Stop Ratchet`)

为了防止期权因 IV 波动失真或现货走出深幅有利位移后反转回吐，现货止损价 $SL_t$ 随期权峰值浮盈 $M$ 同步向有利方向阶梯式收紧（以做空 `BEARISH` 为例，$SL_{\text{init}} > S_0 > S_{\text{peak}}$，每次取 $\min$；做多 `BULLISH` 对称取 $\max$）：

| 阶梯 | 触发条件 (期权峰值 $M$) | 做空 (`BEARISH`) 现货止损线 $SL_t$ 更新公式 | 做多 (`BULLISH`) 现货止损线 $SL_t$ 更新公式 | 战术作用 |
| :---: | :--- | :--- | :--- | :--- |
| **Tier 1** | $M \ge +10.0\%$ | $\min\big(SL_t,\ SL_{\text{init}} - 0.50(SL_{\text{init}} - S_0)\big)$ | $\max\big(SL_t,\ SL_{\text{init}} + 0.50(S_0 - SL_{\text{init}})\big)$ | 初始现货止损距离砍半 |
| **Tier 2** | $M \ge +15.0\%$ | $\min(SL_t,\ S_0)$ | $\max(SL_t,\ S_0)$ | 现货止损推至入场现货价 $S_0$（现货保本） |
| **Tier 3** | $M \ge +25.0\%$ | $\min\big(SL_t,\ S_0 - 0.50(S_0 - S_{\text{peak}})\big)$ | $\max\big(SL_t,\ S_0 + 0.50(S_{\text{peak}} - S_0)\big)$ | 锁定 50% 的现货最大有利位移 |
| **Tier 4** | $M \ge +40.0\%$ | $\min\big(SL_t,\ S_0 - 0.70(S_0 - S_{\text{peak}})\big)$ | $\max\big(SL_t,\ S_0 + 0.70(S_{\text{peak}} - S_0)\big)$ | 锁定 70% 的现货最大有利位移 |
| **Tier 5** | $M \ge +60.0\%$ | $\min\big(SL_t,\ S_0 - 0.80(S_0 - S_{\text{peak}})\big)$ | $\max\big(SL_t,\ S_0 + 0.80(S_{\text{peak}} - S_0)\big)$ | 锁定 80% 的现货最大有利位移 |

- 当现货触及抬升后的 $SL_t$（且 $SL_t$ 已越过入场价 $S_0$）时，立即触发 **`SPOT_PROFIT_STOP`（现货锁利止盈）**。

---

### 2.4 轨道三：因果三阶段日内零动能/滞涨耗损截断 (`Causal Time-Decay Truncation`)

买方期权最忌“入场后横盘不动被 Theta 慢性抽干”。针对所有日内单（非尾盘隔夜仓），按持仓时长 `held_minutes` 执行三阶快刀截断：

| 阶段 | 持仓时长 (`held_minutes`) | 历史峰值上限 ($M$) | 当前浮亏触发线 ($R_t$) | 平仓原因枚举 | 针对场景 |
| :---: | :---: | :---: | :---: | :--- | :--- |
| **Stage 0 (0DTE 专享)** | $\ge 8.0\text{ min}$ | $M \le +1.0\%$ | $R_t \le -12.0\%$ | `DEAD_MOMENTUM_TRUNCATE` | 0DTE 末日轮入场 8 分钟毫无脉冲且已亏 12%，立即砍仓防归零 |
| **Stage 1 (零动能截断)** | $\ge 10.0\text{ min}$ | $M < +4.0\%$ | $R_t \le -20.0\%$ | `DEAD_MOMENTUM_TRUNCATE` | 入场 10 分钟从未突破双边滑点摩擦成本且下挫 20%，说明假突破 |
| **Stage 2 (Theta 滞涨截断)** | $\ge 25.0\text{ min}$ | $M < +6.0\%$ | $R_t \le -15.0\%$ | `THETA_STAGNATION_TRUNCATE` | 持仓 25 分钟连一阶软保护 (+6%) 都没摸到且浮亏 15%，果断换仓 |

---

### 2.5 轨道四：尾盘隔夜仓与 0DTE 收盘门控 (`Session & Overnight Gates`)
1. **0DTE 收盘前强制平仓 (`EOD_0DTE_CLOSE`)**：所有当日到期（`expiration <= session_date`）的期权，在美东时间 **`15:50 ET`** 强制市价平仓，绝不过夜。
2. **尾盘新建隔夜仓保护 (`is_same_day_overnight`, `15:00 - 15:55 ET`, $\text{DTE} \ge 3$)**：
   - 建仓当日下午豁免日内移动止盈棘轮与时间截断，仅受初始结构现货止损线 $SL_{\text{init}}$ 与 `-35%` 硬止损保护（若收盘前直接暴涨 $\ge +60\%$ 则减仓 50%）；
   - 次日 `09:30 ET` 开盘后进入 **隔夜跳空收获引擎 (`Overnight Gap Engine`)**：
     - 若浮盈 $\ge +25\%$ 或触及目标位：触发 `GAP_EXIT_HARVEST`；
     - 若出现任何正浮盈 ($M > 0$)：激活跳空专属高比例锁利（$M \ge 20\%$ 锁 $0.85M$；$M \ge 12\%$ 锁 $0.80M$；$M \ge 6\%$ 锁 $0.70M$）；
     - 若 `09:35 ET` 后仍无跳空红利且处于浮亏 ($R_t < 0$)：立即触发 `GAP_LOSS_TRUNCATE` 截断离场。

---

## 三、给其他 AI Agent 的一键复现指令模板 (Copy-Paste Prompt)

> 只需将 [`strategy_core_spec.py`](../strategy_core_spec.py) 与下面这段 Prompt 一起发给其他 Agent，即可直接接入任何交易框架：

```text
请根据 `strategy_core_spec.py` 中的无依赖标准实现，在当前项目中接入以下两个解耦模块：
1. Heatmap 信号判定模块：使用 `HeatmapWeights`、`compute_local_gamma_regime()` 和 `compute_structure_confirmation_score()`，严格实现三层确认评分（基础分 58 分，结合节点 15m 生长率 GROWING/DECAYING、局部 Gamma 环境 TREND_DRIVER/RANGE_BUFFER/SPEED_BUMP_MUD、30m 墙体迁移、跨到期日同号共振与 5m/15m 动量共振，>=55 分放行）。
2. 期权双轨动态棘轮止盈止损模块：使用 `ExitWeights` 和 `evaluate_exit_tick()`，在每次行情更新时同步运行：
   - 期权连续分段线性棘轮 L(M)（6%/10%/20%/40%/80% 五档斜率锁利 + 80% 减半仓 + 250% 硬顶）
   - 现货五阶止损抬升棘轮（M 达 10%/15%/25%/40%/60% 时依次抬升现货止损线至砍半/保本/锁定 50%/70%/80% 现货位移）
   - 因果三阶段零动能/Theta 滞涨截断（8m 0DTE / 10m 零动能 / 25m 滞涨）
   - 15:50 ET 0DTE 强平门控与 15:00-15:55 ET 跨周期隔夜仓保护。
```
