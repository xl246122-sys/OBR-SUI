# TASK_A_EVIDENCE_REPORT｜P1 遗留验收证据补齐与 P0 信任边界加固报告

> **阶段状态**：`P0 REMEDIATED (PASS) / P1 CORE FAULT MATRIX PASS / REAL 72H WALL-CLOCK & 3RD-PARTY CLOUD PUSH PENDING`  
> **提交时间（UTC）**：`2026-10-31T06:09:30Z`（北京时间 `2026-10-31 14:09:30`）  
> **核心原则**：零真实交易（`0`）、零交易密钥（`0`）、严格区分加速模拟与真实 72h 墙钟观测、所有未完成或缺少第三方凭据项一律诚实标记为 **`PENDING`**，绝不伪报 `PASS`。

---

## 1. 任务 A.1：严格区分“真实 72 小时墙钟稳定性运行”与“72 小时加速模拟”

### 1.1 代码级强制分类标识
1. **72 小时虚拟时钟加速模拟（[`src/sui_quant/daemon/soak_runner.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/soak_runner.py#L91-L303)）**：
   - 仅用于快速回归验证 2,160 个虚拟周期的内存线性斜率、文件句柄泄漏与 `R-01` 裁决延迟分位数。
   - 输出字典强制写入硬编码标识，禁止任何报告将其混充为真实墙钟时间：
     ```json
     {
       "test_classification": "ACCELERATED_SIMULATION_ONLY",
       "is_real_72h_wallclock_proof": false,
       "warning": "Accelerated virtual-clock simulation only (~2.5s wall-clock for 2,160 ticks). Cannot substitute for the real 72-hour continuous wall-clock observation."
     }
     ```
2. **日本服务器真实连续 72 小时墙钟观测器（[`src/sui_quant/daemon/real_72h_observer.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/real_72h_observer.py)）**：
   - 作为独立 `systemd` 守护单元 [`deploy/systemd/sui-quant-72h-observer.service`](file:///D:/OBR-SUI/deploy/systemd/sui-quant-72h-observer.service) 部署于日本服务器，按真实物理时钟每 `60 秒` 采样一次。
   - **硬编码防伪报门禁（[`real_72h_observer.py#L304-L315`](file:///D:/OBR-SUI/src/sui_quant/daemon/real_72h_observer.py#L304-L315)）**：
     ```python
     REQUIRED_WALLCLOCK_SECONDS = 72 * 3600  # 259,200 real seconds
     is_completed = elapsed_sec >= REQUIRED_WALLCLOCK_SECONDS
     verification_status = "PASS" if is_completed else "PENDING"
     ```
   - 只要真实流逝秒数 `< 259,200s`（`72.0` 真实小时），状态文件 `/var/log/sui-quant/real_72h_status.json` 中的 `verification_status` **恒为 `"PENDING"`**。

| 对比维度 | `FI-10A`：72 小时加速模拟 (`soak_runner.py`) | `FI-10B`：日本服务器真实连续 72 小时墙钟观测 (`real_72h_observer.py`) |
|---|---|---|
| **时钟推进方式** | 虚拟步进（`sim_now += 120s`，真实耗时约 `2.4s`） | 真实物理墙钟 `time.sleep(60)` 持续采样 `259,200 秒` |
| **验证目标** | 代码路径内存泄漏回归、FD 句柄回归、`R-01` P99 延迟 | 真实 Linux 内核内存/Swap 漂移、OOM、进程重启、SQLite/日志磁盘增长 |
| **产物标识** | `"test_classification": "ACCELERATED_SIMULATION_ONLY"` | `"test_classification": "REAL_WALLCLOCK_72H_OBSERVATION"` |
| **当前验收状态** | **`PASS`** | **`PENDING`**（已启动真实连续采样，未满 72.0 真实小时绝不标 `PASS`） |

---

## 2. 任务 A.2：日本服务器真实连续 72 小时运行观测原始数据与遥测记录

- **观测服务单元**：`sui-quant-72h-observer.service`（`active (running)`）
- **云端原始日志路径**：
  - 逐分钟采样 JSONL：`/var/log/sui-quant/real_72h_observation.jsonl`（本地快照归档：[`evidence/real_72h_observation_snapshot.jsonl`](file:///D:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)）
  - 实时汇总状态 JSON：`/var/log/sui-quant/real_72h_status.json`（本地快照归档：[`evidence/real_72h_status_snapshot.json`](file:///D:/OBR-SUI/evidence/real_72h_status_snapshot.json)）
- **起止时间与进度元数据（实测快照）**：
  - 真实启动时间（`started_at_utc`）：`2026-10-31T06:05:40.064449+00:00`
  - 最新采样时间（`latest_sample_utc`）：`2026-10-31T06:08:40.367140+00:00`
  - 目标满 72h 完成时间（`target_completion_utc`）：`2026-11-03T06:05:40.064449+00:00`
  - 当前已流逝真实墙钟时间：`180.3 秒`（`0.0501 小时` / `72.0 小时`，完成度 `0.0696%`）
  - **当前验收状态**：**`PENDING`**（`Real continuous 72h observation in progress: 180.3s / 259200s elapsed`）

### 2.1 连续采样记录表（Sample #1 ~ #4 实测原始指标）

| 采样序号 | 采样 UTC 时间 | 流逝秒数 | CPU Load (`1m/5m/15m`) | 物理内存 `Used / Avail (MiB)` | Swap `Used (MiB)` | 内核 `oom_kill` (总数/增量) | `r01` PID / RSS / 重启数 / 心跳延迟 | `guardian` PID / RSS / 重启数 / 心跳延迟 | 交易状态 (`system_kv`) | 状态库增长 / 日志增长 (`Bytes`) |
|---|---|---|---|---|---|---|---|---|---|---|
| **#1** | `06:05:40.064Z` | `0.0s` | `0.31 / 0.25 / 0.18` | `541.49 / 1421.00` | `0.46` | `0 / +0` | `96453` / `13.44 MiB` / `0` / `3.871s` | `96454` / `13.46 MiB` / `0` / `3.871s` | `READY_NO_POSITION` | `+0 B` / `+0 B` |
| **#2** | `06:06:40.174Z` | `60.11s` | `0.11 / 0.20 / 0.17` | `545.69 / 1416.80` | `0.46` | `0 / +0` | `96453` / `13.49 MiB` / `0` / `3.977s` | `96454` / `13.51 MiB` / `0` / `3.976s` | `READY_NO_POSITION` | `+98,880 B` / `+3,808 B` |
| **#3** | `06:07:40.266Z` | `120.20s` | `0.19 / 0.22 / 0.18` | `543.79 / 1418.70` | `0.46` | `0 / +0` | `96453` / `13.78 MiB` / `0` / `4.064s` | `96454` / `13.56 MiB` / `0` / `4.063s` | `READY_NO_POSITION` | `+197,760 B` / `+5,213 B` |
| **#4** | `06:08:40.367Z` | `180.30s` | `0.07 / 0.18 / 0.17` | `545.21 / 1417.27` | `0.46` | `0 / +0` | `96453` / `13.59 MiB` / `0` / `4.159s` | `96454` / `13.61 MiB` / `0` / `4.159s` | `READY_NO_POSITION` | `+296,640 B` / `+6,601 B` |

---

## 3. 任务 A.3：外部 Dead-Man's Switch 部署位置、运行主体、独立超时判断、告警出口与成本额度核算

### 3.1 双层架构与当前实际运行状态
| 维度 | Tier-1：已实装并完成跨机器实测的外部故障域探测器（当前生效） | Tier-2：7×24 公有云边缘 Serverless 目标（待用户云凭据） |
|---|---|---|
| **实际部署位置** | 独立于腾讯云日本机房之外的**本地 Windows 11 物理工作站**（主机名 `Seth`，不同物理硬件、不同操作系统、不同 ISP 网络） | **Cloudflare Global Edge Network**（[`deploy/cloudflare_worker/worker.js`](file:///D:/OBR-SUI/deploy/cloudflare_worker/worker.js)） |
| **运行主体** | [`sui_quant.daemon.external_dms_live_probe.ExternalCrossMachineProbe`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_dms_live_probe.py) 独立 Python 进程 | Cloudflare Workers Isolate + Cron Trigger (`*/1 * * * *`) + Workers KV |
| **独立超时判断** | 由 Windows 11 外部主机本地时钟（`datetime.now(timezone.utc)`）与本地 Socket 超时（`tcp_timeout_sec=3.0s`、守护心跳阈值 `daemon_stale_threshold_sec=10.0s`）独立裁决，完全不依赖日本服务器内部定时任务 | 由 Cloudflare Cron 每 60s 独立唤醒，比较 `Date.now() - beacon.received_at_ms > 180000ms` |
| **告警出口** | 外部工作站独立 Webhook 接收服务（`http://127.0.0.1:18990/alert-receipt`，[`ExternalAlertWebhookSink`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_dms_live_probe.py#L29-L108)），生成带 HMAC-SHA256 签名的持久化 JSON 回执（[`evidence/external_alert_receipts.jsonl`](file:///D:/OBR-SUI/evidence/external_alert_receipts.jsonl)） | FCM HTTP v1 API / Telegram Bot Webhook（需注入 `ALERT_WEBHOOK_URL` Secret） |
| **当前验证状态** | **`PASS`**（已完成跨网模拟失联与真实防火墙断联实测及回执取证） | **`PENDING`**（代码已就绪；因未提供用户 Cloudflare Token 与 FCM 私钥，标记为 `PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS`） |

### 3.2 成本与免费额度核算表（Cloudflare Workers Free Tier + FCM）
| 资源项 | 计费规则 / 免费额度上限（Free Plan） | SUI-Quant V1.0 实际消耗量（30s 信标 + 60s Cron） | 额度占用比 | 月度费用 |
|---|---|---|---|---|
| **Workers HTTP 请求数** | `100,000 次 / 日` 免费 | 每 30s 接收 1 次信标 = `2,880 次 / 日` | **`2.88%`** | **`$0.00 / 月`** |
| **Workers Cron Triggers** | 每分钟 1 次触发免费 | 每 60s 触发 1 次 `scheduled()` = `1,440 次 / 日` | **`1.44%`**（计入总调用） | **`$0.00 / 月`** |
| **Workers KV 读取 (`KV.get`)** | `100,000 次 / 日` 免费 | 每次 Cron 读取 1 次 `latest_beacon` = `1,440 次 / 日` | **`1.44%`** | **`$0.00 / 月`** |
| **Workers KV 写入 (`KV.put`)** | `1,000 次 / 日` 免费 | 仅在 `boot_id` 变更、状态切换或每 3 分钟检查点落盘 KV = `480 次 / 日` | **`48.00%`** | **`$0.00 / 月`** |
| **Firebase Cloud Messaging (FCM)** | 不限条数完全免费 | 仅在失联/入侵时触发且受 15m 冷却窗限制（`< 100 条 / 月`） | **`0.00%`** | **`$0.00 / 月`** |
| **合计总成本** | — | — | — | **`$0.00 / 月`** |

---

## 4. 任务 A.4：真正的整机失联、模拟失联与外部通知回执实测证据

我们在独立外部主机（Windows 11 `Seth`）与日本云主机（`<JAPAN_CLOUD_IP_REDACTED>:22`）之间跨真实公网执行了三阶段对比实测，原始回执保存在 [`evidence/external_alert_receipts.jsonl`](file:///D:/OBR-SUI/evidence/external_alert_receipts.jsonl)：

### 4.1 场景一：模拟失联（`SIMULATED_BLACKOUT_DAEMON_SILENT` — 整机网络可达，但守护服务停止）
- **注入操作**：在日本服务器上执行 `sudo systemctl stop sui-quant-guardian.service`，等待 `11 秒`（此时日本服务器网络与 SSH 正常在线，但 `guardian` 心跳停滞）。
- **外部独立探测结果**：外部探测器成功连接日本服务器，检测到 `host_network_reachable: true`，但 `guardian_active_state == "inactive"` 且心跳年龄 `16.846s > 10.0s`，独立判定为 **`SIMULATED_BLACKOUT_DAEMON_SILENT`**。
- **外部通知回执原始记录（`RCPT-CC671D2B2FF5`）**：
  ```json
  {
    "receipt_id": "RCPT-CC671D2B2FF5",
    "channel_type": "EXTERNAL_WORKSTATION_WEBHOOK_SINK",
    "third_party_cloud_push_status": "PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS",
    "http_status": 200,
    "received_at_utc": "2026-10-31T06:04:09.421796+00:00",
    "incident_type": "EXTERNAL_DAEMON_HEARTBEAT_STALE",
    "fault_classification": "SIMULATED_BLACKOUT_DAEMON_SILENT",
    "target_host": "<JAPAN_CLOUD_IP_REDACTED>:22",
    "independent_probe_host": "Seth",
    "probe_details": {
      "probe_started_utc": "2026-10-31T06:04:08.039805+00:00",
      "host_network_reachable": true,
      "guardian_active_state": "inactive",
      "guardian_heartbeat_age_sec": 16.846,
      "stale_threshold_sec": 10.0,
      "r01_active_state": "active",
      "independent_judgment": "Host network is reachable, but guardian daemon is stopped/stale; classified as SIMULATED_BLACKOUT_DAEMON_SILENT."
    },
    "receipt_hmac_sha256": "2e62a8164dff60ed8aaa69c31ec07c06ad9d5f80c198359fce03432b257a0f51"
  }
  ```

### 4.2 场景二：真正的整机网络断联（`REAL_HOST_UNREACHABLE_BLACKOUT` — 内核 Netfilter 丢弃入站新建连接 + 12 秒硬件定时器自动回滚）
- **注入操作**：遵循防锁死协议，先在日本服务器上挂载 12 秒自动恢复定时器（`systemd-run --on-active=12s` + 后台延时删除双保险），随后在内核 `iptables INPUT` 链第 1 条插入真实丢包规则：
  `sudo iptables -I INPUT 1 -p tcp --dport 22 -m conntrack --ctstate NEW -m comment --comment SUIQ_BLACKOUT_TEST -j DROP`
  使日本服务器在网络层对外部探测器的连接请求呈现**真正的静默黑洞丢包（无 SYN-ACK、无 RST）**。
- **外部独立探测结果**：Windows 11 外部探测器向日本服务器发起连接，在等待 `3,623.65 ms` 后因底层传输超时失败，由外部探测器本地时钟独立判定为 **`REAL_HOST_UNREACHABLE_BLACKOUT`** 并触发 `EXTERNAL_HOST_BLACKOUT_CRITICAL` 告警。
- **外部通知回执原始记录（`RCPT-A8AF5B7E0EDB`）**：
  ```json
  {
    "receipt_id": "RCPT-A8AF5B7E0EDB",
    "channel_type": "EXTERNAL_WORKSTATION_WEBHOOK_SINK",
    "third_party_cloud_push_status": "PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS",
    "http_status": 200,
    "received_at_utc": "2026-10-31T06:04:17.059341+00:00",
    "incident_type": "EXTERNAL_HOST_BLACKOUT_CRITICAL",
    "fault_classification": "REAL_HOST_UNREACHABLE_BLACKOUT",
    "target_host": "<JAPAN_CLOUD_IP_REDACTED>:22",
    "independent_probe_host": "Seth",
    "probe_details": {
      "probe_started_utc": "2026-10-31T06:04:13.434086+00:00",
      "tcp_timeout_sec": 3.0,
      "observed_wait_ms": 3623.65,
      "transport_exception": "SSHException: No existing session",
      "independent_judgment": "Host failed to respond over network within timeout; classified as REAL_HOST_UNREACHABLE_BLACKOUT."
    },
    "receipt_hmac_sha256": "a947c72ecbc1a4d851b5e0907238ae3f6100b03ca9d5602da7777e94714ef090"
  }
  ```
- **12 秒后自动恢复验证**：`systemd-run` 定时器在 12 秒到期后自动移除 `DROP` 规则，外部探测器复测返回 `"healthy": true, "trading_state": "READY_NO_POSITION", "guardian_heartbeat_age_sec": 1.266`。

---

## 5. 任务 A.5：P0 授权信任边界加固证据

已在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py) 与 [`tests/test_p0_contracts_and_gates.py`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py) 完成三项硬性修复并通过双端 `pytest` 验证：
1. **可信公钥不可被不可信配置替换**：
   - 新增 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX = None` 与 `FORBIDDEN_UNTRUSTED_CONFIG_KEYS`（[`config.py#L57-L70`](file:///D:/OBR-SUI/src/sui_quant/config.py#L57-L70)）。
   - `SystemConfig.from_untrusted_mapping()` 拦截任何包含 `pinned_approver_pubkey_hex`、`trusted_approver_pubkey_hex`、`consumed_token_ids`、`audited_git_commit_sha`、`runtime_code_tree_sha256`、`nonce_registry` 的外部配置字典及 `*PUBKEY*`/`*TRUST_ROOT*` 环境变量，直接抛出 `LiveAuthorizationError`。
2. **重放令牌跨进程重启持久化防护**：
   - 新增 [`PersistentNonceRegistry`](file:///D:/OBR-SUI/src/sui_quant/config.py#L91-L173)，采用 SQLite WAL + `BEGIN IMMEDIATE` 原子事务将已消费 `token_id` 写入磁盘 `consumed_live_tokens` 表。即使进程重启且内存 `consumed_token_ids` 清空，重放同一令牌仍被磁盘注册表直接拦截。
3. **运行时可信构建代码树哈希绑定**：
   - 新增 [`compute_runtime_code_tree_sha256()`](file:///D:/OBR-SUI/src/sui_quant/config.py#L73-L88)，在 `verify_live_gate()` 时直接扫描并计算磁盘上 `src/sui_quant/**/*.py` 所有源码文件的聚合 SHA-256，与 Ed25519 签名令牌中的构建哈希比对；任意源码文件被改动 1 字节即刻拒绝解锁 `LIVE`。

---

## 6. 任务 A.6：修正文档中对 `root` 入侵的过度安全保证

已在 [`docs/THREAT_MODEL.md §4`](file:///D:/OBR-SUI/docs/THREAT_MODEL.md#L50-L82)、[`docs/P1_PLAN.md §03.(3)-(4)`](file:///D:/OBR-SUI/docs/P1_PLAN.md#L124-L147) 及 [`PHASE_HANDOFF.md`](file:///D:/OBR-SUI/PHASE_HANDOFF.md) 中删除所有“即使获得 `root` 权限也能由本机 `S-01` 或外部信标检测篡改”的过度承诺，明确写入真实安全边界：
- **一旦攻击者获得单台日本云主机的 `root` 权限**：攻击者可修改 `S-01` 进程内存中的公钥常量、篡改本地 SQLite 日志，并提取本机 `DMS_BEACON_WRITE_TOKEN` 向外部伪造携带合法根哈希的心跳信标；**任何同机软件机制均无法在 `root` 沦陷后提供可信安全保证**。
- **针对单机 `root` 沦陷的唯一真实机外防线（Out-of-Host Controls）**：
  1. 当前 P0/P1 阶段实行**零交易所密钥部署（Zero-Key Policy）**，主机上无任何可窃取的币安凭据；
  2. 未来实盘阶段依靠**币安交易所服务端权限隔离**（禁用提币权限 `Enable Withdrawals = OFF`、仅允许现货 `SUIUSDT`、子账户物理资金硬上限）限制最大敞口；
  3. 一旦怀疑 `root` 入侵，立即从外部独立设备吊销 API Key 并彻底销毁重建云主机，绝不信任被入侵主机上的自愈逻辑。

---

## 7. 任务 A.7：全量验证项状态核查表（未验证项严格保持 `PENDING`）

| 核查编号 | 验收事项描述 | 当前状态 | 证据路径 / 保持 `PENDING` 的具体原因 |
|---|---|---|---|
| **A7-01** | P0 契约、滤器边界、Ed25519 签名、不可覆盖公钥、持久化防重放、运行时代码树哈希绑定 | **`PASS`** | [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py), [`tests/test_p0_contracts_and_gates.py`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py) (`13 passed`) |
| **A7-02** | P1 本地与云端故障注入矩阵 `FI-01 ~ FI-09` 及 `FI-10A`（72h 虚拟时钟加速模拟） | **`PASS`** | [`tests/test_p1_fault_injection_matrix.py`](file:///D:/OBR-SUI/tests/test_p1_fault_injection_matrix.py) (`11 passed`) + 云端真实 `kill -9` 恢复 (`5.70s`) |
| **A7-03** | 跨物理机器（Windows 11 外部故障域 <-> 日本云主机）模拟失联与真实网络断联探测及本地 Webhook 回执 | **`PASS`** | [`src/sui_quant/daemon/external_dms_live_probe.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_dms_live_probe.py), [`evidence/external_alert_receipts.jsonl`](file:///D:/OBR-SUI/evidence/external_alert_receipts.jsonl) |
| **A7-04** | 文档对单机 `root` 沦陷的真实安全边界修正 | **`PASS`** | [`docs/THREAT_MODEL.md §4`](file:///D:/OBR-SUI/docs/THREAT_MODEL.md#L50-L82), [`docs/P1_PLAN.md`](file:///D:/OBR-SUI/docs/P1_PLAN.md#L124-L147) |
| **A7-05** | `FI-10B` 日本服务器**真实连续 72 小时墙钟稳定性运行观测** | **`PENDING`** | `sui-quant-72h-observer.service` 正在日本服务器连续采样（已采 4 组 60s 样本），**真实墙钟时间未满 72.0 小时（`259,200s`）前严格保持 `PENDING`** |
| **A7-06** | 第三方公有云（Cloudflare Workers 账号）生产部署与手机端 FCM 实网推送回执 | **`PENDING`** | [`deploy/cloudflare_worker/worker.js`](file:///D:/OBR-SUI/deploy/cloudflare_worker/worker.js) 已编写并完成额度核算；**因未提供用户 Cloudflare 与 FCM 生产凭据，严格保持 `PENDING`** |
| **A7-07** | 币安私有账户 API 权限核验（`QA-02` 子项）及 P2–P8 后续业务阶段 | **`PENDING`** | 当前严格执行零密钥与 `PAPER` 模式，后续阶段尚未实施，**严格保持 `PENDING`** |
