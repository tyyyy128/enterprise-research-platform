# 3E-6D-4A — Graph Checkpoint Boundary（图检查点边界）

## 本阶段做了什么

在已完成的 3E-6D-3C `bf9de6a` 基线上增加一个**可选、只读**的 LangGraph
Checkpoint 边界，不假设当前 `deerflow-extension-api>=0.2,<0.3` 会暴露宿主的 Graph
或其 Checkpointer。本阶段既不安装 LangGraph，也不创建假的持久化 Checkpointer。

* `ResearchGraphCheckpointBinding`：用 `task_id`、`run_id`、研究阶段、`fencing_token`
  生成确定的 `thread_id` 和 `checkpoint_ns`。实际接入顶层 LangGraph 后，`checkpoint_ns`
  必须为根命名空间 `""`，因此阶段、代次的隔离信息位于 `thread_id` 中（v2 布局）。
  新一代执行权使用不同 Thread，避免旧 Worker 的 Graph 状态被误识别为新 Worker 的状态。
* `LangGraphCheckpointReader`：接受由**可信集成层明确传入**的、实现 `aget_tuple(config)`
  的真实异步 Saver；读取并校验检查点的 Thread/Namespace/Checkpoint ID，
  只向领域层返回不可变的标识，不暴露 Graph 内部消息和状态内容。
* `ResearchSafeResumeInspector(graph_checkpoints=...)`：提供**可选接入点**。
  缺失 Checkpoint 不阻止已有的业务证据判定；存在且身份一致的 Checkpoint
  仅参与恢复证据摘要，身份冲突则暂停。**Checkpoint 永远不能单独允许跳过阶段**。

## 双层权威来源

| 层次 | 权威数据 | 中文职责 |
| --- | --- | --- |
| 研究业务层 | PostgreSQL `research_runs`、Lease、Stage Journal、Plan/Source/Document/Evidence | 判断是否有执行权、阶段与业务产物是否可靠 |
| Graph 运行层 | 未来由宿主提供的 LangGraph Checkpointer | 记录 Agent 内部 Graph 状态；仅作为辅助观察证据 |

`Stage Journal=completed` 仍不等于业务原子提交。即便 Checkpoint 存在，
`Safe Resume` 仍按 3E-6D-3C 的保守规则核对完整 Plan、Search 来源和 Retrieval 结果。
旧 Worker 即使继续写入自己旧 generation 的 Graph Thread，也不能借此通过
现有业务 `save_guarded()` 的 Fencing 检验。**隔离命名空间不能替代数据库写保护**。

## 持久化不一致时的处理

1. **Checkpoint 有、业务产物无**：暂停，不自动重试或跳过业务阶段。
2. **业务产物和 Journal 可验证、Checkpoint 无**：保持 3C 的原有保守判定，
   不依赖一个当前尚未接线的 Graph 后端。
3. **Checkpoint 的 Task/Run/Stage/Generation 不匹配**：暂停并要求人工核对。
4. **成功获取新一代 Lease 后**：3C 会再次运行恢复检查；如果已经接线的检查点引用
   发生变化，`proof_digest` 变化导致释放 Lease，不启动后续阶段。
5. **Graph Saver 不可用或读取报错**：检查过程失败，不能据此获取继续执行的许可。

## 接入示例（未来 4B/4C 由实际宿主提供 saver 时）

```python
binding = ResearchGraphCheckpointBinding.for_lease(task_id, stage, lease)
config = binding.runnable_config()
# 由可信执行边界将 config 传入实际 LangGraph graph.ainvoke/astream。
# checkpoint_reader = LangGraphCheckpointReader(actual_async_saver)
# inspector = ResearchSafeResumeInspector(..., graph_checkpoints=checkpoint_reader)
```

**不要**从 HTTP 请求自行构造可写入的 Lease、不要从用户输入选择 graph `thread_id`、
不要用 Checkpoint 取代 PostgreSQL 的 `claim_lease` 或 `save_guarded`。

## 尚未完成 / 下一步

* 本版本只提供边界定义、真实 `aget_tuple` 读适配和测试；`plugin.py` 未自动启用。
* 4B：在**真正的 LangGraph 调用入口**接入共享 Saver，并设计 Checkpoint/Stage Journal
  不同提交顺序的读写协议、故障注入及加密/反序列化策略。
* 4C：基于实际宿主开放的能力，整合 Graph 恢复，但不可从 Graph state 直接修改业务 Run。
* 不涉及新的 PostgreSQL 表或 Alembic 迁移，数据库版本仍为 `7c3a1e9d4b62`。

## 本地测试

```bash
uv run --no-project pytest tests/application/test_graph_checkpoint.py -q
uv run --no-project pytest tests/application/test_safe_resume.py -q
uv run --no-project pytest -q
```
