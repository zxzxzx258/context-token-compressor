# DeepSeek 兼容桥说明

`deepseek_chat_bridge` 用于只有 Chat Completions 接口、但客户端发送 OpenAI Responses 请求的 provider。

桥接层负责：

- 将 Responses `input` 转成 Chat `messages`。
- 将 Responses tools 转成 Chat tools，包括 `tool_choice` 对象形态（`{"type": "function", "name": ...}`）到 Chat 嵌套形态的转换。
- 将 Chat message、tool calls 和 usage 转回 Responses JSON 或 SSE。
- 在工具调用续接时缓存并恢复 `reasoning_content`。
- 过滤 thinking 模式不支持的采样参数。
- 续接轮中把 `instructions` 写回历史首位的 system 消息（原地替换，不追加到末尾，避免破坏 assistant tool_calls 与 tool 输出的相邻关系）。
- 续接轮中跳过客户端重放的、历史已包含的 tool 输出，避免重复 tool 消息。
- thinking 开启且带工具时，为所有历史 assistant 轮补齐 `reasoning_content`（DeepSeek 要求，缺失会 400）。
- 视觉输入：用户消息中的 `input_image`（URL / data URL / file_id）转换为 Chat `image_url` / `file` 部件；system、assistant、tool 消息中的图片降级为文本占位符（DeepSeek 仅接受用户消息携带图片）。
- `finish_reason: length` / `content_filter` 映射为 Responses `status: incomplete` 与对应的 `incomplete_details`，不再把截断响应报成 completed。
- 按官方文档映射 reasoning effort：`minimal→low`、`medium→high`、`xhigh→high`、`ultra→max`；thinking 关闭时不发送 `reasoning_effort`。
- 结构化 `function_call_output.output`（dict/list）以 JSON 序列化传递，不再生成 Python repr。

限制：

- 流式响应建立后不会自动重试。
- 桥接请求会被完整缓冲后一次性转成 Responses SSE（DeepSeek 侧非流式），慢生成期间客户端不会收到增量字节。
- 缺少前序工具调用上下文时，桥接层会回退到可用的原生 Responses provider；没有回退 provider 时返回 409。
- 为维护续接状态，桥接会在进程内存中缓存最近的会话消息（默认 64 条、1 小时 TTL，重启即清空）；磁盘上仍然零留存。
- provider 的具体模型名由管理员配置，不在源码中绑定个人账号或固定上游。

`deepseek_responses` 类型提供 DeepSeek 原生 `/responses` 端点的直通转发：保留上游流式语义、压缩与统计照常生效。注意 DeepSeek 原生 Responses 是无状态的（`previous_response_id` 不生效），且其流式响应不发送 `data: [DONE]` 哨兵，客户端兼容性请先自行验证。

发行测试必须覆盖非流式、流式、工具调用、工具输出续接、reasoning replay、视觉输入转换和缺少上下文回退。
