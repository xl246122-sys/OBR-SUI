# OBR-SUI (SUI-Quant V1.0)｜币安现货 SUIUSDT 数学量化交易系统与移动监控终端

[![Python Tests](https://img.shields.io/badge/pytest-78%20passed-brightgreen.svg)](tests/)
[![Android Version](https://img.shields.io/badge/Android%20APK-V1.04-blue.svg)](Release/)
[![Execution Mode](https://img.shields.io/badge/Mode-PAPER%20%2F%20LIVE%20Switch-orange.svg)](src/sui_quant/daemon/live_paper_runtime_server.py)
[![Security Invariant](https://img.shields.io/badge/Zero%20Secret%20Keys-Verified-success.svg)](docs/GO_NO_GO_AUDIT.md)

---

## 1. 项目简介与系统边界

**OBR-SUI** 是一套面向币安现货 `SUIUSDT` 的高可靠事件驱动量化交易系统与原生 Android 监控终端（P0 ～ P8 阶段全量收官）。

- **交易标的**：币安现货 `SUIUSDT`（`BTCUSDT` 仅用作宏观环境与风险参考，绝不下单）。
- **实网行情接入**：实时直连币安主网行情（`SUIUSDT` 与 `BTCUSDT` 2s 高频更新）。
- **执行模式**：
  - **默认状态**：严密锁定在 `PAPER` 实网模拟模式（零资金风险、全链路撮合演练）。
  - **实盘控制**：支持通过 Android 终端自主切换开启 `LIVE` 实盘交易开关（默认关闭 `OFF`）。
- **移动监控端**：基于 Kotlin + Jetpack Compose + Material3 的机构级 4-Tab 暗色终端，提供独立模拟盘与实盘仓位配置、课前盈亏比测算及防重放风控指令。

---

## 2. 核心架构与目录结构

```text
OBR-SUI/
├── src/sui_quant/                  # 量化系统核心 Python 源码
│   ├── contracts.py                # 强类型 Decimal / UTC 不变式契约
│   ├── config.py                   # 系统全局配置与 GoNoGoChecklist 门禁
│   ├── state_machine.py            # D-01 交易状态机定义 (READY, PROTECTED, FROZEN, LOCKDOWN)
│   ├── exchange_rules.py           # 币安 SUIUSDT 现货精度与最小名义额规则
│   ├── daemon/                     # 守护进程与服务
│   │   ├── live_paper_runtime_server.py # 实时行情 + PAPER/LIVE 双工作台 REST 网关
│   │   ├── alert_manager.py        # G-02 分级告警与风暴抑制
│   │   ├── storage.py              # SQLite WAL 状态与审计持久化
│   │   └── heartbeat.py            # F-02 守护进程心跳监控
│   ├── data/                       # 行情接收与清洗 (RingBuffer, SyncEngine, QualityGuard)
│   ├── models/                     # 科学模型 (A-01 市场状态分类, A-02 放量突破检测)
│   ├── execution/                  # 执行引擎 (B-01 限价优化, D-01 状态机, R-01 独立风控)
│   ├── exit/                       # 出场策略 (C-01 止盈止损防超锁协调器)
│   ├── registry/                   # 模型注册表 (E-01 签名模型热晋升与自动回滚)
│   ├── api/                        # G-01 移动网关契约与 Ed25519 签名验证
│   └── audit/                      # P8 全量验收总审计与 GO/NO-GO 门禁校验器
├── android/                        # G-01 原生 Android Kotlin + Jetpack Compose 客户端
│   ├── app/src/main/java/com/suiquant/monitor/
│   │   ├── ui/
│   │   │   ├── MainActivity.kt     # 主入口 (多网关自动故障转移 + 币安直连兜底)
│   │   │   └── MonitorDashboardScreen.kt # 4-Tab 机构级金融终端 UI
│   │   ├── data/AlertDatabase.kt   # Room 本地持久化缓存
│   │   └── push/                   # FCM 接收与 WorkManager 离线补偿
│   ├── build.gradle.kts            # 自动化版本自增与发布构建配置
│   └── version.properties          # 版本状态持久化 (每次打包递增 0.01)
├── Release/                        # 编译就绪的 Android APK 发布产物
│   ├── OBR-SUI V1.04.apk           # 当前最新版本 APK
│   └── version.properties          # 发布版本控制清单
├── scripts/
│   └── run_live_trading_server.py  # 实时行情与网关启动入口
├── tests/                          # 自动化测试套件 (78 项 100% 通过)
├── docs/                           # 完整技术设计、威胁模型、分阶段计划与终审报告
│   ├── SUI-Quant_V1_总体技术设计方案.md
│   ├── GO_NO_GO_AUDIT.md           # P8 终审报告与实盘门禁裁决书
│   └── REQUIREMENTS_TRACEABILITY_MATRIX.md
├── build_apk.ps1                   # 一键自增编译打包脚本
├── pyproject.toml                  # Python 依赖与工程配置
└── PHASE_HANDOFF.md                # 阶段交接与验收档案
```

---

## 3. 移动端 UI 架构与交互设计 (V1.04)

移动端 App 提供 4 个专业工作台：

1. **🧪 模拟盘 (`PAPER`)**：
   - 接入币安实时盘口行情，常驻运行全链路模拟撮合。
   - 内置**模拟盘专属仓位配置面板**（轻量模拟/标准 C1/深度压力三档快选、单笔开仓比例滑块、课前风险盈亏比 `R:R` 实时测算及独立保存同步）。
2. **🔥 实盘交易 (`LIVE`)**：
   - **实盘主控开关（默认关闭 `OFF`）**：拨动开启时弹出带课前风控摘要的二次确认框，由用户自主决定何时激活实盘。
   - 内置**实盘专属仓位配置面板**（保守实盘/标准实盘/进阶实盘三档快选、独立实盘资金上限与开仓参数）。
3. **🛡️ 风控演练**：
   - `D-01` 状态机四态矩阵直观高亮展示。
   - 防重放战术操作台：支持一键触发「冻结新开仓」、「紧急安全锁仓」与「恢复就绪」。
4. **🔔 审计网关**：
   - `G-02` 实时告警分级筛选流（全部/高优紧急/常规审计）。
   - 网关地址一键切换：预置东京云服务器主网、局域网主机与 Android 模拟器地址。

---

## 4. 快速开始与运行指南

### 4.1 运行全套自动化测试

```powershell
python -m pytest -v
# 输出: 78 passed
```

### 4.2 启动实时行情与双模网关服务

```powershell
python scripts/run_live_trading_server.py
```

服务将自动启动：
- 接入币安实时 `SUIUSDT` 与 `BTCUSDT` 盘口行情（2s 高频轮询）。
- 启动本地 HTTP REST API（端口 `8765`）。

### 4.3 一键编译打包 Android APK

```powershell
.\build_apk.ps1
```

每次执行脚本将自动完成：
1. Gradle 依赖校验与 Jetpack Compose 编译。
2. 自动递增版本号 `+0.01` 并同步更新 Android 原生 `versionCode`。
3. 导出命名为 `Release/OBR-SUI V{version}.apk`。

---

## 5. 安全与治理规范 (§1, §12.1, QA-11)

1. **零生产密钥原则**：代码库与移动端 APK 严格保持 **0 交易所私密 API 凭证**。
2. **防重放控制**：所有移动端风控指令（冻结/锁仓）均通过 Ed25519 签名与时间戳随机数防重放校验。
3. **保全在途止损单**：无论是手动冻结还是系统异常熔断，已在途持仓的硬止损保护卖单（`-1%`）强制 100% 保留。
4. **诚实披露审计**：当前系统历史样本外统计裁决保持 `NO_EDGE`，已按严格风控规范实施物理门禁保护。

---

## 6. 开源与版权声明

本项目代码依据总体技术设计方案规范开发完成，供量化交易与系统架构研究使用。
