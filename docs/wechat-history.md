# 本机微信聊天记录 MCP（可选）

该功能只面向当前机器上的私人、非商用使用，默认构建保持关闭。它固定读取旧账户
`wxid_bo75…7498`，并在首次读取时验证自身资料包含“杨博文”、`Spencer` 或
`KurisuRakko`。任何不匹配都会停止，不会尝试另一个账户。

实现参考了 Apache-2.0 许可的
[`huohuoer/wechat-cli`](https://github.com/huohuoer/wechat-cli/tree/a3789232d4f79bf0b30634d9dadbce71e4acd601)，
但增加了账户白名单、日志脱敏、逐页 HMAC、WAL 提交边界、只读快照及 tmpfs
明文缓存。CipherTalk 不作为运行依赖，因为它的原生数据库核心没有 Linux 构建。

## 安装与首次扫描

本机的 `compose.local.yml` 已把 `INSTALL_WECHAT_HISTORY` 打开。重新构建并启动：

```powershell
docker compose up -d --build
```

在微信界面手动切换到“杨博文 Spencer KurisuRakko”旧账户，并保持微信运行，随后执行：

```powershell
pwsh -NoProfile -File .\integrations\wechat-history\scripts\keyscan.ps1
```

扫描权限只存在于一次性的 `history-keyscan` profile。主微信容器没有
`SYS_PTRACE`，密钥保存在独立 named volume 中，权限为 `0600`。微信重启、升级、
重新登录或工具报告 `KEY_STALE` 后，需要显式重新扫描。

密钥目录权限为 `0700`、密钥文件为 `0600`，所有者是主容器中的非特权 `abc`
用户。当前不是目标旧账户时，扫描器会在读取任何进程内存前返回
`TARGET_ACCOUNT_NOT_ACTIVE`。

## MCP 配置

MCP 使用 stdio，不监听任何网络端口。Claude Code、Codex 或其他 MCP 客户端都应把
以下命令配置为服务器启动命令：

```text
pwsh -NoProfile -File C:\projects\wechat-selkies\integrations\wechat-history\scripts\mcp.ps1
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
        "C:\\projects\\wechat-selkies\\integrations\\wechat-history\\scripts\\mcp.ps1"
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
