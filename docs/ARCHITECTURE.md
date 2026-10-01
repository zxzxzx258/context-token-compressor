# Context Token Compressor 架构

## 组件

- `ctc.main`：启动本地代理、可选远程代理和 Dashboard 三个 ASGI 应用，并提供 `--check-config` 配置校验入口。
- `ctc.proxy`：认证后的协议转发、压缩、provider 选择、重试、流式响应和统计写入。
- `ctc.compressor`：保守工具输出压缩、开发文本压缩和同请求重复输出去重。
- `ctc.factsheet`：从工具输出中提取错误码、路径、版本号、ID、统计和短 hash 的精确 factsheet sidecar。
- `ctc.deepseek_bridge`：Responses 与 DeepSeek Chat Completions 的协议桥接、工具调用续接状态、reasoning replay、视觉输入转换和 Responses SSE 生成。
- `ctc.providers`：provider 配置、上游凭据、健康检查和三种 provider 类型（`openai_responses`、`deepseek_chat_bridge`、`deepseek_responses`）。
- `ctc.runtime_config`：来源到 provider 的动态路由。
- `ctc.profiles`：profile 解析、别名归一化和来源规则匹配。
- `ctc.storage`：SQLite schema、增量迁移和不含原文的统计查询。
- `ctc.metering`：上游 usage 快照解析与缓存对齐的反事实计量。
- `ctc.dashboard`：管理员 API 与静态管理界面。
- `ctc.security`：Bearer token、回环判断和 Web 安全响应头。
- `ctc.permissions`：状态文件写入后的 `0600` 权限强制。
- `ctc.tokens`：基于 tiktoken 的 token 估算。

## 请求顺序

1. 非回环入口验证 `CTC_PROXY_TOKEN`，并消费客户端 Authorization。
2. 使用 TCP 直接对端作为来源；只有可信代理可通过 X-Forwarded-For 覆盖。
3. 从数据库规则、环境规则和安全默认值解析 profile。
4. 读取不超过 `CTC_MAX_BODY_BYTES` 的请求体。
5. 对 Responses 或 Chat 请求执行对应压缩器。
6. 按来源路由选择 provider；`deepseek_chat_bridge` 在压缩后把 Responses 请求转换为 Chat Completions，并维护工具调用续接状态。
7. provider Key 替换客户端访问 token；provider 配置了模型名时改写 `model` 字段。
8. 转发非流式或 SSE 响应（桥接路径将上游 Chat 响应重新编码为符合 Responses 流契约的 SSE），并记录计量与错误摘要。

## 认证边界

Dashboard 的静态 HTML 可以匿名读取，但所有 `/api/*` 都必须使用 `CTC_ADMIN_TOKEN`。这使浏览器能够先加载登录界面，同时阻止匿名读取统计或修改 provider。

代理 token 与上游 Key 是两类凭据。代理 token 授权客户端使用 CTC；上游 Key 授权 CTC 使用 provider。二者不得复用或互相转发；代理 token 即使在回环监听（无鉴权中间件）下也不会被转发给上游。

## 状态边界

默认 Linux 布局：

```text
/opt/ctc/app       只读程序快照
/opt/ctc/.venv     Python 虚拟环境
/etc/ctc/ctc.env  root:ctc 0640
/var/lib/ctc       ctc:ctc 0700
```

状态目录包含 SQLite、`providers.json` 和 `runtime_config.json`。源码同步脚本明确排除这些文件以及 Git、缓存、测试、文档和构建产物。

## 失败策略

- 重试范围收窄为连接阶段错误与 502/503：仅当请求未到达上游（连接阶段 `httpx.TransportError`）或上游返回 502/503 时，延迟后重试一次。不重试 504 与读阶段错误，避免对已计费的补全重复扣费。
- 流式请求在建立上游流后不重试，避免重复事件。
- 请求体错误返回 400，超限返回 413，认证失败返回 401，桥接缺少前序工具调用上下文且无可用回退 provider 时返回 409。
- 非流式上游成功响应不要求是 JSON：204、二进制、纯文本按原样转发，不伪造成 502。
- 未处理的上游错误只向客户端返回固定 `upstream_error`；详细异常仅进入受保护统计。统计写入失败不影响响应转发。
