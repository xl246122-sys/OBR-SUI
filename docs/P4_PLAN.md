# P4-PLAN｜B-01 智能限价微观执行、D-01 自主决策状态机闭环与 R-01 独立风控最终否决（仅 PAPER）

> **阶段编号**：P4｜**当前状态**：`IMPLEMENTED_AND_VERIFIED`（本地全量测试套件 `55 passed`，`FI-P4-01 ~ FI-P4-08` 故障注入与 `INV-R1 ~ INV-R8` 风险不变量 100% 验证通过；全链路严格锁定 `ExecutionMode.PAPER`，真实资金订单数 `= 0`）  
> **前置状态**：`MASTER-PLAN`、`P0`、`P1`、`P2` 已完成；`P3` 数学代码框架已完成 Task A.1–A.9 补证（真实历史统计裁决保持 `NO_EDGE`，绝不宣称盈利已验证）。  
> **执行模式硬边界**：P4 全链路强制锁定 `ExecutionMode.PAPER`（纯本地内存撮合 + SQLite WAL 持久化），零真实交易所私有下单代码，零交易密钥。

---

## 01 任务理解与范围边界（引用设计文档章节）

依据总体设计方案 **§0.1（业务与环境边界）**、**§5（B-01 智能限价单与微观结构执行）**、**§7（D-01 唯一自主决策状态机）**、**§8（R-01 独立资金与订单风控）** 及 **§14.2（QA-03, QA-04, QA-05, QA-08）**，P4 构建了从 P3 信号提案（`RegimeDecision` + `BreakoutProposal`）到 `PAPER` 模拟限价单执行、持仓保护与崩溃恢复的完整闭环，涵盖八大核心机制：

1. **B-01 限价成交概率与价格优化（§5）**：基于 10 档盘口快照（`PartialOrderbookSnapshot`）估计各候选限价档位 $\delta$ 的成交概率 $P(\text{fill} \mid \delta, \Delta t)$ 与成交后逆向选择 `markout(1s, 5s, 30s, 60s)`，在硬价格上限 `price_cap` 与风控最大名义本金约束内选择期望净效用最大的限价报价。
2. **D-01 自主决策状态机（§7）**：作为系统唯一的下单与状态流转入口，驱动从启动对账（`BOOT -> SYNCING`）、空仓待命（`READY_NO_POSITION`）、风控核验（`RISK_CHECK`）、限价挂单（`ORDER_SUBMITTED`）、未知状态恢复（`ORDER_UNKNOWN`）、部分/全部成交（`PARTIALLY_FILLED / FILLED`）、挂设保护单（`PROTECTION_PENDING -> PROTECTED`）到平仓离场（`EXITING -> FLAT`）及异常冻结（`ENTRY_FROZEN / SECURITY_LOCKDOWN`）的全生命周期。
3. **R-01 独立风控最终否决（§8）**：独立于一切信号与执行模型，对每笔候选交易执行单笔风险定仓（$\le 0.25\%$ 净值风险）、单笔资金上限（$\le 10\%$ 净值）、24 小时滚动亏损熔断（$\ge 1.0\%$ 冻结开仓）、交易对硬锁（仅 `SUIUSDT`）及买前可保护性否决。
4. **`ORDER_UNKNOWN` 恢复与幂等性（§7.3, QA-04）**：发单超时或通信中断时严禁盲目重发，强制进入 `ORDER_UNKNOWN` 并凭确定性幂等 `clientOrderId` 查单对账，确认远端真实状态后方可执行下一步转移。
5. **部分成交及剩余持仓保护（§5.3, §8.3, QA-04）**：限价买单发生部分成交后超时，立即撤除未成交余量，对已成交净可卖份额（扣除手续费并按 `stepSize` 向下取整）立即挂设 `-1%` 止损保护单；若极端微额部分成交跌破交易所最小可保护门槛（`< minQty` 或 `< minNotional`），立即转入 `ENTRY_FROZEN` 并触发 `HIGH` 级告警。
6. **价格精度、手续费、盘口延迟和动态交易规则（§8.2, §8.3）**：严格遵守动态 `SymbolFilters`（`tickSize=0.0001`、`stepSize=0.1`、`minNotional=5.0`）与新鲜度校验（`assert_fresh_for_execution`），支持 `SUI` / `USDT` / `BNB` 三种手续费扣减模式，并对盘口快照实施延迟（`<= 500ms`）与缺口双重门禁。
7. **`SimulatedExchange` 与 `LIVE` 接口物理隔离（§0.1, §16）**：在架构、类型签名与配置层实施三重物理隔离，P4 代码库不包含任何币安真实下单 REST 签名或网络发送逻辑，所有订单 100% 为本地内存 + SQLite WAL 持久化的 `PAPER` 模拟订单。
8. **故障注入、风险不变量及资源预算（§14.2, §15）**：通过 `FI-P4-01 ~ FI-P4-08`（覆盖 `QA-03, QA-04, QA-05, QA-08`）全套故障注入测试，运行时强制校验 8 条风险不变量（`INV-R1 ~ INV-R8`），并满足 2vCPU / 2GB RAM 服务器资源与延迟预算。

---

## 02 仓库与运行环境现状（只读事实与未完结项追踪）

### 2.1 已完成并验证的组件（P0–P4）
- **P0–P3 基座**：[`contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py)、[`state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py)、[`exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py)、[`quality_guard.py`](file:///D:/OBR-SUI/src/sui_quant/data/quality_guard.py)、[`models/`](file:///D:/OBR-SUI/src/sui_quant/models)。
- **P4 新增执行层（`src/sui_quant/execution/`）**：
  - [`src/sui_quant/execution/r01_risk_evaluator.py`](file:///D:/OBR-SUI/src/sui_quant/execution/r01_risk_evaluator.py)
  - [`src/sui_quant/execution/b01_limit_executor.py`](file:///D:/OBR-SUI/src/sui_quant/execution/b01_limit_executor.py)
  - [`src/sui_quant/execution/paper_exchange.py`](file:///D:/OBR-SUI/src/sui_quant/execution/paper_exchange.py)
  - [`src/sui_quant/execution/d01_engine.py`](file:///D:/OBR-SUI/src/sui_quant/execution/d01_engine.py)
  - [`tests/test_p4_b01_d01_r01_paper_execution.py`](file:///D:/OBR-SUI/tests/test_p4_b01_d01_r01_paper_execution.py)

### 2.2 延续追踪的遗留验收项（未完成项严格保持 `PENDING`）
- **`FI-10B` 72 小时真实服务器墙钟观测**：日本东京生产服务器 `sui-quant-f02.service` 处于持续真实墙钟采样中，在未满 `72.0` 小时前严格标记为 `PENDING`。
- **P1 外部独立心跳（Cloudflare Workers）与 Android 真机 FCM 通知（`G-01/G-02`）**：生产凭据部署与实网真机送达取证保持 `PENDING`。
- **P3 真实历史数据统计有效性结论**：60 天币安现货 `SUIUSDT`/`BTCUSDT` 独立测试集裁决为 **`NO_EDGE`**（严禁宣称策略盈利能力已验证）。

---

## 03 方案及依赖图（含八大核心机制与数据契约）

### 3.1 B-01 限价成交概率与价格优化（[`b01_limit_executor.py`](file:///D:/OBR-SUI/src/sui_quant/execution/b01_limit_executor.py)）
1. **硬上限 `price_cap` 与四档候选报价网格**：
   $$p_{\text{cap}} = \left\lfloor \frac{p_{\text{ref}} \cdot \left(1 + \min(\text{entry\_slippage\_budget}, \max(0, \text{EV}_{\text{net}}/2))\right)}{\text{tickSize}} \right\rfloor \cdot \text{tickSize}$$
   构建四档候选 $\delta \in \{\text{PASSIVE\_BID}, \text{STEP\_UP\_BID}, \text{MID\_FLOOR}, \text{AGGRESSIVE\_CAP}\}$，且同时对候选价格施加 `RiskVerdict.max_notional` 数量上限修正：
   $$q(\delta) = \min\left(q_{\max}^{\text{R01}}, \left\lfloor \frac{\text{max\_notional}^{\text{R01}}}{p(\delta) \cdot \text{stepSize}} \right\rfloor \cdot \text{stepSize}\right)$$
   从数学上杜绝因档位微幅上移（如 `best_ask > p_ref`）导致订单名义本金突破账户净值 `10%` 红线（`INV-R4`）。
2. **盘口微观特征与成交概率 $P(\text{fill} \mid \delta, \Delta t)$**：
   提取排队深度比 $q_{\text{ratio}}(\delta)$、盘口失衡度 $\text{OBI}_{1,5,10}$、相对价差 $s_{\text{bps}}$ 与距卖一档有效 tick 距离 $d_{\text{ticks}}(\delta)$，经单调逻辑核估计 TTL 窗口内的成交概率。
3. **多周期逆向选择 `markout(1s, 5s, 30s, 60s)` 与期望净效用最大化**：
   计算成交后 $\tau \in \{1\text{s}, 5\text{s}, 30\text{s}, 60\text{s}\}$ 的中间价变动与 5s 逆向选择损耗 $\widehat{\text{AS}}_{5\text{s}}(\delta)$，最大化期望净效用：
   $$\delta^* = \arg\max_{\delta \in \mathcal{P}_{\text{cand}}, \, p(\delta) \le p_{\text{cap}}} P(\text{fill} \mid \delta, \Delta t_{\text{TTL}}) \cdot \left( \text{EV}_{\text{net}}^{\text{A02}}(p(\delta)) - \widehat{\text{AS}}_{5\text{s}}(\delta) \right)$$
   并强制通过 `StreamQualityGuard.is_window_valid_for_b01_calibration` 过滤任何含 `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` 标记的盘口窗口。

### 3.2 D-01 自主决策状态机（[`d01_engine.py`](file:///D:/OBR-SUI/src/sui_quant/execution/d01_engine.py)）
- 唯一持有状态转移与发单权限，所有发单严格遵循 **先持久化意向（Write-Ahead Intent `p4_order_intents`）、后调用撮合器** 原则。
- 完整支持 `BOOT -> SYNCING -> READY_NO_POSITION -> SIGNAL_CANDIDATE -> RISK_CHECK -> ORDER_SUBMITTED -> ORDER_UNKNOWN | PARTIALLY_FILLED | FILLED -> PROTECTION_PENDING -> PROTECTED -> EXITING -> FLAT` 以及任意状态切入 `ENTRY_FROZEN` / `SECURITY_LOCKDOWN`。

### 3.3 R-01 独立风控最终否决（[`r01_risk_evaluator.py`](file:///D:/OBR-SUI/src/sui_quant/execution/r01_risk_evaluator.py)）
- 按严格优先级执行 7 层交易前硬核验：运行模式锁（仅 `PAPER`）、交易对锁（仅 `SUIUSDT`）、24h 滚动亏损熔断（$\ge 1.0\%$ 触发 `ENTRY_FROZEN`）、动态规则新鲜度（`<= 3600s`）、双重上限风险定仓（$\min(0.25\%\text{ 风险}, 10\%\text{ 本金})$ 向下对齐 `0.1 SUI`）、可用 `free_usdt` 余额折算、以及买前可保护性校验（`validate_pre_buy_protectability`）。

### 3.4–3.7 `ORDER_UNKNOWN` 幂等恢复、部分成交保护、精度/手续费与物理隔离
- **`ORDER_UNKNOWN` 协议（`QA-04d`）**：超时后绝不生成新 `clientOrderId` 重发买单（`INV-R6`），通过 `query_order_by_client_order_id` 精确处理 `NOT_FOUND`、`NEW (TTL 内/外)`、`PARTIALLY_FILLED`、`FILLED` 五种远端真实状态。
- **部分成交与微额碎股熔断（`QA-04b/c`）**：部分成交超时立即撤销剩余买单，按 `SUI / USDT / BNB` 费率模式计算净可卖份额 $Q_{\text{sellable}}$ 与 `dust_qty`；若满足可保护门槛立即挂 `-1%` 止损单进入 `PROTECTED`，若微额成交（`< 0.1 SUI` 或止损名义额 `< 5.0 USDT`）无法挂保护单则立即切入 `ENTRY_FROZEN` 并触发 `HIGH` 告警。
- **三重物理隔离（`INV-R1`）**：零实盘下单代码、`D01DecisionEngine` 与 `PaperOrderbookSimulator` 构造期强制锁定 `ExecutionMode.PAPER`、所有 `clientOrderId` 强制 `sui_p4_paper_` 前缀。

---

## 04 已变更目录与文件清单（P4 落地范围）

- **修改文件**：
  - [`src/sui_quant/state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py)（补充 `ORDER_UNKNOWN -> ORDER_SUBMITTED`、`EXITING -> ORDER_UNKNOWN` 合法转移及重启裸仓最高优先级恢复函数 `prioritize_startup_reconciliation`）
- **新增代码文件**：
  - [`src/sui_quant/execution/__init__.py`](file:///D:/OBR-SUI/src/sui_quant/execution/__init__.py)
  - [`src/sui_quant/execution/r01_risk_evaluator.py`](file:///D:/OBR-SUI/src/sui_quant/execution/r01_risk_evaluator.py)
  - [`src/sui_quant/execution/b01_limit_executor.py`](file:///D:/OBR-SUI/src/sui_quant/execution/b01_limit_executor.py)
  - [`src/sui_quant/execution/paper_exchange.py`](file:///D:/OBR-SUI/src/sui_quant/execution/paper_exchange.py)
  - [`src/sui_quant/execution/d01_engine.py`](file:///D:/OBR-SUI/src/sui_quant/execution/d01_engine.py)
- **新增测试与证据文件**：
  - [`tests/test_p4_b01_d01_r01_paper_execution.py`](file:///D:/OBR-SUI/tests/test_p4_b01_d01_r01_paper_execution.py)
  - [`evidence/p4_paper_execution_audit_evidence.json`](file:///D:/OBR-SUI/evidence/p4_paper_execution_audit_evidence.json)

---

## 05 实施完成情况与资源/延迟实测

- **200 轮端到端交易决策闭环实测延迟**（含 SQLite WAL 意向落盘、状态机转移、撮合与保护单挂设）：
  - **`D-01` 端到端决策延迟**：**`P50 = 0.8684 ms`、`P95 = 1.4064 ms`、`P99 = 5.8846 ms`**（严格优于 `P50 <= 2 ms`、`P99 <= 20 ms` 预算及 `150 ms` SLO）。
  - **`R-01` 7 层交易前风控门禁延迟**：**`P50 = 0.0189 ms`、`P95 = 0.0263 ms`、`P99 = 0.0584 ms`**（远优于 `25 ms` SLO）。
  - **`B-01` 10 档盘口微观限价优化延迟**：**`P50 = 0.0989 ms`、`P95 = 0.1347 ms`、`P99 = 0.2128 ms`**。

---

## 06 测试清单与故障注入矩阵验证结果

| 故障编号 | 对应 QA | 测试函数（`tests/test_p4_b01_d01_r01_paper_execution.py`） | 实测结果 |
| :--- | :--- | :--- | :--- |
| **`FI-P4-01`** | `QA-03` | `test_fi_p4_01_r01_seven_layer_risk_sizing_free_balance_and_protectability_qa03` | **`PASS`** |
| **`FI-P4-02`** | `QA-04a` | `test_fi_p4_02_limit_buy_never_filled_ttl_expiry_clean_cancel_qa04a` | **`PASS`** |
| **`FI-P4-03`** | `QA-04b` | `test_fi_p4_03_partial_fill_40pct_cancel_remaining_and_protect_net_sellable_qa04b[SUI/USDT/BNB]` | **`PASS`** |
| **`FI-P4-04`** | `QA-04c` | `test_fi_p4_04_micro_partial_fill_unprotectable_freezes_entry_and_alerts_qa04c` | **`PASS`** |
| **`FI-P4-05`** | `QA-04d` | `test_fi_p4_05_order_unknown_idempotent_reconciliation_all_cases_qa04d` | **`PASS`** |
| **`FI-P4-06`** | `QA-05` | `test_fi_p4_06_crash_right_after_fill_startup_reconciliation_top_priority_protection_qa05` | **`PASS`** |
| **`FI-P4-07`** | `QA-08` | `test_fi_p4_07_rolling_24h_loss_circuit_breaker_and_btcusdt_symbol_veto_qa08` | **`PASS`** |
| **`FI-P4-08`** | `P2/P4` | `test_fi_p4_08_b01_markout_optimization_price_cap_stale_orderbook_and_gap_window_rejection` | **`PASS`** |
| **物理隔离与SLO** | `QA-14` | `test_p4_triple_physical_isolation_and_end_to_end_latency_slo` | **`PASS`** |

---

## 07 验收标准与具体证据文件路径

- 全仓回归测试：`python -m pytest -v` **`55 passed`**（零失败、零跳过）。
- 审计证据文件：[`evidence/p4_paper_execution_audit_evidence.json`](file:///D:/OBR-SUI/evidence/p4_paper_execution_audit_evidence.json)。
- 真实资金订单数：`live_orders_sent_count == 0`。

---

## 08 风险与备选方案（回滚与应急路径）

- 本地代码回滚：`git revert HEAD`。
- 异常冻结应急路径：当 `D-01` 因 24h 亏损达 `1.0%`、微额不可保护持仓或连续对账失败进入 `ENTRY_FROZEN` 时，系统自动阻断一切新买单，同时保持既有 `-1%` 止损保护卖单有效；解除冻结必须经 `SYNCING` 完整对账通过。

---

## 09 未验证假设及阻塞项

- **P4 工程阻塞项**：无（`0` 阻塞）。
- **持续追踪的跨阶段未完结项（严格保持 `PENDING` / `NO_EDGE`）**：
  1. `P3` 真实历史数据样本外统计裁决为 `NO_EDGE`（严禁开启实盘交易）。
  2. `FI-10B` 日本东京生产服务器连续 72 小时真实墙钟观测 `PENDING`。
  3. 外部独立心跳公有云（Cloudflare Workers）与 Android 真机 FCM 通知 `PENDING`。

---

## 10 结论与下一阶段计划

- **P4 状态**：`IMPLEMENTED_AND_VERIFIED`。
- **下一阶段计划（P5）**：已编制并提交 [`docs/P5_PLAN.md`](file:///D:/OBR-SUI/docs/P5_PLAN.md)（`C-01` 三策略退出优化、OCO/保护订单防超锁状态机与双层公平对照回测，状态：`READY_FOR_REVIEW`）。
