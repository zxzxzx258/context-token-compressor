# Release Notes v1.1.0

## 概述

v1.1.0 是一次修复为主的发行版：修复了审计发现的全部 P0/P1 缺陷（桥接续接状态损坏、流式 Chat 损坏、非 JSON 上游响应被伪造成 502、重试重复计费、压缩器破坏关键内容），并根据 2026 年 10 月的模型与协议现状新增 DeepSeek 原生 Responses 直通与桥接视觉支持。

## 修复（详见 CHANGELOG）

- **桥接续接状态（P0）**：续接轮的 system 消息原地替换，不再追加到历史末尾；重放的历史 tool 输出不再重复。
- **流式 Chat Completions**：按 SSE 透明转发，不再返回"HTTP 200 包错误体"。
- **非 JSON 上游响应**：204 / 二进制 / 纯文本成功不再变成假 502；破坏性操作不再因伪失败被重复执行。
- **重试语义**：仅连接阶段错误与 502/503 重试一次；504 与读阶段错误不重试，消除双重计费。
- **压缩安全**：视觉会话的当前任务永不压缩；单行超长输出保留片段；Chat 端 JSON 输出保留信封结构；dev 摘要永不大于原文。
- **安全加固**：拒绝 `/v1/..` 路径逃逸；代理 token 回环监听下也不转发上游；`cookie` / `x-forwarded-for` 等 5 类头不再外发；SSE 审计修复跨 chunk UTF-8 丢失。

## 新增

- Provider 类型 `deepseek_responses`（DeepSeek 原生 Responses 直通，注意上游无状态且流不发 `[DONE]`）。
- 桥接视觉转换（用户消息 `input_image` → Chat `image_url` / `file`）。
- 同请求重复工具输出去重（sha256 引用法）。
- thinking + 工具场景的 `reasoning_content` 全轮回填与官方 effort 映射。
- Dashboard range 校验（400）与 trend 参数生效；`since`/`until` 支持任意 ISO-8601。

## 升级

- 破坏性变更：无配置格式变更。行为变更集中在重试语义（更保守）与 header 转发（更严格），正常客户端不受影响。
- 建议同时升级到本版本后再配置 DeepSeek `deepseek-flash` 等视觉模型。
