# 3E-6D-7A — Research Stage Completion Contract / 禁止阶段空跑

> **基线**：`7a4b8e4`（实现代码从此前上传的 `1cfa666` 源码归档读取；5C-3 的 `7a4b8e4` 只新增 benchmarking 模块与测试，无核心执行文件改动）。正式应用前以 `git apply --check` 验证。
>
> **目的**：阻止“Handler 缺失 / disabled / Writing 未实现，却被 Stage Journal 标为 COMPLETED 并推进到 Run COMPLETED”的假成功。不是自动 Report Writer，也不是完整产物证明协议。

## 一、真实风险与根因

`ResearchRunExecutor._execute()` 将 `run_stage()` 正常返回解释为本阶段可完成，并调用 `finish_stage(..., COMPLETED)`。旧 `ResearchPipelineStageRunner` 仅在 Searching / Extracting / Verifying Handler 存在时调用服务，不存在时从函数末尾自然返回；Writing 完全没有分支。`SkeletonResearchStageRunner` 以前也会 `asyncio.sleep(0)` 后正常返回。

因此一个“只配置 Planner”或“什么 Provider 都未配置”的运行可以没有搜索、没有抓取、没有证据、没有报告，却被标为 `COMPLETED`，SSE 甚至可能发出 `run.completed`。

## 二、本次修复的完成判定

| 阶段 | 7A 最低可执行条件 | 未满足时 | 此阶段尚未证明的内容 |
|---|---|---|---|
| Planning | 使用真实 Planner Service；禁用配置下 Skeleton 明确拒绝执行 | Journal FAILED / Run FAILED | 还需 7B 校验持久化 Plan 的唯一性与完整性 |
| Searching | 有 Search Service；**计划中每个问题必须至少找到一个 Source**；一次搜索只能在全部问题有来源时持久化 | Journal FAILED / Run FAILED | 还需 7B 进行数据库侧持久化来源证明 |
| Extracting | 有 Retrieval Service；原有业务服务已要求至少一个可用文档 | Journal FAILED / Run FAILED | 还需 7B 构造文档覆盖与 Run 范围证明 |
| Verifying | 有 Evidence Extraction Service；原有服务要求存在有效文档 | Journal FAILED / Run FAILED | Evidence 可合法提取为 0；仍缺 7B 的 Run 级完成清单 |
| Writing | **当前没有 Report Writer，实现前绝不允许成功** | Journal FAILED / Run FAILED | 待 7C 增加 Report Artifact 完成证明与持久化 Writer |

这里的 **Run FAILED 是应用执行失败的诊断状态**，不是自动重试。旧 `ResearchSafeResumeInspector` 不会重放 FAILED Run；修复配置后应创建新的 Run 或设计明确的受控重试迁移，不要绕过 Lease/Journaling。

## 三、事务 / Checkpoint 行为

- 出错发生在 StageRunner 内部，原有 Executor 会记录 `finish_stage(..., FAILED, error=...)`，然后在持有 Lease 的条件下将 Run 标为 `FAILED`，并通过 SSE 发出 `run.failed`。
- 不改变 PostgreSQL Schema、Lease、Fencing Token、Checkpoint ThreadID 规则；所有既有事务防护继续生效。
- 如果使用 `ResearchLangGraphStageRunner`，delegate 的异常自然通过 StageGraph 传播；Graph 节点完成与业务完成仍不是同一事务。
- Search 的全部问题来源检查在保存 Aggregate 之前执行，避免本次 Run 的部分搜索结果被当作成功提交；若外部搜索 API 已发出，不能把它视为可回滚的事务。
- 本次没有引入生产配置中的“允许未实现阶段空跑”的绕过开关。

## 四、文件职责（中文）

| 文件 | 修改作用 |
|---|---|
| `src/enterprise_research/application/pipeline.py` | **阶段分发保护**：在服务缺失和 Writing 未实现时抛出显式错误 |
| `src/enterprise_research/application/executor.py` | **Skeleton 空实现保护**：禁用 Planner 时不再伪造成功 |
| `src/enterprise_research/application/search_service.py` | **研究问题覆盖完整性**：有问题无来源则阻止持久化该批搜索结果 |
| `src/enterprise_research/application/errors.py` | **明确异常语义**：新增 `ResearchStageUnavailableError` |
| `src/enterprise_research/application/__init__.py` | **公开应用错误类型**，方便调用方及测试引用 |
| `tests/application/test_stage_completion_contract.py` | **应用层反例**：多种缺失 Handler、Skeleton、WRITING / 终态误调度 |
| `tests/application/test_pipeline*.py` | **行为契约更新**：历史“禁用 = 允许 no-op”用例改为必须抛错 |
| `tests/application/test_search_service.py` | **空搜索 / 部分问题空结果**：禁止提交部分新来源 |
| `tests/api/test_runs.py`, `tests/api/test_sse_run_events.py` | **API / SSE 不再假报成功**：禁用运行产生 failed 状态和事件 |
| `tests/integration/test_sse_pipeline_integration.py` | **实时 SSE 验证**：用显式测试替身执行到 VERIFYING，确认 WRITING 不能假成功 |
| `tests/infrastructure/test_postgres_stage_completion_contract.py` | **真实数据库烟雾测试**：Planning 成功、缺失 Searching 时 Journal/Run 正确失败 |

## 五、验收流程

先在项目根目录确认 HEAD 为 `7a4b8e4` 且工作区干净，应用补丁：

```bash
PATCH='/mnt/d/Downloads/3E-6D-7A-fail-closed-stage-dispatch.patch'
git apply --check "$PATCH" && git apply "$PATCH"
```

然后在 WSL2 的完整项目环境检查改动过的 Python 文件的 Ruff、默认 pytest：

```bash
PY_FILES=(
  src/enterprise_research/application/__init__.py
  src/enterprise_research/application/errors.py
  src/enterprise_research/application/executor.py
  src/enterprise_research/application/pipeline.py
  src/enterprise_research/application/search_service.py
  tests/application/test_stage_completion_contract.py
  tests/application/test_pipeline.py
  tests/application/test_pipeline_search.py
  tests/application/test_pipeline_retrieval.py
  tests/application/test_pipeline_evidence.py
  tests/application/test_search_service.py
  tests/application/test_safe_resume.py
  tests/api/test_runs.py
  tests/api/test_sse_run_events.py
  tests/integration/test_sse_pipeline_integration.py
  tests/infrastructure/test_postgres_stage_completion_contract.py
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/application/test_stage_completion_contract.py -q
uv run --no-project pytest -q
```

如仅格式错误，先对以上改动文件执行 `ruff format`，不要绕过现有 Git Hook。

**仅在确定连接的是本地 `enterprise_research` 开发库时**，运行真实数据库测试（独立随机 Task/Run，测试后定向删除）：

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_STAGE_CONTRACT_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_stage_completion_contract.py -v -rs
```

目标：`1 passed, 0 skipped`。如失败保留完整 traceback、暂不提交；任何修改后的生产行为都需要重新执行 5A、5B 的适当回归。

## 六、尚未完成、不能声称已经实现的能力

1. **7B：Artifact Completion Proof**：为 Planning、Searching、Extracting、Verifying 建立独立于 StageRunner 正常返回的、Run 范围内可持久化验证的完成凭据；明确哪些零结果是合法空集（例如找不到可支持的 Evidence），哪些代表阶段未执行。
2. **7C：Writing / Terminal Contract**：增加真实 Writer 和 run-scoped Report Artifact，只有数据库确认最终报告已持久化且关联的 Claim/Evidence 满足策略时，才允许 `WRITING → COMPLETED`。
3. 当前没有提供重新打开 `FAILED` Run 的协议；补上正式失败重试必须同时保持 Lease/Fencing、Stage Journal 和副作用幂等性。
4. 当前这份补丁不改变已验收的 5C Benchmark 模型负载与分母；如果后续替换模型 Runner，应重建新的版本基线并重新测量。
