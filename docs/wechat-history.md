# 本机微信聊天记录 MCP（可选）

该功能只面向当前机器上的私人、非商用使用，默认构建保持关闭。它只读取由环境变量
指定的唯一账户（单账户白名单，见「环境变量」一节），并在首次读取时验证自身资料
包含配置的身份标记。任何不匹配都会停止，不会尝试另一个账户。

实现参考了 Apache-2.0 许可的
[`huohuoer/wechat-cli`](https://github.com/huohuoer/wechat-cli/tree/a3789232d4f79bf0b30634d9dadbce71e4acd601)，
但增加了账户白名单、日志脱敏、逐页 HMAC、WAL 提交边界、只读快照及 tmpfs
明文缓存。CipherTalk 不作为运行依赖，因为它的原生数据库核心没有 Linux 构建。

## 安装与首次扫描

本机的 `compose.local.yml` 已把 `INSTALL_WECHAT_HISTORY` 打开。重新构建并启动：

```powershell
docker compose up -d --build
```

在微信界面手动切换到目标旧账户（即 `WECHAT_HISTORY_ACCOUNT_DIR` 指定的账户），
并保持微信运行，随后执行：

```powershell
pwsh -NoProfile -File .\integrations\wechat-history\scripts\keyscan.ps1
```

扫描权限只存在于一次性的 `history-keyscan` profile。主微信容器没有
`SYS_PTRACE`，密钥保存在独立 named volume 中，权限为 `0600`。微信重启、升级、
重新登录或工具报告 `KEY_STALE` 后，需要显式重新扫描。

密钥目录权限为 `0700`、密钥文件为 `0600`，所有者是主容器中的非特权 `abc`
用户。当前不是目标旧账户时，扫描器会在读取任何进程内存前返回
`TARGET_ACCOUNT_NOT_ACTIVE`。

## 环境变量

账户身份不在源码或镜像里，全部通过环境变量注入。主容器 `wechat-selkies` 服务的
`environment`（以及独立的 wechat-insights 容器各自配置）需要设置：

- `WECHAT_HISTORY_ACCOUNT_DIR`：目标账户目录名，即 `SOURCE_ROOT`（默认
  `/history-source/xwechat_files`）下的单个目录名。这是单账户白名单的核心：
  密钥校验、登录活跃度检查和所有读取都只认这一个目录；机器上同时存在其他
  微信账户目录时，这个白名单保证工具绝不触碰它们。
- `WECHAT_HISTORY_USERNAME`：账户自身的 wxid（contact.db 中自己那条记录的
  username），用于把“自己”从会话列表里排除，并把发出的消息标记为 outgoing。
- `WECHAT_HISTORY_IDENTITY_TOKENS`：逗号分隔的身份标记。首次读取时把自身资料的
  昵称、备注、别名拼起来，必须包含其中任意一个才放行，防止密钥被拿到别的账户上
  使用。

示例（写入部署用的 compose 文件；这是私有配置，不要提交到公开仓库）：

```yaml
environment:
  - WECHAT_HISTORY_ACCOUNT_DIR=wxid_example_0000
  - WECHAT_HISTORY_USERNAME=wxid_example
  - WECHAT_HISTORY_IDENTITY_TOKENS=姓名,昵称,别名
```

这三个变量不是便利性设置，而是安全边界：任一个缺失或留空，任何实际读取都会以
`IDENTITY_UNCONFIGURED` 错误失败，并点名缺失的变量。工具绝不会回退到默认账户、
自动发现账户目录或降级为读取全部 —— 未配置时的唯一行为就是拒绝服务。

## MCP 配置

MCP 使用 stdio，不监听任何网络端口。Claude Code、Codex 或其他 MCP 客户端都应把
以下命令配置为服务器启动命令：

```text
pwsh -NoProfile -File <项目根目录>\integrations\wechat-history\scripts\mcp.ps1
```

例如客户端使用 JSON 配置时：

```json
{
  "mcpServers": {
    "wechat-history": {
      "command": "pwsh",
      "args": [
        "-NoProfile",
        "-File",
        "<项目根目录>\\integrations\\wechat-history\\scripts\\mcp.ps1"
      ]
    }
  }
}
```

提供的工具为：

- `health_check`
- `list_sessions`
- `get_messages`
- `search_messages`
- `prepare_reply`

不存在 `send_message`。`prepare_reply` 只会把不超过 4000 字符的草稿填入输入框，
不会模拟 Enter。使用者必须在微信中核对账户、会话和内容后手动发送。

## 安全与限制

- 源数据库通过 `/history-source` 的只读挂载读取；不会修改 `/config`。
- DB 与 WAL 复制前后状态不一致时重试三次，之后返回 `SOURCE_BUSY`。
- 解密文件只存放在 512 MiB tmpfs，MCP 进程退出后立即清理。
- 图片、语音、视频和文件只返回消息类型及安全元数据，不导出媒体。
- 搜索会流式解压文本，但不建立持久索引；媒体内容不参与搜索。
- 不后台轮询、不自动回复、不跨账户读取、不发布包含本功能的公共镜像。

同类微信数据库项目曾收到腾讯的
[DMCA 通知](https://github.com/github/dmca/blob/master/2026/07/2026-07-13-wechat.md)。
使用者需自行确认当地法律、微信条款及对聊天参与者隐私的合规要求。

## 验证

运行容器内测试：

```powershell
pwsh -NoProfile -File .\integrations\wechat-history\scripts\test.ps1
```

真实草稿验证只应使用“文件传输助手”，输入唯一测试文本，确认内容停留在输入框且
没有发送，然后手动清空。自动化测试不会触发任何真实发送。
