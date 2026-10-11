# 3I-2 真 Tavily Extract 超长正文修复：Opt-in Prefix Truncation

## 故障证据（真实开发库实验）

在 3I-2 的第二次真实研究中，Planning 和 Searching 已完成。两条 HTTP 200 的 Tavily Extract 结果均因 `Tavily Extract content exceeds max_content_chars.` 被适配器判为失败，导致 Extracting 阶段 `Retrieval produced no usable documents.`。这属于本地安全边界，而不是 HTTP 200 能保证的有效提取。

默认 `ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS=1000000`，适配器遇到更长正文原本直接拒绝；Evidence 虽有 Chunk 限制，但取不到持久化文档就无法继续。

## 变更和启用条件

- **默认行为不变**：`ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY=reject`，超限立即失败，既有测试继续有效。
- 仅在显式设置 `ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY=truncate` 后，对超长正文保留**原始正文前** `max_content_chars` 个 Unicode 字符，不拼接、不改写、不补造摘要；若截取后仅为空白，仍拒绝。
- 持久化文档 metadata 中额外记录：`retrieval_truncated=true`、`retrieval_original_content_chars`、`retrieval_stored_content_chars`、`retrieval_truncation_strategy="prefix"`。未截取文档不添加这些字段。
- 后续 SHA-256、Evidence 引用依旧针对**实际保存的**截取片段，不能声称验证了网页剩余的未保留正文；`SUCCEEDED` 只表示该片段可用，**不表示完整页面已处理**。
- 此修复限制的是**持久化正文的字符数**，不是 Tavily 返回的 HTTP 响应体网络字节数；HTTPX 仍需先接收和解析整个 JSON。如果要严格限制网络下载字节数，需要另行实现 streaming response size guard。
- 此策略在覆盖率评测中必须明确统计/披露被截取的网页。仅凭机械引用比对不代表全文覆盖或语义准确。

## 建议本地验收

先对原未提交的 3I-2 工作区应用增量补丁，执行 Ruff 与本文件新增的 4 项测试及原有集成回归。此变更不涉及 PostgreSQL 迁移。

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3I-2-tavily-oversize-hotfix.patch
git apply --check "$PATCH" && git apply "$PATCH"
uv run --no-project ruff check src/enterprise_research/plugin.py src/enterprise_research/infrastructure/retrieval/tavily_extract.py tests/infrastructure/test_tavily_extract.py
uv run --no-project ruff format --check src/enterprise_research/plugin.py src/enterprise_research/infrastructure/retrieval/tavily_extract.py tests/infrastructure/test_tavily_extract.py
uv run --no-project pytest tests/infrastructure/test_tavily_extract.py tests/acceptance -q
uv run --no-project pytest -q
git diff --check
```

无真实 API 调用的环境检查：

```bash
export ENTERPRISE_RESEARCH_RETRIEVAL_OVERSIZE_POLICY=truncate
export ENTERPRISE_RESEARCH_RETRIEVAL_MAX_CONTENT_CHARS=1000000
uv run --no-project python -m enterprise_research.acceptance.live_harness
```

检查旧 Run 保留原样，不对失败 Run 做强制状态修改。只有在测试、开发数据库备份与预算确认完成后，才自愿执行新的、会计费的真实验收。

## 截取结果的脱敏复核

对新 Run 进行只读查询，注意不要输出原始正文：

```sql
SELECT
  split_part(split_part(s.url, '://', 2), '/', 1) AS host,
  d.status,
  LENGTH(d.content) AS stored_chars,
  d."metadata"->>'retrieval_truncated' AS truncated,
  d."metadata"->>'retrieval_original_content_chars' AS original_chars,
  d."metadata"->>'retrieval_stored_content_chars' AS prefix_chars
FROM research_sources s
JOIN research_source_documents d ON d.source_id = s.id
WHERE s.task_id = '<new task uuid>';
```

## 局限性与下一阶段

“保留头部片段”是低风险的技术验收降级方案，不是完整研究质量方案。如果事实位于正文后半段，Evidence 可能完全遗漏关键数字；不应为了让 Run `COMPLETED` 就把此结果解释为全面、正确的研究报告。长期应开发查询相关的正文定位、来源质量排序和**带原文定位的**窗口选择，先改善事实覆盖，再做人工 Claim/引用质量评估。
