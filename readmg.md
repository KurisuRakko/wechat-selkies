# 微信聊天记录读取 Agent Prompt

将下面的提示词完整交给需要读取本机微信聊天记录的 agent，并替换其中的任务参数。

```text
你正在处理本机项目 C:\projects\wechat-selkies 中的私人微信聊天记录。请仅通过项目提供的
wechat-history stdio MCP 读取记录，不要直接访问、复制或解析 /config 下的数据库，也不要输出
数据库密钥、完整 wxid、内部 session_id 或数据库路径。

MCP 启动命令：
pwsh -NoProfile -File C:\projects\wechat-selkies\integrations\wechat-history\scripts\mcp.ps1

任务参数：
- 目标会话：<联系人或群名>
- 读取范围：<默认“最近 100 条”；也可以填写时间范围或“全部可用记录”>
- 用户目的：<摘要、查找某件事、理解上下文等>

必须遵守以下边界：

1. 只允许读取 MCP 已固定并验证的单一旧账户。首先调用 health_check()，只有同时满足
   identity_verified=true、key_status=valid、database_status=readable 和
   snapshot_status=ready 时才继续。任何检查失败都应停止并报告错误码，不得尝试其他账户。
2. 调用 list_sessions(query="<联系人或群名>", limit=20) 定位会话。优先要求
   display_name 完全相等：没有匹配时如实报告；匹配超过一个时停止并请用户消歧，绝不猜测。
3. 唯一匹配后，在工具内部使用 session_id 调用 get_messages()，但不得在回复、日志或临时文件中
   展示 session_id。默认 limit=100；只有用户明确要求更早或全部记录时，才使用 next_cursor 作为
   before_cursor 继续分页，直到 next_cursor 为空或达到用户指定范围。
4. 需要按关键词或日期查找时，使用 search_messages(query, session_id?, since?, until?, limit,
   cursor?)。尽量把 session_id 和时间范围限定到用户授权的会话，不要顺便搜索其他人的聊天。
5. 图片、语音、视频和文件在 v1 中只读取类型及安全元数据，不尝试导出、播放或转写媒体。
6. 默认给出简洁摘要，包括：实际读取条数、覆盖时间、主要话题、关键来回、最后一条双方消息、
   是否还有更早分页。时间戳应注明时区。除非用户明确要求，不要逐条复述全部私聊原文。
7. 不建立持久明文缓存，不把聊天正文写入仓库或磁盘，不在回答中泄露完整账户标识、密钥、
   数据库路径或其他未获授权会话的内容。
8. 本任务默认只读。不要调用 prepare_reply；只有用户明确要求“填写草稿”时才可以调用它。
   即便调用，也只能把内容填入微信输入框，绝不模拟 Enter、点击发送或自动回复，并提醒用户在
   微信界面核对账户、会话和正文后手动发送。
9. 如果 MCP 无法启动或提示依赖缺失，停止读取并说明可选历史功能未正确构建；不得绕过 MCP
   直接转储数据库或密钥。正确的本机构建参数是 INSTALL_WECHAT_HISTORY=true。

建议执行顺序：
health_check → list_sessions（确认唯一匹配）→ get_messages / search_messages → 脱敏摘要。

最终回复应明确说明实际读取范围、是否读完当前可用记录，以及确认没有填写或发送消息。
```

## 示例任务参数

```text
- 目标会话：池鱼羁鸟
- 读取范围：最近 100 条
- 用户目的：概括最近聊天内容和当前待回复的上下文
```

该 MCP 仅使用 stdio，不监听网络端口。可用工具为 `health_check`、`list_sessions`、
`get_messages`、`search_messages` 和仅用于草稿填写的 `prepare_reply`；不存在
`send_message`。
