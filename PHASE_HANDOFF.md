# PHASE_HANDOFF｜阶段交接与验收档案

## 1. 当前阶段元信息
- **项目名称**：`SUI-Quant V1.0`
- **已完成阶段**：`MASTER-PLAN` + `P0` + `P1（服务器基础安全 S-01、全系统守护 F-02、独立风控进程 R-01 与外部存活探测）`
- **下一阶段待审计划**：[`docs/P2_PLAN.md`](file:///D:/OBR-SUI/docs/P2_PLAN.md)（`F-01` 实时行情采集与云-本地增量同步，状态：`READY_FOR_REVIEW`）
- **当前运行模式**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 默认禁用且受 Ed25519 非对称签名 + 7 项 GO/NO-GO + Nonce 防重放多重硬闸门拦截）
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易密钥数量**：`0`（NO）

---

## 2. P1 阶段完成交付摘要
1. **`F-02` 全系统守护与恢复门禁（[`src/sui_quant/daemon/heartbeat.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/heartbeat.py)）**：
   - 实现 5s 业务心跳写入、30s 事件循环卡死检测（同时核验 `monotonic_ns` 与 `loop_tick_seq` 推进）、5 分钟 5 次崩溃自动熔断器（`Crash-Loop Circuit Breaker`）及 `BOOT -> SYNCING` 五项恢复门禁（`Gate 1 ~ Gate 5`）。
   - 验证重启时发现未保护持仓（`QA-05`）自动优先进入 `PROTECTION_PENDING` 补挂保护单，确认后再进入 `PROTECTED`。
2. **`R-01` 独立风控进程与双向互监看门狗（[`src/sui_quant/daemon/r01_server.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/r01_server.py)）**：
   - 纯 `Decimal` 独立进程，零 ML 库依赖，配置 `Nice=-10`, `OOMScoreAdjust=-500`，实测 P99 裁决延迟 `0.136 ms`（远优于 `25ms` SLO）。
   - 实现 `R-01` 失联/超时 Fail-Closed 冻结新开仓（保留减仓保护卖出通道）及反向监控 `guardian` 的双向看门狗。
3. **`S-01` 主机安全总管与离线 Ed25519 签名清单（[`src/sui_quant/daemon/security_supervisor.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/security_supervisor.py)）**：
   - 实现 `SignedReleaseManifest`（离线 Ed25519 私钥签名 + 实时文件 SHA-256 与 `RiskConfig` 哈希校验，彻底防御同机摘要伪造攻击）。
   - 覆盖 SSH 异常登录、账户/权限/`0600` 检查、文件篡改、可疑进程/反弹 Shell、未授权监听端口与磁盘水位（`<20%` `HIGH` 自动清理；`<15%` `CRITICAL` 立即 `ENTRY_FROZEN`），严重安全事件立即触发 `SECURITY_LOCKDOWN` 并保存取证快照。
4. **`G-02` 告警分级、风暴抑制与持久化状态机（[`src/sui_quant/daemon/alert_manager.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/alert_manager.py)）**：
   - 实现 `INFO/HIGH/CRITICAL` 分级、冷却窗去重（100 条风暴合并为 `occurrence_count=100` 的单次外发）及 `PENDING / SENT_AWAITING_ACK / DELIVERED / FAILED_RETRYING` 诚实送达状态机。
5. **外部独立故障域存活探测（[`src/sui_quant/daemon/external_probe.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/external_probe.py)）**：
   - 实现云主机单向签名信标（`CloudHostBeaconSender`）与外部独立超时/根哈希核验器（`ExternalDeadMansSwitchEvaluator`），失联 `>90s` 或 `manifest_root_sha256` 篡改时独立触发 `CRITICAL` FCM 告警。
6. **云端部署、72h 压测与真实 `kill -9` 恢复验证（[`src/sui_quant/daemon/soak_runner.py`](file:///D:/OBR-SUI/src/sui_quant/daemon/soak_runner.py), [`deploy/`](file:///D:/OBR-SUI/deploy)）**：
   - 已部署至日本 2vCPU/2GB 云主机（`/opt/sui-quant`），通过全部 22 项测试、72h 加速压测及真实 `kill -9` 注入恢复（`5.70 秒` 内恢复并完成 `BOOT -> SYNCING -> READY_NO_POSITION` 对账）。

---

## 3. 验证命令与实测结果
- 本地与云端测试命令：`python -m pytest -v` → **`22 passed in 0.62s`（云端） / `0.94s`（本地）**
- 云端 72h 压测命令：`python3 -m sui_quant.daemon.soak_runner --mode soak --duration-hours 72 --cycles 2160`
  - 心跳检测延迟：`P50 = 0.344 ms`, `P95 = 0.399 ms`, `P99 = 0.452 ms`
  - `R-01` 风控裁决延迟：`P50 = 0.093 ms`, `P95 = 0.118 ms`, `P99 = 0.136 ms`（目标 `<= 25.0 ms`）
  - `S-01` 完整性与信标延迟：`P50 = 0.313 ms`, `P95 = 0.357 ms`, `P99 = 0.372 ms`
  - 内存稳定性：初始 `31.91 MB`，结束 `32.27 MB`，斜率 `0.0049 MB/h`（目标 `<= 0.1 MB/h`），FD 句柄恒定为 `7`。
- 云端 `systemd` 实测资源与 `kill -9` 恢复：
  - `sui-quant-r01.service` cgroup 内存：`13.5 MiB`（上限 `120 MiB`）；`kill -9` 后自动恢复耗时 **`5,702 ms`（`5.70s <= 10s`）**。
  - 整机内存：总计 `1,962 MiB`，已用 `539 MiB`，可用 `1,422 MiB`，Swap 使用 `0 MiB`；磁盘已用 `6.2 GB / 49 GB`（`14%`）。

---

## 4. 回滚方法
- 云端一键回滚：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p1.sh`
- 本地代码回滚：`git revert HEAD` 或 `git checkout <prev-sha>`

---

## 5. 下一阶段（P2）依赖与待审查事项
- 下一阶段为 **P2（`F-01` 行情采集与云-本地数据增量同步）**，计划已写入 [`docs/P2_PLAN.md`](file:///D:/OBR-SUI/docs/P2_PLAN.md)，状态 `READY_FOR_REVIEW`，等待审查批准后执行。
