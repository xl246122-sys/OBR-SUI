# Binance Spot API 能力矩阵与实测核验表 (P0)

> **核验时间**：2026-10-31 04:03:47 UTC｜**探测源**：日本云服务器 (`<JAPAN_CLOUD_IP_REDACTED>`) 只读公开接口查询  
> **严格声明**：本表仅涵盖 **币安现货（Binance Spot API v3）**，绝不混入合约（Futures/Delivery）或杠杆（Margin）接口。所有未录入账户密钥的私有接口均明确标为 `PENDING_ACCOUNT_KEY`。

---

## 1. 交易对基础能力与精度过滤器（`GET /api/v3/exchangeInfo` 已实测确认）

| 属性 / 过滤器 | `SUIUSDT`（唯一交易标的） | `BTCUSDT`（仅作参考，禁止下单） | 验证状态 | 工程设计影响 |
|---|---|---|---|---|
| `status` | `TRADING` | `TRADING` | `CONFIRMED` | 现货市场正常交易 |
| `permissions` | `SPOT` (支持现货) | `SPOT` (支持现货) | `CONFIRMED` | 系统强制锁定仅使用 `SPOT` |
| `baseAssetPrecision` / `quoteAssetPrecision` | `8` / `8` | `8` / `8` | `CONFIRMED` | 内部账本使用 `Decimal` 保留 8 位小数 |
| `PRICE_FILTER.tickSize` | `0.00010000` (`1e-4` USDT) | `0.01000000` (`1e-2` USDT) | `CONFIRMED` | `SUIUSDT` 所有限价/触发价必须为 `0.0001` 的整数倍 |
| `LOT_SIZE.minQty` / `stepSize` | `0.10000000` (`0.1` SUI) | `0.00001000` (`1e-5` BTC) | `CONFIRMED` | **关键发现**：`SUIUSDT` 最小下单量与步长均为 `0.1 SUI`；若买单扣取 SUI 手续费导致净持仓含 `<0.1 SUI` 尾数（Dust），卖单必须按 `0.1` 向下取整（`ROUND_DOWN`） |
| `NOTIONAL.minNotional` | `5.00000000` (`5.0` USDT) | `5.00000000` (`5.0` USDT) | `CONFIRMED` | 单笔订单或 C2 分批止盈的每一档名义金额必须 `>= 5.0 USDT`（建议安全水位 `>= 5.5 USDT`） |
| `PERCENT_PRICE_BY_SIDE` | 买：`[0.5, 1.2]`；卖：`[0.8, 2.0]` (5m均价) | 买：`[0.5, 1.2]`；卖：`[0.8, 2.0]` | `CONFIRMED` | 限价单与止盈止损限价不可偏离 5m 加权均价超出此范围 |
| `orderTypes` | `LIMIT`, `LIMIT_MAKER`, `MARKET`, `STOP_LOSS`, `STOP_LOSS_LIMIT`, `TAKE_PROFIT`, `TAKE_PROFIT_LIMIT` | 同左 | `CONFIRMED` | 支持被动做市单 `LIMIT_MAKER` 及服务端条件止损单 |
| `ocoAllowed` / `otoAllowed` | `True` / `True` | `True` / `True` | `CONFIRMED` | 支持现货 OCO（止盈+止损二选一自动锁定同一笔持仓，避免双占余额） |
| `allowTrailingStop` | `True` (`TRAILING_DELTA`: `10 ~ 2000` bps) | `True` (`10 ~ 2000` bps) | `CONFIRMED` | 支持 0.1% ~ 20% 追踪止损 delta，可用于 C2/C3 尾仓追踪对照 |
| `cancelReplaceAllowed` | `True` (`MAX_NUM_ORDER_AMENDS = 10`) | `True` | `CONFIRMED` | 支持原子撤改单 `/api/v3/order/cancelReplace`，且单笔订单最多修改 10 次 |
| `MAX_NUM_ORDERS` / `MAX_NUM_ALGO_ORDERS` | `200` / `5` (`MAX_NUM_ORDER_LISTS = 20`) | `200` / `5` | `CONFIRMED` | 同一交易对最多同时挂 5 个条件/算法单（含 OCO 中的止损腿），C2 分批设计不得超过该硬上限 |

---

## 2. 全局速率限制（`rateLimits` 已实测确认）

| 限制类型 (`rateLimitType`) | 周期 (`interval`) | 官方上限 (`limit`) | SUI-Quant 本地配额预算（安全水位 ≤ 30%） |
|---|---|---|---|
| `REQUEST_WEIGHT` | `1 MINUTE` | `6,000` | `<= 1,200 / min`（日常靠 WebSocket，REST 仅按需对账/下单） |
| `ORDERS` | `10 SECOND` | `100` | `<= 10 / 10s`（严控 B-01 撤改单频率，防触发 429） |
| `ORDERS` | `1 DAY` | `200,000` | `<= 500 / day`（R-01 单日异常开仓/撤单熔断） |
| `RAW_REQUESTS` | `5 MINUTE` | `300,000` | `<= 5,000 / 5min` |

---

## 3. 行情与私有接口能力状态矩阵

| 接口类别 | 具体端点 / 流名称 | 实测网络延迟 (日本云主机) | 当前验证状态 | 待现场验证事项（P2/P4 阶段） |
|---|---|---|---|---|
| REST 连通性与时钟 | `GET /api/v3/ping`, `/api/v3/time` | `15.65 ~ 28.24 ms` | `CONFIRMED` | 已验证连通与微秒级 NTP 对齐 |
| 现货 WebSocket 市场流 | `wss://stream.binance.com:9443/ws` (`@aggTrade`, `@bookTicker`, `@depth10@100ms`, `@kline_1m/5m/15m/1h`) | TLS 握手 `15.58 ms` | `CONFIRMED` | P2 验证 23h 提前热轮换与 72h 连续稳定性 |
| 账户信息与专属费率 | `GET /api/v3/account`, `GET /api/v3/account/commission` | — | `PENDING_ACCOUNT_KEY` | 需在接入只读/测试 API Key 后读取真实 Maker/Taker 费率及 BNB 抵扣状态 |
| 测试下单核验 | `POST /api/v3/order/test` (`computeCommissionRates=true`) | — | `PENDING_ACCOUNT_KEY` | P4 阶段用于在不产生真实成交的前提下验证签名、过滤器与费率 |
| 现货 OCO 订单列表 | `POST /api/v3/orderList/oco` | — | `PENDING_ACCOUNT_KEY` | P4/P5 阶段在 Spot Testnet 验证 OCO 锁仓语义、部分成交与取消联动 |
| 账户事件流 | WebSocket API `userDataStream.subscribe` / `listenKey` (`executionReport`, `outboundAccountPosition`) | — | `PENDING_ACCOUNT_KEY` | P2/P4 阶段验证实盘/测试网账户推送与断线重连对账 |
