# 3I-2 Evidence Coverage Hotfix：有界关键词聚焦调度（显式启用）

## 真实故障证据

第三次真实 GLM/Tavily 验收的 SQL 记录：Planning、Searching、Extracting、Verifying 阶段完成，但 1 个 Evidence Chunk 生成 0 条 Evidence，随后 3F/3G 候选数量也为 0，Writing 正确拒绝“无证据报告”。3 篇文档已经成功保存：Scribd 4,166 字符、Microsoft 12,000 字符（前缀截取）、Stocklight 12,000 字符（前缀截取）。

按原调度器的 UUID 顺序，先选 Scribd；但其 `revenue` 位于字符 **3276**（第一份 2500 字符 Chunk 之外），而 Microsoft 的 `revenue` 位于 **1419**（第一份 Chunk 之内），`2024` 位于 **28**。这说明原有的「一文档、一个 Chunk」预算没有把已经持久化的微软片段送给 Evidence 模型。

这只证明 `revenue` 关键词存在，**不证明该附近一定是微软 2024 财年总营业收入的事实段落**；更不能因此认定模型会返回非空 Evidence。

## 修复范围与安全设计

修改 `application/evidence_workload.py`，仅对调度策略提供 opt-in `selection_mode=focus`：

1. **默认仍是 `uuid`**：既有 UUID 排序、原始 Chunk 前缀顺序和 Round-robin 预算不变；所有既有研究行为可保持原样。
2. **显式关键词模式**：只检查已经落库、已经被 `chunk_document()` 按真实字符偏移切分的原文 Chunk，不在调度时下载网页，不调用额外模型、不拼接和改写片段。
3. **确定性排序**：按每个 Chunk 匹配的**不同关键词数**降序；相同分数优先原文中更早的 Chunk，最后按 Source UUID 打破平局。每个来源也按它的最佳 Chunk 排序。完全没有命中时退回原本的 UUID/首 Chunk 选择。
4. **原文定位不变**：选中的 `DocumentChunk` 仍保留原 `index/start_offset/end_offset/content`，继续经过 `verify_evidence_candidate()` 的精确且唯一子串验证，不能将别的文档内容或不存在的摘要当成 Evidence。
5. **模型预算不变**：`max_documents`、`max_chunks_per_document`、`max_total_chunks` 与模型请求门禁未改。在本次 3I-2 的一文档/一 Chunk 配置下，**Evidence 模型仍最多发 1 次请求**。如果 Evidence 变得有效，3F、3G、3H 阶段可能开始发出此前没有发生的请求，整体请求数仍须受原 Harness 总额保护。
6. **不改可信完成契约**：无 Evidence 时 Writing 继续 fail-closed；3G 验证、Fencing Token、Stage Journal、Evidence Manifest、7C/3H 报告完成证明保持原要求。
7. **关键词只是词法线索**：没有语义召回、没有中英术语自动翻译、没有权威性排序；命中 `revenue` 不等于命中财务数据。最终研究事实质量仍需人审。

### 配置

以下变量只用于有界 Evidence 调度，与你的 GLM API Key/Tavily Key 无关：

```bash
export ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE=focus
export ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS='2024,revenue'
```

这次保持验收 Harness 已有的单文档/单 Chunk 限制，无需提高 API 预算。最多 8 个不重复焦点词，每词不超过 64 个字符；无焦点词而开启 `focus` 会在启动阶段拒绝配置。`uuid` 为默认模式，要关闭聚焦可以执行 `unset ENTERPRISE_RESEARCH_EVIDENCE_SELECTION_MODE ENTERPRISE_RESEARCH_EVIDENCE_FOCUS_TERMS`。不要将私人秘密写入焦点词。

**启动配置/离线预检本身不会验证网页 `revenue` 上下文是否正确，也不会测试真实服务是否返回 Evidence。** 每次真实实验前应先核对来源可信性、保存文档的局部内容、账户预算和开发数据库备份。

## 代码和验收

这是在已成功应用 3I-2 Live Harness、Content-Length Hotfix、Tavily Oversize Hotfix 的**未提交**工作区上叠加的增量补丁；不是用来重装这三项补丁。所交付 Patch 基于 `2c6fd1f` 原始代码生成，并额外验证了叠加 Tavily Oversize Patch 的可应用性；最终仍须以你的 WSL2 `git apply --check` 为准。

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3I-2-evidence-coverage-hotfix.patch
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
```

只检查本次及既有**变更文件**，不要将历史未格式化 `src tests` 全目录的问题混入本补丁范围：

```bash
mapfile -t PY_FILES < <({ git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_evidence_workload.py tests/application/test_evidence_extraction_service.py tests/test_plugin_evidence_runtime.py -q
uv run --no-project pytest tests/acceptance tests/infrastructure/test_tavily_extract.py -q
uv run --no-project pytest -q
```

若仅有 I001 或格式问题，针对报错文件执行 `ruff check --fix` 与 `ruff format` 后复测，不要跳过其他测试。不需要 Alembic 迁移；应保持 `c8d4a762e901 (head)`。

### 现有测试覆盖

- `uuid` 默认及所有原有 Round-robin、不超预算等契约不变；
- 依真实 SQL 的“收入关键词位置”合成数据，证明 1 Chunk 预算下能选择微软第一 Chunk；
- 同一文档的相关段落位于后续 Chunk 时，调度不会重写原文绝对字符位置；
- 通过 `ResearchEvidenceExtractionService` 端到端模拟模型返回精确摘录，确保证据 `chars:start-end` 指向原文；
- 无任何词法命中时，保留原 UUID/首 Chunk 确定性选择；
- 错误选择模式、空词、过长词、重复词和过多词被拒绝；
- Extension Runtime 确认 opt-in 环境变量正确传递给 `EvidenceWorkloadSettings`。

## 推荐的下一步

1. 先发 Ruff 与本地全量回归结果。**现在不要立即发起第四次收费研究**。
2. 在本地只读地查看 Microsoft 保存正文 `revenue` 附近是否真的有 2024 财年“总营业收入”的金额和计量单位；可能仅是页眉导航。不要把整篇文档或保密文本上传到公共位置。
3. 若确实相关，再确认服务启动使用的是 `focus` 模式以及 Harness 仍是 1 Chunk 上限；按同一 16/10/6 请求预算自愿开展一次真实研究。新的成功/失败 Run 与前三次实验独立，不修改旧记录。
4. 成功后分别验收“完成契约”和“研究正确性”；特别披露来源前缀截取和第三方内容风险。此阶段没有生产成功率改善的对照数据，不能声称部署后的性能或幻觉率提升。
