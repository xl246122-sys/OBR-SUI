# P1-PLAN｜服务器基础安全（S-01）、全系统守护（F-02）与外部存活探测

> **阶段编号**：P1｜**当前状态**：`READY_FOR_REVIEW`（等待审阅批准，尚未在远端服务器部署 P1 守护进程）  
> **前置已完成**：`MASTER-PLAN`、`P0`（契约、风控硬闸门、API 能力实测及前期主机基础 SSH/UFW/Fail2Ban/内核加固）

---

## 01 任务理解与范围边界（引用设计文档章节）
- 依据总体设计方案 **§9（F-02 整套系统运行守护与自动恢复）** 与 **§10（S-01 服务器安全总管）**：
  - 在 2vCPU / 2GB RAM 日本 Ubuntu 云服务器上建立五类逻辑服务的 `systemd` 守护、受限资源配额（`MemoryMax` / `CPUQuota`）、业务级心跳监控（5s 周期 / 30s 卡死判定 / 崩溃 10s 内开始恢复）及日志轮转。
  - 强制实现**服务启动默认进入 `BOOT -> SYNCING`（在完成对账前禁止新开仓）**，避免进程崩溃重启后盲目开仓。
  - 落地 `S-01` 主机安全巡检守护（SSH 成功/失败审计、关键配置与代码 SHA-256 完整性校验、异常端口与进程监控、INFO/HIGH/CRITICAL 三级响应与 `SECURITY_LOCKDOWN` 冻结联动）。
  - 落地外部独立故障域存活探测（Dead-Man's Switch 心跳推送接口 + 独立失联判定逻辑）。

## 02 仓库/运行环境现状（只读事实，明确哪些无法验证）
- 日本云主机（`43.163.192.120`）已在前期完成 SSH 加固（`PermitRootLogin no`、`AllowUsers ubuntu`、`MaxAuthTries 3`）、`UFW` 默认拒绝入站、`Fail2Ban` 激进模式（`maxretry=2`, `bantime=24h`）及 `/etc/sysctl.d/99-security-hardening.conf`。
- 当前云主机尚未部署 `sui-quant` 守护目录、SQLite 审计表及 `systemd` 服务单元。

## 03 方案及依赖图（含数据合同和关键失败模式）
- **五类受控服务单元与内存配额（总预算 ≤ 1.15 GB，预留 >800 MB 系统余量）**：
  1. `sui-quant-f01.service`（行情与扫描，`MemoryMax=350M`）
  2. `sui-quant-d01.service`（状态机与模型推断，`MemoryMax=250M`）
  3. `sui-quant-r01.service`（独立风控进程，`MemoryMax=150M`）
  4. `sui-quant-g01.service`（Android 管理 API 与告警中心，`MemoryMax=150M`）
  5. `sui-quant-guardian.service`（F-02 业务看门狗 + S-01 安全巡检，`MemoryMax=100M`）
- **关键失败模式防护**：
  - **僵死进程（进程存在但事件循环卡死）**：各服务每 5 秒向本地 SQLite WAL / 共享内存写入带 `monotonic_ns` 的业务心跳；`F-02` 超过 30 秒未收到新心跳即判定 `STALLED`，触发冻结并重启目标服务。
  - **关键文件被篡改**：`S-01` 定期核对代码、风控配置与 `sshd_config` 的 SHA-256 清单；一旦失配立即发出 `CRITICAL` `AlertEvent`，将系统状态置为 `SECURITY_LOCKDOWN`（冻结新开仓、保留已有保护单、保全证据）。

## 04 待变更目录/文件清单
- **新增代码**：
  - `src/sui_quant/daemon/__init__.py`
  - `src/sui_quant/daemon/heartbeat.py`（5s 业务心跳、30s 卡死检测、重启 `SYNCING` 门禁）
  - `src/sui_quant/daemon/security_supervisor.py`（`S-01` 文件哈希完整性、SSH/端口巡检、三级响应与 `SECURITY_LOCKDOWN` 触发器）
  - `src/sui_quant/daemon/external_probe.py`（外部独立故障域 Dead-Man's Switch 心跳信标与失联告警器）
  - `deploy/systemd/`（五类服务的 `systemd` unit 模板与 `logrotate` 配置）
- **新增测试**：
  - `tests/test_p1_guardian_and_security.py`（覆盖 `QA-09`、`QA-10` 故障注入与入侵演练）

## 05 逐步实现计划
1. 实现 `heartbeat.py` 业务看门狗状态机（5s 心跳、30s 卡死判定、重启强制 `SYNCING`）。
2. 实现 `security_supervisor.py`（`S-01` 文件 SHA-256 基线校验、SSH 登录审计解析、端口白名单核验、CRITICAL 冻结联动）。
3. 实现 `external_probe.py`（外部存活信标签名发送与超时失联判定）。
4. 编写 `deploy/systemd/` 服务单元与资源限制模板，并在本地与云端沙盒完成故障注入验证（kill -9、业务卡死、文件篡改、磁盘水位告警）。

## 06 测试清单
- `test_heartbeat_stall_detection_30s_qa09`：注入 30s 无心跳卡死，验证自动触发重启与 `ENTRY_FROZEN`。
- `test_restart_forces_syncing_before_ready_qa05_qa09`：验证服务恢复后必须进入 `SYNCING` 而非直接恢复开仓。
- `test_s01_file_tamper_triggers_security_lockdown_qa10`：注入关键配置文件篡改，验证立即触发 `CRITICAL` 告警并进入 `SECURITY_LOCKDOWN`（且不撤销已有持仓保护）。
- `test_disk_watermark_freezes_new_entry_qa09`：注入磁盘可用空间不足告警，验证自动禁止新开仓。
- `test_external_dead_mans_switch_timeout_alert_qa10`：验证云主机心跳中断超过阈值时外部探测器独立生成 `CRITICAL` 失联告警。

## 07 验收标准：可测量门槛与具体证据文件路径
- 本地与云端集成测试 100% 通过；云端守护进程 RSS 内存占用 `<= 80 MB`。

## 08 风险与备选方案
- 若云端 systemd 单元异常，可通过一键回滚脚本 `systemctl stop sui-quant-*` 停止并恢复初始状态。

## 09 未验证假设及阻塞项
- 外部免费/低成本心跳接收端（如 Healthchecks.io 或 Cloudflare Worker 独立端点）的具体 URL 可在 P1/P7 由用户指定，代码层支持标准 HTTPS Webhook 接口抽象。

## 10 等待审查的问题与结论
- 状态：`READY_FOR_REVIEW`。确认 P0 验收通过后即可批准执行 P1。
