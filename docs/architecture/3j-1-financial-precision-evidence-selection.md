# 3J-1：精确财务数据检索与 Evidence Selection 增强（离线首版）

**实施目标：** 在上一阶段 `4a53745`（3I-2 已成功完成真实 GLM + Tavily 全链路）的基础上，为 **FY2024 Total revenue** 类精确财务问题提供显式、严格且不增加模型请求数的 Evidence 调度模式。该模式使用完整保存的原始文档，而不是从管理层概述猜测精确数值。

## 1. 工程问题与验收依据

3I-2 的成功报告中，模型引用微软官方年报的管理层概述：`over $245 billion in annual revenue, up 16 percent`，因此结论“超过 2450 亿美元”是受支持的，但缺少报表级精度。微软官方 2024 Annual Report 的 INCOME STATEMENTS 里，`Total revenue` 2024/2023/2022 行为 `245,122 / 211,915 / 198,270`，表头单位是 `(In millions, except per share amounts)`，所以 FY2024 精确值为 **245,122 million USD = 245.122 billion USD = 2,451.22 亿美元**：<https://www.microsoft.com/investor/reports/ar24/index.html>。

之前的真实查询曾只保存微软网页的前 12,000 字符。**仅在存储前缀中重新排序 Chunk，无法找回位于截断之后的财务报表。** 下一次试验需在明示容量限制下允许较完整的网页内容入库；旧 Run 和其 SHA-256 原样保留。

## 2. 新策略的严格边界

- `selection_mode=financial_precision` **显式启用，默认仍为 `uuid`**；既有 `focus` 仍按原词法命中规则运行。
- 首版**仅支持 `Total revenue`**，要求一个唯一的四位目标财年和焦点词 `revenue`（或 `total revenue`）。不声称支持所有财务科目，不自动从中文问题生成英文关键词。
- 先在已落库原文的 `DocumentChunk` 里识别：`Total revenue` 原始行、逗号分组的数值（如 `245,122`）、附近的目标财年（如 `2024`）、附近单位（如 `In millions`）；财务报表标题会提高优先级。匹配只是**词法排序线索**，不负责根据表格列计算、换算或确认事实。
- 只把 **年份 + 原始金额行 + 单位同处一个 Chunk** 的文档列入候选；无合格 Chunk 就在 Evidence 模型调用前 fail-closed，**不会回退为管理层的近似说明来伪装精确答案**。如果成功检索的原文有 `retrieval_truncated=true`，错误会附加“至少一份原文已截断”的诊断（不会输出网页正文）。
- 对已通过完整年份、数值行及单位门禁的候选，可通过 `ENTERPRISE_RESEARCH_EVIDENCE_PREFERRED_DOMAINS` 优先权威来源，优先域名先于次级表格排版分数。使用 HTTPS `hostname` 的 DNS label 边界比较，`evil-microsoft.com` 和 `microsoft.com.evil.example` 都不会命中 `microsoft.com`。这是明确配置的**可信来源优先偏好，不是域名真实性证明**；模型和人工仍需独立核验。
- Evidence LLM 系统提示词补充：精确财务数字优先引用**包含年份/列头、单位及金额行的连续原文**，禁止跨段拼接或擅自换算单位；模型仍可能不遵守，故机械验证必不可少。
- 保留原 Chunk 的 `index/start_offset/end_offset/content`，不拼接窗口、不重写字符偏移。继续执行 `verify_evidence_candidate()` 严格摘录验证、3G Claim Verification、Fencing/Stage Journal、3H Receipt、Artifact SHA-256 和终态证明。
- `max_documents`、`max_chunks_per_document`、`max_total_chunks` 以及 Harness 原请求额度不变。无合格证据时不会额外调用模型。无需数据库迁移。

**技术限制：** 年份位于别的 Chunk、表格 Markdown 被损坏、数字未用千位分隔符、页面未返回财务报表或 Tavily 抽取不完整时可能保守拒绝；这是当前策略的已知范围，不允许手工填入真值来绕过。对于跨边界表格，可以提高原有 `OVERLAP_CHARS` 以取得真实的重叠原文，但重叠窗口也必须保留原始偏移。

## 3. WSL2 应用（基线：干净的 `4a53745`）

下载补丁到 Windows `D:\Downloads`，在 WSL2 **逐行**执行。终端出现意外的 `>` 续行提示时先 `Ctrl+C`。

```bash
cd ~/projects/enterprise-research-platform
git log -1 --oneline
git status --short
PATCH=/mnt/d/Downloads/3J-1-financial-precision-evidence-selection.patch
ls -lh "$PATCH"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
git status --short
```

`git log` 应为 `4a53745`，打补丁前工作区应为空。如果 `git apply --check` 失败，**不要用 `--reject` 或 `--3way` 强制套用**；先提供失败输出与当前 HEAD。

新增、修改均为 Python 调度、适配测试及一份文档。建议只针对变更 Python 文件执行 Ruff，不要一次性格式化历史源码：

```bash
mapfile -t PY_FILES < <({ git diff --name-only -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_financial_precision_selection.py tests/application/test_evidence_workload.py tests/application/test_evidence_extraction_service.py tests/infrastructure/test_tavily_extract.py tests/test_plugin_evidence_runtime.py -q
uv run --no-project pytest tests/acceptance -q
uv run --no-project pytest -q
git diff --check
```

若 Ruff 仅报告排版，可逐个格式化报错的 Python 文件后复查。**不要执行任何 `--execute --allow-billing` 命令**，直到离线和 PostgreSQL 回归通过、核对实际存储的财务表格后另行授权。

### 测试用例包含

1. 财务报表位于完整正文 13K 字符之后，仍在原有 1 Chunk 预算下被选中；管理层的 `over $245 billion` 不获优先。
2. `Total revenue` 不被 `Total cost of revenue` 混淆；错误财年、不含单位、无法找到原文表格时，不能通过严格门禁。
3. 指定域名优先，但 URL query、相似域名、嵌套恶意域名及 HTTP 不冒充官方 HTTPS。
4. 研究服务能将包含单位、财年列头、`Total revenue` 的连续原文提取并保存为准确 `chars:start-end`，只使用 1 个模型调用；源被截断且无法满足精确条件时，模型调用为 0。
5. 模拟 Tavily Extract 验证足够的 `max_content_chars` 会完整保留深层财务表格，且不虚报截断。
6. Extension Runtime 传递 `financial_precision` 与 preferred-domain 配置，保留旧 `focus` 兼容性。

## 4. 仅在离线测试通过后准备配置（目前不要收费实测）

这些都是 Shell 环境变量，**不是一次性真实运行命令**：

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

`450000` 并非绝对最大值或自动优化参数，只是在之前返回约 282K～355K 字符的样本上，预计可以避免 12K 级前缀截断的**实验性容量选择**。增大文档保存量会增加数据库/内存占用；不会自动提高单 Chunk 模型请求预算。Provider 可能返回超过 450K 字符的新网页，仍可能被截断或无法覆盖财务报表。HTTP 仍先接收完整提供商响应；字符上限不是网络流量上限。

**特别注意：** 如果 3I-2 Harness 会重写 Evidence 的 Chunk 大小或重叠参数，请以本机实际生效的设置为准，而不是仅凭这些 `export` 断言成功。`OFFLINE preflight` 只检查配置，不会验证 Tavily 返回了完整财务表格。

## 5. 后续实体验收判定（尚未开展）

未来新 Run 应独立创建 Task/Run，不改写 3I-2 成功报告。建议核对：

- Tavily 保存的微软官方正文长度、`retrieval_truncated`、所选原始 Chunk 中的 `2024`、`Total revenue`、`245,122` 和 `In millions`。**这只是原文可见性检查，不等于 Claim 已经正确。**
- Evidence 的字符偏移能够还原原始行；Claim 明确包括财年、金额、**单位**、主体；3G `SUPPORTED` 且 3H Receipt/Report SHA-256 成立。
- 最终报告用原始证据支持 `FY2024 总营业收入 245,122 百万美元（约 2,451.22 亿美元）`，不把 FY2023 的 211,915 混进 FY2024，不混淆收入与营业利润/收入成本；金额单位换算要独立核验。
- 保留 3I-2 原有单次 **16 总请求 / 10 模型 / 6 Tavily** 上限；费用以服务商实际计费为准。任何失败不自动重试。最后分别验收工程完整性与人工事实正确性，不能把机械一致率当作事实准确率。

## 6. 本地研发验证与风险披露

本补丁在从 `2c6fd1f` 归档叠加既有 Oversize、Evidence Coverage、3H JSON 补丁而重建的离线工作树上开发。原始快照不包含后来新增的 3I-2 Live Harness，所以本地全量回归**不能代替你在真实 `4a53745` 上执行的 578+ 测试和 PostgreSQL 集成测试**。该限制需在交付与代码评审中明确。
