# Context Token Compressor v1.0.0

Context Token Compressor v1.0.0 是首个正式发行版本，提供通用 OpenAI-compatible 上下文压缩、协议桥接与 provider 路由能力。

重点变化：

- Dashboard API 和远程代理分别使用独立 Bearer token。
- 所有网络监听采用安全默认值，远程入口缺少 token 时拒绝启动。
- 可信代理、请求体限制、错误脱敏和私有文件权限进入代码级保障。
- 支持 wheel、sdist、`ctc` CLI、半交互/无交互 systemd 安装和完整中文部署文档。
- CI 覆盖 Python 3.11/3.12、测试、静态检查、依赖审计、秘密扫描与构建验证。

升级前请阅读 README 的安全模型。v1.0.0 的认证默认值与旧内部版本不兼容，不应直接替换生产实例而不更新客户端配置。
