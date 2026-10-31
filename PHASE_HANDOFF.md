# PHASE_HANDOFF｜阶段交接与验收档案（P4 PAPER 微观执行与状态机闭环完成 + P5 正式计划待审 + 遗留项持续追踪）

## 1. 当前阶段元信息与阶段执行状态
- **项目名称**：`SUI-Quant V1.0`
- **当前已完成阶段**：
  - **P0 + P1 + P2**：基础契约、主机安全与守护、实时 WebSocket 采集与 `Parquet + DuckDB` 同步。
  - **P3（数学代码框架与真实历史审计）**：`Task A.1–A.9` 全部数学正确性补证已完成（详见 [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)）；**60 天币安现货真实历史数据独立样本外统计裁决保持 `NO_EDGE`（严禁宣称策略盈利能力已验证）**。
  - **P4（B-01 智能限价微观执行、D-01 自主决策状态机闭环与 R-01 独立风控最终否决）**：**`IMPLEMENTED_AND_VERIFIED`**（全仓测试 `55 passed`，`FI-P4-01 ~ FI-P4-08` 故障注入与 `INV-R1 ~ INV-R8` 运行时风险不变量 100% 通过，详见 [`docs/P4_PLAN.md`](file:///D:/OBR-SUI/docs/P4_PLAN.md) 与 [`evidence/p4_paper_execution_audit_evidence.json`](file:///D:/OBR-SUI/evidence/p4_paper_execution_audit_evidence.json)）。
- **下一阶段计划（P5）**：[`docs/P5_PLAN.md`](file:///D:/OBR-SUI/docs/P5_PLAN.md) 已提交，状态 **`READY_FOR_REVIEW`**（未获批准前绝不执行 P5 交易代码修改）。
- **当前运行模式硬边界**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 物理禁用）。
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易账户密钥数量**：`0`（NO）

---

## 2. P4 核心交付与验收摘要

1. **新增/更新可审查源码与测试文件**：
   - [`src/sui_quant/state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py)（新增崩溃重启裸仓最高优先级恢复函数 `prioritize_startup_reconciliation`）
   - [`src/sui_quant/execution/r01_risk_evaluator.py`](file:///D:/OBR-SUI/src/sui_quant/execution/r01_risk_evaluator.py)（7 层独立交易前风控审查与最终否决门禁 `R01PreTradeRiskGate`）
   - [`src/sui_quant/execution/b01_limit_executor.py`](file:///D:/OBR-SUI/src/sui_quant/execution/b01_limit_executor.py)（10 档盘口特征、成交概率、`markout(1s, 5s, 30s, 60s)` 逆向选择惩罚、`price_cap` 与名义额双重约束优化器 `B01LimitOrderOptimizer`）
   - [`src/sui_quant/execution/paper_exchange.py`](file:///D:/OBR-SUI/src/sui_quant/execution/paper_exchange.py)（纯本地内存 + SQLite WAL 持久化模拟撮合器 `PaperOrderbookSimulator`，支持 `SUI / USDT / BNB` 三种手续费模式与超时故障注入）
   - [`src/sui_quant/execution/d01_engine.py`](file:///D:/OBR-SUI/src/sui_quant/execution/d01_engine.py)（唯一自主决策状态机 `D01DecisionEngine`，强制 Write-Ahead Intent、`ORDER_UNKNOWN` 幂等查单对账与微额碎股熔断）
   - [`tests/test_p4_b01_d01_r01_paper_execution.py`](file:///D:/OBR-SUI/tests/test_p4_b01_d01_r01_paper_execution.py)（11 项 P4 专项测试与参数化费率验证，全库 `55 passed`）
2. **故障注入矩阵（`FI-P4-01 ~ FI-P4-08`）与风险不变量（`INV-R1 ~ INV-R8`）验证**：
   - `FI-P4-01 (QA-03)`：双重上限（`0.25%` 风险 & `10%` 仓位）向下取整 `stepSize=0.1`、可用余额不足自动缩减/拒单、买前可保护性拦截与过期 `SymbolFilters` 拦截 `PASS`。
   - `FI-P4-02 (QA-04a)`：限价买单在 TTL 内 `0%` 成交到期自动撤单，干净回退 `READY_NO_POSITION` `PASS`。
   - `FI-P4-03 (QA-04b)`：`40%` 部分成交后超时立即撤除剩余 `60%` 挂单，在 `SUI` / `USDT` / `BNB` 三种费率模式下精确计算净可卖份额并挂设 `-1%` 保护止损单进入 `PROTECTED` `PASS`。
   - `FI-P4-04 (QA-04c)`：极微额部分成交（`0.2 SUI`，名义价值 `< 5.0 USDT`）无法挂保护单时立即撤余单、转入 `ENTRY_FROZEN` 并触发 `HIGH` 告警 `PASS`。
   - `FI-P4-05 (QA-04d)`：`submit_limit_buy` 超时进入 `ORDER_UNKNOWN`，期间新发买单次数严格为 `0`（`INV-R6`），凭幂等 `clientOrderId` 对账恢复 `NOT_FOUND` 与 `FILLED` 全场景 `PASS`。
   - `FI-P4-06 (QA-05)`：买单 `FILLED` 瞬间模拟进程崩溃，重启进入 `BOOT -> SYNCING` 最高优先级切入 `PROTECTION_PENDING -> PROTECTED` 补齐 `-1%` 止损单 `PASS`。
   - `FI-P4-07 (QA-08)`：24h 滚动亏损达 `1.0%` 触发 `ENTRY_FROZEN` 熔断，`BTCUSDT` 下单请求被 `SYMBOL_FORBIDDEN` 100% 拦截 `PASS`。
   - `FI-P4-08 (P2/P4)`：陈旧盘口（`> 500 ms`）拒单与 `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` 缺口窗口硬过滤 `PASS`。
3. **200 轮端到端延迟基准实测**（归档于 [`evidence/p4_paper_execution_audit_evidence.json`](file:///D:/OBR-SUI/evidence/p4_paper_execution_audit_evidence.json)）：
   - `D-01` 端到端决策延迟（含 SQLite WAL 持久化）：**`P50 = 0.8684 ms`、`P95 = 1.4064 ms`、`P99 = 5.8846 ms`**（远优于 `150 ms` SLO）。
   - `R-01` 7 层交易前风控门禁延迟：**`P50 = 0.0189 ms`、`P95 = 0.0263 ms`、`P99 = 0.0584 ms`**（远优于 `25 ms` SLO）。
   - `B-01` 10 档盘口微观限价优化延迟：**`P50 = 0.0989 ms`、`P95 = 0.1347 ms`、`P99 = 0.2128 ms`**。

---

## 3. 持续追踪的跨阶段遗留项（未完成项严格保持 `PENDING` / `NO_EDGE`）
1. **P3 真实历史数据样本外统计有效性**：**`NO_EDGE`**（60 天真实历史数据独立测试集 `FINAL_TEST` 平均净期望 `-28.12 bps`、平均实际净收益 `-70.98 bps`，未获正期望统计优势，严禁开启实盘交易）。
2. **`FI-10B` 日本东京生产服务器连续 72 小时真实墙钟运行观测**：**`PENDING`**（后台持续采样中，证据归档于 [`evidence/real_72h_observation_snapshot.jsonl`](file:///D:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)，未满 `72.0` 真实小时前绝不伪报 `PASS`）。
3. **P1 外部独立心跳（Cloudflare Workers 公有云生产部署）与 Android 真机实网通知（`G-01/G-02` FCM 推送）**：**`PENDING`**。

---

## 4. 风险清单与回滚方法
- **本地代码回滚**：`git revert HEAD`
- **异常冻结恢复**：任何 `ENTRY_FROZEN` 或 `SECURITY_LOCKDOWN` 状态仅允许既有保护平仓卖单执行，恢复正常状态必须通过 `BOOT -> SYNCING` 完整对账门禁。
