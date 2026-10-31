# PHASE_HANDOFF｜阶段交接与验收档案（P0 + P1 + P2 已实现，含显式 PENDING 诚实声明）

## 1. 当前阶段元信息
- **项目名称**：`SUI-Quant V1.0`
- **当前阶段状态**：`P0 + P1 + P2 IMPLEMENTED_AND_VERIFIED (WITH EXPLICIT PENDING ITEMS)`
- **下一阶段待审计划**：[`docs/P3_PLAN.md`](file:///D:/OBR-SUI/docs/P3_PLAN.md)（`A-01` 市场状态数学模型与 `A-02` 向上放量突破三障碍校准概率建模，状态：`READY_FOR_REVIEW`）
- **当前运行模式**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH`（`LIVE` 默认禁用且受代码级不可替换公钥常量 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX` + Ed25519 非对称签名 + `PersistentNonceRegistry` 磁盘持久化防重放 + `compute_runtime_code_tree_sha256` 运行时源码树哈希绑定 + 7 项 GO/NO-GO 硬闸门拦截）
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易密钥数量**：`0`（NO）

---

## 2. P2（`F-01` 行情采集与 `E-01` 云-本地增量同步）交付摘要
1. **定长内存环形缓冲区与有界背压队列（[`src/sui_quant/data/ring_buffer.py`](file:///D:/OBR-SUI/src/sui_quant/data/ring_buffer.py)）**：
   - 实现 `FixedRingBuffer`（O(1) 追加、自动覆写最旧槽位、零内存泄漏）与 `BoundedBackpressureQueue`（`maxsize=4096`，队列水位达 `80%` 自动将 `depth10@100ms` 降级为 `1000ms` 并触发告警；满队列时优先保留关键收盘 K 线 `k.x=True` 并淘汰最旧瞬时快照）。
2. **行情质量守卫、收盘/未收盘 K 线物理隔离、跨连接缺口检测与不可恢复盘口缺口标记（[`src/sui_quant/data/quality_guard.py`](file:///D:/OBR-SUI/src/sui_quant/data/quality_guard.py)）**：
   - 实现重复帧（`DataQuality.DUPLICATE`）、乱序帧（`DataQuality.OUT_OF_ORDER`）、流内与跨连接序列号断层（`SequenceGapRecord` -> `DataQuality.GAP_DETECTED`）及超 2000ms 延迟帧（`DataQuality.STALE` 联动冻结新开仓）拦截。
   - 物理隔离未收盘 K 线（`UnclosedKlineTransientView`, `is_closed=False`）与已收盘 K 线（`ClosedKlineBar`, `is_closed=True`），保证同一根 5m K 线收盘回调至多触发唯一一次（`QA-01`）。
   - 针对 WebSocket 断线或热切换缺口期间的 `depth10` 部分盘口缺口，强制写入 `OrderbookGapMarker(recoverable=False, calibration_tag="GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION", allow_b01_fill_calibration=False)`，严禁将缺口窗口用于 `B-01` 成交概率校准。
3. **`F-01` 实时采集器、RFC 6455 协议加固客户端、23h 双连接热切换（含跨连接去重与缺口检测，不承诺绝对零丢包）与 1s/5m 双层扫描（[`src/sui_quant/data/collector.py`](file:///D:/OBR-SUI/src/sui_quant/data/collector.py)）**：
   - 自研 `BinanceSpotWebSocketStream` 通过 RFC 6455 全面审查与测试：核验 `Sec-WebSocket-Accept` 签名、支持多段分片帧重组及分片中间穿插 `PING/PONG` 控制帧、支持 `CLOSE` 双向关闭握手、强制 `TLSv1.3` (`CERT_REQUIRED`) 与 `4MiB/8MiB` 内存防爆边界、支持断线自动重连。
   - 23 小时提前双连接热切换（`execute_23h_hot_rotation`）：验证跨连接重叠去重与跨连接序列号缺口识别（`missing_seq_ranges`），**显式声明不承诺绝对零丢包（`zero_loss_guaranteed=False`）**；一旦发生跨连接序列断层立即置为 `GAP_DETECTED` 并转入 `ENTRY_FROZEN`。
   - 背压策略（`BoundedBackpressureQueue` + `BackpressureTelemetry`）：硬性保证关键已收盘 K 线（`k.x=True`）与数据质量事件（`STALE`/`GAP_DETECTED`）**零丢失（`critical_dropped_count == 0`）**，并完整记录 `100ms <-> 1000ms` 降级恢复轨迹与分类丢弃统计 `dropped_by_category`。
   - 已部署 [`deploy/systemd/sui-quant-f01.service`](file:///D:/OBR-SUI/deploy/systemd/sui-quant-f01.service) 至日本云主机运行，8s 基准实测接收处理 `1,130` 条币安现货事件，协商 `TLSv1.3 (TLS_AES_128_GCM_SHA256)`，进程内处理延迟 **`P50 = 0.050 ms`、`P95 = 0.139 ms`、`P99 = 0.208 ms`**（远优于 `50ms` SLO），常驻内存 `MemoryCurrent = 21.61 MB`（`~20.61 MiB` RSS，远低于 `120 MiB` 预算）。
4. **`E-01` 云-本地增量同步与 `Parquet + DuckDB` 研究仓入库（[`src/sui_quant/data/sync_engine.py`](file:///D:/OBR-SUI/src/sui_quant/data/sync_engine.py)）**：
   - 云端 `CloudShardExporter` 生成带 SHA-256 的 JSONL 分片与原子 `manifest.json`，强制执行 `<= 4.0 GB` 滚动配额。
   - 本地 Windows 11 `LocalResearchSyncEngine` 实现断点续传（`sync_checkpoint.json`）、SHA-256 篡改拒收（`SyncIntegrityError`）、幂等写入本地 Snappy 压缩 `Parquet` 与 `DuckDB` 表（`market_klines`, `agg_trades`），并内置凭据/密钥路径硬黑名单（`SecretSyncBlockedError`）。

---

## 3. 验证命令与实测结果（含显式 `PENDING` 清单，绝不因模拟通过而关闭问题）
- 本地 Windows 11 测试命令：`python -m pytest -v` → **`32 passed in 3.17s`**
- 日本云主机测试命令：`PYTHONPATH=src python3 -m pytest -v` → **`32 passed in 1.46s`**
- P2 遗留整改详细证据报告：[`docs/P2_REMEDIATION_REPORT.md`](file:///D:/OBR-SUI/docs/P2_REMEDIATION_REPORT.md)
- 日本云主机守护进程状态：`sui-quant-r01.service` (`active`)、`sui-quant-guardian.service` (`active`)、`sui-quant-f01.service` (`active`)、`sui-quant-72h-observer.service` (`active`)。
- **当前仍处于 `PENDING` 的未完结项（严格保持开启，绝不因模拟/局域测试通过而关闭或伪报 `PASS`）**：
  1. `FI-10B` 日本服务器真实连续 72 小时墙钟稳定性运行观测：**`PENDING`**（`sui-quant-72h-observer.service` 正在持续采样，最新已记录 `65` 组连续样本 / `3,846.4s` = `1.0684h` 真实流逝时间，完整原始证据保存在 [`evidence/real_72h_observation_snapshot.jsonl`](file:///D:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)，需满 `259,200s` = `72.0` 真实小时后方可转 `PASS`）。
  2. 外部独立心跳（Cloudflare Workers 7×24 公有云生产部署）与 Android 实网通知（`G-01/G-02` 原生客户端 + FCM 真机前后台实网推送回执）：**`PENDING`**（虽已完成跨机断联探测与服务端告警状态机验证，但公有云与真机实网推送尚未接入验证，严格保持 `PENDING` 不关闭问题）。
  3. 币安私有账户 API 权限核验（`QA-02` 私有账户子项）及 P3–P8 后续阶段模块验收：**`PENDING`**。

---

## 4. 回滚方法
- 云端 P2 独立回滚（保留 P1 服务）：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p2.sh`
- 云端 P1+P2 全量回滚：`sudo /bin/bash /opt/sui-quant/deploy/scripts/rollback_p1.sh`
- 本地代码回滚：`git revert HEAD` 或 `git checkout <prev-sha>`

---

## 5. 下一阶段（P3）依赖与待审查事项
- 下一阶段为 **P3（`A-01` 市场状态数学模型与 `A-02` 突破概率科学建模）**，计划已写入 [`docs/P3_PLAN.md`](file:///D:/OBR-SUI/docs/P3_PLAN.md)，状态 `READY_FOR_REVIEW`，等待审查批准后执行。
