# 3E-6D-4B — Checkpoint / Stage Journal 一致性对账协议

## 本阶段交付范围

在 `0b36489`（4A Checkpoint Boundary）的真实源码基线上，增加**只读对账协议**，
用于解释两个独立持久化系统在不同提交顺序下的状态。它不添加数据库表，不改变
Lease/Fencing 写入事务，也**没有实现** LangGraph Saver 的安装、Graph 写入或跨库原子提交。

主要文件（中文说明）：

| 文件 | 作用 |
| --- | --- |
| `application/checkpoint_consistency.py` | **阶段对账器**。读取唯一的 Stage Journal 尝试与可选 Graph Checkpoint 引用；区分未配置、缺失、存在、身份冲突、读取不可用等状态；绝不读取/导出 Graph payload。 |
| `application/safe_resume.py` | **安全恢复决策**。先对账，再验证 Run 对应的 Plan/Source/Document；对账结果加入恢复证明摘要。 |
| `application/__init__.py` | **公开应用层契约**。导出观察枚举、对账报告和只读函数。 |
| `tests/application/test_checkpoint_consistency.py` | **故障窗口单测**。验证各类提交顺序、无效代次、读取中断、接管竞争和证明变更。 |
| `tests/infrastructure/test_postgres_checkpoint_consistency.py` | **真实数据库验收用例**。Stage Journal、Plan 与 Lease 来自 PostgreSQL，Graph 检查点是受控模拟读者，用于模拟独立故障窗口。 |

## 谁拥有最终解释权？

- **PostgreSQL Run + Lease/Fencing**：决定 Worker 是否有权运行及执行受保护业务写入。
- **PostgreSQL Stage Journal**：提供阶段启动/完成/失败记录。`completed` 只说明
  Stage Runner 返回且阶段日志提交成功，不是外部 API 或业务副作用的原子提交凭证。
- **Run 关联的持久化业务产物**：安全跳过阶段的必要证据，目前只有 Planning、
  Searching、Extracting 具有足够保守的可核对规则。
- **LangGraph Checkpoint**：仅属于 Graph 内部状态参考。已保存一个 checkpoint
  **不代表** Graph 阶段或研究业务已经完整完成。

## 提交顺序与恢复决策矩阵

| Stage Journal | Checkpoint 观察 | 业务产物 | 决策与原因 |
| --- | --- | --- | --- |
| `started` / `failed` | 存在或不存在 | 任意 | **暂停**。有不确定副作用，禁止盲目重放。 |
| 缺失、重复尝试或跨 Run | 无唯一可信 Generation | 任意 | **暂停**，不猜测线程/命名空间，也不调用 Reader。 |
| `completed` | 未配置 Reader | 完整 | **允许业务核验后继续**，兼容此前的独立扩展运行方式。 |
| `completed` | 缺失 Checkpoint | 完整 | **允许业务核验后继续**；Graph 未保存并不推翻受保护业务记录。 |
| `completed` | 身份一致的 Checkpoint | 完整 | **允许业务核验后继续**；Checkpoint 仅为辅助证据。 |
| `completed` | 存在 Checkpoint | 缺失或不匹配 | **暂停**；Graph 不能代替 Plan/Source/Document。 |
| `completed` | 错误 Task/Run/Stage/Token 或非法返回 | 任意 | **暂停**；防止混入其他任务/执行代次。 |
| `completed` | Reader 已配置但读取失败 | 任意 | **暂停**；读取失败绝不伪装成 Checkpoint 不存在。 |
| `completed` | 前后 Checkpoint ID/有无状态改变 | 完整 | **接管失败**；释放新 Lease，禁止派发后续阶段。 |

**区分 `NOT_CONFIGURED`、`ABSENT` 与 `UNAVAILABLE`：**

- `NOT_CONFIGURED`：没有接入真实 Saver，4A/3C 行为保持兼容。
- `ABSENT`：已配置且成功查询，确实没有匹配的 Checkpoint；可以继续核验数据库产物。
- `UNAVAILABLE`：已配置但读取报错，不能把未知结果当成不存在，因此暂停。

## 非原子“双写”故障窗口

```text
Run 状态进入 Planning (PostgreSQL, fenced)
    -> Stage Journal: started (PostgreSQL, fenced)
    -> Graph 内部执行/可能保存 Checkpoint (未来集成)
    -> 研究业务产物经 save_guarded() 提交 (PostgreSQL, fenced)
    -> Stage Journal: completed (PostgreSQL, fenced)
```

这是一种**可核对的执行顺序约束**，不是两个数据库之间的事务或 exactly-once 协议。
任何一步崩溃，都不得仅凭 Checkpoint 跳过未知副作用。

案例 A（Graph 先保存，Journal 尚未完成）：

```text
Checkpoint observed -> Journal started -> PAUSE, NO replay
```

案例 B（Journal 完成、业务产物齐全，Graph 未保存）：

```text
Journal completed + Artifact valid -> Checkpoint absent -> READY (仅可显式接管)
```

案例 C（取得新一代 Lease 后，Graph Reference 改变）：

```text
inspect -> claim fenced lease -> inspect AGAIN -> proof changed
    -> release claimed lease -> NO dispatch
```

## 代码契约与安全边界

1. `reconcile_stage_checkpoint()` 不修改任何仓储，不申请 Lease，不执行外部工具；
   在 Journal 不唯一或 Run 不匹配时，不猜测 Graph Namespace。
2. 观察报告只包含 Stage、尝试数、Journal Outcome、Fencing Token、Checkpoint
   是否存在及其 ID；不会返回 `checkpoint["channel_values"]` 或私密 Graph payload。
3. 当 Saver 读取出现普通异常，报告 `UNAVAILABLE` 并暂停，诊断信息不包含异常详情；
   任务取消 (`asyncio.CancelledError`) 继续向上传递。
4. 恢复 `proof_digest` 纳入「Reader 未配置 / 成功读到缺失 / 存在及其 ID」的区别，
   提供接管前后二次检查的变化检测。摘要**不是跨数据库原子快照**。
5. `may_verify_artifacts=True` **不是恢复许可**。`ResearchSafeResumeInspector`
   仍须核对业务产物；`ResearchRunExecutor.resume()` 仍须取得新 Lease 并再次检查。
6. 当前对 `VERIFYING` 和 `WRITING` 继续暂停，尚无可验证的 Run 级 Evidence/Report 完成清单。

## 仍未完成

- 尚未让实际 DeerFlow/LangGraph Runtime 使用共享持久化 Saver，故本阶段的 Graph
  故障场景使用受控 Reader/Saver 替身，**不能声称验证了真实 Graph 双写**。
- 无法保证 Graph 和 PostgreSQL 跨库事务原子性、Graph 内部执行 exactly-once、外部 LLM
  调用 exactly-once，也无法让只读对账识别全部并发修改或过期 Graph 内容。
- 下一步 3E-6D-4C：先明确宿主可信 Runtime 的 Graph 构建入口、Checkpointer
  生命周期、并发和鉴权约束，再实现真实接线与端到端恢复实验。

## WSL2 验收命令

```bash
unset ENTERPRISE_RESEARCH_RUN_DB_TESTS
uv run --no-project pytest tests/application/test_checkpoint_consistency.py -q
uv run --no-project pytest -q

# 仅在开发 PostgreSQL 已就绪且 Alembic 处于 7c3a1e9d4b62 时运行：
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 uv run --no-project pytest \
  tests/infrastructure/test_postgres_checkpoint_consistency.py \
  tests/infrastructure/test_postgres_safe_resume.py \
  tests/infrastructure/test_postgres_stage_journal.py \
  tests/infrastructure/test_postgres_run_leases.py \
  -q
```
