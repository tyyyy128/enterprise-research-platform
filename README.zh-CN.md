# 企业级可信深度研究与知识 Agent 平台

**版本：** v0.1.0 Preview（研究与工程预览版，尚非生产认证产品）

这是一个基于 DeerFlow 生态开发的**独立扩展项目**，关注研究结果的证据来源、可追溯性以及拒绝不合格证据的安全执行机制。

[English README](README.md)

## 核心能力

- **研究证据链**：Source → Document → Chunk → Evidence → Claim → Report。
- **来源年份一致性**：要求 FY2024 原始年报时，不允许用 FY2026 年报中的 FY2024 历史比较列冒充原始来源。
- **Fail-closed**：没有达到门槛的证据时拒绝继续，不把错误研究报告标记为完成。
- **财年感知搜索**：增强搜索问题、排除明确错误版本、优先排序官方来源，未知版本只作为待核实候选。
- **成本控制**：限制检索候选和证据 Chunk，使用离线回放降低重复外部调用。
- **执行与追踪**：研究 Run、阶段日志、SSE 事件以及 PostgreSQL 相关能力。

## 核心模块

| 代码文件 | 中文用途 |
| --- | --- |
| `application/financial_source_discovery.py` | 搜索阶段财年感知、报告版本初筛与排序 |
| `application/search_service.py` | 搜索业务编排、候选筛选与来源记录 |
| `application/financial_evidence_scoring.py` | 财务原始表格、财年列和报告版本校验 |
| `application/evidence_workload.py` | 文档和 Chunk 的准入与预算控制 |
| `application/evidence_extraction_service.py` | 证据抽取及无合格证据时的拒绝边界 |
| `plugin.py` | DeerFlow 扩展服务装配、HTTP API 与运行时组件 |

上表代码位于 `src/enterprise_research/` 路径下。

## 安装与运行边界

- Python 3.12+
- 需要兼容的 DeerFlow Extension Runtime（提供 `deerflow_extension_api`）
- 依赖由 `pyproject.toml` 声明
- PostgreSQL 和外部模型、搜索服务按功能显式启用

当前不声称“公开仓库克隆后即可一键运行”。正式发布安装指南前，仍需验证 DeerFlow 兼容版本、完整依赖解析与全新环境运行。

在已经配置好的原有开发环境中：

```bash
uv run --no-project pytest -q
```

目前本地回归基线：**664 passed，63 skipped**。跳过的主要是需显式启用且具备迁移数据库的集成测试。

## 已验证与未验证

**已验证：** 3J-1B/C/D 对历史持久化文档进行只读、内存隔离的 Fail-closed 回放；3J-2B 固定合成搜索候选的搜索选择与拒绝行为。

**尚未验证：** 真实 Tavily 历史候选是否包含正确原始年报；原始 FY2024 年报的完整正向端到端研究；生产数据库事务原子性；公开代码全新环境安装。

## 后续计划

3J-2C 历史真实搜索候选回放 → 3J-2D 原始年报正向验收 → 独立数据库集成与 GitHub CI → 受控真实供应商实验。

## 发布与许可证

发布前必须完成敏感信息和 Git 历史审计，确认 DeerFlow 上游代码与各第三方组件的许可证义务，再确定本项目 LICENSE。详见 `SECURITY.md`。
