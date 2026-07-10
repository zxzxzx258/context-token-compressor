# Context Token Compressor 架构

## 组件

- `ctc.main`：启动本地代理、可选远程代理和 Dashboard 三个 ASGI 应用。
- `ctc.proxy`：认证后的协议转发、压缩、provider 选择、重试、流式响应和统计写入。
- `ctc.compressor`：保守工具输出压缩、开发文本压缩和精确 factsheet sidecar。
- `ctc.providers`：provider 配置、上游凭据、健康检查和 provider 类型。
- `ctc.runtime_config`：来源到 provider 的动态路由。
- `ctc.storage`：SQLite schema、增量迁移和不含原文的统计查询。
- `ctc.dashboard`：管理员 API 与静态管理界面。
- `ctc.security`：Bearer token、回环判断和 Web 安全响应头。

## 请求顺序

1. 非回环入口验证 `CTC_PROXY_TOKEN`，并消费客户端 Authorization。
2. 使用 TCP 直接对端作为来源；只有可信代理可通过 X-Forwarded-For 覆盖。
3. 从数据库规则、环境规则和安全默认值解析 profile。
4. 读取不超过 `CTC_MAX_BODY_BYTES` 的请求体。
5. 对 Responses 或 Chat 请求执行对应压缩器。
6. 按来源路由选择 provider；provider Key 替换客户端访问 token。
7. 转发非流式或 SSE 响应，并记录计量与错误摘要。

## 认证边界

Dashboard 的静态 HTML 可以匿名读取，但所有 `/api/*` 都必须使用 `CTC_ADMIN_TOKEN`。这使浏览器能够先加载登录界面，同时阻止匿名读取统计或修改 provider。

代理 token 与上游 Key 是两类凭据。代理 token 授权客户端使用 CTC；上游 Key 授权 CTC 使用 provider。二者不得复用或互相转发。

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

- 非流式请求遇到 502、503、504 或 `httpx.TransportError` 时延迟后重试一次。
- 流式请求在建立上游流后不重试，避免重复事件。
- 请求体错误返回 400，超限返回 413，认证失败返回 401。
- 未处理的上游错误只向客户端返回固定 `upstream_error`；详细异常仅进入受保护统计。
