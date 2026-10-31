# SUI-Quant V1.0｜币安现货 SUIUSDT 数学量化交易系统

> **当前阶段**：`P0 + P1 + P2 已完成（F-01 实时行情采集、双层扫描与云-本地 Parquet/DuckDB 增量同步已验证；P3_PLAN 待审）`  
> **默认运行模式**：严格锁定为 `PAPER` 或 `RESEARCH`（**禁止默认启用 `LIVE` 实盘**）  
> **唯一交易标的**：币安现货 `SUIUSDT`（仅做向上放量突破/趋势延续；`BTCUSDT` 仅作环境与风险参考，绝不下单）

---

## 1. 核心架构与目录结构

- [`docs/SUI-Quant_V1_总体技术设计方案.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_总体技术设计方案.md)：系统唯一设计与验收基线
- [`docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md)：P0–P8 分阶段开发与监理规范
- [`docs/MASTER_PLAN.md`](file:///D:/OBR-SUI/docs/MASTER_PLAN.md)：总体工程规划（组件图、故障域、状态机、资源预算）
- [`docs/P0_PLAN.md`](file:///D:/OBR-SUI/docs/P0_PLAN.md)：P0 阶段计划与交付清单
- [`docs/API_CAPABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/API_CAPABILITY_MATRIX.md)：币安现货 `SUIUSDT` / `BTCUSDT` 实测精度过滤器与速率限制矩阵
- [`docs/THREAT_MODEL.md`](file:///D:/OBR-SUI/docs/THREAT_MODEL.md)：身份、密钥、主机与执行状态机威胁模型
- [`docs/REQUIREMENTS_TRACEABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/REQUIREMENTS_TRACEABILITY_MATRIX.md)：端到端需求覆盖与 `QA-01 ~ QA-14` 追踪矩阵
- [`docs/adr/`](file:///D:/OBR-SUI/docs/adr)：架构决策记录（ADR-001 至 ADR-004）
- [`src/sui_quant/`](file:///D:/OBR-SUI/src/sui_quant)：核心跨模块数据契约、风控硬边界、状态机、精度过滤器与日志脱敏基座
- [`tests/`](file:///D:/OBR-SUI/tests)：可复现自动化测试套件

## 2. 快速运行测试

```powershell
pytest
```
