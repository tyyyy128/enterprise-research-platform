# 3I-2：3H Report Synthesis JSON 互操作与故障分类修复

## 证据与边界

第四次真实研究 Run `e856d3a3-7e7b-456e-aa9a-d58c487981b3` 的 PostgreSQL 结果为：

- Planning、Searching、Extracting、Verifying 均已完成；Writing 失败。
- 1 个已验证 Evidence、1 个候选 Claim、1 个待验证 Claim。
- 第五次模型请求返回 HTTP 200（约 94 output tokens），但 3H 抛出 `Report synthesis model returned malformed JSON.`。
- **现有验收报告不保存原始模型响应，不能断定此次具体是 fenced JSON、JSON 语法、schema 还是 token 截断。**

## 修复策略

默认仍只允许一个完整、类型严格的 JSON 对象。增加一种受限互操作：仅当模型返回的**整个文本**恰好是一个 ` ```json ` 或 ` ``` ` Markdown 围栏时，去掉围栏并执行原本的 JSON/Pydantic 校验。绝不使用正则从任意散文中提取 JSON，不修补畸形 JSON、不补 Claim ID、不生成内容、不放松 3H Domain 及 DB 完成凭据检查。

错误信息使用不包含原始文本的分类：`response_shape`、`content_type_or_length`、`syntax`、`schema`、`truncated`。当模型返回 `finish_reason=length` 时，无条件拒绝，不把半截 JSON 当作有效计划。原始模型回答及 API Key 不写入 Run error 或 Harness JSON。

增加可选配置 `ENTERPRISE_RESEARCH_SYNTHESIS_RESPONSE_FORMAT`：
- `text`（默认）：与原请求完全兼容；不添加额外 provider 参数。
- `json_object`：向 3H **单独**传入 `response_format={"type":"json_object"}`，必须事先确认目标提供商和模型支持；不支持时可能返回 HTTP 400。JSON 模式只保证格式倾向，**Pydantic 和 Domain 检验仍然必须执行**。

## WSL2 验收

基线：已应用的未提交 3I-2 Harness、Content-Length、Oversize、Evidence Coverage 变更。只需追加本补丁；**不需要 Alembic 迁移、数据库清理或重新创建先前失败的 Run**。

```bash
cd ~/projects/enterprise-research-platform
PATCH=/mnt/d/Downloads/3I-2-report-synthesis-json-hotfix.patch
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
mapfile -t PY_FILES < <({ git diff --name-only -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u)
uv run --no-project ruff check "${PY_FILES[@]}"
uv run --no-project ruff format --check "${PY_FILES[@]}"
uv run --no-project pytest tests/infrastructure/test_openai_compatible_report_synthesis.py tests/acceptance -q
uv run --no-project pytest -q
```

对任何 Ruff 错误仅格式化相关变更文件，不要整体修改旧 `src/ tests/` 文件。

## 付费执行边界

- 完成上述检查前，不再发送真实 GLM/Tavily 请求。
- 是否开启 `json_object` 必须按实际 GLM 型号/账号接口确认；默认 `text` 是安全兼容选择。
- 原来 4 次失败 Run 与数据库证据保留不变。下一次运行是**新 Run**，不是重放历史的已终态失败 Run。
- 任何模型输出即使语法合法，只要 Claim ID 非法、重复/遗漏或引用未验证，写入仍然 fail-closed。
- 需要人工审阅最终引用、来源截取标记及报告真实性；HTTP 200 或 `COMPLETED` 不等于财报答案语义正确。
