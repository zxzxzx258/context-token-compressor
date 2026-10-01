# 变更日志

## 1.1.0 - 2026-10-01

### 修复

- DeepSeek 桥接：续接轮不再把 `instructions` 追加到历史末尾（此前会产生重复且错位的 system 消息，破坏 tool_calls 相邻关系并触发上游 400），改为原地替换历史首位的 system 消息。
- DeepSeek 桥接：客户端重放历史中已有的 tool 输出时不再生成重复 tool 消息。
- `/v1/chat/completions`：流式请求改为 SSE 透明转发。此前流式请求被缓冲后按 JSON 解析失败，客户端会收到 HTTP 200 包着错误体（统计还误记为非流式）。
- 上游返回非 JSON 成功响应（204、二进制、纯文本）不再被伪造成 502；桥接上游返回非 JSON 时给出明确的 502 错误而非静默成功。
- 重试范围收窄为连接阶段错误与 502/503；不再重试 504 与读阶段错误，避免对已计费补全重复扣费。
- 压缩器：视觉会话中最近用户消息（当前任务）不再被 dev 模式改写。
- 压缩器：单行超长输出（minified JSON、base64）按首/中/尾截断保留片段，不再压成空壳。
- 压缩器：Chat 端工具输出走与 Responses 相同的 JSON 感知路径，任意超长字符串值在 JSON 信封内原位压缩，不再整串改写。
- 压缩器：dev 摘要保证不大于原文；摘要缓存键包含 model 与 target。
- 透明代理拒绝 `/v1/../` 点段路径，无法再借道访问上游任意路径。
- CTC 代理 token 即使在回环监听（无鉴权中间件）下也不会被转发上游。
- `cookie`、`x-forwarded-for`、`x-real-ip`、`forwarded`、`x-ctc-profile` 不再转发给上游 provider。
- SSE 审计改用增量 UTF-8 解码，跨 chunk 的多字节字符不再丢失。
- 统计写入失败不再导致正常响应变成 502；chat 端 413 等请求错误不再被吞成 upstream_error。
- Dashboard：`since`/`until` 非法值与倒序区间返回 400 而不是 500；trend 参数（`trend_hourly`、`trend_recent_limit`）正式生效。
- 配置：超时环境变量非法值不再崩溃，启动校验拒绝非正数超时。
- SQLite 连接用完即关闭，不再依赖 GC 回收文件句柄。
- 安装器写入 systemd EnvironmentFile 改用 systemd 引用规则，含特殊字符的 API Key 不再被读坏。
- Release 工作流按 tag 自动选择 release notes 并校验 tag 与包版本一致。

### 新增

- 新 provider 类型 `deepseek_responses`：直通 DeepSeek 原生 `/responses` 端点。
- 桥接器视觉支持：用户消息 `input_image` 转 Chat `image_url`/`file` 部件；其他角色的图片降级为文本占位符。
- 同请求内重复工具输出去重：以短引用替代字节级重复的后续出现（sha256 比对，不落盘）。
- Chat dev 模式支持结构化（多模态）消息：文本部件原位压缩，图片/文件部件零改动。
- thinking + 工具场景为所有历史 assistant 轮回填 `reasoning_content`（DeepSeek 硬性要求）。
- reasoning effort 按 DeepSeek 官方映射（minimal→low、medium→high、xhigh→high、ultra→max）。
- Dashboard provider 表单增加 `deepseek_responses` 选项。

## 1.0.0 - 2026-07-10

- 首个正式发行版。
- 增加 Dashboard 管理 token 和远程代理访问 token。
- 默认关闭 LAN 监听、profile header 覆盖和进程自重启。
- 只信任显式配置的反向代理来源头。
- 增加 16 MiB 请求体上限、固定错误响应和最小健康接口。
- provider、运行配置和 SQLite 强制使用私有文件权限。
- 增加半交互/无交互 systemd 安装、PEP 621 打包、CI、安全扫描和 GitHub Release 工作流。
- 项目、Python 包、CLI、配置前缀、服务、文档、示例和测试统一为 Context Token Compressor / CTC。
