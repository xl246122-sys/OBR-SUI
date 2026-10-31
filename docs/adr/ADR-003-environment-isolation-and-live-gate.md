# ADR-003：RESEARCH / SHADOW / PAPER / LIVE 环境隔离与“默认绝不实盘”硬闸门

- **状态**：Accepted (P0 冻结)
- **日期**：2026-10-31

## 背景
根据总体设计方案 §0.1 与 §16，系统默认必须运行在 `RESEARCH` 或 `PAPER` 模式，严禁因配置文件笔误、环境变量污染或模型自动升级而误触发 `LIVE` 真实资金交易。

## 决策
1. **枚举与默认锁定**：`ExecutionMode` 仅包含 `RESEARCH`、`SHADOW`、`PAPER`、`LIVE` 四种模式，`SystemConfig` 默认值硬编码为 `ExecutionMode.PAPER`。
2. **三重 `LIVE` 激活门禁（`verify_live_gate`）**：任何试图将系统初始化或切换为 `ExecutionMode.LIVE` 的操作，必须同时满足以下三项可验证条件，缺一不可，否则立即抛出 `LiveAuthorizationError` 并阻断启动：
   - **条件 1（显式开关）**：`allow_live_trading=True` 显式传入；
   - **条件 2（全量 GO/NO-GO 门禁）**：7 项上线门禁（API 权限、保护单验证、风险限额批准、异常停新仓、样本外净正收益、安全与外部心跳演练、独立审计记录）全部为 `True`；
   - **条件 3（用户签名授权凭证）**：提供非空且结构合法的 `LiveAuthorizationToken`（包含批准人标识、唯一交易对 `SUIUSDT`、批准的最大单笔仓位与日亏损上限、UTC 签发与过期时间、以及基于授权内容的 SHA-256 摘要校验）。
