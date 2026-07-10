# Context Token Compressor 项目协作规则

本仓库是 Context Token Compressor（CTC）的正式发行源。默认使用中文沟通，结论先行，并以真实测试输出为依据。

## 安全边界

- 禁止提交或输出 `.env`、API Key、token、Authorization、provider 配置、SQLite、日志和认证缓存。
- 配置检查只报告变量是否存在、路径、模式和脱敏标识，不打印秘密内容。
- Dashboard API 的认证、远程代理认证、可信代理判断和文件权限属于发行阻断项，不得为了兼容而改回不安全默认值。
- 公开 profile 仅允许 `safe`、`dev` 和 `off`；不得重新加入旧名称或隐藏兼容别名。

## 修改与验证

1. 使用 Python 3.11 或 3.12。
2. 修改后至少运行：
   - `python -m compileall ctc tests scripts`
   - `python -m pytest tests -q`
   - `python -m ruff check ctc tests scripts`
3. 发行前还要运行 Bandit、pip-audit、gitleaks、`python -m build` 和 `python -m twine check dist/*`。
4. 安全行为必须有回归测试；不得只靠 README 警告替代代码保护。
5. 不直接修改运行目录中的源码；从版本化源码或 Release artifact 安装。

## 发行规则

- 版本号在 `pyproject.toml` 与 `ctc/__init__.py` 中保持一致。
- tag 使用 `vMAJOR.MINOR.PATCH`，GitHub Release 必须包含 wheel、sdist 和 SHA-256 清单。
- 不发布到 PyPI，除非仓库所有者明确授权。
- Release 失败时不得把普通 commit 或 tag 宣称为已发布版本。
