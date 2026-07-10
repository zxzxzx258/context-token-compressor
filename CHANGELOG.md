# 变更日志

## 1.0.0 - 2026-07-10

- 首个正式发行版。
- 增加 Dashboard 管理 token 和远程代理访问 token。
- 默认关闭 LAN 监听、profile header 覆盖和进程自重启。
- 只信任显式配置的反向代理来源头。
- 增加 16 MiB 请求体上限、固定错误响应和最小健康接口。
- provider、运行配置和 SQLite 强制使用私有文件权限。
- 增加半交互/无交互 systemd 安装、PEP 621 打包、CI、安全扫描和 GitHub Release 工作流。
- 项目、Python 包、CLI、配置前缀、服务、文档、示例和测试统一为 Context Token Compressor / CTC。
