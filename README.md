# Context Token Compressor

Context Token Compressor（CTC）是一个本地部署的 OpenAI-compatible 上下文压缩、协议桥接与模型路由代理。它位于 Agent 客户端和模型 provider 之间，在转发前压缩冗长的工具输出与较旧会话文本，并按来源把请求路由到不同上游。

CTC 不只用于节省 token。Codex、IDE Agent、CLI Agent 和其他使用 OpenAI-compatible 协议的软件都可以把 CTC 配置为模型入口。对于只会发送 OpenAI Responses 请求的客户端，CTC 可以通过 `deepseek_chat_bridge` 将请求转换为 DeepSeek Chat Completions，维护工具调用续接状态，再把结果转换回客户端可消费的响应。实际可用范围取决于目标 provider 的兼容性和当前桥接覆盖，不应理解为支持任意模型或任意私有协议。

## 主要能力

- 代理 `/v1/responses`、`/v1/chat/completions` 和其他 `/v1/*` 请求，两种主协议均支持流式 SSE 透明转发。
- 以 `safe` 模式压缩工具输出，以 `dev` 模式进一步压缩较旧的 user/assistant 文本。
- 通过 factsheet sidecar 保留错误、路径、版本号、文件名、ID、统计和短 hash。
- JSON 工具输出只压缩超长字符串值，保留 JSON 结构本身；单行超长输出（minified JSON、base64）也会保留首尾片段而非压空。
- 同一请求内字节级重复的工具输出会以短引用替代（记录 sha256，不落盘原文）。
- 支持 OpenAI Responses/Chat 直通、DeepSeek 原生 Responses 直通，以及 Responses 到 DeepSeek Chat Completions 的协议桥接。
- 桥接器支持视觉输入转换（用户消息中的 `input_image` 转为 Chat `image_url`），并按 DeepSeek 官方映射传递 reasoning effort。
- 按可信客户端来源选择压缩 profile 和 provider，实现模型路由中转。
- 非流式连接阶段网络错误和 502/503 可延迟后重试一次；不重试 504 与读阶段错误，避免对已计费的补全重复扣费。
- 提供 SQLite 统计、真实 usage 对齐和受管理员 token 保护的 Dashboard。
- 不保存完整请求体、用户正文、原始工具输出或 Authorization（磁盘零留存；DeepSeek 桥接为维护续接状态会在进程内存中缓存最近会话消息，最长 1 小时，重启即清空）。

## 数据流

```text
OpenAI-compatible client (Codex / IDE Agent / CLI Agent)
        |
        |  Authorization: Bearer <CTC_PROXY_TOKEN>  (远程监听时)
        v
Context Token Compressor
  1. 验证代理访问 token
  2. 识别可信客户端来源
  3. 选择 compression profile
  4. 压缩请求上下文
  5. 选择 provider 和协议路径
  6. 必要时执行 Responses <-> Chat 协议桥接
  7. 使用服务端上游凭据转发并记录脱敏统计
        |
        v
OpenAI-compatible / DeepSeek-compatible provider
```

## 项目特点：按场景控制压缩风险

CTC 最初面向 Hermes 等长期运行的智能 Agent 设计。对这类 Agent 来说，过度压缩的风险不只是漏掉几行日志：用户偏好、禁止事项、时间范围、人物关系和记忆检索结果中的否定、条件或优先级一旦被近义改写，后续轮次就可能形成持续性的用户画像与记忆偏差。因此，CTC 把 Agent 的长期认知稳定放在最大压缩率之前，再按场景分配不同的压缩风险预算。

RTK、Caveman 与 CTC 处理的是三个相邻但不同的阶段：

| 项目 | 工作阶段 | 主要优势 | CTC 未完整照搬的原因 |
|---|---|---|---|
| [RTK](https://github.com/rtk-ai/rtk) | 工具命令执行后 | 识别 `git`、`pytest`、`rg` 等具体命令，使用专用解析器压缩输出 | API 代理通常只能看到工具结果，未必知道原始命令；依赖命令 Hook 也无法覆盖所有内置工具与非命令输出 |
| [Caveman](https://github.com/JuliusBrussee/caveman) | 模型生成回答时 | 通过简洁表达减少当前回复的输出 token | CTC 主要压缩下一次请求中重放的历史上下文；全面改写当前回复或长期记忆可能改变语气、条件和细节 |
| CTC | 历史上下文再次发给模型前 | 统一处理 Responses、Chat、工具输出和较旧消息，并与认证、路由、桥接和统计协同 | 采用通用确定性规则，覆盖面更广，但不具备逐命令解析器的全部精度，也不宣称语义绝对无损 |

CTC 只吸收其中风险可控、适合 API 中间层的部分：

- 借鉴 RTK 的工具输出去重、头尾保留，以及错误、路径、diff、统计和命令行提取，但不要求客户端安装命令重写 Hook。
- 借鉴 Caveman 的去填充词和简洁表达，但只在 `dev` profile 中处理较旧的长消息，并保护最近 6 条消息。
- 借鉴 [headroom](https://github.com/headroomlabs-ai/headroom)、[kompact](https://github.com/npow/kompact) 的 JSON 结构保留思路：对 JSON 工具输出只压缩超过阈值的字符串值（不再限定字段名），保留 JSON 信封本身；单行超长输出按首/中/尾截断保留片段。
- 借鉴 [sqz](https://github.com/ojuschugh1/sqz) 的重复输出引用法：同一请求内字节级重复的工具输出以短引用替代，通过 sha256 确认完全一致；CTC 不落盘原文，因此不做跨请求的"召回句柄"。
- 通过 factsheet sidecar 额外保留路径、版本号、文件名、ID、错误码、统计和短 hash；它用于保护精确 token，不代表摘要已经语义无损。
- 使用本地确定性规则，不额外调用摘要模型；同时不保存完整请求体、用户正文或原始工具输出，避免为了压缩引入新的模型成本和数据留存风险。
- 明确不采纳：LLMLingua 类 perplexity 剪枝（有公开基准显示其破坏工具调用质量）、语义摘要模型（引入新模型依赖与延迟）、可逆压缩+原文存储（与 CTC 的零留存隐私设计冲突）。

### Profile 风险分级

| Profile | 行为 | 推荐场景 |
|---|---|---|
| `safe` | 只压缩 Responses 的 `function_call_output` 或 Chat 的 `role=tool`，不压缩 user/assistant 正文 | Hermes 等长期运行 Agent，优先降低用户画像、长期记忆和行为约束发生偏移的风险 |
| `dev` | 在 `safe` 基础上压缩较旧的 user/assistant 文本，保留最近 6 条消息 | Codex、IDE Agent 等代码代理，以及长工具链和调试会话 |
| `off` | 完全透传 | 小说、法律文本、重要配置和其他必须逐字保留的请求 |

Profile 通常按可信客户端来源分配，但不与产品类型强绑定。代码代理在精确审计等任务中也可以使用 `safe` 或 `off`。回环来源默认使用 `safe`，其他来源默认使用 `off`；管理员可通过 Dashboard 配置显式规则，默认不允许客户端用 header 覆盖 profile。

## 压缩效果

以下结果来自约 26 天真实运行数据的只读回溯，公开值只保留聚合指标：

| 指标 | 结果 |
|---|---:|
| 成功请求 | 13,395 |
| 实际发生压缩的请求 | 12,339 |
| 压缩请求覆盖率 | 92.12% |
| 可压缩片段原始 token | 1,255,842,918 |
| 压缩后 token | 442,406,743 |
| 估算节省 token | 813,436,175 |
| 可压缩片段缩减率 | 64.77% |
| 折算完整上游输入缩减率 | 40.49% |
| 官方定价反事实主估计成本降幅 | 47.25% |
| 官方定价反事实最保守成本降幅 | 37.78% |

最近 24 小时窗口的成功率约为 98.2%。固定窗口核对中，CTC 记录与上游 usage 的输入差异约为 0.26%，输出差异约为 0.04%，说明统计口径在该窗口内基本对齐。

这些数据必须结合以下限制理解：

- `estimated_saved_tokens` 来自 tokenizer 估算，不等于 provider 的最终计费 token。
- 64.77% 是可压缩片段的缩减率，不是整个请求的缩减率；完整输入缩减率为 40.49%。
- 成本降幅基于逐请求官方定价、缓存比例和长上下文档位重建的反事实，不是账单承诺或收益保证。
- CTC 不保存原始正文，因此现阶段没有覆盖全部历史请求的语义无损 A/B 质量结论。
- 效果取决于工具输出长度、会话结构、profile、模型 tokenizer、缓存策略和 provider 计费方式。

## Provider 类型

| 类型 | 行为 |
|---|---|
| `openai_responses` | 按原协议转发到支持 Responses/Chat 的 OpenAI-compatible 上游 |
| `deepseek_chat_bridge` | Chat 请求直接转发；Responses 请求转换为 Chat Completions，并维护 reasoning/tool-call 续接状态 |
| `deepseek_responses` | 直通转发到 DeepSeek 原生 `/responses` 端点（无状态，保留上游流式语义；注意 DeepSeek 流不发送 `data: [DONE]` 哨兵） |

provider URL 必须是绝对 `http://` 或 `https://` 地址，不允许嵌入用户名、密码或 URL fragment。provider 管理属于管理员权限，因为错误配置可能访问内网服务。

### 视觉与多模态

- OpenAI Responses 与 Chat 直通路径对图片内容零改动：`input_image`、`image_url`、`prompt_cache_breakpoint` 等部件原样透传，压缩器只处理文本。
- `deepseek_chat_bridge` 会把用户消息中的 `input_image`（URL、data URL 或 file_id）转换为 DeepSeek 视觉模型（`deepseek-flash`）接受的 Chat `image_url` / `file` 部件；system/assistant/tool 消息中的图片会降级为文本占位符（DeepSeek 仅接受用户消息携带图片）。
- 携带视觉输入的会话中，dev 模式仍会压缩较旧的纯文本上下文，但当前任务（最后一条用户消息及其之后的内容）永不压缩。

## 安全默认值

- 代理默认只监听 `127.0.0.1:8787`。
- Dashboard 默认只监听 `127.0.0.1:8788`，所有 `/api/*` 都需要 `CTC_ADMIN_TOKEN`。
- LAN 代理默认关闭；启用非回环监听时必须配置 `CTC_PROXY_TOKEN`，否则拒绝启动。
- 代理 token 只用于访问 CTC，不会转发给上游；provider Key 与代理 token 必须分离。
- 默认忽略 `X-Forwarded-For`；只有 `CTC_TRUSTED_PROXY_HOSTS` 中的直接对端可以提供该头。
- 默认忽略 `X-CTC-Profile`；需显式设置 `CTC_ALLOW_PROFILE_HEADER=1` 才可启用。
- 默认请求体上限为 16 MiB，超限返回 HTTP 413。
- SQLite、provider 配置和运行配置写入后强制使用 `0600` 权限。
- `/healthz` 只返回状态、组件和版本；上游详情位于认证后的 `/api/status`。

不要把 8788 或 8799 直接暴露到公网。跨主机使用时，应同时配置主机防火墙、TLS 反向代理和强随机 token。

## Linux 一键安装

当前正式安装流程只在 Linux、systemd、Python 3.11/3.12 上完成测试。需要 `git`、`curl`、Python 3.11+、root 权限和可用的 systemd。

交互式安装会询问上游 URL、上游 API Key 和是否启用 LAN 代理，自动生成管理员/代理 token，并将秘密配置写入权限为 `0640` 的 `/etc/ctc/ctc.env`。生成的 token 不会打印到终端。

```bash
git clone --depth 1 --branch v1.1.0 https://github.com/zxzxzx258/context-token-compressor.git && cd context-token-compressor && sudo bash scripts/install_linux.sh
```

安装器会创建专用 `ctc` 系统用户，将只读程序快照放到 `/opt/ctc/app`，虚拟环境放到 `/opt/ctc/.venv`，状态放到 `/var/lib/ctc`，并启用强化后的 `ctc.service`。重复运行安装器可更新同一安装。

非交互安装可直接提供环境变量。注意：把 Key 写在命令行可能进入 shell 历史；自动化环境更推荐准备仅 root 可读的 env 文件，并使用 `CTC_ENV_SOURCE`。

```bash
sudo env \
  CTC_NONINTERACTIVE=1 \
  CTC_UPSTREAM_BASE_URL=https://your-provider.example/v1 \
  CTC_UPSTREAM_API_KEY='<UPSTREAM_KEY>' \
  bash scripts/install_linux.sh
```

或使用配置文件：

```bash
sudo install -d -m 0750 /etc/ctc
sudo install -m 0600 deploy/ctc.env.example /etc/ctc/ctc.env
sudoedit /etc/ctc/ctc.env
sudo CTC_ENV_SOURCE=/etc/ctc/ctc.env bash scripts/install_linux.sh
```

安装后验证：

```bash
systemctl is-active ctc.service
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8788/healthz
```

## 给 Agent 的一句话安装提示词

> 阅读本仓库的 README、`pyproject.toml`、`scripts/install_linux.sh`、`deploy/ctc.service` 和 `ctc/` 源码，在这台 Linux systemd 主机上以安全默认值安装 Context Token Compressor，先向我确认上游 URL 和是否启用 LAN 代理，不输出任何 Key，安装后验证 `ctc.service`、两个 `/healthz`、匿名认证拒绝和一次隔离的 OpenAI-compatible smoke test。

## 从 GitHub Release 手动安装

```bash
mkdir ctc-release && cd ctc-release
gh release download v1.0.0 -R zxzxzx258/context-token-compressor
sha256sum -c SHA256SUMS.txt
python3.11 -m venv .venv
./.venv/bin/python -m pip install context_token_compressor-1.0.0-py3-none-any.whl
CTC_ADMIN_TOKEN="$(openssl rand -hex 32)" ./.venv/bin/ctc
```

如果 Release 暂时不可用，从 tag 安装：

```bash
git clone https://github.com/zxzxzx258/context-token-compressor.git
cd context-token-compressor
git checkout v1.0.0
python3.11 -m venv .venv
./.venv/bin/python -m pip install .
```

## 启用远程代理

在 `/etc/ctc/ctc.env` 中同时设置监听和独立代理 token：

```text
CTC_LAN_PROXY_HOST=0.0.0.0
CTC_LAN_PROXY_PORT=8799
CTC_PROXY_TOKEN=<独立强随机值>
```

客户端把 OpenAI-compatible base URL 配置为 `http://<CTC_HOST>:8799/v1`，并把 `CTC_PROXY_TOKEN` 作为客户端 API Key。CTC 验证并消费该 Authorization 后，使用服务端 provider Key 调用上游。

## Dashboard

默认地址为 `http://127.0.0.1:8788`。远程主机建议通过 SSH 隧道访问：

```bash
ssh -L 8788:127.0.0.1:8788 user@server
```

浏览器第一次请求管理 API 时会提示输入 `CTC_ADMIN_TOKEN`。token 只保存在当前标签页的 `sessionStorage`，关闭标签页后需要重新输入。

主要 API：

| 路径 | 说明 |
|---|---|
| `GET /healthz` | 最小健康状态，无需认证 |
| `GET /api/status` | 运行状态和当前 provider，需要管理员 token |
| `GET /api/dashboard` | 统计、趋势、最近请求与错误 |
| `GET/POST /api/profiles` | 来源 profile 管理 |
| `GET/POST/PATCH/DELETE /api/providers` | provider 管理 |
| `PATCH /api/runtime-config/provider-routing` | 来源到 provider 路由 |
| `POST /api/admin/restart` | 进程重启，默认关闭 |

## 主要环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CTC_UPSTREAM_BASE_URL` | 空 | 默认上游基础 URL |
| `CTC_UPSTREAM_API_KEY` | 空 | 默认上游 Key |
| `CTC_ADMIN_TOKEN` | 空 | Dashboard API 必填 token |
| `CTC_PROXY_HOST` / `PORT` | `127.0.0.1` / `8787` | 本地代理监听 |
| `CTC_LAN_PROXY_HOST` / `PORT` | 空 / `0` | 可选远程代理监听 |
| `CTC_PROXY_TOKEN` | 空 | 非回环或 LAN 代理必填 token |
| `CTC_DASHBOARD_HOST` / `PORT` | `127.0.0.1` / `8788` | Dashboard 监听 |
| `CTC_DEFAULT_PROFILE` | `safe` | 默认压缩 profile |
| `CTC_PROFILE_RULES` | 空 | 来源到 profile 的静态规则 |
| `CTC_TRUSTED_PROXY_HOSTS` | 空 | 允许提供 X-Forwarded-For 的直接对端 |
| `CTC_ALLOW_PROFILE_HEADER` | `0` | 是否允许 X-CTC-Profile |
| `CTC_MAX_BODY_BYTES` | `16777216` | 请求体上限 |
| `CTC_DB_PATH` | 运行目录下 `ctc.sqlite3` | SQLite 路径 |
| `CTC_PROVIDER_CONFIG_PATH` | 与 DB 同目录 | provider JSON 路径 |
| `CTC_RUNTIME_CONFIG_PATH` | 与 DB 同目录 | 路由 JSON 路径 |
| `CTC_TRUST_ENV_PROXY` | `0` | httpx 是否读取系统代理环境变量 |

## 升级、回滚与隐私

升级前备份 `/etc/ctc/ctc.env` 和 `/var/lib/ctc`，但不要把备份提交到 Git。检出目标 tag 后重新运行安装器。回滚时恢复旧 tag 和升级前状态备份，再验证 `/healthz` 与隔离的真实协议 smoke test。

CTC 存储请求时间、模型、路径、状态码、来源、provider 标识、字符/token 估算、延迟、错误摘要和压缩项 hash。它不应存储完整请求体、用户正文、原始工具输出、Authorization 或 provider Key。错误摘要仍可能包含上游库生成的主机或协议细节，因此 Dashboard 与 SQLite 都应按敏感运行数据保护。详见 [SECURITY.md](SECURITY.md)。

## 平台支持与参与贡献

目前只在 Linux + systemd + Python 3.11/3.12 上完成正式安装和运行测试。macOS 和 Windows 尚无官方安装器，也没有完成正式兼容验证。macOS/Windows 用户可以让 Codex、Claude Code 等 Agent 阅读本 README、`pyproject.toml`、`ctc/main.py` 和部署脚本后自行实现适配；欢迎 Fork，但请不要把未经验证的适配描述为官方支持。

欢迎提交 Issue 和 PR。Dashboard 的交互、可视化、可访问性、响应式布局和运维体验是优先贡献方向；提交前请运行 compileall、pytest、Ruff 和现有安全检查，并确保不包含 Key、provider 配置、数据库、日志或真实请求正文。

本项目采用 MIT License，见 [LICENSE](LICENSE)。

## 致谢

Context Token Compressor 的开发文本压缩思路受以下开源项目启发，谨向作者和贡献者致谢：

- [RTK](https://github.com/rtk-ai/rtk)：面向开发命令输出的高效 token 压缩工具。
- [Caveman](https://github.com/JuliusBrussee/caveman)：以极简表达压缩 Agent 上下文的 Claude Code skill。
