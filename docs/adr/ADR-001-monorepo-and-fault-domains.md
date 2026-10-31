# ADR-001：单仓架构（Monorepo）与端-云-移动端物理故障域隔离

- **状态**：Accepted (P0 冻结)
- **日期**：2026-10-31

## 背景
系统由三端构成：本地 Windows 11 研究工作站（i5-13400F / 16GB RAM / RTX 5060）、日本 Ubuntu 云主机（2 vCPU / 2GB RAM / 49GB SSD）与 Android 原生监控端（Kotlin + Jetpack Compose）。若将三端拆分为多个独立仓库，极易出现数据契约版本漂移、特征定义不一致或风控配置脱节；同时日本云主机仅有 2GB 内存，绝不能承担离线训练或重型中间件。

## 决策
1. **采用单一代码仓库（Monorepo）**：统一维护跨模块数据契约（`src/sui_quant/contracts.py`）、风控边界（`src/sui_quant/config.py`）、云端执行服务、本地研究回测脚本及 `android/` 原生工程。
2. **严格物理职责与资源隔离**：
   - **云端（2vCPU / 2GB RAM）**：仅部署轻量级 Python 异步进程 + `SQLite WAL` 账本 + 固定容量环形内存缓存；严禁安装 PyTorch/CUDA、Kafka、Kubernetes 或存储全量历史盘口。
   - **本地研究端（Windows 11）**：使用 `Parquet + DuckDB` 存储历史行情与盘口快照，承担全部 A-01/A-02/B-01/C-01 训练、校准与回测，通过带 SHA-256 校验的增量同步与云端交互。
   - **故障域补充**：鉴于单台云主机内“独立风控进程（R-01）”仍共享同一宿主机内核与电源故障域，强制要求：① 持仓止损优先采用币安交易所端原生保护单（OCO / `STOP_LOSS_LIMIT`）；② 部署宿主机外部的独立存活探测（Dead-Man's Switch）。
