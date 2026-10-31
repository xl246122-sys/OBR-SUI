# SUI-Quant V1.0｜币安现货 SUIUSDT 数学量化交易系统

> **当前阶段**：`P0 + P1 + P2 + P3（数学代码框架补证完毕，历史统计裁决 NO_EDGE）+ P4（B-01/D-01/R-01 PAPER 闭环已验证）+ P5（C-01 三策略退出优化、防超锁协调器与双层公平回测已验证；P6_PLAN 待审）`  
> **默认运行模式**：严格锁定为 `PAPER` 或 `RESEARCH`（**禁止默认启用 `LIVE` 实盘；真实订单数 = 0**）  
> **唯一交易标的**：币安现货 `SUIUSDT`（仅做向上放量突破/趋势延续；`BTCUSDT` 仅作环境与风险参考，绝不下单）

---

## 1. 核心架构与目录结构

- [`docs/SUI-Quant_V1_总体技术设计方案.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_总体技术设计方案.md)：系统唯一设计与验收基线
- [`docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md`](file:///D:/OBR-SUI/docs/SUI-Quant_V1_Plan模式分阶段开发Prompts.md)：P0–P8 分阶段开发与监理规范
- [`docs/MASTER_PLAN.md`](file:///D:/OBR-SUI/docs/MASTER_PLAN.md)：总体工程规划（组件图、故障域、状态机、资源预算）
- [`docs/P0_PLAN.md`](file:///D:/OBR-SUI/docs/P0_PLAN.md) ～ [`docs/P5_PLAN.md`](file:///D:/OBR-SUI/docs/P5_PLAN.md)：P0–P5 阶段实施与验收文档
- [`docs/P6_PLAN.md`](file:///D:/OBR-SUI/docs/P6_PLAN.md)：P6 阶段正式计划（`READY_FOR_REVIEW`）
- [`docs/P3_MATH_AUDIT_REPORT.md`](file:///D:/OBR-SUI/docs/P3_MATH_AUDIT_REPORT.md)：P3 数学正确性补证与 60 天真实历史数据样本外审计报告（统计结论：`NO_EDGE`）
- [`docs/API_CAPABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/API_CAPABILITY_MATRIX.md)：币安现货 `SUIUSDT` / `BTCUSDT` 实测精度过滤器与速率限制矩阵
- [`docs/THREAT_MODEL.md`](file:///D:/OBR-SUI/docs/THREAT_MODEL.md)：身份、密钥、主机与执行状态机威胁模型
- [`docs/REQUIREMENTS_TRACEABILITY_MATRIX.md`](file:///D:/OBR-SUI/docs/REQUIREMENTS_TRACEABILITY_MATRIX.md)：端到端需求覆盖与 `QA-01 ~ QA-14` 追踪矩阵
- [`src/sui_quant/`](file:///D:/OBR-SUI/src/sui_quant)：核心契约、守护进程（`daemon/`）、数据采集（`data/`）、科学建模（`models/`）、模拟执行（`execution/`）与退出回测（`exit/`）
- [`tests/`](file:///D:/OBR-SUI/tests)：可复现自动化测试套件（全量 `64 passed`）

## 2. 快速运行测试

```powershell
python -m pytest -v
```
