# P2_REMEDIATION_REPORT｜P2 遗留问题专项整改与证据档案

> **阶段状态**：`P2 REMEDIATED (PASS) / REAL 72H WALL-CLOCK & EXTERNAL CLOUD DMS & ANDROID REAL PUSH STRICTLY PENDING`  
> **提交日期**：`2026-10-31`  
> **核心原则**：零真实交易（`0`）、零交易密钥（`0`）、23h 热切换绝不承诺绝对零丢包（强制跨连接缺口检测）、关键收盘 K 线与数据质量事件零丢弃保证（`critical_dropped_count == 0`）、所有未完成或未接入实网的外部心跳与 Android 推送严格保持 **`PENDING`**。

---

## 1. 整改项一：自研 RFC 6455 WebSocket 客户端协议兼容性与故障安全性审查及测试证据

### 1.1 协议兼容性与故障安全加固（[`src/sui_quant/data/collector.py`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L708-L962)）
我们对 [`BinanceSpotWebSocketStream`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L708-L962) 进行了逐条 RFC 6455 协议合规性与 2GB 内存主机故障安全加固：

| 审查维度 | RFC 6455 规范要求 / 故障风险 | 代码实现与加固措施 | 自动化验证与实测证据 |
|---|---|---|---|
| **1. 握手校验 (`Sec-WebSocket-Accept`)** | §4.2.2 要求客户端必须核验服务端返回的 `HTTP/1.1 101`、`Upgrade: websocket`、`Connection: Upgrade` 及 `SHA1(Sec-WebSocket-Key + GUID)` Base64 签名 | 实现 [`compute_rfc6455_accept_key`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L84-L87) 与头部完整性核验，不匹配立即关闭传输层并抛出 [`WebSocketHandshakeError`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L73-L74) | 注入篡改的 `Sec-WebSocket-Accept` 响应头，验证客户端立即抛出 `WebSocketHandshakeError` |
| **2. 分片帧重组 (`Fragmented Frames`)** | §5.4 要求支持 `FIN=0, opcode=0x1/0x2` 起始帧 + 若干 `FIN=0, opcode=0x0` 延续帧 + `FIN=1, opcode=0x0` 终止帧，以及 16-bit (`126`) / 64-bit (`127`) 扩展长度；禁止无起始帧的孤立 `0x0` 或未完结前重开 `0x1/0x2` | 在 [`recv_json`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L823-L926) 中维护分片缓冲区 `fragments`，支持任意段数的 `0x1 -> 0x0 -> 0x0(FIN=1)` 重组并统计 `fragmented_messages_reassembled`；对孤立 `0x0` 或未完结重开数据帧立即抛出 [`WebSocketProtocolError`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L69-L70) | 注入 3 段分片（含 `>125B` 的 16-bit 扩展长度 `126`）的 `SUIUSDT@aggTrade` JSON 消息，验证完整无损重组（`fragmented_messages_reassembled == 1`） |
| **3. `Ping/Pong` 与分片内穿插控制帧** | §5.4 & §5.5 规定控制帧（`0x9 PING`, `0xA PONG`）**允许穿插在分片数据帧中间到达**，且控制帧 `FIN` 必须为 `1`、负载 `<= 125 Bytes`；客户端收到 `PING` 必须回复带掩码（Masked）且负载完全一致的 `PONG` (`0xA`) | 在分片循环内优先拦截 `0x9 PING`，立即回发带掩码的 `0xA PONG`（原样回显负载）后 `continue` 继续拼接未完结的数据分片，绝不破坏分片上下文；同时提供 [`send_ping`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L817-L821) 主动探测；对 `FIN=0` 或 `>125B` 的非法控制帧立即抛 `WebSocketProtocolError` | 在 3 段分片消息的第 1 段与第 2 段之间注入 `PING(b"BINANCE-HB-PING-42")`，验证服务端收到精确匹配的 Masked `PONG` 且 3 段分片 JSON 正常重组；同时验证客户端主动 `send_ping(b"CLIENT-PROBE-99")` 收到服务端 `PONG` |
| **4. 关闭帧 (`Close Frame`, `0x8`)** | §5.5.1 规定收到服务端 `0x8 CLOSE` 时需解析 2 字节大端状态码（如 `1000`, `1001`）与 UTF-8 原因字符串，向对端回发带掩码的 `CLOSE` 响应帧后关闭底层连接 | 解析 `close_code` 与 `close_reason`，未发过关闭帧时自动回发 Masked `CLOSE(code)`，关闭 Socket 并抛出结构化 [`WebSocketClosedError(close_code, close_reason)`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L77-L81)，联动 `F-01` 标记 `DataQuality.GAP_DETECTED` 与 `ENTRY_FROZEN` | 注入服务端 `CLOSE(code=1001, reason="Server rotation")`，验证服务端收到客户端回发的 Masked `CLOSE(1001)`，客户端捕获 `WebSocketClosedError(1001)` 并立即冻结新开仓 |
| **5. TLS 证书校验与加密套件** | 必须强制 `ssl.CERT_REQUIRED`、主机名校验（`check_hostname=True`）及 `TLSv1.2+` 最低版本，防止中间人攻击 | [`build_default_tls_context`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L761-L766) 强制 `CERT_REQUIRED` 与主机名校验，并在连接后提取 `ssl_object.version()` 与 `cipher()` | 默认上下文连接自签名测试服被 `ssl.SSLCertVerificationError` 严格拦截；受信 CA 下协商为 `TLSv1.3`；日本服务器连接币安实测协商为 `TLSv1.3` (`TLS_AES_128_GCM_SHA256`) |
| **6. 内存防爆边界与断线自动重连** | 防止恶意/损坏的 64-bit 帧长度引发 2GB 云主机 OOM；断线后需支持干净重连与状态恢复 | 在 `readexactly` 分配内存前强制检查 `max_frame_payload_bytes (4 MiB)` 与 `max_message_bytes (8 MiB)`；提供 [`reconnect()`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L811-L815) 并在重连后联动 REST K 线补齐与盘口缺口打标 | 验证 `CLOSE(1001)` 后调用 `reconnect()` 成功建立新连接（`reconnect_count == 1`）并接收后续 `#502` 消息 |

- **专项测试证据**：[`tests/test_p2_f01_collector_and_sync.py::test_p2_remediation_item1_rfc6455_websocket_ping_pong_fragmented_close_tls_and_reconnect`](file:///d:/OBR-SUI/tests/test_p2_f01_collector_and_sync.py#L121-L343)（本地 Windows 11 与日本 Ubuntu 云主机双端 `PASSED`）。

---

## 2. 整改项二：23 小时热切换跨连接去重、缺口识别与“不承诺绝对零丢包”诚实边界

1. **删除所有“绝对零丢包”承诺**：
   - 已在 [`src/sui_quant/data/collector.py`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L533-L619)、[`docs/P2_PLAN.md`](file:///d:/OBR-SUI/docs/P2_PLAN.md#L45-L49) 与 [`PHASE_HANDOFF.md`](file:///d:/OBR-SUI/PHASE_HANDOFF.md) 中明确声明：双连接提前热切换仅用于通过重叠窗口降低轮换中断，在公网抖动或双路丢包下**绝不承诺绝对零丢包**，返回报告中硬编码 `"zero_loss_guaranteed": False`。
2. **跨连接去重与序列号缺口识别（[`StreamQualityGuard.evaluate_event`](file:///d:/OBR-SUI/src/sui_quant/data/quality_guard.py#L283-L347) & [`execute_23h_hot_rotation`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L533-L619)）**：
   - **跨连接去重**：当主连接（`primary`）与备用连接（`standby`）在热切换窗口内收到相同事件（如 `203, 204, 205`）时，由 `StreamQualityGuard` 基于 `(symbol, source, event_id)` 与 `seq_or_update_id` 精确识别为 `DataQuality.DUPLICATE` 并丢弃，保证下游零重复派发。
   - **跨连接缺口检测**：热切换全程开启 `strict_consecutive_seq=True`；一旦备用连接首帧或切换过程中的序列号相比上一帧出现断层（`next_seq > prev_seq + 1`，例如主连接止于 `208` 而备用连接始于 `212`，缺失 `209..211`），立即：
     1. 生成 [`SequenceGapRecord`](file:///d:/OBR-SUI/src/sui_quant/data/quality_guard.py#L23-L39) 并记录缺失区间 `missing_seq_ranges = [(209, 211)]`；
     2. 将返回值标记为 `"gap_detected": True`；
     3. 将行情健康度置为 `DataQuality.GAP_DETECTED` 并强制状态机转入 `TradingState.ENTRY_FROZEN`（冻结新开仓）；
     4. 写入不可恢复盘口缺口标记 [`OrderbookGapMarker(recoverable=False)`](file:///d:/OBR-SUI/src/sui_quant/data/quality_guard.py#L179-L208)。
- **专项测试证据**：[`tests/test_p2_f01_collector_and_sync.py::test_qa01_23h_hot_connection_rotation_dedup_and_gap_detection_item2`](file:///d:/OBR-SUI/tests/test_p2_f01_collector_and_sync.py#L517-L586)（同时验证重叠去重场景与 `209..211` 跨连接缺口触发 `GAP_DETECTED + ENTRY_FROZEN` 场景，`PASSED`）。

---

## 3. 整改项三：背压策略关键收盘 K 线与数据质量事件零丢失保证及降级/丢弃统计

1. **关键事件零丢失硬不变式（`critical_dropped_count == 0`）**：
   - 在 [`BoundedBackpressureQueue`](file:///d:/OBR-SUI/src/sui_quant/data/ring_buffer.py#L130-L333) 与 [`F01MarketCollector._is_critical_raw_frame`](file:///d:/OBR-SUI/src/sui_quant/data/collector.py#L283-L311) 中，将以下事件定义为不可丢弃的关键事件（Critical Items）：
     - 所有已收盘 K 线（`e == "kline"` 且 `k.x == True`）；
     - 所有显式数据质量与控制事件（`is_data_quality_event == True` 或 `data_quality != OK`）；
     - 所有超时陈旧帧（`recv_time_utc - exchange_time_utc > max_staleness_ms`，必须保留以触发 `DataQuality.STALE` 停新仓）。
   - 当队列达到 `maxsize` 时：
     - 若新到达的是关键事件，优先扫描并淘汰队列中最旧的非关键瞬时快照（`depth10`、`bookTicker`、`kline_unclosed`、`aggTrade`）；即便极端情况下队列内 100% 均为关键事件，也强制保留该关键事件而不淘汰任何关键事件。
     - 若新到达的是非关键瞬时快照且队列已满，仅淘汰最旧瞬时快照（若队列内全为关键事件则直接丢弃新到达的瞬时快照），从而在数学与代码路径上保证 **`critical_dropped_count == 0`**。
2. **完整的快照降级与分类丢弃统计（[`BackpressureTelemetry`](file:///d:/OBR-SUI/src/sui_quant/data/ring_buffer.py#L26-L42)）**：
   - `collector.get_backpressure_telemetry()` 实时记录并输出：
     - `critical_enqueued_count` / `critical_dropped_count`（恒为 `0`）
     - `transient_enqueued_count` / `transient_dropped_count`
     - `dropped_by_category`：按类别精确统计丢弃数量（如 `{"depth10": 25, "kline_closed": 0, "data_quality": 0}`）
     - `high_watermark_triggers`（达到 80% 水位自动将 `depth10@100ms` 降级为 `1000ms` 的次数）
     - `low_watermark_recoveries`（回落至 40% 水位以下自动从 `1000ms` 恢复为 `100ms` 的次数）
     - `degradation_events`：完整记录每次降级（`DEGRADED_100MS_TO_1000MS`）与恢复（`RECOVERED_1000MS_TO_100MS`）的队列水位与序号。
- **专项测试证据**：[`tests/test_p2_f01_collector_and_sync.py::test_qa02_100ms_depth_backpressure_critical_zero_loss_and_drop_telemetry_item3`](file:///d:/OBR-SUI/tests/test_p2_f01_collector_and_sync.py#L589-L698)（`PASSED`）。

---

## 4. 整改项四：持续真实 72 小时墙钟稳定性观测与完整原始监控证据

- **观测服务状态**：日本服务器 `sui-quant-72h-observer.service` 自 `2026-10-31T06:05:40.064449+00:00` 启动以来**未发生任何中断或重置**，持续按真实物理墙钟每 60 秒采样一次。
- **完整原始证据归档路径**：
  - 逐分钟完整原始遥测 JSONL（含从 Sample `#1` 至最新样本的全部连续原始记录）：[`evidence/real_72h_observation_snapshot.jsonl`](file:///d:/OBR-SUI/evidence/real_72h_observation_snapshot.jsonl)（云端源路径：`/var/log/sui-quant/real_72h_observation.jsonl`）
  - 实时汇总状态 JSON：[`evidence/real_72h_status_snapshot.json`](file:///d:/OBR-SUI/evidence/real_72h_status_snapshot.json)（云端源路径：`/var/log/sui-quant/real_72h_status.json`）
- **当前诚实状态**：**`PENDING`**（截至 `2026-10-31T07:09:46.468632+00:00`，已连续采集 **65 组** 60s 原始样本，真实流逝时间 `3,846.4 秒` = `1.0684 小时`，完成度 `1.484%`；在未满 `259,200 秒 = 72.0 小时` 前严格保持 `PENDING`）。

---

## 5. 整改项五：外部心跳与 Android 实网通知严格保持 `PENDING`（绝不因模拟通过而关闭问题）

我们在 [`PHASE_HANDOFF.md`](file:///d:/OBR-SUI/PHASE_HANDOFF.md)、[`docs/REQUIREMENTS_TRACEABILITY_MATRIX.md`](file:///d:/OBR-SUI/docs/REQUIREMENTS_TRACEABILITY_MATRIX.md) 及本报告中严格执行以下状态界定：

| 验收项 | 已完成的模拟/跨机局域验证 | 尚未完成的实网生产闭环 | 当前正式状态 |
|---|---|---|---|
| **外部独立存活探测 (`QA-10` / Dead-Man's Switch)** | 跨物理机器（Windows 11 <-> 日本云主机）模拟失联与真实 `iptables DROP` 断联探测及本地 Webhook 签名回执已验证 | **7×24 第三方公有云（Cloudflare Workers）生产部署及独立实网告警出口尚未部署**（待提供 Cloudflare 生产凭据） | **`PENDING`**（问题保持开启，绝不因跨机测试通过而关闭） |
| **Android 实网通知与前后台告警 (`QA-11` / `G-01 & G-02`)** | 服务端 `G-02` 告警分级、风暴抑制与重试状态机已验证 | **Kotlin + Jetpack Compose 原生 Android 客户端、FCM 实网推送凭据及真机前后台通知回执尚未实现与核验** | **`PENDING`**（问题保持开启，绝不因服务端状态机通过而关闭） |
| **日本服务器真实连续 72 小时墙钟观测 (`FI-10B` / `QA-09`)** | `FI-10A` 72h 虚拟时钟加速模拟已通过；`sui-quant-72h-observer.service` 正在日本服务器持续采样 | **真实物理墙钟时间尚未满 72.0 小时（`259,200 秒`）** | **`PENDING`**（问题保持开启，直至满 72.0 真实小时） |
