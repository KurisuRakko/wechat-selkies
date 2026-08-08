# 第三方组件

## Apache ECharts

看板的图表使用 [Apache ECharts](https://echarts.apache.org/)，Apache License 2.0。
构建镜像时从 npm registry 下载 `echarts@5.6.0` 的发布包并校验 sha256，只取出
`dist/echarts.min.js` 与其 `LICENSE`，分别放到镜像内的
`wechat_insights/static/vendor/echarts.min.js` 与 `vendor/LICENSE.echarts`。
运行时不请求任何外部 CDN。

要升级版本，改 [`Dockerfile`](Dockerfile) 里的 `ECHARTS_VERSION` 与
`ECHARTS_SHA256` 两个构建参数即可。

## wechat_history

解密、只读快照、账户白名单与私聊过滤复用同仓库的
[`integrations/wechat-history`](../wechat-history)，其上游出处与许可见该目录下的
[`THIRD_PARTY_NOTICES.md`](../wechat-history/THIRD_PARTY_NOTICES.md)。
