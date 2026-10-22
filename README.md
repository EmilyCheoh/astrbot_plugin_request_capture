# Request Capture 🐈‍⬛

记录 AstrBot 发给 LLM 的 HTTP 请求 JSON **原始 body**。

## 使用

1. 通过 AstrBot「插件 → 安装插件 → 从文件安装」上传 ZIP。
2. 在 QQ 或小窝发送 `/print`，开启记录。
3. 正常聊天，每次发往 OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 的请求都会生成独立 `.json`。
4. 发送 `/stopprint`，关闭记录。

存放位置：`AstrBot/data/plugin_data/astrbot_plugin_request_capture/requests/`。

## 与普通 on_llm_request 钩子的区别

本插件监听 Provider 所使用的 HTTPX 客户端的 request 事件：
**SDK 已经序列化请求体，网络请求即将发出**。
因此包含最终的 messages、system、tools、thinking、stream 等请求体字段，
包括工具调用后续请求和流式请求。它并不改变传输内容。

**范围**：仅 OpenAI / Anthropic SDK 使用 HTTPX，且 API 路径为
`/chat/completions`、`/messages`、`/responses` 的 POST 请求。
其他服务商、绕过 SDK 的请求不会记录。提供商发生切换时，
会在下一次 AstrBot `on_llm_request` 钩子中刷新客户端。

**注意**：
- 记录的是即将发送的 HTTP 请求体，可能包括最终发送失败的尝试，不表示服务端一定成功收到。
- 同一次聊天若触发工具调用、重试、压缩或备用模型，可能产生多个 JSON 文件。
- 不记录 HTTP 请求头（包括 Authorization / API Key），也不记录响应。
- 文件包含完整 system prompt、历史消息及图片 base64，请勿公开分享。
- `/print` 状态作用于本 AstrBot 实例。插件重启后默认关闭。
