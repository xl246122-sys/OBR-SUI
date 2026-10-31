# PHASE_HANDOFF｜阶段交接与验收档案（含任务 A 补齐证据与诚实状态声明）

## 1. 当前阶段元信息
- **项目名称**：`SUI-Quant V1.0`
- **当前阶段状态**：`P0 + P1 IMPLEMENTED / TASK A REMEDIATED (WITH EXPLICIT PENDING ITEMS)`
- **下一阶段待审计划**：[`docs/P2_PLAN.md`](file:///D:/OBR-SUI/docs/P2_PLAN.md)（`F-01` 实时行情采集与云-本地增量同步，状态：`READY_FOR_REVIEW`）
- **当前运行模式**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 默认禁用且受代码级不可替换公钥常量 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX` + Ed25519 非对称签名 + `PersistentNonceRegistry` 磁盘持久化防重放 + `compute_runtime_code_tree_sha256` 运行时源码树哈希绑定 + 7 项 GO/NO-GO 硬闸门拦截）
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易密钥数量**：`0`（NO）

---

## 2. P1 及任务 A 交付与整改摘要
1. **`F-02` 全系统守护与恢复门禁（[`src/sui_quant/daemon/heartbeat.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/heartbeat.py)）**：
   - 实现 5s 业务心跳写入、30s 事件循环卡死检测（同时核验 `monotonic_ns` 与 `loop_tick_seq` 推进）、5 分钟 5 次崩溃自动熔断器（`Crash-Loop Circuit Breaker`）及 `BOOT -> SYNCING` 五项恢复门禁（`Gate 1 ~ Gate 5`）。
   - 验证重启时发现未保护持仓（`QA-05`）自动优先进入 `PROTECTION_PENDING` 补挂保护单，确认后再进入 `PROTECTED`。
2. **`R-01` 独立风控进程与双向互监看门狗（[`src/sui_quant/daemon/r01_server.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/r01_server.py)）**：
   - 纯 `Decimal` 独立进程，零 ML 库依赖，配置 `Nice=-10`, `OOMScoreAdjust=-500`，实测 P99 裁决延迟 `0.136 ms`（远优于 `25ms` SLO）。
   - 实现 `R-01` 失联/超时 Fail-Closed 冻结新开仓（保留减仓保护卖出通道）及反向监控 `guardian` 的双向看门狗。
3. **`S-01` 主机安全总管与离线 Ed25519 签名清单（[`src/sui_quant/daemon/security_supervisor.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/security_supervisor.py)）及 `root` 威胁边界诚实声明（Task A.6）**：
   - 实现 `SignedReleaseManifest`（离线 Ed25519 私钥签名 + 实时文件 SHA-256 与 `RiskConfig` 哈希校验，防御非特权同机摘要伪造与配置漂移）。
   - **明确声明单机 `root` 沦陷局限**：若攻击者已获取日本云主机的 `root` 权限，任何同机软件校验（含 `S-01`、本地公钥验签、对外信标发送）均可被 `root` 绕过或伪造；针对 `root` 入侵的唯一有效兜底边界是机外隔离（当前 P0/P1 零交易所私钥上云、未来实盘币安服务端禁提币 + 现货 `SUIUSDT` 白名单 + 子账户物理资金上限）。
4. **`G-02` 告警分级、风暴抑制与持久化状态机（[`src/sui_quant/daemon/alert_manager.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/alert_manager.py)）**：
   - 实现 `INFO/HIGH/CRITICAL` 分级、冷却窗去重（100 条风暴合并为 `occurrence_count=100` 的单次外发）及 `PENDING / SENT_AWAITING_ACK / DELIVERED / FAILED_RETRYING` 诚实送达状态机。
5. **外部独立故障域存活探测（[`src/sui_quant/daemon/external_probe.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_probe.py), [`src/sui_quant/daemon/external_dms_live_probe.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_dms_live_probe.py), [`deploy/cloudflare_worker/worker.js`](file:///D:/OBR-SUI/deploy/cloudflare_worker/worker.js)）**：
   - 完成跨物理机器（独立 Windows 11 外部故障域 <-> 日本云主机）的 **模拟失联（`SIMULATED_BLACKOUT_DAEMON_SILENT`）** 与 **真实整机网络断联（`REAL_HOST_UNREACHABLE_BLACKOUT`）** 独立探测与本地 Webhook 签名回执取证（[`evidence/external_alert_receipts.jsonl`](file:///D:/OBR-SUI/evidence/external_alert_receipts.jsonl)）。
   - 第三方公有云（Cloudflare Workers 账号）及移动端 FCM 生产推送凭据因未提供，严格标记为 **`PENDING`（`PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS`）**。
6. **严格区分 72h 虚拟时钟加速模拟与日本服务器真实连续 72 小时墙钟观测（Task A.1 & A.2）**：
   - **72h 加速模拟（`FI-10A`, [`soak_runner.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/soak_runner.py)）**：显式标记 `"test_classification": "ACCELERATED_SIMULATION_ONLY"`, `"is_real_72h_wallclock_proof": false`（状态：`PASS`）。
   - **日本服务器真实连续 72 小时墙钟运行观测（`FI-10B`, [`real_72h_observer.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/real_72h_observer.py)）**：已作为 `sui-quant-72h-observer.service` 在日本服务器持续采样写入 `/var/log/sui-quant/real_72h_observation.jsonl`；在未满 `259,200` 真实秒（72.0 小时）前，状态严格标记为 **`PENDING`**。
7. **P0 授权信任边界深度加固（Task A.5，[`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py)）**：
   - 禁止不可信配置或环境变量注入/覆盖 `pinned_approver_pubkey_hex` 等信任根参数；
   - 新增 SQLite WAL 持久化的 `PersistentNonceRegistry` 防止进程重启后重放 `LiveAuthorizationToken`；
   - 新增 `compute_runtime_code_tree_sha256()` 实时计算磁盘上 `src/sui_quant/**/*.py` 源码树 SHA-256 并与签名令牌强制绑定。

---

## 3. 验证命令与实测结果（含显式 `PENDING` 清单）
- 本地与云端测试命令：`python -m pytest -v` → **`24 passed`（云端 `1.05s` / 本地 `1.77s`）**
- 完整验收证据报告：详见 [`docs/TASK_A_EVIDENCE_REPORT.md`](file:///D:/OBR-SUI/docs/TASK_A_EVIDENCE_REPORT.md)
- **当前仍处于 `PENDING` 的未完结项（绝不伪报 `PASS`）**：
  1. `FI-10B` 日本服务器真实连续 72 小时墙钟稳定性运行观测：**`PENDING`**（已启动真实连续采样，需满 72.0 真实墙钟小时后方可转 `PASS`）。
  2. 第三方公有云 Cloudflare Workers 生产部署与移动端 FCM 实网推送回执：**`PENDING`**（待用户提供第三方云与 FCM 凭据）。
  3. 币安私有账户 API 权限核验（`QA-02` 子项）及 P2–P8 后续阶段模块验收：**`PENDING`**。

---

## 4. 回滚方法
- 云端一键回滚：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p1.sh`
- 本地代码回滚：`git revert HEAD` 或 `git checkout <prev-sha>`

---

## 5. 下一阶段（P2）依赖与待审查事项
- 下一阶段为 **P2（`F-01` 行情采集与云-本地数据增量同步）**，计划已写入 [`docs/P2_PLAN.md`](file:///D:/OBR-SUI/docs/P2_PLAN.md)，状态 `READY_FOR_REVIEW`，等待审查批准后执行。
