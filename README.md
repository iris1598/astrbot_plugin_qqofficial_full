# astrbot_plugin_qqofficial_full

**QQ Official Full Adapter** — 基于 QQ 开放平台，为 AstrBot 提供完整 QQ Bot 接入能力的平台适配器插件。

支持 **WebSocket Gateway** 与 **HTTP Webhook** 两种事件接收方式，覆盖群聊、频道、私聊三大场景，实现丰富媒体类型的收发、流式输出、大文件分片上传等能力。

> 插件版本：v0.5.0 | 作者：OpenCode

---

## 功能特性

### 连接方式
| 模式 | 适配器 ID | 说明 |
|------|-----------|------|
| WebSocket Gateway | `qq_official_full` | 通过 WebSocket 长连接接收事件推送，支持自动重连 |
| HTTP Webhook | `qq_official_full_webhook` | 通过 HTTP 回调接收事件，支持 unified 和独立两种模式 |

### Agent 技能与工具（移植自 OpenClaw skills/tools）
| 名称 | 类型 | 说明 |
|------|------|------|
| `qqbot_platform_api` | LLM 工具 | QQ 开放平台统一 HTTP API 网关，自动填充鉴权 Token；支持 GET/POST/PUT/PATCH/DELETE 代理任意 OpenAPI 路径（含路径安全校验与结构化错误返回） |
| `qqbot-channel` | Agent Skill | QQ 频道（Guild/Channel）管理技能：子频道/成员/公告/论坛/日程接口速查与操作流程，附 `references/api_references.md` 完整参数参考 |
| `qqbot-remind` | Agent Skill | 提醒技能：强制「必须调用工具而非口头承诺」规则、时间换算指引、note 提醒模板、cron 速查表、AI 决策指南与回复模板（基于 AstrBot **内置** `future_task` 工具，插件不再单独提供提醒工具；需开启 `provider_settings.proactive_capability.add_cron_tools`，默认开启） |

> 技能由 AstrBot 原生插件技能机制自动发现（`skills/<name>/SKILL.md`），在 WebUI「技能」页可开关；工具的完整 JSON Schema（enum/required）经 `context.add_llm_tools()` 注册。
> `qqbot-upgrade` 技能为 OpenClaw npm 自升级专用，未移植——AstrBot 插件请通过插件管理器更新。

### 消息场景
| 场景 | 说明 |
|------|------|
| 群聊 (`group`) | QQ 群消息收发，基于 v2 API (openid) |
| 频道 (`channel`) | QQ 频道子频道消息收发 |
| 频道私信 (`dm`) | 频道内直接消息 |
| 好友/单聊 (`c2c`) | QQ 好友私聊，基于 v2 API |

### 消息类型支持
| 类型 | 说明 |
|------|------|
| 文本 | 纯文本消息 |
| @提及 | 解析 `@user` 和 `@all` |
| 回复 | 引用回复消息 |
| 图片 | 自动上传媒体文件，支持 file_info 透传 |
| 语音 | 自动上传，支持 Silk 格式转换 |
| 视频 | 自动上传媒体文件 |
| 文件 | 自动上传并携带文件名 |
| Markdown | 原生 Markdown 消息（支持失败自动降级为纯文本） |
| 按钮/键盘 | 通过 JSON 组件解析 `keyboard` 字段 |
| 流式消息 | C2C 场景下的流式逐字输出 |
| 输入状态 | C2C 场景下发送「输入中」提示 |

### 高级能力
- **大文件分片上传**：超过 20MB 的文件自动分片、并发上传
- **Markdown 降级**：当 QQ 拒绝原生 Markdown 时自动降级为纯文本重试
- **自动重连**：WebSocket 断线后指数退避重连（1s ~ 60s），会话状态智能恢复
- **消息撤回**：支持群聊和 C2C 消息删除
- **会话持久化**：会话信息自动保存到 JSON 文件
- **频道管理 API**：频道/子频道列表、成员查询等

### 移植自 openclaw-qqbot 的能力
- **流式控制器**：QQ 流式 API 要求已下发文本前缀不可变。控制器实现
  IDLE→STREAMING→DONE/FAILED 状态机：检测模型输出「新回复」（长度回退）时
  自动收尾当前气泡并开新流；前缀被改写时合并尾部继续追加；发送失败且零分片
  时自动降级为静态消息兜底
- **被动回复限额 + 主动降级**：QQ 被动回复有次数限制（默认 4 次/消息、
  msg_id 1 小时过期）。超限时自动去掉 `msg_id`/`message_reference`，
  降级为主动消息发送，避免回复被平台静默丢弃
- **出站文本清理**：发送前剥离 `<thinking>`、`` `think` ``、
  `<system-reminder>` 等模型推理/框架脚手架标签，防止内部内容泄漏到 QQ 消息
- **SSRF 防护**：以 URL 方式上传媒体、探测图片尺寸前，校验目标协议并解析
  DNS，拦截指向内网/回环/链路本地/云元数据端点的地址（QQ 官方域名白名单放行，
  重定向逐跳校验）
- **Markdown 图片尺寸提示**：发送 Markdown 消息前通过 HTTP Range 请求探测
  图片头部（PNG/JPEG/GIF/WebP），将图片标签重写为 `![img #宽x高](url)`
  格式，优化 QQ 客户端渲染比例（结果缓存 1 小时）
- **msgId 缓存**：按目标缓存最近 10 条入站 msgId（群 5 分钟 / C2C 30 分钟
  TTL），主动发送时自动借用未过期 msgId 转为被动回复，规避主动消息频控
- **引用索引持久化**：JSONL + LRU（上限 5 万条、超限自动 compact）存储
  REFIDX 键 ↔ 消息内容映射；出站成功（含流式收尾）自动登记 `ext_info.ref_idx`，
  入站同时解析 `message_scene.ext` 的 `msg_idx`/`ref_msg_idx` 并登记自身索引，
  引用只带 key 不带内容时回查还原，换设备引用也能解析
- **按钮交互事件路由**：`INTERACTION_CREATE` 先 ACK 再包装为 AstrBot 事件
  （`Json` 组件 + `qq_interaction` extra）投递到事件流，插件可监听按钮点击
- **Table-aware 长文本分块**：超过 5000 字符的出站文本按行边界自动拆分，
  GFM Markdown 表格（表头+分隔行）保持完整不被切断
- **语音降级发送**：语音（SILK）上传失败时自动改以文件类型发送，避免消息丢失
- **Data URL 大小限制**：base64/data URL 媒体源解码后超过约 20MB 直接拒绝（对齐官方 SDK 单次上传上限）
- **上传重试策略**：upload_prepare / 单次上传 / 分片 PUT / upload_part_finish /
  complete 按官方 SDK 策略对瞬态 5xx 指数退避重试；`40093001` 进入持久重试循环
  （1s 间隔、2 分钟超时）；`40093002` 报告当日上传限额；分片请求恒带 `file_name`
  （缺失会触发平台 500 call inner proxy error）；200MB 平台硬限制前置检查
- **Webhook 入站防护**：固定窗口限流（60s/600 次/IP）、1MB body 上限、
  JSON Content-Type 强制校验、事件 ID 去重；op:0 分发转后台执行，
  ACK 立即返回（避免 QQ 回调超时重推）
- **自动输入提示**：收到 C2C 消息时自动发送「正在输入」状态（失败静默忽略）

---

## 安装

### 方式一：AstrBot 管理面板
1. 在 AstrBot Web 管理面板中进入「插件管理」
2. 添加本插件（源码或市场安装）
3. 在「平台适配器」中添加对应的适配器

---

## 配置

### WebSocket Gateway 模式 (`qq_official_full`)

在 AstrBot 平台配置中添加适配器，配置项如下：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `appid` | string | `""` | QQ Bot 的 AppID |
| `secret` | string | `""` | QQ Bot 的 Secret（密码字段） |
| `is_sandbox` | bool | `false` | 是否使用沙箱环境 |
| `use_markdown` | bool | `true` | 是否优先使用原生 Markdown 消息 |
| `intents` | list | `["guilds", "guild_members", "public_messages", "public_guild_messages", "direct_message", "interaction"]` | 网关监听的事件类型别名（对齐 openclaw `FULL_INTENTS` 默认） |
| `intent_mask` | string | `""` | 原始 intent 位掩码（覆盖 `intents`） |
| `api_base_url` | string | `""` | API 基础 URL 覆盖（默认 prod: `https://api.sgroup.qq.com`，sandbox: `https://sandbox.api.sgroup.qq.com`） |
| `gateway_url` | string | `""` | WebSocket 网关 URL 覆盖（默认 `wss://api.sgroup.qq.com/websocket`） |
| `chunked_upload_threshold` | int | `5242880` | 分片上传阈值，单位字节（默认 5MB，对齐官方 SDK 推荐值；单次 base64 上限 20MB，平台硬限制 200MB） |
| `session_store_path` | string | `""` | 会话持久化路径覆盖 |
| `url_direct_upload` | bool | `true` | 公网 URL 直传 QQ 平台自行拉取；关闭时插件先下载再 Base64 上传（对齐 openclaw `urlDirectUpload`） |
| `enable_streaming` | bool | `true` | 启用 C2C 流式回复；关闭时流式内容缓冲后整条发送（对齐 openclaw `streaming`） |
| `enable_c2c_typing` | bool | `true` | 收到私聊消息时自动发送「正在输入」状态 |
| `user_agent_suffix` | string | `""` | 追加在 HTTP User-Agent 尾部的标识（对齐 openclaw `userAgentSuffix`） |

**Intent 别名说明：**

| 别名 | 对应事件 |
|------|---------|
| `guilds` | 频道（GUILD）基础事件 |
| `guild_members` | 频道成员基础事件 |
| `public_messages` | 频道公开消息 + 群聊和 C2C 消息 |
| `public_guild_messages` | 频道公开消息 |
| `group_and_c2c` / `group_c2c` | 群聊和 C2C 消息 |
| `direct_message` / `direct_messages` / `guild_direct_message` | 频道私信 |
| `interaction` / `interactions` | 交互事件（按钮回调等） |

### Webhook 模式 (`qq_official_full_webhook`)

继承上述所有配置项，额外支持：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `unified_webhook_mode` | bool | `true` | 是否使用 AstrBot 统一 Webhook 服务 |
| `webhook_uuid` | string | `""` | 统一 Webhook UUID |
| `verify_webhook_signature` | bool | `true` | 是否验证 Ed25519 Webhook 签名 |
| `webhook_timestamp_tolerance_seconds` | int | `300` | Webhook 时间戳容差（秒） |
| `callback_server_host` | string | `0.0.0.0` | 独立 Webhook 服务监听地址 |
| `port` | int | `6197` | 独立 Webhook 服务端口 |

---

## 简单示例

### 在 AstrBot 配置中添加适配器

编辑 `astrbot_platform.json` 或通过管理面板添加：

```json
{
  "platforms": [
    {
      "type": "qq_official_full",
      "config": {
        "appid": "your_app_id",
        "secret": "your_app_secret",
        "is_sandbox": false,
        "use_markdown": true,
        "intents": ["public_messages", "direct_message", "interaction"]
      }
    }
  ]
}
```

Webhook 模式：

```json
{
  "platforms": [
    {
      "type": "qq_official_full_webhook",
      "config": {
        "appid": "your_app_id",
        "secret": "your_app_secret",
        "unified_webhook_mode": true,
        "webhook_uuid": "your_webhook_uuid"
      }
    }
  ]
}
```

---

## 架构说明

```
┌─────────────────────────────────────────────────┐
│                  AstrBot Core                    │
│  ┌───────────────────────────────────────────┐  │
│  │          Platform Adapter Layer            │  │
│  │                                             │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  │  │
│  │  │ Gateway Adapter │  │ Webhook Adapter │  │  │
│  │  │ (WebSocket)     │  │ (HTTP)          │  │  │
│  │  └────────┬────────┘  └────────┬────────┘  │  │
│  └──────────┼───────────────────┼────────────┘  │
└──────────────┼───────────────────┼───────────────┘
               │                   │
     ┌─────────▼──────┐   ┌───────▼────────┐
     │  QQ Gateway    │   │  QQ Webhook    │
     │  wss://...     │   │  HTTP Callback │
     └────────────────┘   └────────────────┘
               │                   │
     ┌─────────▼───────────────────▼────────┐
     │        QQ Open Platform API          │
     │        https://api.sgroup.qq.com     │
     └──────────────────────────────────────┘
```

插件内部组件：
- **QQOfficialClient** — REST API 客户端，封装了 token 获取、消息发送、媒体上传、频道管理等所有 HTTP 接口
- **QQOfficialFullPlatformAdapter** — WebSocket Gateway 适配器，负责 WebSocket 连接管理、心跳维持、事件分发、消息发送
- **QQOfficialFullWebhookPlatformAdapter** — Webhook 适配器，继承 Gateway 适配器，使用 HTTP 回调替代 WebSocket
- **QQOfficialFullMessageEvent** — AstrBot 消息事件，适配 QQ 消息格式

---

## 依赖

### Python 包
| 包 | 用途 |
|----|------|
| `httpx` | 异步 HTTP 客户端 |
| `websockets` | WebSocket 客户端 |
| `cryptography` | Ed25519 签名验证 |

### AstrBot 模块
- `astrbot.api.platform` — 平台适配器注册与基类
- `astrbot.api.event` — 消息事件系统
- `astrbot.api.message_components` — 消息组件（Plain, At, Image, Record, Video, File, Json, Reply）
- `astrbot.core.utils.media_utils` — 媒体文件解析
- `astrbot.core.webhook_server` — 统一 Webhook 服务

---

## 常见问题

**Q: 如何查看 WebSocket 连接状态？**
可以在 AstrBot 日志中查看适配器的连接日志。插件会在连接断开、重连、认证失败等关键节点输出日志。

**Q: 收到事件但没有触发消息处理？**
检查 `intents` 配置是否正确覆盖了所需的事件类型。例如群聊需要 `group_and_c2c` intent。

**Q: Markdown 消息发送失败？**
QQ 开放平台对原生 Markdown 有审核要求。插件已实现自动降级机制——当 API 返回「不允许发送原生 markdown」错误时，会自动转换为纯文本重试。

**Q: 大文件上传失败？**
检查 `chunked_upload_threshold` 配置。默认 20MB，可调小阈值以测试分片上传流程。

**Q: Webhook 签名验证失败？**
确保 QQ 开放平台配置的 Bot Secret 与插件配置一致。如果暂不需要验证，可设置 `verify_webhook_signature: false`。

---

## 许可证

本项目基于 MIT 许可证开源。
