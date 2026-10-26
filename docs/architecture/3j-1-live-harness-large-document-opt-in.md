# 3J-1：Live Harness 高精度财务模式的受限正文保存开关

## 根因和修复目标

在第五次真实 3I-2 Run 中，微软官方年报 Extract 返回约 282,600 字符，而持久化的原文只有前 12,000 字符。`live_harness.SMALL_WORKLOAD` 固定 `ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS=12000`，`temporary_workload_env()` 在执行时通过 `env.update(overrides)` 强制覆盖用户 Shell 环境。因此，仅在 Shell 导出 `450000` 不会使真实 Harness 保留更完整的年报。

此修复仅为**显式** `ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE=financial_precision` 启用受限大正文窗口。普通 `uuid` / `focus` 的 3I-2 行为原样保留：仍为 **12,000 字符**、**2,500 字符 Chunk**、**200 字符重叠**，以及 Harness 固有的一文档一 Chunk / 请求预算上限。

## 显式配置与安全约束

必须配置 `ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS`（严格十进制，**12,001～450,000**），不能隐式放开任意大小。`ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS` 在 **256～2,500** 范围内（缺省 2,500）；`ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS` 在 **0～600** 且小于 Chunk 大小（缺省 200）。`ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS` 必须包含唯一目标四位财年及英文 `revenue`，最多 8 个词；检索超长策略必须是 `truncate` 或 `reject`。

非法配置在 **CLI 离线 preflight** 及 `execute_live()` 启动数据库/模型操作前报告，不能借非法数值绕过边界。错误只包含配置键名和允许范围，不回显任何值或凭据。`temporary_workload_env` 仍在退出及异常时还原原 Shell 环境。

同一个 Harness 的 **16 总请求 / 10 模型 / 6 Tavily / 32 KB 模型请求体 / 2048 输出 tokens / 420 秒**门禁、最大 1 Evidence Chunk、Evidence Quote 验证、3G/3H 终态证明保持原样。放大正文保存上限可能增加数据库/内存/解析时间；模型请求限制不等于金额上限。HTTP 提供商正文仍可能比字符上限更大，且不保证包含财务报表。

## WSL2 验收（仅离线）

在已应用 **3J-1 Financial Precision Evidence Selection**、仍未提交 Git 的工作树上，用新增增量补丁应用。先运行：

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3J-1-live-harness-large-document-opt-in.patch
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
uv run --no-project ruff check src/enterprise_research/acceptance/financial_workload.py src/enterprise_research/acceptance/live_harness.py tests/acceptance/test_financial_workload.py tests/acceptance/test_financial_workload_harness.py
uv run --no-project ruff format --check src/enterprise_research/acceptance/financial_workload.py src/enterprise_research/acceptance/live_harness.py tests/acceptance/test_financial_workload.py tests/acceptance/test_financial_workload_harness.py
uv run --no-project pytest tests/acceptance -q
uv run --no-project pytest -q
```

已设置的 Shell 变量可保留，特别是：

```bash
export ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE=financial_precision
export ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS=2024,revenue
export ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS=microsoft.com
export ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS=450000
export ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY=truncate
export ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_SIZE_CHARS=2500
export ENTERPRISE_RESEARCH_EVIDENCE_CHUNK_OVERLAP_CHARS=600
uv run --no-project python -m enterprise_research.acceptance.live_harness
```

离线预检只证明参数合法，**不会联网或证实年报返回内容含 245,122**。要确认覆盖值在 Harness 内实际生效，请先以环境隔离单测为准，不启动 `--execute --allow-billing`；绝不为了让 Run 成功而放松严格 Evidence/Claim/Report 校验。下一轮真实验收另行确认服务商预算与授权后启动。
