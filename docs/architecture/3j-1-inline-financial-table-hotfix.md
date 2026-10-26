# 3J-1：HTML 压平利润表的 Evidence Selection 兼容修复

## 故障复现

微软 FY2024 官方年报的 HTML 被压成单行文本后，包含利润表标题、财年列头、单位和
`Total revenue 245,122 211,915 198,270`，但原评分器要求 `Total revenue` 位于物理行首。
真实诊断为 144 个 Chunk、tier ≥ 3 数量 0、最高 tier 1。这是格式兼容缺口。

## 修复及安全约束

- 仅新增用于 `financial_precision` 的单行利润表评分分支，保留已有 `uuid`、`focus` 和多行识别行为。
- 在**同一原始 Chunk** 中，必须依次出现 `INCOME STATEMENTS`、`In millions` 单位、
  `Year Ended June 30, 2024 2023 2022` 等财年列头，以及 `Total revenue` 后的逗号分组数值。
- 最近的年份列头必须包含目标财年，且金额列数须与年份列数一致；单位必须属于该表格。
- 明确拒绝：FY2024 只在其他叙述中、列头为其他财年、金额数量不匹配、单位缺失、
  行名为 `Total cost of revenue`、不是表格的数值、跨 Chunk 拼接或过远标题。
- 向前窗口限定为 700 个原始字符；不拼接原文、不改变偏移、不增加 Evidence 模型调用。
- 词法门槛通过只表示**优先选择**，并非财年金额的事实证明；引用摘录、3G 验证和人工复核照旧。
- 不涉及数据库迁移、报告 Receipt、Fencing 或 Stage Journal。

## WSL2 应用（当前尚未提交的 3J-1 工作区）

先下载 Patch 到 `D:\Downloads`。每条 Shell 命令单独复制：

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3J-1-flat-income-statement-evidence-hotfix.patch
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
uv run --no-project ruff check src/enterprise_research/application/financial_evidence_scoring.py tests/application/test_financial_precision_selection.py
uv run --no-project ruff format --check src/enterprise_research/application/financial_evidence_scoring.py tests/application/test_financial_precision_selection.py
uv run --no-project pytest tests/application/test_financial_precision_selection.py -q
uv run --no-project pytest -q
```

如果只有格式不符，单独对上述两个文件执行 `ruff format`，然后重新检查。
若 `git apply --check` 失败，不要强制应用，请先反馈失败消息。

重跑之前读取 `/tmp/msft-ar24.html` 的**本地实证评分命令**，预期
`eligible_tier_3_plus > 0` 与 `best_tier >= 3`，具体数量以 WSL2 真实输出为准。
这一步既不调用 GLM/Tavily，也不写入 PostgreSQL。

## 边界

新算法未直接验证 Tavily 返回的正文格式，也不能独立证明原文三列与数字的财务真实性。
真实 Tavily 运行与收费验收仍需在离线全部通过后另行授权。该 Patch 经过重建工作树的
离线功能测试，但 WSL2 的运行数据和已安装依赖才是最终验收依据。
