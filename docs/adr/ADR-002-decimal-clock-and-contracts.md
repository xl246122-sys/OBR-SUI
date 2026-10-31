# ADR-002：严格十进制（Decimal）、双时钟（UTC + Monotonic）与不可变跨模块契约

- **状态**：Accepted (P0 冻结)
- **日期**：2026-10-31

## 背景
量化交易系统若使用二进制浮点数（`float`），在计算 `tickSize=0.0001`、`stepSize=0.1`、加权成交均价、`-1%` 止损触发价及 `+1.5%` 扣费净止盈目标价时，会产生 IEEE 754 精度误差（例如 `0.1 + 0.2 = 0.30000000000000004`），进而触发币安 API `Filter failure: PRICE_FILTER` 或 `LOT_SIZE` 拒单。此外，操作系统的墙上时钟（Wall Clock）在 NTP 校准时可能发生微小回拨，若直接用于计算进程内处理延迟会导致失真。

## 决策
1. **全面禁用 `float` 表示金融数值**：所有跨模块契约中的价格、数量、名义金额、手续费、概率与收益率字段一律强制使用 `decimal.Decimal`，且在构造器中显式拦截 `float` 类型（传入 `float` 立即抛出 `TypeError`）。
2. **双时钟标准**：
   - **业务与持久化时间戳**：统一使用带显式 `timezone.utc` 的 `datetime`（拒绝无时区 naive datetime 或非 UTC 时区）。
   - **进程内延迟度量（SLO 统计）**：统一使用 `time.monotonic_ns()` 单调递增纳秒时钟计算 `recv_monotonic_ns -> decision_monotonic_ns`，确保不受 NTP 步进影响。
3. **不可变数据契约**：§7.1 定义的十大标准契约全部使用 `@dataclass(frozen=True)`，一经创建不可原地篡改，每次状态演进均生成带唯一 ID 的新事件以便审计。
