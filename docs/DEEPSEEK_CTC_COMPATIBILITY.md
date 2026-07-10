# DeepSeek 兼容桥说明

`deepseek_chat_bridge` 用于只有 Chat Completions 接口、但客户端发送 OpenAI Responses 请求的 provider。

桥接层负责：

- 将 Responses `input` 转成 Chat `messages`。
- 将 Responses tools 转成 Chat tools。
- 将 Chat message、tool calls 和 usage 转回 Responses JSON 或 SSE。
- 在工具调用续接时缓存并恢复 `reasoning_content`。
- 过滤 thinking 模式不支持的采样参数。

限制：

- 流式响应建立后不会自动重试。
- 缺少前序工具调用上下文时，桥接层会回退到可用的原生 Responses provider；没有回退 provider 时返回 409。
- provider 的具体模型名由管理员配置，不在源码中绑定个人账号或固定上游。

发行测试必须覆盖非流式、流式、工具调用、工具输出续接、reasoning replay 和缺少上下文回退。
