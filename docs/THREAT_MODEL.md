# SUI-Quant V1.0｜身份、密钥、主机与执行安全威胁模型 (THREAT_MODEL)

> **编制阶段**：P0｜**更新日期**：2026-10-31｜**对应模块**：`S-01`（主机安全总管）、`R-01`（独立风控）、`D-01`（状态机）、`E-01`（制品发布）

---

## 1. 资产与信任边界定义

### 1.1 核心受保护资产
1. **用户交易所账户资金与持仓**：仅允许在预先批准的风险预算内交易现货 `SUIUSDT`，严禁任何未保护裸仓或超额下单。
2. **Binance Spot API 凭据**：最高敏感度资产；一旦泄漏可能被攻击者用于恶意对敲或消耗资金。
3. **风控边界配置（`RiskConfig`）与模型制品（`ModelManifest`）**：一旦被篡改可能导致止损失效或策略失控。
4. **交易账本与审计日志（`SQLite WAL`）**：事实核对与事故溯源依据。

### 1.2 四大信任域隔离
- **域 A（本地 Windows 11 研究域）**：持有历史数据与离线训练代码，可生成签名模型制品，但**绝不持有、不存储、不接触币安生产 API 私钥**。
- **域 B（日本 Ubuntu 云端执行域）**：运行实时采集、推断、风控与订单执行；API 凭据仅在未来实盘授权后以最小权限（`0600` root/专用系统账户只读挂载或 systemd LoadCredential）存在于该主机内存/受保护区。
- **域 C（Android 原生监控域）**：仅持有与云端 `G-01` 通信的受限设备令牌（存于 Android Keystore），只允许读取状态及触发 `pause_new_entries` / `emergency_lock`，**无任何任意下单接口，不保存交易所密钥**。
- **域 D（外部独立存活探测域）**：仅接收云主机的周期性单向心跳推送（或探测云主机健康端口），不持有任何云主机 SSH 权限或交易凭据。

---

## 2. STRIDE 威胁分析与强制缓解措施

| 威胁编号 | 威胁类别 | 攻击场景描述 | 严重度 | 强制工程控制措施（P0–P8 落地） |
|---|---|---|---|---|
| **TM-01** | 权限提升 / 远程入侵 | 自动化僵尸网络或 AI 渗透工具扫描爆破云主机 SSH（已在前期日志中实测观测到每分钟针对 `root`/`support` 的爆破） | `CRITICAL` | 已启用：`PermitRootLogin no`、`AllowUsers ubuntu`、`MaxAuthTries 3`、`UFW` 默认拒绝入站、`Fail2Ban` 激进模式动态封禁 + 内核 `sysctl` 加固；P1 增加 `S-01` 实时监控异常登录并联动 `SECURITY_LOCKDOWN`。 |
| **TM-02** | 凭据泄漏（Git / 日志 / Prompt） | API Key、Secret、服务器密码被意外写入日志文件、异常堆栈、Prompt 或提交至 Git 仓库 | `CRITICAL` | P0 已落地：`.gitignore` 强制排除凭据文件；`sui_quant.security.redaction` 日志过滤器自动拦截并掩码所有 Key/Secret/Signature/Password；单元测试强制核验。 |
| **TM-03** | 交易所 API Key 被盗用 | 云主机被入侵或凭据外泄，攻击者尝试提币或跨市场对敲 | `CRITICAL` | 密钥策略强制要求：**禁用提现权限（Enable Withdrawals = OFF）**、**仅开启现货权限**、**强制绑定日本云主机固定公网 IP（`<JAPAN_CLOUD_IP_REDACTED>`）**、交易对白名单锁定 `SUIUSDT`。 |
| **TM-04** | 默认或误触实盘（Accidental Live） | 开发测试、配置错误或环境变量污染导致在未获用户批准时发出真实资金订单 | `CRITICAL` | P0/Task A.5 已落地多重硬闸门：`ExecutionMode` 默认锁定为 `PAPER`/`RESEARCH`；切换 `LIVE` 必须同时通过：① 代码级不可覆盖公钥锚点 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX`、② Ed25519 签名校验 + 磁盘持久化防重放 `PersistentNonceRegistry` + 运行时代码树 `compute_runtime_code_tree_sha256` 绑定、③ `GO_NO_GO` 门禁检查全绿。不可信配置或环境变量尝试注入公钥/Nonce 立即抛出 `LiveAuthorizationError`。 |
| **TM-05** | 策略/AI 模型越权改写风控 | 训练脚本或异常模型制品尝试修改止损比例（`-1%`）、扩大单笔仓位（`>10%`）或交易其他币种 | `CRITICAL` | P0 已落地：`RiskConfig` 在代码层内置不可放宽硬上限（Hard Ceiling）；任何超出候选上限的配置实例化即抛 `RiskBoundaryViolationError`；`D-01` 禁止模型直接调用下单接口。 |
| **TM-06** | 买单成交但止损保护失败（裸仓风险） | 买单全部或部分成交后，网络超时、进程崩溃或余量不足（Dust）导致止损单未能成功挂出 | `CRITICAL` | 状态机强制经由 `PROTECTION_PENDING` 验证交易所确认后才可进入 `PROTECTED`；若超时或失败，立即转入 `ENTRY_FROZEN` 并触发 `PROTECTION_DEGRADED` 高优告警，同时执行受控降级退出。 |
| **TM-07** | 云服务器整机宕机或断网 | 云主机断电、内核崩溃或机房断网，本机内部的 `F-02`/`S-01` 无法发出告警 | `HIGH` | 1. 持仓保护优先采用币安交易所端托管条件单/OCO；2. P1/P7 部署独立于云主机的外部存活探测（Dead-Man's Switch），失联超阈值独立推送 Android 告警。 |
| **TM-08** | 模型或配置传输篡改 | Windows 研究端向云端同步模型制品时遭遇中间人篡改或文件损坏 | `HIGH` | `ModelManifest` 强制携带 `sha256`、`features_schema_hash` 与 `rollback_ref`；`E-01` 在云端校验哈希与 schema 兼容后才允许进入 `SHADOW`/`PAPER`。 |

---

## 3. 安全事件分级与自动响应铁律（§10）

1. **`INFO`**：常规安全巡检、正常配置审计、被 `Fail2Ban` 常规拦截的外部扫描 → 计入安全日报。
2. **`HIGH`**：单次同步校验失败、API 速率接近预算阈值、非关键文件变动 → 发出 App 告警并执行可逆限流。
3. **`CRITICAL`**：检测到未授权 SSH 登录成功、关键二进制/风控配置哈希被篡改、API 权限异常、日志被清空 → **立即触发 `SECURITY_LOCKDOWN`**：
   - 立刻冻结一切新增开仓（`allow_new_entry = False`）；
   - **绝不盲目撤销已有持仓的交易所止损保护单**（防止造成无保护裸仓）；
   - 保全现场证据快照（不自动删除入侵痕迹）；
   - 向 Android 端发送最高优先级告警，必须由人工审查确认安全并从可信环境恢复后方可解除锁定。

---

## 4. 单机 `root` 权限沦陷的真实信任边界与防御局限声明（Task A.6 强制修正）

> [!CAUTION]
> **严禁对单机 `root` 入侵做出过度安全承诺**：当攻击者已经获取日本云服务器（`<JAPAN_CLOUD_IP_REDACTED>`）的 **`root` 权限（或内核特权）** 时，任何运行于同一台云主机内部的软件控制措施均**失效且无法提供绝对安全保证**。

### 4.1 明确声明在 `root` 沦陷下失效的同机机制
一旦攻击者获得日本服务器的 `root` 权限，以下同机防御机制**均可被攻击者绕过或伪造**：
1. **同机进程与文件完整性检查（`S-01` / `SignedReleaseManifest`）**：拥有 `root` 权限的攻击者可以直接卸载 `chattr -i`、修改 `/opt/sui-quant` 中的 Python 字节码或直接篡改 `S-01` 内存中固定的公钥常量（`pinned_release_pubkey_hex` / `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX`），甚至注入 `ptrace` 劫持运行中的 `R-01` / `guardian` 进程。
2. **同机防重放数据库（`PersistentNonceRegistry`）与本地审计日志（`SQLite WAL`）**：`root` 攻击者可以直接读写或伪造 `/var/lib/sui-quant/state.db` 与 `/var/log/sui-quant/`。
3. **云主机向外发送的健康信标（`CloudHostBeaconSender`）**：只要 `root` 攻击者提取了本机内存中的 `BEACON_WRITE_TOKEN` 和 `manifest_root_sha256` 常量，即可伪造看似正常的对外心跳报文，使外部 Dead-Man's Switch 无法单凭心跳字段发现主机已被植入恶意逻辑。

### 4.2 `S-01` 与离线签名机制的真实定位（仅防御非 `root` 篡改与运维误操作）
- `SignedReleaseManifest`（离线 Ed25519 签名）与 `compute_runtime_code_tree_sha256` 的真实安全边界仅限于：
  - 防御**非特权进程/普通用户权限下**的意外文件损坏、配置漂移、供应链中间人篡改或热更新脚本错误；
  - 绝不声称能够防御已取得同机 `root` 权限的主动攻击者。

### 4.3 当日本云主机 `root` 沦陷时的唯一有效外部残余防线（Out-of-Host Residual Controls）
当单台云服务器 `root` 被攻破时，系统唯一能够限制损失的边界必须位于**日本云主机之外**：
1. **Binance 交易所服务端 API Key 权限硬隔离（Out-of-Host Exchange Boundary）**：
   - 严禁开启提币权限（`Enable Withdrawals = OFF`），即使 `root` 沦陷且 API Key 泄露，攻击者无法通过 API 直接提走资产；
   - 交易对白名单在币安服务端限制为现货 `SUIUSDT`（禁止合约、禁止杠杆借贷、禁止小币种对敲转移资金）；
   - 当前 P0/P1 阶段服务器实行**零交易所私钥部署（Zero-Key Policy）**，主机上不存在任何可被 `root` 窃取的币安凭据。
2. **交易所端托管止损/保护单（Exchange-Hosted OCO / Stop-Loss）**：
   - 已有持仓的下行风险由已挂在币安撮合引擎端的保护单兜底（但须承认：若主机存在实盘 API Key 且 `root` 沦陷，攻击者理论上可调用撤单接口，因此实盘资金规模必须严格受限于账户物理划拨上限）。
3. **账户级物理资金隔离（Sub-Account / Capital Cap）**：
   - 未来若经投资人授权进入小资金实盘，仅在专用交易子账户存放批准的最小试验本金（如 `<= 1000 USDT`），主资产存放于无 API Key 的冷钱包或母账户。
4. **`root` 沦陷后的应急处置铁律**：
   - 一旦怀疑主机遭受 `root` 级入侵，不得信任本机任何日志或恢复脚本；必须立即从外部设备（投资人手机/本地电脑）登录币安后台**一键吊销 API Key 并手工平仓/接管持仓**，随后彻底销毁并从干净镜像重建云主机。

