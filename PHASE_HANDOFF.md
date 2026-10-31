# PHASE_HANDOFF｜阶段交接与验收档案（P0 + P1 + P2 + P3 已实现，含显式 PENDING 诚实声明）

## 1. 当前阶段元信息
- **项目名称**：`SUI-Quant V1.0`
- **当前阶段状态**：`P0 + P1 + P2 + P3 IMPLEMENTED_AND_VERIFIED (WITH EXPLICIT PENDING ITEMS)`
- **下一阶段待审计划**：[`docs/P4_PLAN.md`](file:///D:/OBR-SUI/docs/P4_PLAN.md)（`B-01` 盘口微观限价执行、`D-01` 决策状态机闭环与 `R-01` 独立风控硬闸门，仅 `PAPER`，状态：`READY_FOR_REVIEW`）
- **当前运行模式**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 默认禁用且受代码级不可替换公钥常量 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX` + Ed25519 非对称签名 + `PersistentNonceRegistry` 磁盘持久化防重放 + `compute_runtime_code_tree_sha256` 运行时源码树哈希绑定 + 7 项 GO/NO-GO 硬闸门拦截）
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易密钥数量**：`0`（NO）

---

## 2. P3（`A-01` 市场状态数学分类器与 `A-02` 向上放量突破三障碍校准模型）交付摘要
1. **因果零泄漏多周期特征管道（[`src/sui_quant/models/features.py`](file:///D:/OBR-SUI/src/sui_quant/models/features.py)）**：
   - 物理拦截未收盘 K 线（`UnclosedKlineTransientView` 或 `complete_bar_only=False` 立即抛出 `UnclosedBarRejectedError`）与非单调时间序列（`CausalityViolationError`）。
   - 严格基于前 20 根已完成收盘 5m K 线窗口 `[t-20 .. t-1]` 计算突破基准 `prev_20_high`、突破幅度 `breakout_pct`、相对放量倍数 `rvol_20`、成交量 Z-Score `vol_zscore_20`、上影线比例 `upper_wick_ratio`、实体比例 `body_ratio`、主动买盘占比 `taker_buy_ratio`、多周期收益率（`ret_1/3/6/12/20`, `15m`, `1h`）、`atr_14_pct`、Kaufman 效率系数 `er_20` 及 `BTCUSDT` 跨资产动量与急跌标志 `btc_flash_drop_flag`。
   - 内置 [`verify_causality_no_lookahead`](file:///D:/OBR-SUI/src/sui_quant/models/features.py#L387-L478) 数学自检：截断或极端篡改未来 `t+1 .. t+36` K 线时，时刻 `t` 的特征字典与 64 位 SHA-256 `feature_snapshot_hash` 100% 恒等。
   - 支持直接从本地 Windows 11 `Parquet + DuckDB` 研究仓（[`load_closed_bars_from_warehouse`](file:///D:/OBR-SUI/src/sui_quant/models/features.py#L481-L537)）读取已收盘 K 线序列。
2. **`A-01` 五状态数学分类器与 `BTCUSDT` 急跌否决门禁（[`src/sui_quant/models/a01_regime.py`](file:///D:/OBR-SUI/src/sui_quant/models/a01_regime.py)）**：
   - 输出标准契约 `RegimeDecision`（覆盖 `UP_TREND`、`CONSOLIDATION`、`DOWN_TREND`、`HIGH_VOLATILITY`、`DATA_UNHEALTHY` 五种状态）及配套诊断 `A01RegimeDiagnostics`（含趋势强度与五状态校准概率分布）。
   - 硬性准入约束：当处于 `DOWN_TREND`、`HIGH_VOLATILITY`、`DATA_UNHEALTHY` 时，或者当检测到 `BTCUSDT` 短窗急跌（`btc_ret_1 <= -0.8%` 或 `btc_ret_3 <= -1.5%` 或 `btc_ret_6 <= -2.2%`）时，即便 `SUIUSDT` 处于 `UP_TREND`，`allow_entry` 也强制置为 `False` 并标记 `"BTC_FLASH_DROP_VETO"`。
3. **扣费三障碍打标器、同 Bar 冲突悲观消解与 Purged & Embargoed Walk-Forward 切分器（[`src/sui_quant/models/triple_barrier.py`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py)）**：
   - 向上放量突破候选过滤（[`evaluate_breakout_candidate`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L72-L115)）：要求 `close_t > prev_20_high`、阳线实体、放量达标且上影线比例 `<= 0.45`。
   - 扣费三障碍标签（[`TripleBarrierLabeler`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L168-L287)）：止盈障碍调用 `compute_c1_net_target_sell_price` 要求扣除双边手续费后净收益达到 `+1.5%`（`WIN`）；止损障碍调用 `compute_stop_loss_trigger_price` 设于入场均价 `-1.0%`（`STOP`）；时间障碍为 `3 小时 = 36 根 5m K 线`（`TIMEOUT`）。
   - **同 Bar 冲突悲观原则**：当未来某根 K 线同时触及 `high >= win_barrier` 与 `low <= stop_barrier` 且无逐笔先后证据时，严格按悲观原则判定为 `BarrierOutcome.STOP`（`ambiguous_same_bar_conflict=True`）。
   - [`PurgedWalkForwardSplitter`](file:///D:/OBR-SUI/src/sui_quant/models/triple_barrier.py#L302-L407)：自动剔除（Purge）标签跨度与验证集重叠的训练样本，并在训练集与验证集之间强制执行 `>= 36 根 5m K 线（3 小时 = 10,800,000 ms）` Embargo 隔离带，内置 `verify_fold_zero_leakage` 零重叠校验。
4. **`A-02` 三障碍校准突破模型、精确 `Decimal` 单纯形投影、`ev_net` 与 `ModelManifest` 治理（[`src/sui_quant/models/a02_breakout.py`](file:///D:/OBR-SUI/src/sui_quant/models/a02_breakout.py)）**：
   - 严格限制仅对 `SUIUSDT` 建模与输出 `BreakoutProposal`，传入 `BTCUSDT` 立即抛出 `ValueError`。
   - [`project_to_decimal_simplex`](file:///D:/OBR-SUI/src/sui_quant/models/a02_breakout.py#L64-L105)：确保校准后三分类概率严格满足 `Decimal` 精确恒等 `p_win + p_stop + p_timeout == Decimal("1")`（`1.000000`）。
   - [`A02CostAssumptions.compute_ev_net`](file:///D:/OBR-SUI/src/sui_quant/models/a02_breakout.py#L166-L178)：显式扣除双边现货手续费、入场/止损/超时滑点与逆向选择惩罚计算净期望 `ev_net`。
   - 支持 Purged Walk-Forward 样本外温度/Dirichlet 概率校准与 L2 基线自动回退；若 `ECE > 0.08` 或 `Brier Score` 劣于常数基线，[`export_model_manifest`](file:///D:/OBR-SUI/src/sui_quant/models/a02_breakout.py#L516-L553) 抛出 `ModelCalibrationGateError` 拒绝导出制品。

---

## 3. 验证命令与实测结果（含显式 `PENDING` 清单，绝不因模拟通过而关闭问题）
- 本地 Windows 11 全量测试命令：`python -m pytest -v` → **`38 passed in 6.05s`**
- P3 专项测试命令：`python -m pytest tests/test_p3_a01_regime_and_a02_breakout.py -v` → **`6 passed`**
- 单次 `FeaturePipeline + A-01 + A-02` 完整推断延迟实测：**`P50 = 0.340 ms`、`P95 = 0.645 ms`、`P99 = 0.796 ms`**（远优于 `< 5.0 ms` 验收门槛）。
- **当前仍处于 `PENDING` 的未完结项（严格保持开启，绝不因模拟/局域测试通过而关闭或伪报 `PASS`）**：
  1. `FI-10B` 日本服务器真实连续 72 小时墙钟稳定性运行观测：**`PENDING`**（`sui-quant-72h-observer.service` 正在持续采样，完整原始证据保存在 [`evidence/real_72h_observation_snapshot.jsonl`](file:///D:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)，需满 `259,200s` = `72.0` 真实小时后方可转 `PASS`）。
  2. 外部独立心跳（Cloudflare Workers 7×24 公有云生产部署）与 Android 实网通知（`G-01/G-02` 原生客户端 + FCM 真机前后台实网推送回执）：**`PENDING`**（公有云与真机实网推送尚未接入验证，严格保持 `PENDING` 不关闭问题）。
  3. 币安私有账户 API 权限核验（`QA-02` 私有账户子项）及 P4–P8 后续阶段模块验收：**`PENDING`**。

---

## 4. 回滚方法
- 本地 P3 代码回滚：`git revert HEAD` 或 `git checkout <prev-sha>`
- 模型制品回滚：`ModelManifest.rollback_ref` 指向 `a02-suiusdt-breakout-3barrier:0.9.0-l2-baseline` 稳健 L2 基线模型。
- 云端 P2 独立回滚（保留 P1 服务）：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p2.sh`
- 云端 P1+P2 全量回滚：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p1.sh`

---

## 5. 下一阶段（P4）依赖与待审查事项
- 下一阶段为 **P4（`B-01` 智能限价执行、`D-01` 状态机闭环与 `R-01` 独立风控硬闸门，仅 `PAPER`）**，计划已写入 [`docs/P4_PLAN.md`](file:///D:/OBR-SUI/docs/P4_PLAN.md)，状态 `READY_FOR_REVIEW`，等待审查批准后执行。
