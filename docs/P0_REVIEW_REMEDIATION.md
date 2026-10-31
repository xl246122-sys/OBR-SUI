# P0_REVIEW_REMEDIATION｜P0 监理审查反馈整改与核验补充报告

> **阶段状态**：`P0 REMEDIATION COMPLETED / P1 PLAN REVISED (READY_FOR_REVIEW)`  
> **编制日期**：2026-10-31｜**执行原则**：严格保持 PLAN/P0 整改边界，**零真实交易、零交易密钥、零远程服务器修改、未擅自进入 P1 IMPLEMENT**。

---

## 1. 任务一逐项整改与核验结果

### 1.1 核心代码与可审查差异证据（Task 1.1）
- 已提供并更新以下全部核心代码与测试文件：
  - [`src/sui_quant/contracts.py`](file:///D:/OBR-SUI/src/sui_quant/contracts.py)：十大不可变跨模块契约，强制 `Decimal`（拦截 `float`）与显式 `UTC` 时间戳。
  - [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py)：不可放宽风控硬边界 `RiskConfig`、`GoNoGoChecklist`、升级为 **Ed25519 非对称数字签名** 的 `LiveAuthorizationToken`、以及防环境变量/字典注入的 `SystemConfig`。
  - [`src/sui_quant/exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py)：区分 `P0_STATIC_SNAPSHOT` 与 `DYNAMIC_EXCHANGE_INFO` 动态刷新门禁，实现手续费资产（`SUI`/`BNB`/`USDT`）、剩余碎股（Dust）、最小名义额与买前可保护性预检（`validate_pre_buy_protectability`）。
  - [`src/sui_quant/state_machine.py`](file:///D:/OBR-SUI/src/sui_quant/state_machine.py)：`D-01` 15 状态机及合法转移守卫。
  - [`src/sui_quant/security/redaction.py`](file:///D:/OBR-SUI/src/sui_quant/security/redaction.py)：日志与字典递归敏感字段脱敏器。
  - [`tests/test_p0_contracts_and_gates.py`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py)：扩充至 12 组高覆盖率边界测试套件。

---

### 1.2 `LiveAuthorizationToken` 签名、防重放、版本绑定与密钥保护重构（Task 1.2）
- **原缺陷根因**：初版 `LiveAuthorizationToken` 仅使用无密钥的 `sha256(payload)` 摘要；任何能够修改配置文件或构造对象的进程均可自行重算 SHA-256，无法防范本地篡改或配置伪造。
- **整改方案（已在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py#L148-L320) 落地）**：
  1. **Ed25519 非对称数字签名（`cryptography.hazmat.primitives.asymmetric.ed25519`）**：
     - 签名私钥（`Ed25519PrivateKey`）**仅由出资投资人在离线可信设备生成并持有**，绝不进入云服务器、本地代码仓、配置文件或环境变量。
     - 云端代码仅读取固定的 32 字节公钥（`pinned_approver_pubkey_hex`，默认 `None` 即锁死实盘）。即使服务器配置文件被完全篡改，攻击者或异常脚本也无法伪造合法的 `ed25519_signature_hex`。
  2. **防重放（Anti-Replay）**：
     - 引入必填唯一次数标识 `token_id`（Nonce，长度 `>= 8`），验证时强制核对持久化已消费集合 `consumed_token_ids`；已消费过的 `token_id` 在重启或重载时再次提交会立即抛出 `LiveAuthorizationError("Replay attack or duplicate token activation blocked")`。
  3. **授权范围与参数防篡改绑定（Scope & Parameter Binding）**：
     - 签名载荷（`canonical_Signing_payload`）强制绑定：`token_id`、`approver_id`、`authorized_symbol="SUIUSDT"`、`max_capital_usdt`、`max_single_trade_risk_pct`、`max_rolling_24h_loss_pct`、`risk_config_version`、**`risk_config_sha256`（由 `RiskConfig.canonical_sha256()` 实时计算的全部 15 个风控参数摘要）** 及 **`git_commit_sha`（经审计的代码提交哈希）**。
     - 只要运行时 `RiskConfig` 的任何一个参数或代码版本与签名时刻不一致，校验立即失败。
  4. **严格有效期窗口（TTL Ceiling）**：
     - 强制要求 `0 < (expires_at_utc - issued_at_utc) <= 7 days`（`MAX_LIVE_TOKEN_TTL`），且 `issued_at_utc <= now_utc < expires_at_utc`。

---

### 1.3 证明 PAPER 默认模式及 LIVE 门禁不可绕过（Task 1.3）
已在 [`SystemConfig`](file:///D:/OBR-SUI/src/sui_quant/config.py#L323-L418) 与 [`test_ed25519_live_token_and_non_bypassable_live_gates_task1_2_1_3_qa14`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py#L401-L512) 中证明以下 9 类异常/攻击场景全部被拦截：
1. 默认无参构造 `SystemConfig()` 严格为 `ExecutionMode.PAPER` 且 `allow_live_trading=False`。
2. 环境变量注入 `SUI_QUANT_MODE=LIVE` 或 `ALLOW_LIVE=true/1`：被 `SystemConfig.from_untrusted_mapping` 显式拦截并抛出 `LiveAuthorizationError`。
3. 弱类型配置注入（如 `allow_live_trading="true"` 字符串或 `GoNoGoChecklist` 传入非 `bool` 值）：被严格类型守卫 `isinstance(..., bool)` 拦截。
4. 冻结对象运行时原地修改（如 `risk.allow_leverage = True`）：因 `@dataclass(frozen=True)` 立即抛出 `FrozenInstanceError`。
5. 签名后篡改 `RiskConfig` 任意字段：导致 `risk.canonical_sha256() != token.risk_config_sha256`，被拦截。
6. 代码版本漂移（`audited_git_commit_sha != token.git_commit_sha`）：被拦截。
7. 伪造私钥签名（攻击者自生成 Ed25519 密钥对签名）：因与 `pinned_approver_pubkey_hex` 不匹配抛出 `InvalidSignature -> LiveAuthorizationError`。
8. 进程重启后重放旧 `LiveAuthorizationToken`：因 `token_id` 存在于持久化 `consumed_token_ids` 中被拦截。
9. 令牌过期或有效期超长（`TTL > 7 days`）：被拦截。

---

### 1.4 版本库文件与历史脱敏扫描证据（Task 1.4）
- **`git ls-files` 清单**：仅包含 28 个受控源码、文档与配置文件；`ServerStation-account.txt`、`desktop.ini`、`*.key`、`.env*` 均不在版本库中。
- **Git 历史与全文敏感词扫描**：遍历全部分支提交历史及所有受控文件，已将文档中出现的云主机公网 IP 统一脱敏为 `<JAPAN_CLOUD_IP_REDACTED>`，密码、私钥、API Key 命中数为 **0**。

---

### 1.5 步长、手续费资产、剩余 Dust、最小名义额与可售数量不足边界测试（Task 1.5）
在 [`src/sui_quant/exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py#L223-L353) 中新增了 [`compute_post_fill_sellable_position`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py#L223-L307) 与买前预检函数 [`validate_pre_buy_protectability`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py#L310-L353)，并通过测试验证了 6 类核心边界：
1. **以 `SUI` 扣取买单手续费**：买入 `10.0 SUI`（均价 `2.0000`），扣 `0.0100 SUI` 手续费后净持仓 `9.9900 SUI`，按 `step_size=0.1` 向下取整后可卖出保护数量 `sellable_qty = 9.9 SUI`，产生残余碎股 `dust_qty = 0.0900 SUI`，止损名义额 `9.9 * 1.9800 = 19.602 USDT >= 5.0 USDT`（`is_protectable = True`）。
2. **以 `BNB` 或 `USDT` 扣取手续费**：净基础资产保持 `10.0 SUI` 完整，`sellable_qty = 10.0 SUI`，`dust_qty = 0.0 SUI`。
3. **极小部分成交导致可售数量低于最小步长（`SELLABLE_QTY_BELOW_MIN_QTY`）**：买单仅部分成交 `0.1 SUI` 且扣取 `0.0001 SUI` 手续费后，净持仓 `0.0999 SUI`，向下取整 `sellable_qty = 0.0 < min_qty (0.1)`，准确识别 `is_protectable = False`（触发 `PROTECTION_DEGRADED` 处理分支）。
4. **临界名义金额陷阱（买单满足 `>=5.0 USDT`，但扣费+跌 1% 后止损单跌破 `min_notional=5.0 USDT`）**：以 `$2.0100` 买入 `2.5 SUI`（买入名义额 `$5.025 >= $5.0`），扣 `0.1%` SUI 手续费后净得 `2.4975 SUI`，向下取整仅能卖 `2.4 SUI`；在 `-1%` 止损触发价 `$1.9899` 下，止损单名义金额为 `2.4 * 1.9899 = $4.77576 < $5.0`，币安将拒单！`compute_post_fill_sellable_position` 准确返回 `is_protectable=False, unprotectable_reason="STOP_NOTIONAL_BELOW_MIN_NOTIONAL"`。
5. **买前可保护性门禁（`validate_pre_buy_protectability`）**：在发买单前，按最坏情况（假设无 BNB 抵扣、全额扣 SUI 手续费且跌 1% 触发止损外加 5% 缓冲垫）预检，直接拦截上述 `2.5 SUI @ $2.01` 的危险买单，而放行 `3.0 SUI @ $2.01`（止损名义额 `$5.77071 >= $5.25` 安全线）。

---

### 1.6 区分 P0 静态快照与运行时动态 `exchangeInfo` 核验（Task 1.6）
在 [`src/sui_quant/exchange_rules.py`](file:///D:/OBR-SUI/src/sui_quant/exchange_rules.py#L31-L162) 中新增 `FilterSourceType`：
- `P0_SNAPSHOT_SUIUSDT_FILTERS` 显式标记为 `source = FilterSourceType.P0_STATIC_SNAPSHOT`。
- 当运行时下单校验开启 `require_dynamic_freshness=True` 时，若仍使用 `P0_STATIC_SNAPSHOT` 或动态缓存年龄超过 `max_age_seconds`（默认 `3600s`），`assert_fresh_for_execution` 立即抛出 `StaleExchangeFilterError`，强制要求系统在 `BOOT -> SYNCING` 及周期刷新中调用 `SymbolFilters.from_binance_exchange_info_symbol(...)` 获取实时规则。

---

## 2. 任务 A.5 专项补强：P0 授权信任边界深度加固证据

针对监理在 `任务A` 第 5 项指出的三项信任边界隐患（① 可信公钥不能被不可信配置替换、② 重放令牌持久化防护、③ 可信构建绑定生效），已在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py) 与 [`tests/test_p0_contracts_and_gates.py`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py) 完成如下代码级闭环加固：

### 2.1 可信审批公钥不可被外部配置或环境变量替换
- **代码常量锚定**：在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py#L57-L70) 中定义代码级只读信任根常量 `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX: str | None = None` 及禁止外部注入字段黑名单：
  ```python
  FORBIDDEN_UNTRUSTED_CONFIG_KEYS = frozenset(
      {
          "pinned_approver_pubkey_hex",
          "trusted_approver_pubkey_hex",
          "consumed_token_ids",
          "audited_git_commit_sha",
          "runtime_code_tree_sha256",
          "nonce_registry",
      }
  )
  ```
- **强阻断逻辑**：在 [`SystemConfig.from_untrusted_mapping`](file:///D:/OBR-SUI/src/sui_quant/config.py#L536-L615) 中，若外部配置文件字典 `raw_config` 包含上述任意字段，或环境变量 `env_vars` 包含 `*PUBKEY*` / `*TRUST_ROOT*` / `*COMMIT_SHA*`，立即抛出 `LiveAuthorizationError("Security violation: Untrusted config cannot supply or override trust-root keys")`，彻底阻断“攻击者通过修改配置文件一并替换 `pinned_approver_pubkey_hex` 与 `live_auth_token`”的信任边界绕过路径。

### 2.2 重放令牌跨重启磁盘持久化防护（`PersistentNonceRegistry`）
- **SQLite WAL 原子持久化**：在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py#L91-L173) 实现 [`PersistentNonceRegistry`](file:///D:/OBR-SUI/src/sui_quant/config.py#L91)，使用本地 SQLite WAL 表 `consumed_live_tokens(token_id PRIMARY KEY, approver_id, code_tree_sha256, consumed_at_utc)`。
- **事务级防重放**：当 `LiveAuthorizationToken.validate()` 验签通过时，通过 `BEGIN IMMEDIATE` 独占写事务调用 `nonce_registry.consume_atomically(...)` 将 `token_id` 落盘；当进程重启、内存 `consumed_token_ids=()` 清空后，新进程实例重新挂载同一 SQLite 文件，任何旧令牌重放均被 `nonce_registry.is_consumed(token_id)` 拦截并抛出 `LiveAuthorizationError("Persistent anti-replay block")`。

### 2.3 运行时真实代码树哈希绑定（`compute_runtime_code_tree_sha256`）
- **实时源码树扫描**：在 [`src/sui_quant/config.py`](file:///D:/OBR-SUI/src/sui_quant/config.py#L73-L88) 实现 [`compute_runtime_code_tree_sha256`](file:///D:/OBR-SUI/src/sui_quant/config.py#L73)，不再信任配置文件中声称的 `audited_git_commit_sha` 字符串，而是在 `verify_live_gate()` 执行时刻直接遍历磁盘上 `src/sui_quant/**/*.py` 全部源码文件计算确定性聚合 SHA-256，并与 Ed25519 签名令牌中的 `git_commit_sha`（构建摘要）比对。磁盘上任意 `.py` 文件改动 1 个字符均立即触发 `LiveAuthorizationError("Runtime build binding failed")`。
- **单元测试验证**：见 [`test_task_a5_trust_boundary_hardening_pubkey_persistence_and_code_tree`](file:///D:/OBR-SUI/tests/test_p0_contracts_and_gates.py#L644-L742)，已在 Windows 本地与日本 Ubuntu 服务器双端 `pytest` 通过。

