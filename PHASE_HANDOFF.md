# PHASE_HANDOFF｜阶段交接与验收档案

## 1. 当前阶段元信息
- **项目名称**：`SUI-Quant V1.0`
- **已完成阶段**：`MASTER-PLAN` + `P0（含监理审查反馈全量整改：Ed25519签名门禁、防重放、买前可保护性及碎股边界、静态/动态过滤器隔离）`
- **当前运行模式**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 默认禁用且受 Ed25519 非对称签名 + 7 项 GO/NO-GO + Nonce 防重放多重硬闸门拦截）
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易密钥数量**：`0`（NO）

---

## 2. 本阶段完成交付摘要
1. **总体与阶段规划归档**：
   - [`docs/SUI-Quant_V1_总体技术设计方案.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_总体技术设计方案.md)
   - [`docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md)
   - [`docs/MASTER_PLAN.md`](file:///D:/OBR-SUI/docs/MASTER_PLAN.md)
   - [`docs/P0_PLAN.md`](file:///D:/OBR-SUI/docs/P0_PLAN.md)
   - [`docs/P1_PLAN.md`](file:///D:/OBR-SUI/docs/P1_PLAN.md)（下一阶段待审计划，`READY_FOR_REVIEW`）
2. **只读核验与规范矩阵**：
   - [`docs/API_CAPABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/API_CAPABILITY_MATRIX.md)：通过日本云主机实测币安现货公开 API `/api/v3/exchangeInfo`（`SUIUSDT` & `BTCUSDT` 精度、过滤器、速率限制与 15.6ms 网络延迟）。
   - [`docs/THREAT_MODEL.md`](file:///D:/OBR-SUI/docs/THREAT_MODEL.md)：四域信任边界、STRIDE 威胁矩阵与 INFO/HIGH/CRITICAL 三级响应铁律。
   - [`docs/REQUIREMENTS_TRACEABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/REQUIREMENTS_TRACEABILITY_MATRIX.md)：`REQ-01 ~ REQ-14` 与 `QA-01 ~ QA-14` 全量覆盖映射。
   - [`docs/adr/`](file:///D:/OBR-SUI/docs/adr)：`ADR-001` 至 `ADR-004` 四份架构决策记录。
3. **核心代码基座落地（[`src/sui_quant/`](file:///D:/OBR-SUI/src/sui_quant)）**：
   - [`contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py)：十大标准跨模块不可变契约，强制 `Decimal`（拒绝二进制 `float`）与 `UTC` 时间戳。
   - [`config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py)：`RiskConfig` 不可放宽硬边界（仅限 `SUIUSDT` 现货、`-1%` 止损线、单笔风险 `<=0.25%`、单次资金占用 `<=10%`、24h 亏损熔断 `<=1%`）与 `SystemConfig` 三重 `LIVE` 门禁校验。
   - [`exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py)：实测 `SUIUSDT`（`tickSize=0.0001`, `stepSize=0.1`, `minNotional=5.0`）过滤器核验、买单扣费碎股向下取整保护、C1 扣费净 `+1.5%` 目标卖价公式与 `-1%` 止损触发价计算。
   - [`state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py)：`D-01` 交易与保护状态机（15 个标准状态及合法状态转移守卫）。
   - [`security/redaction.py`](file:///D:/OBR-SUI/src/sui_quant/security/redaction.py)：全链路日志与字典敏感字段脱敏器。
   - [`utils/clock.py`](file:///D:/OBR-SUI/src/sui_quant/utils/clock.py) & [`utils/ids.py`](file:///D:/OBR-SUI/src/sui_quant/utils/ids.py)：UTC/单调纳秒双时钟与幂等 `clientOrderId` 生成器。

---

## 3. 验证命令与测试结果
```powershell
python -m pytest -v
```
- **测试结果**：`12 passed in 0.07s`（覆盖 `QA-03`, `QA-04`, `QA-05`, `QA-06`, `QA-07`, `QA-08`, `QA-10`, `QA-14` 及监理补充的 Task 1.1 ~ 1.6 全部边界用例，详见 [`docs/P0_REVIEW_REMEDIATION.md`](file:///D:/OBR-SUI/docs/P0_REVIEW_REMEDIATION.md)）。

---

## 4. 前后兼容与回滚方法
- P0 为初始架构基座，未修改远端云服务器运行服务。
- 如需回滚本地更改，可执行 `git checkout <commit-sha>` 或重置工作区。

---

## 5. 最严重的三个技术风险与应对（供监理审查）
1. **`SUIUSDT` 步长（`stepSize=0.1`）与精度（`baseAssetPrecision=8`）不对称引发的碎股（Dust）锁死风险**：
   - 实测发现 `SUIUSDT` 最小数量步长为 `0.1 SUI`。若买入 `10.0 SUI` 且以 `SUI` 支付 `0.1%` 手续费，实际到账 `9.99 SUI`；若止损/止盈单直接按 `10.0` 或 `9.99` 下单将被币安 `LOT_SIZE` 拒绝导致裸仓。
   - **已在 P0 落地应对**：在 `exchange_rules.py` 中强制实现 `quantize_qty_down`（向下取整为 `9.9 SUI`），并在 `PositionLedger` 中强制校验 `reserved_qty <= actual_qty`。
2. **单台 2vCPU/2GB 云主机内“独立风控进程”共享物理故障域风险**：
   - 若云主机发生宿主机掉电或机房断网，本机内的软止损与本机告警均无法发出。
   - **应对**：① 持仓保护优先使用币安交易所端托管条件单/OCO；② 在下一阶段 **P1** 落地宿主机外部的独立存活探测（Dead-Man's Switch）。
3. **OpenSSH 9.8+ (`sshd-session`) 日志标识变更导致传统 Fail2Ban 漏检风险**：
   - 在前期加固 Ubuntu 26.04 时已实测发现并修复（通过配置 `journalmatch = _SYSTEMD_UNIT=ssh.service` 捕获全部 `sshd-session` 爆破日志），将在 **P1** 的 `S-01` 安全巡检中持续监控。

---

## 6. 下一阶段（P1）依赖与待批准事项
- 下一阶段为 **P1（服务器基础安全 S-01、全系统守护 F-02、外部存活探测）**，详细计划见 [`docs/P1_PLAN.md`](file:///D:/OBR-SUI/docs/P1_PLAN.md)，等待用户/监理审查批准后执行。
