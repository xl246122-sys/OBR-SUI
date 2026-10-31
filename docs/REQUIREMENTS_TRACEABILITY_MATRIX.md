# SUI-Quant V1.0｜端到端需求覆盖与 QA 验收追踪矩阵

> **编制阶段**：P0｜**基线版本**：V1.0｜**状态说明**：`PASS (P0)` 表示已在 P0 完成契约/门禁代码与单元测试验证；`PLANNED (Px)` 表示在后续指定阶段落地验证。

---

## 1. 模块级需求覆盖矩阵（需求编号 → 所属模块 → 代码职责 → 验证用例 → 阶段）

| 需求编号 | 需求描述与设计章节 | 所属模块 | 代码职责与目标文件 | 对应 QA 用例 | 落地阶段 | 当前状态 |
|---|---|---|---|---|---|---|
| **REQ-01** | 唯一交易币安现货 `SUIUSDT`；`BTCUSDT` 仅作参考，禁合约/杠杆/做空/补仓 (§0.1, §8.1) | `R-01` / `D-01` | [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py), [`src/sui_quant/contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py) | `QA-08`, `QA-14` | P0, P4 | `PASS (P0 基座)` |
| **REQ-02** | 默认严格 `RESEARCH` 或 `PAPER`；禁止默认 `LIVE` 或配置绕过 (§0.1, §16) | `R-01` / `D-01` | [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py) (`ExecutionMode`, `verify_live_gate`) | `QA-14` | P0, P8 | `PASS (P0 基座)` |
| **REQ-03** | 所有金额/价格/数量严格使用 `Decimal`；时间戳严格 UTC + 单调时钟 (§2.1, §7.1) | 全模块契约 | [`src/sui_quant/contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py), [`src/sui_quant/utils/clock.py`](file:///D:/OBR-SUI/src/sui_quant/utils/clock.py) | `QA-01`, `QA-03` | P0 | `PASS (P0 基座)` |
| **REQ-04** | `SUIUSDT` 价格 `tickSize=0.0001`、数量 `stepSize=0.1`、`minNotional=5.0` 及扣费碎股向下取整校验 (§5, §6.2) | `B-01` / `C-01` / `R-01` | [`src/sui_quant/exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py) | `QA-03`, `QA-06` | P0, P4, P5 | `PASS (P0 基座)` |
| **REQ-05** | 凭据隔离与日志脱敏：禁止日志/异常/Git 出现任何 API Key/Secret/密码 (§10) | `S-01` / 基础库 | [`src/sui_quant/security/redaction.py`](file:///D:/OBR-SUI/src/sui_quant/security/redaction.py), [`.gitignore`](file:///D:/OBR-SUI/.gitignore) | `QA-10` | P0, P1 | `PASS (P0 基座)` |
| **REQ-06** | 唯一 `clientOrderId` 幂等生成与 `D-01` 交易保护状态机迁移约束 (§5, §7) | `D-01` / `B-01` | [`src/sui_quant/state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py), [`src/sui_quant/utils/ids.py`](file:///D:/OBR-SUI/src/sui_quant/utils/ids.py) | `QA-04`, `QA-05` | P0, P4 | `PASS (P0 基座)` |
| **REQ-07** | 五类服务 `systemd` 守护、5s 心跳、30s 卡死检测、重启进入 `SYNCING`、外部失联探测 (§9, §10) | `F-02` / `S-01` | [`src/sui_quant/daemon/`](file:///D:/OBR-SUI/src/sui_quant/daemon) | `QA-09`, `QA-10` | P1 | `PASS (P1 已验收)` |
| **REQ-08** | 实时订阅 `SUIUSDT`/`BTCUSDT` WebSocket 流、100ms 10档盘口、严格区分已收盘 K 线、增量同步 (§2, §11.1) | `F-01` / `E-01` | `src/sui_quant/data/` (P2 新增) | `QA-01`, `QA-02` | P2 | `PLANNED (P2)` |
| **REQ-09** | `A-01` 五状态识别与 `A-02` 向上放量突破三障碍校准概率模型（`p_win+p_stop+p_timeout=1`） (§3, §4) | `A-01` / `A-02` | `src/sui_quant/models/` (P3 新增) | `QA-12`, `QA-13` | P3 | `PLANNED (P3)` |
| **REQ-10** | `B-01` 盘口微观结构限价优化、成交概率与逆向选择 `markout`、部分成交立即保护 (§5) | `B-01` / `R-01` | `src/sui_quant/execution/` (P4 新增) | `QA-04`, `QA-05` | P4 | `PLANNED (P4)` |
| **REQ-11** | 真实加权买价 `-1%` 触发止损；`C1`（净+1.5%）、`C2`（上涨分批）、`C3`（混合）两层公平回测与 OCO 锁仓 (§6) | `C-01` | `src/sui_quant/exit/` (P5 新增) | `QA-06`, `QA-07`, `QA-13` | P5 | `PLANNED (P5)` |
| **REQ-12** | `E-01` 签名模型制品上架、原子切换、已持仓锁定旧退出版本、劣化自动回滚与 72h 压测 (§11.2, §13) | `E-01` / `D-01` | `src/sui_quant/registry/` (P6 新增) | `QA-12` | P6 | `PLANNED (P6)` |
| **REQ-13** | `G-01` Kotlin+Jetpack Compose 原生监控 App、`G-02` 前台弹窗 + 后台 FCM 通知、受控紧急锁仓 (§12) | `G-01` / `G-02` | `android/` & `src/sui_quant/api/` (P7 新增) | `QA-11` | P7 | `PLANNED (P7)` |
| **REQ-14** | 全量 `QA-01 ~ QA-14` 模拟长跑审计、GO/NO-GO 核验与独立实盘风险授权书草稿 (§14, §16) | 全系统 | `docs/GO_NO_GO_AUDIT.md` (P8 新增) | `QA-01 ~ QA-14` | P8 | `PLANNED (P8)` |

---

## 2. QA-01 ~ QA-14 专项验收矩阵映射

| 验收 ID | 必须注入或证明的场景 (§14.2) | 负责阶段 | P0 契约/门禁覆盖情况 |
|---|---|---|---|
| **QA-01** | 行情重复、乱序、缺失、失联、重连与 K 线收盘去重 | P0, P2 | P0 已在 `MarketEvent` 与 `MarketFeatures(complete_bar_only)` 建立数据结构强约束 |
| **QA-02** | 100ms 部分盘口正常处理、背压不无限增长、数据质量停新仓 | P0, P2 | P0 已定义 `DataQuality` 与 `RegimeType.DATA_UNHEALTHY` 强制阻断新开仓 |
| **QA-03** | 价格/数量 tick、step、最小名义额、实际费率、资金不足处理 | P0, P4 | P0 已在 [`exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py) 实现并由单元测试验证 |
| **QA-04** | 限价单永远不成交、部分成交、撤单和改单、买单超时状态未知 | P0, P4 | P0 已在 [`state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py) 强制定义 `ORDER_UNKNOWN` 与 `PARTIALLY_FILLED` 转移约束 |
| **QA-05** | 买单成交瞬间风险模块/服务器崩溃，重启对账并优先恢复保护 | P0, P4 | P0 已强制定义 `BOOT -> SYNCING -> PROTECTION_PENDING` 恢复路径 |
| **QA-06** | 止盈/止损组合订单数量锁定，不存在重复占用或裸仓误判 | P0, P5 | P0 已在 `PositionLedger` 实现 `reserved_qty <= actual_qty` 强校验与碎股向下取整 |
| **QA-07** | 快速跳价、-1% 触发实际亏损超过 1%、压力滑点、触发订单拒绝 | P0, P5 | P0 已在 `RiskConfig` 区分 `-1%` 价格触发线与含滑点/手续费的压力风险预算 |
| **QA-08** | 24h 权益阈值、仓位上限、权限/配置被恶意放宽时拒绝 | P0, P4 | P0 已在 [`config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py) 实现不可放宽硬边界并由单元测试通过 |
| **QA-09** | systemd 崩溃/卡死/开机重启/网络中断/磁盘满/时钟异常 | P1 | `PASS (P1 FI-01~FI-10 全部通过)` |
| **QA-10** | SSH 异常成功登录、文件篡改、密钥泄漏演练、外部服务器掉线告警 | P0, P1 | `PASS (P0+P1 已落地并通过注入验证)` |
| **QA-11** | Android 前后台通知、权限被拒、FCM 延迟/丢弃、断网告警补偿 | P7 | P7 实施并测试 |
| **QA-12** | 模型热切换/失败回滚/旧持仓策略版本锁定/旧模型兼容 | P0, P6 | P0 已在 `PositionLedger(exit_strategy_version)` 与 `ModelManifest(rollback_ref)` 冻结字段 |
| **QA-13** | 不同止盈方案 C1/C2/C3 严格同入场、同风控、同费用对照 | P5 | P5 实施两层公平回测 |
| **QA-14** | LIVE 默认关闭；没有明确用户批准无法通过配置绕过 | P0, P8 | P0 已在 [`config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py) 实现加密令牌+多门禁校验并由单元测试验证 |
