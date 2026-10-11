# 3I-2：真实 GLM + Tavily 有预算端到端验收（中文设计与操作手册）

**开发基线：** `2c6fd1f`（3I-1 已通过可控外部适配器 + 真 PostgreSQL 全链路）。
**新增源码：** `src/enterprise_research/acceptance/live_harness.py`、`summary.py`。
**对应测试：** `tests/acceptance/test_live_harness.py`、`test_summary.py`。
**本阶段不改变任何生产阶段处理器、没有 Alembic 迁移。**

## 1. 目标及严格边界

3I-1 已证明模型/搜索/抓取层为确定性适配器时，HTTP → Extension → Executor → PostgreSQL → Stage Journal → Evidence/Claim/Receipt → Report API 能够闭环。

3I-2 保留 **实际的** `EnterpriseResearchService.start`、FastAPI Router、`ResearchRunExecutor`、Planning/Searching/Extracting/Verifying/Writing 服务、PostgreSQL Repository、Lease/Fencing 和 7B/7C 完成证明，只将外部适配器从假结果切换成 **真实 GLM/OpenAI-compatible 模型和真实 Tavily Search/Extract 请求**。

真实服务可能因网页失效、模型格式不合规、候选 Claim 被评为 UNSUPPORTED、没有结果而 `FAILED`。不能为提高成功率而降低 3G 引文与 7C 完成门槛；失败是需要记录的验收结果。

**不在本工具内自动重试同一 Run，也不删除运行所产生的数据库业务数据。** 中断后的 Run 必须按现有 Safe Resume 手续检查，不得简单重新运行/重放。

## 2. 显式启用与三重门禁

CLI **默认仅进行离线预检**，不会发起数据库连接或外部 API 请求。真正执行必须同时满足：

1. `--execute`；
2. `--allow-billing`；
3. Shell 环境变量 `ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE=1`。

另外要求：

- `ENTERPRISE_RESEARCH_DB_HOST` 只能是 `127.0.0.1`、`localhost` 或 `::1`；数据库名必须恰好是 `enterprise_research`；当前 Schema **`c8d4a762e901 (head)`**。
- 五个模型阶段（PLANNER、EVIDENCE、CLAIM、VERIFY、SYNTHESIS）各自提供 `BASE_URL`、`API_KEY`、`MODEL`。可以指向同一个已授权 GLM 服务和模型。
- 提供 `TAVILY_API_KEY`。Tavily Search 固定使用官方 `https://api.tavily.com/search`，Extract 固定使用 `https://api.tavily.com/extract`。
- 模型 URL 必须为 HTTPS、不能携带 URL 中的用户名、密码、查询参数。所有允许的 HTTP 终点均按**完整请求 URL + POST 方法精确匹配**；其他外部 HTTP 请求默认拒绝。
- 运行结果目录必须是 Git 工作区以外的绝对路径。

**安全提醒：** Env 中的 Key 仅保留在本地进程中，不写入 JSON 报告，不在聊天里发送。数据库会持久化真实研究问题、网页原文、证据及报告，故只使用专用开发数据库，先备份，并注意研究数据本身的敏感性。

## 3. WSL2 环境检查（不收费）

```bash
cd ~/projects/enterprise-research-platform

git rev-parse --short HEAD               # 必须为 2c6fd1f，应用补丁前工作区干净
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current     # 必须为 c8d4a762e901 (head)
```

**在本地 Shell 加载既有私密配置，不上传 `.env`、API Key、PostgreSQL 密码。** 3I-2 需要下面的变量名（值为你在 GLM/Tavily 控制台获得的真实值）：

```text
ENTERPRISE_RESEARCH_DB_HOST=127.0.0.1
ENTERPRISE_RESEARCH_DB_NAME=enterprise_research
ENTERPRISE_RESEARCH_DB_USER=...
ENTERPRISE_RESEARCH_DB_PASSWORD=...
TAVILY_API_KEY=...
ENTERPRISE_RESEARCH_PLANNER_BASE_URL=...
ENTERPRISE_RESEARCH_PLANNER_API_KEY=...
ENTERPRISE_RESEARCH_PLANNER_MODEL=...
ENTERPRISE_RESEARCH_EVIDENCE_BASE_URL=...
ENTERPRISE_RESEARCH_EVIDENCE_API_KEY=...
ENTERPRISE_RESEARCH_EVIDENCE_MODEL=...
ENTERPRISE_RESEARCH_CLAIM_BASE_URL=...
ENTERPRISE_RESEARCH_CLAIM_API_KEY=...
ENTERPRISE_RESEARCH_CLAIM_MODEL=...
ENTERPRISE_RESEARCH_VERIFY_BASE_URL=...
ENTERPRISE_RESEARCH_VERIFY_API_KEY=...
ENTERPRISE_RESEARCH_VERIFY_MODEL=...
ENTERPRISE_RESEARCH_SYNTHESIS_BASE_URL=...
ENTERPRISE_RESEARCH_SYNTHESIS_API_KEY=...
ENTERPRISE_RESEARCH_SYNTHESIS_MODEL=...
```

这些值必须为**不包含 `/chat/completions` 后缀的兼容接口 base_url**；代码会附加 `/chat/completions`。可以沿用现有已确认可用的配置。

```bash
uv run --no-project python -m enterprise_research.acceptance.live_harness
```

输出 `Local configuration checks passed` 才继续。此命令不做网络连通性测试，因此不代表 API Key 或数据库密码正确。

## 4. 请求数、Token 与时长预算

接受默认预算（可用 CLI 参数进一步缩小）：

| 控制项 | 默认 | 实际含义 |
|---|---:|---|
| `--max-requests` | 16 | 所有外部 HTTP 请求发起数合计；在发出第 17 个前拒绝 |
| `--max-model-requests` | 10 | GLM/OpenAI-compatible 模型 HTTP 请求发起数合计 |
| `--max-tavily-requests` | 6 | Tavily Search + Extract 请求发起数合计 |
| `--max-output-tokens` | 2048 | 在每个模型 JSON 请求中显式设置/收紧 `max_tokens` 字段 |
| `--max-model-request-bytes` | 32000 | 在请求发出前拒绝超过此字节数的模型输入 JSON |
| `--timeout-seconds` | 420 | 单次 Run 轮询的墙钟时间上限；超时取消本进程 Executor |

3I-2 同时将工作负载临时固定为：每个研究问题最多一个搜索结果、Evidence 最多一个文档与一个 2,500 字符 Chunk、最多两个 Evidence candidates、两个 Claim candidates、模型后端全部启用，LangGraph 独立图 Checkpoint 关闭。退出后恢复原环境变量。

**这些是请求级、模型请求体字节数与部分输出级的保护，并非严格的美元价格上限，也不能精确限制每个模型请求的输入 Token 或厂商对同一请求的计费策略。** HTTP 请求已发出但随后超时或失败，供应商仍可能收费；额度/余额/报警阈值务必事先在模型和 Tavily 控制台设置。尽量先使用小额可控凭据，避免高价模型；连续执行不要自动重试。

## 5. 真正执行一次（会产生计费）

```bash
export ENTERPRISE_RESEARCH_LIVE_ACCEPTANCE=1

uv run --no-project python -m enterprise_research.acceptance.live_harness \
  --execute --allow-billing \
  --query '根据微软2024财年年度报告，2024财年总营业收入是多少？请引用报告中的原始依据。' \
  --output-dir /mnt/d/Downloads/enterprise-research-live-3i2 \
  --max-requests 16 \
  --max-model-requests 10 \
  --max-tavily-requests 6 \
  --max-output-tokens 2048 \
  --max-model-request-bytes 32000 \
  --timeout-seconds 420
```

成功并核验数据库产物时 CLI 返回 `0`；Run `FAILED`、超时、预算拦截、报告/引用证明失败则返回 `1`；没有显式授权、DB/Key 配置不安全返回 `2`。**退出码只表示本次验收结果，不代表研究结论语义可靠。**

如果需要保存完整报告（可能含真实网页内容，需保护本地文件）：在命令上添加 `--save-report`。

验收还会通过实际 HTTP SSE endpoint 核查终态 `run.completed` / `run.failed`，只保存是否看到终态和阶段事件个数，不保存事件原始文本。

输出：`run-<UUID>.json`；使用 `--save-report` 时另外生成 `run-<UUID>.md`。不会导出原始 HTTP 请求与响应正文、API Key、数据库密码、原始研究 Query；仅保留 Query SHA-256、Task/Run ID、阶段、机械计数、请求时延与可用 Token 用量。

如任务在受限搜索、证据提取或模型格式校验中失败，先读取 `database.stage_journal`、`database.failure_category` 和 `outbound.per_request` 的 HTTP 状态/耗时；**不要删除失败数据或直接调用不安全恢复**。此外，可使用 Task/Run ID 在本地数据库受控地核对实际来源与证据。

## 6. JSON 的度量定义：机械指标 ≠ 模型质量

- **`question_search_coverage`：** 本 Run 有搜索命中的计划问题数 / 总计划问题数。仅证明该问题检索到了来源，不能证明问题已被正确回答。
- **`retrieval_success_ratio`：** 本 Run 命中来源中已成功提取正文的来源数 / 总命中来源数。
- **`run_evidence_count`：** 3E Evidence Manifest 中的 Run 级 Evidence IDs 数。
- **`candidate_claim_count`：** 3F Generation Receipt 中候选 Claim IDs 数。
- **`verified_supported/unsupported/conflicted`：** 3F 候选 Claim 的最终 3G 验证状态分布。
- **`mechanically_verified_citation_ratio`：** 报告中的 Evidence 引用同时满足：所属 Run Manifest、已支持候选 Claim、数据库 Claim-Evidence 关联、数据库 Evidence 来源及内容指纹和引用 URL 对得上的比例。
- **`report_sha256_valid`：** 报告正文 SHA-256 与持久化报告摘要一致；7C 还在完成事务中做更完整的报告证明。
- **`sse_terminal_observed`：** HTTP SSE 是否发出与 Run 状态一致的终态事件；未捕获到则即使报告可读，也不能让 3I-2 CLI 返回成功。
- **`stage_journal[].duration_seconds`：** PostgreSQL 持久化的阶段开始/结束时间间隔；包含模型/搜索等待，不等同于某个 HTTP 请求的独立耗时。
- **`outbound.per_request[]`：** 仅保存提供商类型、耗时、HTTP 状态和可用 Token/credits；缺失用量绝不按真实 0 解释。

这些指标**不代表**“幻觉率”“语义准确率”“可用生产成功率”，不能据此在简历里写任意未经对照组验证的提升幅度。

### 人工质量验收（在报告之外单独记录）

对每个样例至少抽查全部关键 `SUPPORTED` Claim：

1. **原始来源权威性：** 是否为官方年度报告、监管文件、学术原文等可靠一手材料？来源的发布日期是否匹配问题时间？
2. **引用有效性：** 按 Report URL 定位网页，Evidence 原文能否定位到对应段落？是否发生断章取义？
3. **Claim 支持性：** 证据是否支持完整主张中的主体、数字、时间、条件，而不只是出现相似词？
4. **未解决冲突：** 相反证据是否在 “冲突与限制” 部分准确披露，未被伪装成确证事实？
5. **问题覆盖：** 对照原 ResearchPlan，报告是否回应每个研究问题？

建议人工每条记录 `0=错误 / 1=正确 / NA=无法核查`，显式记录核查数；不要把 NA 算成正确。建议小样本先执行 1–3 个不同、已有官方材料可核查的研究问题，再按相同配置扩展至少 5 次以观察延迟分布。

## 7. 多次运行结果的离线汇总

重复独立验收后（无自动重试、不删除数据库原始试验记录），执行：

```bash
uv run --no-project python -m enterprise_research.acceptance.summary \
  --input-dir /mnt/d/Downloads/enterprise-research-live-3i2 \
  --output /mnt/d/Downloads/enterprise-research-live-3i2/summary.json
```

输出包括 Run 成功率、失败类别、外部请求次数、已观察到的 Token 和 credits、Run 与阶段延迟 P50/P95、完成 Run 的机械搜索覆盖率与机械引用溯源率。**样本数量小于 5 时不报告 P50/P95**，不把单次观测推广为统计结论。

## 8. 本阶段验收顺序与通过门槛

应用补丁（基线 `2c6fd1f`，工作区先干净）：

```bash
PATCH=/mnt/d/Downloads/3I-2-live-provider-acceptance.patch

git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
```

Ruff 与离线回归：

```bash
mapfile -t PY_FILES < <(
  { git diff --name-only -- '*.py'; git ls-files --others --exclude-standard | grep '\.py$'; } | sort -u
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/acceptance -q
uv run --no-project pytest -q
```

预期新增 **21 个离线测试**通过；其他测试的准确计数以 WSL2 实际输出为准。3I-1 PostgreSQL 三条确定性全链路用例建议在此阶段再次回归。**没有新增数据库迁移**，应保持 `c8d4a762e901 (head)`。

真正调用 GLM/Tavily 的用例只在你主动执行 `--execute --allow-billing` 时发生。此环境没有真实 API Key，因此本文档和离线结果不声称 3I-2 已完成真正公网成功验收。

## 9. 安全/工程局限及后续方向

1. 外部请求数约束基于 `httpx.AsyncClient.send` 拦截，匹配目前所有 GLM/Tavily 适配器；如果未来换成 SDK、同步 HTTP 或其他传输层，必须更新门禁并补测试，不得假设旧门禁自动覆盖新调用。
2. Runtime 在**当前 Python 进程内**拦截请求和收集事件；其本身不是可跨进程协调的生产配额服务。超时/崩溃可能留下需要按 3E Recovery Protocol 检查的非终态 Run。
3. Run 质量取决于真实数据：Tavily 可能无法检索合适来源，网页可能无法抓取，LLM 可能输出不合法 JSON 或对支持性做错判断；失败不应通过降低验证阈值掩盖。
4. 3I-2 是**小预算端到端验收工具**，并不是完整自动化评测平台或可代替人工验证的语义裁判。生产部署前需补充采样、人工标注集、供应商账单对账、运行隔离和超时恢复运维。
5. 当前使用本地 PostgreSQL 开发库。绝对不要对生产数据库开启自动验收或运行带写入副作用的实验。
