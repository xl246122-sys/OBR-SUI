# PHASE_HANDOFF｜阶段交接与验收档案（P6 E-01 签名模型仓库、SHADOW 原子切换与端到端集成闭环完成 + P7 正式计划待审 + 遗留项持续追踪）

## 1. 当前阶段元信息与阶段执行状态
- **项目名称**：`SUI-Quant V1.0`
- **当前已完成阶段**：
  - **P0 + P1 + P2**：基础契约、主机安全与守护、实时 WebSocket 采集与 `Parquet + DuckDB` 同步。
  - **P3（数学代码框架与真实历史审计）**：`Task A.1–A.9` 全部数学正确性补证已完成（详见 [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)）；**60 天币安现货真实历史数据独立样本外统计裁决保持 `NO_EDGE`（严禁宣称策略盈利能力已验证）**。
  - **P4（B-01 智能限价微观执行、D-01 自主决策状态机闭环与 R-01 独立风控最终否决）**：**`IMPLEMENTED_AND_VERIFIED`**（`FI-P4-01 ~ FI-P4-08` 故障注入与 `INV-R1 ~ INV-R8` 风险不变量 100% 通过）。
  - **P5（C-01 三策略退出优化、保护订单防超锁管理与双层公平对照回测）**：**`IMPLEMENTED_AND_VERIFIED`**（`QA-06 / QA-07 / QA-13` 100% 通过，详见 [`docs/P5_PLAN.md`](file:///D:/OBR-SUI/docs/P5_PLAN.md) 与 [`evidence/p5_c01_exit_comparison_evidence.json`](file:///D:/OBR-SUI/evidence/p5_c01_exit_comparison_evidence.json)）。
  - **P6（E-01 签名模型制品仓库、SHADOW -> PAPER 原子切换与自动回滚、已持仓版本锁定与端到端全链路闭环集成）**：**`IMPLEMENTED_AND_VERIFIED`**（全仓测试 `70 passed`，`QA-12` 100% 通过，详见 [`docs/P6_PLAN.md`](file:///D:/OBR-SUI/docs/P6_PLAN.md) 与 [`evidence/p6_e01_integration_and_rollback_evidence.json`](file:///D:/OBR-SUI/evidence/p6_e01_integration_and_rollback_evidence.json)）。
- **下一阶段计划（P7）**：[`docs/P7_PLAN.md`](file:///D:/OBR-SUI/docs/P7_PLAN.md) 已提交，状态 **`READY_FOR_REVIEW`**（未获批准前绝不执行 P7 代码修改）。
- **当前运行模式硬边界**：严格锁定 `ExecutionMode.PAPER` / `ExecutionMode.RESEARCH` / `ExecutionMode.SHADOW`（`LIVE` 物理禁用）。
- **真实订单产生数量**：`0`（NO）
- **生产密钥/真实交易账户密钥数量**：`0`（NO）

---

## 2. P6 核心交付与验收摘要

1. **新增可审查源码与测试文件**：
   - [`src/sui_quant/registry/e01_model_registry.py`](file:///D:/OBR-SUI/src/sui_quant/registry/e01_model_registry.py)（实现 `E01ArtifactPackager`、`SignedModelArtifactBundle`、`E01ModelRegistry` 六层上架门禁与原子切换、`InFlightVersionPinRegistry` 已持仓退出版本锁与 `AutoRollbackGuard` 自动原子回滚守卫）
   - [`src/sui_quant/registry/integrated_pipeline.py`](file:///D:/OBR-SUI/src/sui_quant/registry/integrated_pipeline.py)（实现 `D01IntegratedPipeline`，将 `F-01 -> FeaturePipeline -> A-01 -> E-01 (ACTIVE+SHADOW) -> R-01 -> B-01 -> D-01 -> C-01 (OcoProtectionCoordinator)` 全链路端到端串联）
   - [`src/sui_quant/registry/__init__.py`](file:///D:/OBR-SUI/src/sui_quant/registry/__init__.py)
   - [`tests/test_p6_e01_registry_and_integrated_pipeline.py`](file:///D:/OBR-SUI/tests/test_p6_e01_registry_and_integrated_pipeline.py)（6 项 P6 专项测试，全库 `70 passed`）
2. **`QA-12` 与端到端闭环核心验证结果**（归档于 [`evidence/p6_e01_integration_and_rollback_evidence.json`](file:///D:/OBR-SUI/evidence/p6_e01_integration_and_rollback_evidence.json)）：
   - **六层制品验证门禁**：100% 拦截二进制篡改（`ARTIFACT_SHA256_MISMATCH`）、伪造签名（`INVALID_ED25519_SIGNATURE`）、特征契约漂移（`FEATURES_SCHEMA_MISMATCH`）、不兼容风控版本（`RISK_COMPATIBILITY_MISMATCH`）、校准劣化（`ECE > 0.05` / `Brier > 0.22`）与缺失回滚锚点（`ROLLBACK_REF_NOT_FOUND`）。
   - **零下单 `SHADOW` 旁路与原子晋升**：新模型上架仅允许进入 `SHADOW` 槽位，`shadow_orders_submitted_count == 0`；原子晋升 `SHADOW -> ACTIVE_PAPER` 实测耗时 **`0.2154 ms`**（优于 `<= 5.0 ms` 预算）。
   - **劣化/异常自动原子回滚**：注入模型推理异常、缺失特征、非有限概率（`NaN/Inf`）与推理超时（`> 15.0 ms`）故障，`AutoRollbackGuard` 在 **`0.0992 ms`** 内自动将 `active_paper` 回滚至 `rollback_ref` 并触发 `HIGH` 级告警。
   - **已持仓退出策略版本锁定（零版本串扰）**：持仓在 `C1` 下开仓进入 `PROTECTED` 后，即便在线热切换至绑定 `C2` 的新模型或回滚至绑定 `C3` 的模型，当前在途持仓仍 100% 沿用 `C1` 完成平仓（`in_flight_version_crosstalk_violations == 0`），直至平仓归零 `FLAT -> READY_NO_POSITION` 后的下一笔新开仓才启用新版本。
   - **200 周期连续端到端闭环压测（含 6 笔完整开平仓、盘中热切换与故障注入自动回滚）**：
     - 全链路单周期耗时：**`P50 = 2.9643 ms`、`P95 = 4.8377 ms`、`P99 = 7.2515 ms`**（远优于 `<= 50.0 ms` 目标与 `<= 150.0 ms` 硬上限）；
     - 内存增量（`tracemalloc`）：**`0.2144 MiB`**（峰值 `0.2416 MiB`，远优于 `<= 15.0 MiB` 预算）；
     - `over_reservation_violations = 0`、`naked_position_violations = 0`、`live_orders_sent_count = 0`。

---

## 3. 持续追踪的跨阶段遗留项（未完成项严格保持 `PENDING` / `NO_EDGE`）
1. **P3 & P5 真实历史数据样本外统计有效性**：**`NO_EDGE`**（60 天真实历史数据独立测试集 `FINAL_TEST` 净期望为负，未获正期望统计优势，严禁开启实盘交易）。
2. **`FI-10B` 日本东京生产服务器连续 72 小时真实墙钟运行观测**：**`PENDING`**（后台持续采样中，未满 `72.0` 真实小时前绝不伪报 `PASS`）。
3. **P1 外部独立心跳（Cloudflare Workers 公有云生产部署）与 Android 真机实网通知（`G-01/G-02` FCM 推送）**：**`PENDING`**（P7 将交付只读监控 API、告警引擎与 Android Kotlin+Compose 工程，无真实 Google Firebase 生产凭据时 FCM 实网回执继续保持诚实 `PENDING`）。

---

## 4. 风险清单与回滚方法
- **本地代码回滚**：`git revert HEAD`
- **模型在线回滚**：调用 `E01ModelRegistry.trigger_atomic_rollback(reason=...)` 原子切回 `rollback_ref`（`< 1 ms`）。
- **异常冻结恢复**：任何 `ENTRY_FROZEN` 或 `SECURITY_LOCKDOWN` 状态仅允许既有保护平仓卖单执行，恢复正常状态必须通过 `BOOT -> SYNCING` 完整对账门禁。
