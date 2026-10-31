# PHASE_HANDOFF｜阶段交接与验收档案（P5 C-01 三策略退出优化与双层公平对照回测完成 + P6 正式计划待审 + 遗留项持续追踪）

## 1. 当前阶段元信息与阶段执行状态
- **项目名称**：`SUI-Quant V1.0`
- **当前已完成阶段**：
  - **P0 + P1 + P2**：基础契约、主机安全与守护、实时 WebSocket 采集与 `Parquet + DuckDB` 同步。
  - **P3（数学代码框架与真实历史审计）**：`Task A.1–A.9` 全部数学正确性补证已完成（详见 [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)）；**60 天币安现货真实历史数据独立样本外统计裁决保持 `NO_EDGE`（严禁宣称策略盈利能力已验证）**。
  - **P4（B-01 智能限价微观执行、D-01 自主决策状态机闭环与 R-01 独立风控最终否决）**：**`IMPLEMENTED_AND_VERIFIED`**（`FI-P4-01 ~ FI-P4-08` 故障注入与 `INV-R1 ~ INV-R8` 风险不变量 100% 通过）。
  - **P5（C-01 三策略退出优化、保护订单防超锁管理与双层公平对照回测）**：**`IMPLEMENTED_AND_VERIFIED`**（全仓测试 `64 passed`，`QA-06 / QA-07 / QA-13` 100% 通过，详见 [`docs/P5_PLAN.md`](file:///D:/OBR-SUI/docs/P5_PLAN.md) 与 [`evidence/p5_c01_exit_comparison_evidence.json`](file:///D:/OBR-SUI/evidence/p5_c01_exit_comparison_evidence.json)）。
- **下一阶段计划（P6）**：[`docs/P6_PLAN.md`](file:///D:/OBR-SUI/docs/P6_PLAN.md) 已提交，状态 **`READY_FOR_REVIEW`**（未获批准前绝不执行 P6 代码修改）。
- **当前运行模式硬边界**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 物理禁用）。
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易账户密钥数量**：`0`（NO）

---

## 2. P5 核心交付与验收摘要

1. **新增可审查源码与测试文件**：
   - [`src/sui_quant/exit/c01_strategies.py`](file:///D:/OBR-SUI/src/sui_quant/exit/c01_strategies.py)（实现 `C1FixedNetTargetExit`、`C2GridTrailingExit`、`C3AdaptiveHybridExit` 与防超锁协调器 `OcoProtectionCoordinator`）
   - [`src/sui_quant/exit/fair_backtester.py`](file:///D:/OBR-SUI/src/sui_quant/exit/fair_backtester.py)（实现双层公平回测对照引擎 `FairExitComparisonBacktester`，支持 Layer 1 悲观 K 线与 Layer 2 盘口微观及压力滑点回测）
   - [`src/sui_quant/exit/__init__.py`](file:///D:/OBR-SUI/src/sui_quant/exit/__init__.py)
   - [`tests/test_p5_c01_exit_and_backtest.py`](file:///D:/OBR-SUI/tests/test_p5_c01_exit_and_backtest.py)（9 项 P5 专项测试与多费率资产参数化验证，全库 `64 passed`）
2. **`QA-06 / QA-07 / QA-13` 核心验证结果**：
   - **`QA-06`（防超锁与 `minNotional` 分批退化保护）**：
     - 全生命周期强制校验 `exchange.locked_sui == PositionLedger.reserved_qty <= sellable_qty <= actual_qty`，每次分批成交严格遵循“先撤旧保护单释放锁仓 $\rightarrow$ 执行分批卖出 $\rightarrow$ 重算剩余可卖份额并挂新保护单”的原子三步协议，超锁违规次数 `= 0`，裸仓遗留次数 `= 0`。
     - `C2GridTrailingExit.plan_tranches()` 在 `$100` 仓位（`50.0 SUI`）启用完整 `3` 档（`40%/30%/30%`），在 `$12` 仓位（`6.0 SUI`）自动退化为 `2` 档（`50%/50%`），在 `$6` 仓位（`3.0 SUI`）自动退化为 `1` 档（`100%` `C1` 模式），彻底消除 `< $5.25` 残余碎股死锁。
   - **`QA-07`（快速跳空跌穿 `-1%` 止损限价、实际亏损 `> 1%` 与保护单拒单回退）**：
     - 注入 `-3.5%` 瞬间闪崩（从 `$2.0000` 跳空至 `$1.9300` 跌穿 `$1.9800` 止损限价）及保护单拒单故障，验证协调器立即撤除被穿透的限价单并按含压力滑点价格强制扫单平仓，如实记录 `-3.84%` 实际净亏损（证明 `-1%` 为触发线而非最大亏损上限），同时触发 `HIGH` 级告警且零裸仓遗留。
     - 同根 K 线同时触及止损与止盈时，100% 按悲观原则优先触发 `STOP`。
   - **`QA-13`（`C1 / C2 / C3` 双层严格同入场、同风控、同费用对照与奥卡姆剃刀择优）**：
     - 在 115 笔独立样本外测试集上验证 100% 相同入场时间、相同入场均价、相同 `R-01` 仓位、相同 `-1%` 初始止损与相同费率：
       - **Layer 1 (`BAR_LEVEL_PESSIMISTIC`)**：`C1 = -71.89 bps`、`C2 = -57.35 bps`、`C3 = -51.88 bps`；
       - **Layer 2 (`ORDERBOOK_MICROSTRUCTURE_STRESS`)**：`C1 = -77.87 bps`、`C2 = -63.43 bps`、`C3 = -58.27 bps`；
     - 统计裁决诚实输出 **`NO_EDGE`**，并遵循奥卡姆剃刀原则将最简基线 **`C1`** 锁定为默认激活退出策略。
3. **345 次交易退出生命周期评估延迟实测**（归档于 [`evidence/p5_c01_exit_comparison_evidence.json`](file:///D:/OBR-SUI/evidence/p5_c01_exit_comparison_evidence.json)）：
   - **`P50 = 0.1108 ms`、`P95 = 0.2388 ms`、`P99 = 0.2986 ms`**（优于 `0.5 ms` 云端预算）。

---

## 3. 持续追踪的跨阶段遗留项（未完成项严格保持 `PENDING` / `NO_EDGE`）
1. **P3 & P5 真实历史数据样本外统计有效性**：**`NO_EDGE`**（60 天真实历史数据独立测试集 `FINAL_TEST` 净期望为负，未获正期望统计优势，严禁开启实盘交易）。
2. **`FI-10B` 日本东京生产服务器连续 72 小时真实墙钟运行观测**：**`PENDING`**（后台持续采样中，未满 `72.0` 真实小时前绝不伪报 `PASS`）。
3. **P1 外部独立心跳（Cloudflare Workers 公有云生产部署）与 Android 真机实网通知（`G-01/G-02` FCM 推送）**：**`PENDING`**。

---

## 4. 风险清单与回滚方法
- **本地代码回滚**：`git revert HEAD`
- **异常冻结恢复**：任何 `ENTRY_FROZEN` 或 `SECURITY_LOCKDOWN` 状态仅允许既有保护平仓卖单执行，恢复正常状态必须通过 `BOOT -> SYNCING` 完整对账门禁。
