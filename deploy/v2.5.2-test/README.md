# TVG v2.5.2 NAS 测试部署

这是独立测试栈，不会复用或覆盖生产容器：

- Compose project：`tvg-v252-test`
- 容器名：`tvg-v2-5-2-test`
- 默认端口：`18787`（可设 `TGDL_TEST_PORT`）
- 数据卷：`tvg-v2-5-2-test-data`
- 镜像：`tvg-v252-test:bf6e067`
- 构建号：`bf6e067`

## 启动

在本目录执行：

```bash
TGDL_TEST_PORT=18787 ./deploy.sh
```

首次启动后读取一次性初始化令牌：

```bash
docker compose -p tvg-v252-test -f compose.yaml exec tvg-v2-5-2-test cat /data/.setup-token
```

浏览器访问 `http://NAS地址:18787`，输入令牌并设置 Web 管理员密码。

## 停止 / 清理

只停止测试栈：

```bash
docker compose -p tvg-v252-test -f compose.yaml down
```

连同测试数据卷一起清理（不要在生产栈执行）：

```bash
docker compose -p tvg-v252-test -f compose.yaml down -v
```

## 兼容性

- RPC 方法集、任务 JSON、导出包格式与 v2.5.1 一致。
- 生产 `compose.yaml` 的 `tg-video-downloader` 容器、`8787` 端口与
  `tg-video-downloader-data` 卷均不被本测试栈引用或修改。
