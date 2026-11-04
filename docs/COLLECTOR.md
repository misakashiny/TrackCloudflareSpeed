# 通用 QB / TR 只读采集

采集器是可选 Compose `collect` profile，默认不运行。支持最多 16 个独立端点，可混合配置多台 qBittorrent Web API v2 和 Transmission 4.0.x 兼容 RPC。Transmission 4.1+ 新 JSON-RPC 协议尚未单独验证，接口不兼容时显示采集失败，不自动修改下载器。

## 添加自己的下载器

在全新部署目录中执行：

```sh
mkdir -p private/secrets
cp config/downloaders.example.json private/downloaders.json
```

编辑 `private/downloaders.json`，每个 `clients` 条目包含：

- `id`：唯一且稳定的标识，如 `qb_main`。允许小写字母开头，后接字母、数字、下划线、连字符，最多 40 字符。
- `name`：页面显示名称。
- `type`：`qb` 或 `tr`。
- `url`：QB 填 Web UI 根地址（可有反向代理路径前缀）；TR 填完整 RPC 地址，如 `http://主机:9091/transmission/rpc`。不能带账号、密码、查询参数或片段。
- `enabled`：是否采集。停用后下一轮从当前报告移除，历史统计保留。
- `username_file` / `password_file`：容器内凭据文件路径。默认示例从 `/private/secrets/` 读取。

请在本机编辑器中分别创建 `private/secrets/qb_user`、`qb_password`、`tr_user`、`tr_password`，每个文件只写对应账号或密码。示例不含实际账号、密码。不要把凭据放在 URL、源码、命令行参数或聊天中。允许文件末尾一个换行，凭据自身不能包含换行。

Linux 中采集器以 UID/GID 1000 运行。仅给它读取权限，例如将 **本项目新建的 private 目录** 归属设为 1000:1000，目录 700、文件 600；不要修改已有下载器目录权限。该目录只挂载到 collector，界面和优选引擎无法读取凭据。

```sh
docker compose build
docker compose --profile collect up -d
docker compose --profile collect ps
```

在“优选效果 → 实际传输”查看。修改、增加或停用下载器后，无需重建镜像，下一轮读取新配置。默认每 60 秒采集一次，最多并行 4 台，每台 HTTP 操作有 12 秒总预算、单次连接/读取等待最多 3 秒、响应最多 16 MiB；慢接口可能被本轮跳过并标记失败。

配置任何一项格式无效会拒绝整份配置，清除当前客户端快照并标记配置不可用，保留历史统计。单台连接/认证失败只影响该台，其他下载器继续采集。采集器健康检查表示配置有效且采集循环更新，不代表每台下载器都连接成功。

Docker bridge 中 `127.0.0.1` 是采集器自身。其他容器需通过可达地址、共同 Docker 网络服务名或宿主机可达地址访问；不要为了采集自动改用 host 网络或关闭下载器认证。对原下载器的 IP 白名单、TR RPC 白名单等限制，按自己的网络情况配置。HTTP 不加密，可信网外使用证书有效的 HTTPS；采集器不跳过证书验证、不跟随重定向、不继承代理环境变量。

## 环境变量与 Docker secrets

每个凭据可改为 `username_env` / `password_env`，值填写环境变量名称；同一个凭据必须在 `_env`、`_file` 中二选一，不能同时配置。真实值由私有 Compose override 的 `env_file` 注入，或在运行服务时传入。不要将 `.env` 当作自动注入任意变量的环境文件；它通常只用于 Compose 插值。

也可以在自己的 Compose override 中声明 Docker secrets 并只挂载给 collector，再将 `_file` 指向 `/run/secrets/对应名称`。此项目不替你创建、保存或上传真实 secrets。只有在下载器本身明确允许免认证时才使用 `"auth": "none"`，采集器不会修改认证设置。

`private/`、`.env*`、runtime 都被 Git 忽略；Docker 构建上下文默认拒绝这些目录。其他文件名的 override、env_file 或 secret 文件也应放进 private，避免误提交。

## 读取范围和数据保护

QB 只调用登录和 4 类 GET：transfer/info、torrents/info、torrents/trackers、sync/maindata。每轮每台轮换读取最多 6 个任务的 Tracker 状态。TR 只调用 session-get、session-stats、torrent-get，包含必要的 409 会话协商。

没有开始/暂停/删除/校验任务、重新汇报、设置限速或修改配置的调用。高级额度/传输试验仍不可用，API 继续返回 501。

只持久化速率、数量、累计计数及站点清单中已启用域名的汇报统计；不保存任务名、保存路径、任务哈希、完整 Tracker URL、passkey、原始错误消息、登录响应、Cookie 或账号密码。某个 Tracker 没有被采集，不代表它正常或根本不存在。

当前快照存入 runtime 的 `state/transfer/latest.json`，报表存入 `state/reports/`。更换端点、增减客户端、累计计数回退、采集失败或断档时，不把不连续数据拼成收益。显示的传输量是现有下载器累计统计，不能据此证明域名优选让文件传输提速。

## 停止采集

```sh
docker compose --profile collect stop collector
```

停止后原始下载器与优选服务继续运行，页面会在数据过期后提示采集未更新。不要使用 `down -v` 来停止单个采集器。
