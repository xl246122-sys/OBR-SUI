# PHASE_HANDOFF｜阶段交接与验收档案（P3 数学审计补证 + P4 正式计划审查 + 遗留项持续追踪）

## 1. 当前阶段元信息与阶段决定执行状态
- **项目名称**：`SUI-Quant V1.0`
- **当前阶段决定执行状态**：
  - **P3 数学代码框架**：已完成 `Task A.1–A.9` 全部数学正确性补证与 12 项专项测试验证（全库 `44 passed`），详见 [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)。
  - **P3 真实历史数据统计有效性裁决（Task A.9）**：**`NO_EDGE`**（基于 60 天币安现货 `SUIUSDT` / `BTCUSDT` `17,280` 根已收盘 5m K 线，独立样本外测试集 `FINAL_TEST` $N=115$ 的平均净期望 `mean_ev_net = -0.002812`、平均实际净收益 `mean_realized_net_return = -0.007098`，95% Bootstrap 置信区间 `[-0.008489, -0.005184]`，**严禁宣称盈利能力已验证**）。
  - **P4 正式实施计划（Task B）**：[`docs/P4_PLAN.md`](file:///D:/OBR-SUI/docs/P4_PLAN.md) 已提交，状态 **`READY_FOR_REVIEW`**（未获批准前绝不执行 P4 交易代码实施）。
- **当前运行模式硬边界**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 物理禁用）。
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易账户密钥数量**：`0`（NO）

---

## 2. 任务 A（P3 数学正确性补证 9 项）完成摘要
1. **可审查源码与测试文件（Task A.1）**：
   - [`src/sui_quant/models/features.py`](file:///D:/OBR-SUI/src/sui_quant/models/features.py)
   - [`src/sui_quant/models/a01_regime.py`](file:///D:/OBR-SUI/src/sui_quant/models/a01_regime.py)
   - [`src/sui_quant/models/triple_barrier.py`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py)
   - [`src/sui_quant/models/a02_breakout.py`](file:///D:/OBR-SUI/src/sui_quant/models/a02_breakout.py)
   - [`tests/test_p3_a01_regime_and_a02_breakout.py`](file:///D:/OBR-SUI/tests/test_p3_a01_regime_and_a02_breakout.py)
2. **信号确认与参考入场报价零时间穿越证明（Task A.2）**：
   - 默认采用 `EntryPriceMode.NEXT_BAR_OPEN`，信号在第 $t$ 根 5m K 线收盘时刻 $T_{\text{sig}} = \text{bars}[t].\text{close\_time\_ms}$ 确认，参考入场报价取下一根 K 线开盘价 $P_{\text{entry}} = \text{bars}[t+1].\text{open\_price}$（报价时刻 $T_{\text{quote}} = \text{bars}[t+1].\text{open\_time\_ms} = T_{\text{sig}} + 1\text{ ms} > T_{\text{sig}}$），且三障碍仅在 `bars[t+1 .. t+36]` 上扫描。
   - [`TripleBarrierLabel.__post_init__`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L209-L234) 与 [`assert_no_signal_to_entry_time_travel`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L396-L420) 对全部 `458` 个真实历史候选事件 100% 验证通过。
3. **三障碍标签与 `A02CostAssumptions` 防重复扣费核查（Task A.3）**：
   - `WIN` 止盈目标价 $P_{\text{win}}$ 已由 `compute_c1_net_target_sell_price` 内嵌双边手续费 $(f_{\text{buy}}=0.10\%, f_{\text{sell}}=0.10\%)$ 以锁死 $+1.50\%$ 扣费净收益；因此 `A02CostAssumptions.net_win_payoff` 仅扣除入场滑点与逆向选择惩罚 $s_{\text{entry}} + a_{\text{as}} = 7\text{ bps}$（得到 $+1.4300\%$），绝不二次扣减双边手续费。
   - `STOP` 与 `TIMEOUT` 分支均采用精确乘性公式 $\frac{P_{\text{exit}}(1 - f_{\text{sell}})}{P_{\text{entry}}(1 + f_{\text{buy}})} - 1$ 扣除 1 次双边手续费，再减去入场摩擦 $7\text{ bps}$。`verify_no_double_fee_counting()` 验证 `double_fee_counting_detected = False`。
4. **`TIMEOUT` 实际净收益计算公式（Task A.4）**：
   - 在第 $t+36$ 根 5m K 线收盘时，取 $P_{\text{raw\_exit}} = \text{bars}[t+36].\text{close\_price}$，扣除超时退出滑点 $s_{\text{timeout}}=2\text{ bps}$ 并向下对齐 `tickSize=0.0001` 得 $P_{\text{exit}}$，实际净收益严格为 $R_{\text{net}}(\text{TIMEOUT}) = \frac{P_{\text{exit}}(1 - f_{\text{sell}})}{P_{\text{entry}}(1 + f_{\text{buy}})} - 1$。
5. **训练 (`TRAIN`)、概率校准 (`CALIBRATION`) 与最终独立测试 (`FINAL_TEST`) 三阶段物理隔离（Task A.5）**：
   - [`PurgedWalkForwardSplitter.split_train_calibration_test`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L510-L569) 与 [`verify_three_stage_zero_leakage`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L571-L607) 在 `TRAIN -> CALIBRATION` 与 `CALIBRATION -> FINAL_TEST` 两个边界同时强制执行标签跨度 Purge 与 $\ge 36$ 根 5m K 线（`3 小时`）Embargo（实测最小间隔均为 `47` 根 5m K 线 = `3.92 小时`）。
   - `TRAIN` ($N=228$) 仅拟合特征缩放与线性权重；`CALIBRATION` ($N=114$) 仅拟合温度 $T$ 与 Dirichlet 校准偏置；`FINAL_TEST` ($N=115$) 完全冻结参数，仅用于一次性最终独立验收。
6. **真实历史数据统计、置信区间与成本敏感性（Task A.6）**：
   - 完整原始证据已持久化于 [`evidence/p3_real_historical_audit_evidence.json`](file:///D:/OBR-SUI/evidence/p3_real_historical_audit_evidence.json) 并汇总于 [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)。
7. **`BTCUSDT` 急跌显式时间窗口与样本外消融对照（Task A.7）**：
   - 显式定义 `btc_ret_5m <= -0.80%`（5分钟/1根5m）、`btc_ret_15m <= -1.50%`（15分钟/3根5m）、`btc_ret_30m <= -2.20%`（30分钟/6根5m）三个时间窗口，并提供 `enable_btc_flash_drop_filter` 开关完成样本外与全样本消融对照。
8. **`1h` 与 `15m` 高周期特征严格已收盘因果校验（Task A.8）**：
   - [`_extract_strict_htf_return`](file:///D:/OBR-SUI/src/sui_quant/models/features.py#L228-L273) 强制校验 `b.is_closed == True`、`b.close_time_ms <= bar_5m.close_time_ms` 且周期跨度完整（`900,000 ms` / `3,600,000 ms`），物理杜绝引用正在形成中的 `15m`/`1h` K 线。
9. **无统计优势时显式输出 `NO_EDGE` / `INSUFFICIENT_EVIDENCE`（Task A.9）**：
   - 真实历史数据独立测试集裁决输出 `StatisticalVerdict.NO_EDGE`。

---

## 3. 任务 B（P4 正式实施计划）与任务 C（遗留验收持续追踪）状态
- **P4 正式实施计划**：已更新至 [`docs/P4_PLAN.md`](file:///D:/OBR-SUI/docs/P4_PLAN.md)，完整覆盖 `B-01` 成交概率与 `markout` 价格优化、`D-01` 12 状态机、`R-01` 7 层独立风控最终否决、`ORDER_UNKNOWN` 幂等恢复、部分成交与微额碎股保护、动态精度/手续费/盘口延迟门禁、`SimulatedExchange` 与 `LIVE` 三重物理隔离及故障注入矩阵。
- **遗留验收项追踪（Task C，未完成项严格保持 `PENDING`）**：
  1. **`FI-10B` 日本东京生产服务器连续 72 小时真实墙钟运行观测**：**`PENDING`**（后台持续采样中，证据归档于 [`evidence/real_72h_observation_snapshot.jsonl`](file:///D:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)，未满 `72.0` 真实小时前绝不伪报 `PASS`）。
  2. **P1 外部独立心跳（Cloudflare Workers 公有云生产部署）与 Android 真机实网通知（`G-01/G-02` FCM 推送）**：**`PENDING`**。
  3. **P2 数据缺口（`GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION`）与极端背压自动降级修复证据**：**`PASS`**（持续在回归测试套件与生产守护中保持可追踪）。

---

## 4. 风险清单（Risk Checklist）
1. **统计优势缺失风险（`NO_EDGE`）**：60 天真实历史数据表明，单纯依赖 5m K 线 20-bar 价格突破与成交量放大在非平稳震荡/阴跌市中样本外胜率仅 `9.57%`，扣费后净期望为负（`-28.12 bps`）。因此绝不可开启真实资金交易，P4 仅能在 `PAPER` 模拟环境中验证微观执行与风控状态机工程闭环。
2. **分布漂移与校准失准风险（Regime Shift & Calibration Drift）**：`TRAIN`（胜率 `34.65%`）到 `FINAL_TEST`（胜率 `9.57%`）的基率剧烈漂移导致样本外 `ECE` 升至 `0.105360`（超过 `0.08` 门限），触发 `ModelCalibrationGateError` 自动阻断模型上架。
3. **被动限价逆向选择与微额部分成交风险**：在 P4 中必须通过 `B-01` 的 `markout(1s, 5s, 30s, 60s)` 惩罚与 `D-01` 对微额不可保护持仓（`< 0.1 SUI` 或 `< 5.0 USDT`）立即冻结（`ENTRY_FROZEN`）机制予以控制。

---

## 5. 回滚方法
- 本地代码回滚：`git revert HEAD`
- 模型制品回滚：`ModelManifest.rollback_ref` 指向稳健 L2 基线或直接由 `ModelCalibrationGateError` 拦截上架。
- 云端服务回滚：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p2.sh`
