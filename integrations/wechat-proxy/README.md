# 域名入口（wechat-proxy）

给两个站点绑域名，同时保持「不挂 Tailscale 就访问不了」：

| 域名 | 上游 | 内容 |
| --- | --- | --- |
| `wechat.<你的域名>` | `wechat-selkies:3000` | 微信主界面（selkies 远程桌面） |
| `relationship.<你的域名>` | `wechat-insights:8300` | 关系洞察看板 |

一个 `caddy:2-alpine` 容器，独立 compose 项目，按 `Host` 头分流。主容器与看板容器
的镜像、编排、卷、端口全都不动；这个代理停掉就回到「IP 加端口直连」的原状态。

## 访问控制：为什么不需要额外规则

域名解析到 Tailscale 地址（`100.64.0.0/10`，CGNAT 段，公网不可路由），并且 443 只
绑定在回环与 Tailscale 地址上。不在 tailnet 里的人即使解析到了这个地址，路由层就走
不通——拦人的是网络，不是一条能被 `X-Forwarded-For` 伪造的规则。看板自己的
`INSIGHTS_AUTH_TOKEN` 鉴权照旧生效，代理不会绕过它。

代价是你的 Tailscale 地址会出现在公开 DNS 里。它暴露的信息只是「这台机器在某个
tailnet 上」；没有 tailnet 成员身份，拿到这个地址仍然什么都做不了。

## DNS 记录

在 DNS 服务商加两条 **A 记录**，都指向 `tailscale ip -4` 的输出：

| 类型 | 名称 | 值 | 代理 |
| --- | --- | --- | --- |
| A | `wechat` | `100.x.x.x` | **关闭**（DNS-only） |
| A | `relationship` | `100.x.x.x` | **关闭**（DNS-only） |

Cloudflare 用户注意：必须是**灰云**。开橙云等于让 Cloudflare 的边缘节点去连
`100.x.x.x`，那是它不可能到达的地址，结果是每个请求都 522。

## 证书：Caddy 本地 CA

`tls internal` 让 Caddy 用内置的本地 CA 签发证书，不需要公网可达、不需要把 DNS API
令牌交给容器。**要在每台用来访问的设备上装一次这个 CA 的根证书**，否则浏览器每次
告警，而且——这一点比告警重要——**Chrome / Firefox 拒绝在证书有错的源上注册
Service Worker，点「继续访问」也不行**，主容器的 PWA 推送会直接失效。装好根证书后
证书就是有效的，安全上下文成立，推送、剪贴板这些功能才正常。

导出根证书：

```bash
docker cp wechat-proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

安装：

- **macOS**：双击导入「钥匙串访问 → 系统」，然后在该证书的「信任」里把「使用此证书时」
  设为「始终信任」。只导入不改信任等于没装。
- **Windows**：`certutil -addstore -f ROOT caddy-root.crt`（管理员）。
- **iOS**：用文件或邮件把 `.crt` 传过去安装描述文件，再到「设置 → 通用 → 关于本机 →
  证书信任设置」里把它打开。第二步同样不能省。
- **Android / Chrome**：从 Android 7 起，用户装的 CA 默认不被应用信任，Chrome 需要
  逐站点例外，体验最差。这类设备建议继续用 IP 加端口访问。

`/data` 卷丢了就等于换了一个 CA，所有装过的根证书全部失效，得重新导出重装——所以
compose 里那个 `wechat-proxy-data` 卷不要删。

如果哪天不想再手动装根证书，改成公网可信证书只需要换掉 `tls internal`：Let's Encrypt
的 DNS-01 验证不要求服务器公网可达，但需要一个带 DNS 写权限的 API 令牌，并且要用带
对应 DNS 插件的 Caddy 镜像（官方镜像不含插件）。

## 上游为什么走 3000

LSIO 的 selkies 基镜像上 3000 是 HTTP、3001 是它自带的自签 HTTPS。这一跳完全在宿主机
的 docker 网络内、不出网卡，套两层 TLS 只会多一次加解密，还多出「要不要验上游证书」
这个没有好答案的问题。

如果发现 3000 被强制跳转到 HTTPS（不同版本行为可能不同），把 Caddyfile 里那行改成：

```caddyfile
reverse_proxy https://wechat-selkies:3001 {
	transport http {
		tls_insecure_skip_verify
	}
}
```

## 上线与验收

```bash
docker compose -f compose.proxy.yml config
docker compose -f compose.proxy.yml up -d
```

必须逐条过：

1. `docker compose -f compose.proxy.yml ps` 里状态是 `Up`，且日志没有重启循环
   （配置写错时 Caddy 直接退出，不会静默代理到错的地方）。
2. 从 tailnet 内的设备访问两个域名，都是 HTTPS 且能打开对应页面。
3. **局域网不可达**：从同一局域网、不在 tailnet 的设备访问宿主机的局域网地址
   `443`，必须连接被拒绝（`Test-NetConnection <局域网IP> -Port 443` 应为 False）。
4. 看板域名在不带 token 时返回 401，带 `?token=` 后能进且地址栏里不再留 token。
5. 装好根证书的设备上，浏览器无告警，`window.isSecureContext` 为 `true`。

回滚：`docker compose -f compose.proxy.yml down`。两个站点原本的端口直连方式全程可用，
不受影响。

## 修改配置后

`admin off` 关掉了热重载接口，改完 `Caddyfile` 要重启容器才生效：

```bash
docker compose -f compose.proxy.yml restart
```

只影响这个代理，两个业务容器不受影响。
