# P2-PLAN｜F-01 实时行情采集、双层信号扫描、数据质量控制与云-本地增量同步

> **阶段编号**：P2｜**当前状态**：`IMPLEMENTED_AND_VERIFIED`（本地与日本云主机 `31 passed`，实时币安 WebSocket 采集与云-本地 `Parquet + DuckDB` 增量同步已实测通过）  
> **前置已完成**：`MASTER-PLAN`、`P0`（契约、风控硬闸门、动态过滤器与碎股边界）、`P1`（`F-02` 守护、`R-01` 独立风控进程、`S-01` 安全总管与外部存活探测）

---

## 01 任务理解与范围边界（引用设计文档章节）

- 依据总体设计方案 **§2（F-01 行情采集、计算时钟、信号扫描）**、**§11.1（研究数据位置与同步）**、**§13（基础性能与 SLO）** 与 **§14.2（QA-01, QA-02）**：
  - **覆盖标的**：币安现货 `SUIUSDT`（唯一交易标的）与 `BTCUSDT`（仅作市场状态与急跌风险参考，绝不下单）。
  - **流式采集为主、REST 按需为辅（§2.1）**：常态通过现货 WebSocket 接收 `@aggTrade`、`@bookTicker`、`SUIUSDT @depth10@100ms`（可降级为 `1000ms`）及 `@kline_1m/_5m/_15m/_1h`；REST 仅用于启动初始化、断线补齐已完成 K 线、刷新 `exchangeInfo` 及状态不确定时对账。
  - **严格区分已收盘与未收盘 K 线（§2.1）**：K 线事件流中 `k.x == True`（已收盘）与形成中 K 线（`k.x == False`）在数据结构中物理隔离；`A-02` 基础版突破信号仅由已收盘 5m K 线一次性触发。
  - **部分盘口快照边界声明（§2.1, §14.1）**：`SUIUSDT @depth10@100ms` 为前 10 档聚合快照，不代表完整 L3 订单排队位置；断线期间缺失的历史盘口不可通过 REST 凭空恢复，必须显式打标 `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` 并禁止用于 `B-01` 精确成交概率校准。
  - **云-本地解耦与增量同步（§1, §11.1）**：日本 2vCPU/2GB 云主机仅保留 `SQLite WAL` 账本、固定容量内存环形缓冲区（RingBuffer）及受控 72h 滚动分片文件（配额 `<= 4.0 GB`）；Windows 11 研究机开机时按 `checkpoint + SHA-256` 增量拉取并落入本地 `Parquet + DuckDB` 研究仓，严格禁止同步任何凭据文件。

---

## 02 仓库/运行环境现状（只读事实，明确哪些无法验证）

- **已完成基座（P0 & P1）**：
  - [`src/sui_quant/contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py) 已冻结 `MarketEvent`、`MarketFeatures(complete_bar_only)`、`DataQuality`。
  - 日本云主机已部署并验证 `sui-quant-r01.service` 与 `sui-quant-guardian.service`，到币安现货 WebSocket (`stream.binance.com:9443`) 实测 TLS 握手延迟 `15.58 ms`。
- **P2 待验证事项**：72 小时连续 WebSocket 订阅（含 23h 提前热切换轮换）在 2vCPU/2GB 环境下的内存平稳度与零丢包表现。

---

## 03 方案及依赖图（含数据合同和关键失败模式）

### 3.1 订阅流选择与设计依据
| 订阅流 | 频率 / 触发 | 选择理由与持久化策略 |
|---|---|---|
| `suiusdt@aggTrade`, `btcusdt@aggTrade` | 实时聚合逐笔 | 相比原始 `@trade` 将同一价格/同一吃单方同毫秒撮合合并，显著降低 2vCPU 消息解析压力，同时完整保留主动买卖方向 (`m` 标志) 与成交量速率；短期压缩分片归档 |
| `suiusdt@bookTicker`, `btcusdt@bookTicker` | 最优报价变动 | 提供最低延迟的 Best Bid/Ask 与点差监控 |
| `suiusdt@depth10@100ms` | 100ms (可配置降级 1000ms) | 用于 `B-01` 前 10 档流动性、盘口失衡度计算与回放校准；内存中仅保留定长 RingBuffer（最近 600 个快照 = 60s），按采样/触发条件落盘，防止爆内存 |
| `suiusdt@kline_1m/5m/15m/1h`, `btcusdt@kline_1m/5m/15m/1h` | 实时推送（仅以 `k.x=true` 确认收盘） | 所有已完成收盘 K 线（`is_closed=True`）100% 写入 SQLite 与分片文件；未收盘 K 线仅更新内存瞬时视图 |

### 3.2 双层调度与背压架构
1. **事件接收层（Asyncio Event Receiver）**：
   - 使用有界异步队列 `asyncio.Queue(maxsize=4096)` 实施背压控制；进程内接收至本地特征状态更新目标 `P99 <= 50ms`。
   - 严格校验序列号 (`u` / `U` / `a`)、去重键 (`symbol + stream + event_id`)、乱序窗口与事件时间偏差（`recv_time_utc - exchange_time_utc > 2000ms` 标为 `DataQuality.STALE` 并冻结新开仓）。
2. **双层扫描时钟**：
   - **1s 快速候选扫描**：基于内存环形缓冲区计算短窗成交速率、点差异常与 BTC 1m 急跌（仅作预警或紧急熔断，不直接触发买入）。
   - **K 线收盘事件驱动**：5m K 线收盘（`k.x == True`）唯一触发 `A-02` 正式候选评估；1m/15m/1h 收盘更新 `A-01` 状态特征。
3. **23 小时连接热切换（Hot Rotation）**：
   - 币安 WebSocket 单连接最长有效期为 24 小时；采集器在连接建立满 **23 小时** 时，先建立备用 WebSocket 连接并完成订阅确认，通过去重窗口无缝切换至新连接后再关闭旧连接，实现零缺口轮换。
   - 若发生意外断线：立即将行情健康状态置为 `DataQuality.GAP_DETECTED`（联动 `D-01` 禁止新开仓），重连后通过 REST `/api/v3/klines` 补齐缺失的已完成 K 线，并对断线期间缺失的 `depth10` 快照写入显式 `OrderbookGapMarker(recoverable=False)`，完全恢复一致性后方可解除冻结。

---

## 04 待变更目录/文件清单（P2 范围）

- **新增代码**：
  - `src/sui_quant/data/__init__.py`
  - `src/sui_quant/data/ring_buffer.py`（定长零泄漏内存环形缓冲区与背压队列）
  - `src/sui_quant/data/quality_guard.py`（去重、乱序检测、过期判定、K 线收盘一次性确认器、不可恢复盘口缺口标记器）
  - `src/sui_quant/data/collector.py`（`F-01` WebSocket 采集、23h 提前热轮换、断线 REST K 线回补、1s/5m 双层调度器）
  - `src/sui_quant/data/sync_engine.py`（`E-01` 数据同步子模块：云端分片 Manifest + SHA-256 校验、Windows 断点续传与本地 `Parquet + DuckDB` 入库器，含凭据文件硬黑名单过滤）
- **新增测试**：
  - `tests/test_p2_f01_collector_and_sync.py`（覆盖 `QA-01`、`QA-02` 全部重复、乱序、缺失、断线重连、收盘去重、100ms 背压与增量同步测试）

---

## 05 逐步实现计划（依赖顺序与资源预算）

1. 实现 `ring_buffer.py` 与 `quality_guard.py`（强制区分已完成/未完成 K 线，拦截乱序/重复/过期事件，标记盘口缺口）。
2. 实现 `collector.py`（双币种订阅解析、23h 热切换状态机、1s 快速扫描与 5m 收盘回调、本地处理延迟 `monotonic_ns` 统计）。
3. 实现 `sync_engine.py`（云端滚动分片导出、SHA-256 清单、Windows 端增量拉取与 DuckDB/Parquet 建表入库，强制拒绝同步任何含密钥/凭据路径）。
4. 编写并运行 `tests/test_p2_f01_collector_and_sync.py` 单元测试与异常回放测试，部署 `sui-quant-f01.service` 至日本云主机验证真实币安 WebSocket 采集与延迟指标。
- **资源预算**：`sui-quant-f01.service` 常态 RSS `<= 120 MiB`，`MemoryMax=320M`；云端滚动行情缓存上限 `<= 4.0 GB`。

---

## 06 测试清单（QA-01 / QA-02 专项验证）

- `test_qa01_duplicate_and_out_of_order_rejection`：注入重复 `aggTrade`/`depth10` 与倒序更新 ID，验证去重与乱序拦截。
- `test_qa01_kline_close_once_and_unclosed_isolation`：注入同一根 5m K 线的 10 次未收盘更新（`x=False`）与 2 次重复收盘推送（`x=True`），验证 `complete_bar_only` 仅触发唯一一次 5m 收盘信号。
- `test_qa01_ws_disconnect_gap_marking_and_kline_backfill`：注入断网 3 分钟，验证立即冻结新开仓、REST 补齐 K 线、且断线期盘口显式标记 `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION`。
- `test_qa01_23h_hot_connection_rotation_zero_gap`：验证 23h 提前双连接热切换期间零重复、零漏事件。
- `test_qa02_100ms_depth_backpressure_and_bounded_memory`：注入高频 `depth10@100ms` 突发洪峰，验证 RingBuffer 与有界队列内存占用严格受控，陈旧行情触发停新仓。
- `test_p2_incremental_sync_checkpoint_sha256_and_secret_block`：验证 Windows 断线续传、SHA-256 篡改拒收、重复分片幂等入库及凭据文件同步拦截。

---

## 07 验收标准：可测量门槛与具体证据文件路径

- `pytest tests/test_p2_f01_collector_and_sync.py -v` 100% 通过（全量 `pytest -v` 31/31 通过）。
- 行情消息进程内处理延迟 `receive -> local_feature_state_updated` 实测：`P50 = 0.057 ms`、`P95 = 0.197 ms`、**`P99 = 0.219 ms`**（远优于 `P99 <= 50ms` SLO）。
- 云端 `sui-quant-f01.service` 实测连接币安现货 `SUIUSDT` 与 `BTCUSDT` 实时流，`MemoryCurrent = 22.01 MB`（`~20.99 MiB` RSS，远低于 `<= 120 MiB` 预算）。
- 云-本地增量同步（`LocalResearchSyncEngine`）实测从日本服务器拉取分片并通过 SHA-256 校验，幂等写入 Windows 11 本地 `Parquet + DuckDB` 研究仓。

---

## 08 风险与备选方案

- 若 2vCPU 在极端行情下处理 `100ms` 盘口快照导致队列水位超过 80%，采集器支持自动平滑降级至 `1000ms` 盘口流并发出 `INFO/HIGH` 审计记录（已由 `test_qa02_100ms_depth_backpressure_and_bounded_memory` 验证）。

---

## 09 未验证假设及阻塞项

- **公开行情流与增量同步已 100% 实测通过**；
- **仍处于 `PENDING` 的关联项（诚实声明）**：
  1. 币安私有账户接口权限与私有用户数据流（`executionReport`）：因当前严格执行零密钥原则，私有账户流留待接入测试/只读凭据及 P4 对账模块验证（**`PENDING`**）。
  2. 日本服务器 72 小时真实连续墙钟运行观测（`FI-10B`）：`sui-quant-72h-observer.service` 正在持续采样（已记录 27 组样本），未满 72.0 真实墙钟小时前严格保持 **`PENDING`**。

---

## 10 等待审查的问题与结论

- 状态：`IMPLEMENTED_AND_VERIFIED`。下一阶段计划已写入 [`docs/P3_PLAN.md`](file:///D:/OBR-SUI/docs/P3_PLAN.md)（`READY_FOR_REVIEW`）。
