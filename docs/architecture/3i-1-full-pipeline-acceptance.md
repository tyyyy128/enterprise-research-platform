# 3I-1 完整研究流水线集成验收：真实 PostgreSQL + 可控外部输入

> 基线：`a90e676`（3H Report Synthesis 已提交）；本阶段**不引入新迁移**，数据库 Head 仍为 `c8d4a762e901`。测试通过前不要提交 3I-1。

## 1. 验收目标与边界

本阶段验证的是**完整软件执行路径**，而不是使用真实 GLM/Tavily 的研究质量评测。生产实现保持不变的部分包括：

1. `EnterpriseResearchService.start()` 及其依赖注入、`build_router()`、真实 HTTP/ASGI 路由；
2. `ResearchRunExecutor` 六阶段调度、Lease/Fencing、Stage Journal、`require_artifact_proof=True`；
3. PostgreSQL 中的 ResearchPlan、ResearchSource/Document、Evidence/Manifest、3F Generation Receipt、3G Verification Receipt、3H Synthesis Receipt、7C Report Artifact 和终态写入；
4. Run 状态 HTTP API、仅在完成后可读的 Report API、进程内 SSE 历史。

仅将七个**外部调用边界**替换为可控函数：Planner、Search、Tavily Extract、Evidence Extractor、3F Claim Generator、3G Claim Verifier、3H Outline Planner。不会调用真实网络、不会读取/消耗 GLM/Tavily API Key（测试对这些服务设置的是 `no-network.invalid` URL 和测试占位凭据）。

> 这与完全 Mock 整条 Pipeline 不同：StageRunner、Executor、Repository、同事务完成证明和最终报告门禁都是真实产品代码。

## 2. 发现并修复的集成前置条件

原 `plugin.py::_build_postgres_repositories()` 的数据库预检是一份维护不完整的硬编码表清单，并未覆盖 3G `research_run_claim_verifications` 和 3H `research_run_report_syntheses`。旧数据库可能在 Extension 启动时被错误接受，随后在 VERIFYING/WRITING 才报 `relation does not exist`。

3I-1 将预检改成**遍历 `Base.metadata.tables`**，通过参数化 `SELECT to_regclass(:table_name)` 核对所有映射表。若任一缺失，**启动立即失败**并销毁引擎，不接受新任务。新增离线负例覆盖缺失 3G 和 3H 表。

这项预检验证的是表的存在，不是完整列/索引/约束校验；Alembic 仍以 `current/head` 为正式版本判据，不能仅依赖预检替代迁移验收。

## 3. 测试矩阵

| 场景 | 真实链路 | 预期结论 |
|---|---|---|
| 成功：一个研究问题，一份搜索来源，一篇提取文档，一条 Evidence/Claim | HTTP Create Task/Run → 5 个 Stage → Run COMPLETED → GET Report + SSE | Plan/Question、Source/Document、Evidence/Manifest、Claim/引用、3F/3G/3H Receipt、7C Report 各 1；5 个 Journal 均 COMPLETED；正文 SHA-256 与 API 一致 |
| 无 SUPPORTED Claim（3G 判定 UNSUPPORTED） | 通过 VERIFYING 后进入 WRITING | Run FAILED；WRITING Journal FAILED；Report API 409；无 Report Artifact/Synthesis Receipt；不能把 UNSUPPORTED Claim 当作报告引用 |
| 搜索返回空结果 | PLANNING → SEARCHING | Run FAILED；SEARCHING Journal FAILED；不进行抓取/模型生成；Report API 409；无后续业务凭据 |
| Schema 预检缺少 3G 表 | 构造异步连接响应，仅模拟 `to_regclass` | 启动前拒绝；数据库引擎正确释放 |
| Schema 预检缺少 3H 表 | 同上 | 启动前拒绝；数据库引擎正确释放 |

成功场景额外检查模型调用顺序（七个外部边界每项调用一次）、统一 Fencing Token、Candidate 与 Evidence/Claim 引用归属、报告指纹和 `run.completed` SSE 事件。

测试用例按任务 UUID 清理自己的数据，使用 PostgreSQL 外键级联；不会清空或重置已有测试数据。

## 4. WSL2 验收步骤

```bash
cd ~/projects/enterprise-research-platform

git rev-parse --short HEAD  # 应为 a90e676
git status --short         # 应当为空

PATCH='/mnt/d/Downloads/3I-1-full-pipeline-acceptance.patch'
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
```

### 4.1 Python 代码检查

```bash
mapfile -t PY_FILES < <(
  {
    git diff --name-only -- '*.py'
    git ls-files --others --exclude-standard | grep '\.py$'
  } | sort -u
)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
```

如只出现 import 格式问题，请对报错的具体文件运行 `ruff check --fix` 和 `ruff format`，随后重新检查，并确保 `git diff --check` 仍然通过。

### 4.2 无数据库测试（应 2 passed）

```bash
uv run --no-project pytest tests/integration/test_database_schema_gate.py -v -rs
```

### 4.3 数据库准备（不要上传凭据）

- 仅允许在本地开发数据库 `enterprise_research` 操作，Host 限制为 `localhost`、`127.0.0.1` 或 `::1`。
- 事先备份开发库，确保环境中的 `ENTERPRISE_RESEARCH_DB_*` 已加载；绝不把密码贴到聊天中。
- 测试不会迁移或删除 Schema；确认版本：

```bash
pg_isready -h 127.0.0.1 -p 5432
uv run --no-project alembic current
# 预期 c8d4a762e901 (head)
```

如果未到这一版本，先核对数据库目标和备份，再按已知迁移程序升级。**3I-1 本身不增加迁移文件。**

### 4.4 PostgreSQL 真实全链路（预期 3 passed，0 skipped）

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_E2E_TESTS=1 \
uv run --no-project pytest \
  tests/integration/test_postgres_full_research_pipeline.py \
  -v -rs
```

验收必须确认成功/失败/搜索空结果三条路径分别被执行，且没有 skipped。该测试**无需真正设置 GLM/Tavily 凭据**；由测试注入临时无效凭据，只复用已有真实数据库连接变量。

### 4.5 全量及历史关键回归

```bash
uv run --no-project pytest -q

ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 \
ENTERPRISE_RESEARCH_SYNTHESIS_TESTS=1 \
ENTERPRISE_RESEARCH_VERIFY_CLAIMS_TESTS=1 \
ENTERPRISE_RESEARCH_TERMINAL_PROOF_TESTS=1 \
uv run --no-project pytest \
  tests/infrastructure/test_postgres_report_synthesis.py \
  tests/infrastructure/test_postgres_claim_verification.py \
  tests/infrastructure/test_postgres_terminal_report_contract.py \
  -v -rs

git diff --check
git status --short
```

默认 pytest 中的 skipped 不计为通过；3I-1 真库用例必须开启双开关单独验收。旧有阶段回归出现错误时不要改低安全门禁，应逐个检查字段/迁移/错误断言。

## 5. 可能遇到的失败与排查方式

| 现象 | 排查方向 |
|---|---|
| Startup 检查 `relation missing` | Alembic 实际版本、连接数据库与所有映射表，勿直接关闭启动门禁 |
| Run FAILED 于 PLANNING/SEARCHING | 插件环境注入、问题集合、Search Hit `run_id`/`question_id` 来源元数据 |
| EXTRACTING FAILED | Source URL 对应的抓取结果、文档 SHA-256 和检索成功状态 |
| VERIFYING FAILED | Evidence 的精确子串、Manifest、3F 输入/候选、3G 支持性引用、Fencing |
| WRITING FAILED | 3G 是否产生当前 Run 的 SUPPORTED Claim、3H 输入摘要、报告引用和终态凭据 |
| API 409 | 先看 Run `status`/`error` 与 Journal，不要直接强制修改成 COMPLETED |
| SSE 中断 | `ResearchRunEventBus` 是单 Worker 进程内历史，不是跨进程可靠消息队列 |

## 6. 未被本阶段证明的事项

- 未调用真实 GLM、Tavily 或互联网网页；**不能据此宣称真实研究问题端到端可用**。
- 未衡量真实语言模型的引文语义支持准确率、研究覆盖率、冲突检测准确率或综合质量。
- 未覆盖多 Worker 同时对同一个 Run 写入的所有竞态；既有 5A/5B/7B/3G/3H 测试负责相应专项故障场景。
- 未验证真实 API 配额、服务端限流、Token 使用量、财务成本和网络时延。
- 3I-1 是**集成预检**。下一步 3I-2 应在事先限定的调用预算下，对真实 GLM+Tavily 做小样本现场验收；3I-3 记录研究质量与可靠性指标。

## 7. 交付与提交建议

在 WSL2 的 Ruff、默认回归、3 条真实 PostgreSQL 端到端用例及历史关键回归均通过后，再检查 Git 暂存范围，建议：

```bash
git add -- \
  src/enterprise_research/plugin.py \
  tests/integration/test_database_schema_gate.py \
  tests/integration/test_postgres_full_research_pipeline.py \
  docs/architecture/3i-1-full-pipeline-acceptance.md

git diff --cached --check
git diff --cached --name-status
git commit -m "test: accept full fenced research pipeline with controlled adapters"
git status --short
git log -1 --oneline
```

预期恰好 **4 个文件**，无新迁移、无凭据文件。
