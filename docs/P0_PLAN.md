# P0-PLAN｜环境只读核验、规范冻结与安全先决条件

> **阶段编号**：P0｜**状态**：`IMPLEMENTED_AND_VERIFIED`｜**日期**：2026-10-31  
> **安全红线**：本阶段仅做只读环境探测、架构规范与数据契约冻结、本地单元测试验证；**对远程云服务器零修改、零真实下单、零交易密钥录入**。

---

## 01 任务理解与范围边界（引用设计文档章节）
- 依据总体设计方案 **§0、§1、§7.1、§8.1、§10、§14.2、§15、§16**，建立单仓骨架、架构决策记录（ADR）、十大跨模块数据契约、风险配置与环境隔离硬闸门（默认 `RESEARCH`/`PAPER`，禁止初始化为 `LIVE`）、币安现货 API 能力实测矩阵、安全威胁模型与日志脱敏器。

## 02 仓库/运行环境现状（只读事实，明确哪些无法验证）
- **本地环境（已实测）**：Windows 11，i5-13400F（10C/16T），15.72 GiB RAM，RTX 5060，D 盘可用空间 620.37 GiB，Python 3.11.5，Git 2.55.0。
- **日本云主机（已只读实测）**：Ubuntu 26.04.1 LTS，2 vCPU（Xeon Platinum 8255C 2.50GHz），1962 MiB RAM，41 GB 可用磁盘，Chrony NTP 同步偏差 6.4 微秒，到币安现货 REST 延迟 ~15.6–28.2 ms，WebSocket TLS 握手 15.58 ms。
- **无法在 P0 验证的事项**：用户个人币安账户的专属手续费率（`/api/v3/account` 需签名凭据），已在 [`docs/API_CAPABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/API_CAPABILITY_MATRIX.md) 中明确标注为账户级待验项。

## 03 方案及依赖图（含数据合同和关键失败模式）
- 冻结 `MarketEvent`、`MarketFeatures`、`RegimeDecision`、`BreakoutProposal`、`RiskVerdict`、`OrderIntent`、`OrderEvent`、`PositionLedger`、`AlertEvent`、`ModelManifest` 十大不可变数据契约（`frozen=True`）。
- **关键失败模式防护**：
  1. 任何价格/数量/费率传入二进制 `float` 时，契约构造器立即抛出 `TypeError`（防止二进制浮点精度污染订单价格和数量）。
  2. 任何时间戳未携带 UTC 时区（`tzinfo != timezone.utc`）时，立即抛出 `ValueError`。
  3. 尝试将交易标的配置为非 `SUIUSDT`、参考标的配置为非 `BTCUSDT`、放宽止损触发线（`> 1%`）、放宽单笔风险预算（`> 0.25%`）或单次仓位上限（`> 10%`）时，`RiskConfig` 立即拒绝并抛出 `RiskBoundaryViolationError`。
  4. 尝试在无用户显式实盘授权令牌及门禁核验的情况下启用 `LIVE` 模式时，立即抛出 `LiveAuthorizationError`。

## 04 待变更目录/文件清单
- **新增文档**：`README.md`、`docs/MASTER_PLAN.md`、`docs/P0_PLAN.md`、`docs/API_CAPABILITY_MATRIX.md`、`docs/THREAT_MODEL.md`、`docs/REQUIREMENTS_TRACEABILITY_MATRIX.md`、`docs/adr/ADR-001` ~ `ADR-004`、`PHASE_HANDOFF.md`。
- **新增代码**：`pyproject.toml`、`src/sui_quant/__init__.py`、`src/sui_quant/contracts.py`、`src/sui_quant/config.py`、`src/sui_quant/state_machine.py`、`src/sui_quant/exchange_rules.py`、`src/sui_quant/security/redaction.py`、`src/sui_quant/utils/clock.py`、`src/sui_quant/utils/ids.py`。
- **新增测试**：`tests/test_p0_contracts_and_gates.py`。
- **拒绝修改项**：远程云服务器任何配置或进程、`ServerStation-account.txt`（已加入 `.gitignore` 严禁入库）。

## 05 逐步实现计划
1. 创建 `.gitignore` 与 `pyproject.toml`，确保敏感凭据文件绝不进入版本控制。
2. 编写 ADR-001 至 ADR-004、API 能力矩阵、安全威胁模型与需求追踪矩阵。
3. 实现 `src/sui_quant/` 核心契约、精度过滤器校验、风险边界配置、环境隔离闸门、状态机转换规则、幂等 ID 与日志脱敏模块。
4. 编写并执行 `tests/test_p0_contracts_and_gates.py` 自动化测试套件。
5. 提交初始 Git 版本并输出 `PHASE_HANDOFF.md`。

## 06 测试清单
- `test_decimal_enforcement_rejects_float`：验证所有金额/价格/数量字段拒绝 `float`。
- `test_utc_timestamp_enforcement`：验证非 UTC 时间戳被拦截。
- `test_breakout_proposal_probability_simplex`：验证 `p_win + p_stop + p_timeout == 1` 且处于 `[0, 1]`。
- `test_suiusdt_exchange_filters_and_dust_rounding`：验证实测 `SUIUSDT` 的 `tickSize=0.0001`、`stepSize=0.1`、`minNotional=5.0` 及扣费后数量向下取整保护（QA-03 / QA-06）。
- `test_risk_config_immutable_boundaries`：验证唯一交易对 `SUIUSDT`、`-1%` 止损线、风险预算不可越界放宽（QA-08）。
- `test_live_mode_strictly_blocked_by_default`：验证默认 `PAPER`/`RESEARCH`，未获完整授权与门禁时开启 `LIVE` 必抛异常（QA-14）。
- `test_state_machine_valid_and_invalid_transitions`：验证 `D-01` 状态机禁止跳过 `RISK_CHECK` 或 `PROTECTION_PENDING` 直接进入正常交易。
- `test_log_redaction_strips_secrets`：验证 API Key、Secret、Signature、Password 在日志中100%脱敏。

## 07 验收标准
- `pytest -v` 全部用例 100% 通过。
- `git status` 确认 `ServerStation-account.txt` 未被追踪，仓库中零密钥。
- 全部文档与代码路径真实可查。

## 08 风险与备选方案
- 回滚方案：P0 仅在本地 Git 仓库创建基线代码与文档，未触碰远端生产服务，可通过 `git reset` 瞬时回滚。

## 09 未验证假设及阻塞项
- 无阻塞项。

## 10 等待审查的问题与结论
- P0 已按计划完成落地与测试验证，下一阶段 **P1（服务器基础安全、全系统守护、外部存活探测）** 计划已就绪，等待审查批准。
