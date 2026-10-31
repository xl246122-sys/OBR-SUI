# ADR-004：密钥最小权限治理、不可放宽风控硬边界与全链路日志脱敏

- **状态**：Accepted (P0 冻结)
- **日期**：2026-10-31

## 背景
交易系统面临两类高危内外部风险：① 外部渗透或日志外泄导致币安 API Key 或服务器凭据暴露；② 策略参数调优或配置热更新时意外放宽了用户设定的风险底线。

## 决策
1. **不可放宽的风控代码硬边界（Hard Ceilings）**：在 `RiskConfig` 中将用户红线写死为代码级不变量校验：
   - `trading_symbol == "SUIUSDT"`，`reference_symbol == "BTCUSDT"`；
   - `allow_short == False`，`allow_leverage == False`，`allow_averaging_down == False`；
   - `stop_loss_trigger_pct == Decimal("0.01")`（严格 -1% 触发，禁止下移放宽）；
   - 在未获独立实盘授权前，候选风险上限锁定为：单笔账户风险预算 `<= 0.25%`（`Decimal("0.0025")`）、单次资金占用 `<= 10%`（`Decimal("0.10")`）、24h 滚动权益亏损熔断 `<= 1.0%`（`Decimal("0.01")`）。
2. **全链路自动脱敏（`RedactingFilter`）**：所有日志输出、异常消息与审计摘要均经过正则与键名双重脱敏器处理，自动将 `api_key`、`api_secret`、`password`、`signature`、`listenKey`、`token` 等字段值替换为 `[REDACTED]`。
