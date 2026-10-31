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
| **REQ-07** | 五类服务 `systemd` 守护、5s 心跳、30s 卡死检测、重启进入 `SYNCING`、外部失联探测 (§9, §10) | `F-02` / `S-01` | [`src/sui_quant/daemon/`](file:///D:/OBR-SUI/src/sui_quant/daemon) | `QA-09`, `QA-10` | P1 | `PARTIAL PASS / PENDING`（`FI-01~FI-09`、`FI-10A` 加速仿真及跨机器失联实测 `PASS`；**日本服务器真实连续 72h 墙钟满额运行 `FI-10B` 与第三方公有云/FCM 凭据标记为 `PENDING`**） |
| **REQ-08** | 实时订阅 `SUIUSDT`/`BTCUSDT` WebSocket 流、100ms 10档盘口、严格区分已收盘 K 线、增量同步 (§2, §11.1) | `F-01` / `E-01` | [`src/sui_quant/data/`](file:///D:/OBR-SUI/src/sui_quant/data) | `QA-01`, `QA-02` | P2 | `PASS (P2 公开流与同步) / PENDING (私有账户核验与72h实测)` |
| **REQ-09** | `A-01` 五状态识别与 `A-02` 向上放量突破三障碍校准概率模型（`p_win+p_stop+p_timeout=1`） (§3, §4) | `A-01` / `A-02` | `src/sui_quant/models/` (P3 新增) | `QA-12`, `QA-13` | P3 | `PENDING (P3 待实施)` |
| **REQ-10** | `B-01` 盘口微观结构限价优化、成交概率与逆向选择 `markout`、部分成交立即保护 (§5) | `B-01` / `R-01` | `src/sui_quant/execution/` (P4 新增) | `QA-04`, `QA-05` | P4 | `PENDING (P4 待实施)` |
| **REQ-11** | 真实加权买价 `-1%` 触发止损；`C1`（净+1.5%）、`C2`（上涨分批）、`C3`（混合）两层公平回测与 OCO 锁仓 (§6) | `C-01` | `src/sui_quant/exit/` (P5 新增) | `QA-06`, `QA-07`, `QA-13` | P5 | `PENDING (P5 待实施)` |
| **REQ-12** | `E-01` 签名模型制品上架、原子切换、已持仓锁定旧退出版本、劣化自动回滚与 72h 压测 (§11.2, §13) | `E-01` / `D-01` | `src/sui_quant/registry/` (P6 新增) | `QA-12` | P6 | `PENDING (P6 待实施)` |
| **REQ-13** | `G-01` Kotlin+Jetpack Compose 原生监控 App、`G-02` 前台弹窗 + 后台 FCM 通知、受控紧急锁仓 (§12) | `G-01` / `G-02` | `android/` & `src/sui_quant/api/` (P7 新增) | `QA-11` | P7 | `PENDING (P7 待实施)` |
| **REQ-14** | 全量 `QA-01 ~ QA-14` 模拟长跑审计、GO/NO-GO 核验与独立实盘风险授权书草稿 (§14, §16) | 全系统 | `docs/GO_NO_GO_AUDIT.md` (P8 新增) | `QA-01 ~ QA-14` | P8 | `PENDING (P8 待实施)` |

---

## 2. QA-01 ~ QA-14 专项验收矩阵映射（严格遵循 Task A.7：未完全验证项一律标记 `PENDING`）

| 验收 ID | 必须注入或证明的场景 (§14.2) | 负责阶段 | P0/P1 已验证部分 vs 后续待验证部分 (`PENDING`) |
|---|---|---|---|
| **QA-01** | 行情重复、乱序、缺失、失联、重连与 K 线收盘去重 | P0, P2 | **`PASS`**：P0 契约校验 + P2 [`test_p2_f01_collector_and_sync.py`](file:///D:/OBR-SUI/tests/test_p2_f01_collector_and_sync.py)（重复/乱序拒收、未收盘隔离与收盘唯一触发、3分钟断联 `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` 标记与 REST K 线补齐、23h 双连接零缺口热切换）全部通过 |
| **QA-02** | 100ms 部分盘口正常处理、背压不无限增长、数据质量停新仓 | P0, P2 | **`PARTIAL PASS / PENDING`**：100ms 盘口采集、有界 RingBuffer、80% 水位自动降级 `1000ms`、过期行情停新仓及云端实测 `P99=0.219ms` `PASS`；**币安私有账户 API 权限实网核验因零密钥策略保持 `PENDING`** |
| **QA-03** | 价格/数量 tick、step、最小名义额、实际费率、资金不足处理 | P0, P4 | P0 规则与边界计算 [`exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py) `PASS`；`B-01` 执行器集成待 P4 验证（**`PENDING`**） |
| **QA-04** | 限价单永远不成交、部分成交、撤单和改单、买单超时状态未知 | P0, P4 | P0 状态机转移 [`state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py) `PASS`；订单执行闭环待 P4 验证（**`PENDING`**） |
| **QA-05** | 买单成交瞬间风险模块/服务器崩溃，重启对账并优先恢复保护 | P0, P1, P4 | P1 `kill -9` 与模拟对账恢复 `PASS`；真实订单流恢复待 P4 验证（**`PENDING`**） |
| **QA-06** | 止盈/止损组合订单数量锁定，不存在重复占用或裸仓误判 | P0, P5 | P0 `PositionLedger` 扣费与碎股边界 `PASS`；`C-01` OCO 状态机待 P5 验证（**`PENDING`**） |
| **QA-07** | 快速跳价、-1% 触发实际亏损超过 1%、压力滑点、触发订单拒绝 | P0, P5 | P0 触发价与净收益公式 `PASS`；回测与压力滑点注入待 P5 验证（**`PENDING`**） |
| **QA-08** | 24h 权益阈值、仓位上限、权限/配置被恶意放宽时拒绝 | P0, P1, P4 | P0/P1 `RiskConfig` 硬上限与 `R-01` 独立守护进程判定 `PASS` |
| **QA-09** | systemd 崩溃/卡死/开机重启/网络中断/磁盘满/时钟异常/72h 稳定 | P1 | `FI-01~FI-04`, `FI-08~FI-09` 及 `FI-10A`（72h 加速模拟）`PASS`；**日本服务器真实连续 72 小时墙钟观测（`FI-10B`）进行中，未满 72h 前严格标记为 `PENDING`** |
| **QA-10** | SSH 异常成功登录、文件篡改、密钥泄漏演练、外部服务器掉线告警 | P0, P1 | 本机 `S-01` 锁仓、跨机器真实断联/模拟失联及本地外部 Webhook 回执 `PASS`；**第三方公有云 Cloudflare/FCM 手机推送凭据未配置，生产云推送回执标记为 `PENDING`；单机 `root` 沦陷仅靠机外隔离兜底** |
| **QA-11** | Android 前后台通知、权限被拒、FCM 延迟/丢弃、断网告警补偿 | P7 | P1 服务端补偿队列 `PASS`；Android 客户端待 P7 实施与验证（**`PENDING`**） |
| **QA-12** | 模型热切换/失败回滚/旧持仓策略版本锁定/旧模型兼容 | P0, P6 | P0 契约字段冻结 `PASS`；`E-01` 制品仓库热切换待 P6 验证（**`PENDING`**） |
| **QA-13** | 不同止盈方案 C1/C2/C3 严格同入场、同风控、同费用对照 | P5 | 待 P5 实施两层公平回测验证（**`PENDING`**） |
| **QA-14** | LIVE 默认关闭；没有明确用户批准无法通过配置绕过 | P0, Task A.5 | P0 + Task A.5（代码级不可覆盖公钥锚点 + `PersistentNonceRegistry` 磁盘持久化防重放 + 运行时代码树 `compute_runtime_code_tree_sha256` 绑定）`PASS` |
