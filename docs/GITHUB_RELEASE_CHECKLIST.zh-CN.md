# GitHub v0.1.0 Preview 发布检查清单

**尚未执行**下面的步骤；不要因为补齐 README 就直接公开推送。

## 1. 来源与依赖

- [ ] 在本机审阅完整 `pyproject.toml`，确认 `deerflow_extension_api` 的来源、可安装性与兼容 DeerFlow 的版本或 commit。
- [ ] 核对上游 DeerFlow 代码是否复制到仓库，以及版权与 LICENSE/NOTICE 要求。
- [ ] 确认发布所用 LICENSE（不能把上游 MIT 当作自动给独立代码授权）。
- [ ] 评估是否需要 `uv.lock`：在干净隔离环境中运行 `uv sync --extra dev`，成功后再决定提交锁文件。
- [ ] 在一份全新克隆上验证项目安装与测试，不把既有 WSL2 虚拟环境当作公开安装证据。

## 2. 安全

- [ ] 审核所有未跟踪文件，尤其是 docs、日志、实验输出及快照。
- [ ] 将 `.gitignore.additions` 中适当规则合并到原 `.gitignore`。
- [ ] 对工作树与整个 Git 历史做 secret scan；建议使用 Gitleaks 的 `git` 和 `dir` 模式（先阅读其版本的帮助）。
- [ ] 人工核查是否存在无规则可识别的密钥、内部 URL、个人信息、数据库快照。
- [ ] 任何已提交过的有效秘密应先在供应商侧撤销／轮换。
- [ ] 根据扫描结果选择保留原 Git 历史或仅公开一份经过审查的新导出仓库。

## 3. 质量

- [ ] `uv run --no-project ruff check src/ tests/` 或项目已有规则的等效命令。
- [ ] `uv run --no-project ruff format --check src/ tests/` 或项目已有规则的等效命令。
- [ ] `uv run --no-project pytest -q`。
- [ ] `git diff --check`。
- [ ] 在独立新环境中验证的 GitHub Actions workflow 才能标为绿色 CI。

## 4. GitHub

- [ ] 审核 `git diff --cached --stat` 及暂存文件清单，不使用不加检查的 `git add .`。
- [ ] 创建 Private 仓库，先查看远端的 README、文件清单及可访问的 Git 历史。
- [ ] 完成安全、授权和新环境安装检查后，再决定切换 Public。
- [ ] 若创建 v0.1.0 Preview tag / release，清楚写明哪些检查通过、哪些仍待验证。

## 5. 现阶段工程边界

- 3J-1B/1C/1D：历史文档上的隔离负向验证通过；未证明生产 PostgreSQL 事务原子性。
- 3J-2B：固定候选回放通过；未证明真实 Tavily 返回了目标来源。
- 全量历史基线：664 passed / 63 skipped，需在发布 commit 上重新确认。
