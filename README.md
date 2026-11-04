# TrackCloudflareSpeed · NAS 域名优选

完整的独立 Docker 源码部署项目：优选引擎、管理界面、DNS 服务、过期覆盖回收、连接对照和历史分析。默认空站点、观察模式，不连接任何下载器。

当前便携版 `1.10-portable.2`，Linux amd64。ARM64 尚未提供经过校验的工具链，不支持直接切换平台构建。

## 部署

需要 Docker Engine 和 Compose v2（支持 `service_completed_successfully`）。在新目录部署，不要覆盖已有运行目录。

```sh
git clone https://github.com/misakashiny/TrackCloudflareSpeed.git
cd TrackCloudflareSpeed
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

打开 `http://127.0.0.1:9078/`。构建从 XIU2 官方 Release 获取固定版本 CloudflareSpeedTest 2.3.5，并核对压缩包与二进制 SHA256；下载失败时构建会终止，不跳过校验。可先执行 `python tools/fetch_cfst.py` 准备离线缓存，再构建。Python 后端只使用标准库。

`init` 只初始化项目自己的 Docker 卷，后续服务以 1000:1000 运行。使用 bridge 网络，不挂载 Docker socket、宿主系统 DNS、媒体或下载目录。Compose 项目名默认为 trackcloudflare；同机运行多个实例须用不同项目名及端口。

## 访问与操作

在“站点”中添加自己有权使用的站点域名；输入域名即可，不填写账号、Cookie、Tracker passkey。新增启用站点会请求一次有预算的检测。初始没有站点时只维护心跳和空 DNS 输出。

要从局域网访问，修改 `.env`：设置 `UI_PUBLISH_ADDRESS` 为服务器的局域网地址、`UI_PUBLIC_HOST` 为浏览器实际使用的 `地址:端口`，并给 `UI_CLIENT_NETWORK` 追加自己的局域网 CIDR。保留 Docker bridge 来源范围；自定义 Docker 地址池时同步修改。不要暴露到公网。此服务没有账号登录，使用网络范围、Host/Origin 校验和 CSRF 保护，适用于可信管理网。健康检查会使用配置的 Host。

全局模式位于 `config/settings.json` 的 `speed.mode`：`shadow` 仅观察；`canary` 仅应用 allow_domains 白名单；`auto` 允许通过原有证书、公开归属、稳定性、收益和确认窗口门槛的域名发布。界面可保存模式，运行值与保存值分别显示。可用环境变量 `EXCLUDED_KEYWORDS` 为 worker、dashboard、guard 同时配置排除关键词；默认没有个人站点偏好。

## DNS 如何生效

默认 DNS 只监听宿主 `127.0.0.1:5353`，同时支持 TCP/UDP；上游为 `DNS_UPSTREAM`。可检查：

```sh
dig @127.0.0.1 -p 5353 example.org A
dig @127.0.0.1 -p 5353 example.org A +tcp
```

优选只生成逐域名 hosts 覆盖。**启动容器不会自动修改路由器、系统或下载器的 DNS。** 确认观察结果后，再将指定客户端的 DNS 转发到该服务；多数客户端需要 53 端口，可自行调整 `DNS_PUBLISH_PORT` 和绑定地址，先确认无现有 DNS 服务冲突。回滚时先把客户端切回原 DNS。

dnsmasq 动态监视共享 DNS 目录，禁用上游缓存、本地 TTL 30 秒；guard 每 15 秒撤回失效覆盖，worker 心跳超过 180 秒会撤回其覆盖。guard 和 dns 服务须一起运行；整套容器停止后 DNS 端点不可用，客户端应配置可靠回退解析器。跨轮确认、证书验证及普通非 CF 域名保护仍由引擎执行。

## 数据、升级和恢复

只读的 `config/` 保存公开参数，Docker 命名卷 `runtime` 保存站点、策略、日志、历史和 DNS 输出，重建容器不会丢失。日志和诊断包可能包含私人站点及使用记录，分享前检查。

修改配置后使用 `docker compose up -d`；更新源码后重新 build 并 up。升级前备份 config、.env 和项目 runtime 卷。停止用 `docker compose down`，不要加 `-v`；恢复旧源码/镜像与兼容备份后重新启动。不要把新项目指向旧 NAS 服务的卷或目录。

## 传输报告与高级试验

连接改善与 PT 文件传输收益分开统计。此独立项目没有默认下载器连接、真实传输样本或限速执行器，因此上传能力、文件收益、下载器 Tracker 状态会显示数据不足。现在可以启用可选的通用 QB/TR 只读采集器，自行添加多台下载器，凭据从私有文件、环境变量或 Docker secrets 读取。设置步骤见 [采集器说明](docs/COLLECTOR.md)。采集器不会设置限速或操作任务。

NAS 专用的 MoviePilot 凭据读取、种子港额度执行、宿主 systemd、上行短测和任务试验脚本不适合移植到任意机器，未作为自动服务打包。高级试验 API 明确返回 501，不会排队等待一个不存在的执行器。管理页面保留分析视图和解释。这套 Docker 项目可以独立完成域名优选和 DNS 服务，但不声称具备通用的下载器限速调度。

## 验证

```sh
python -m unittest test_collector test_public_dns test_reports_health test_effective_policy test_speed
python tools/smoke.py
node tests/ui.test.cjs
```

合成测试和短时 HTTP 冒烟不证明真实文件传输提速。发布验收见 `docs/VALIDATION.md`。

## 第三方

CFST 是独立执行的上游程序，固定来源、哈希及许可见 `docs/THIRD_PARTY.md`。仓库不存放其二进制。Cloudflare 公网 IPv4 段来自 https://www.cloudflare.com/ips-v4/ ，可按官方记录更新；不要加入内网、代理 Fake-IP 或未经核对的 IP 段。
