# DockerMirrorFlow

> 多源聚合，流式加速 —— 多 Registry 镜像代理加速服务

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0) [![Python](https://img.shields.io/badge/Python-3.11+-green.svg)](https://www.python.org/) [![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://www.docker.com/)

DockerMirrorFlow 是一个轻量的 Docker Registry 代理，支持 Docker Hub、GHCR、GCR、Quay、MCR 等多种镜像仓库。
自动从公共镜像源拉取可用节点，定时健康检查，按延迟智能路由，为容器拉取提速。

---

## ✨ 特性

- 🚀 **多源聚合**：定时从 `status.anye.xyz` 自动拉取免费镜像节点
- 🎯 **智能路由**：按延迟择优，支持 `ghcr/...` 与 `ghcr.io/...` 两种前缀形式
- 🔄 **自动 fallback**：一个节点失败自动切换下一个，403 / 5xx / 超时均会自动切换
- 📡 **主动跟随重定向**：上游返回 307 时由代理跟随到 CDN，客户端无需自行处理
- 🔥 **熔断分级**：超时 / 403 / 5xx 使用不同的熔断时长，避免反复踩坑
- 🧊 **blob 级失败缓存**：同一节点对同一 blob 失败后短期不再尝试
- 🧲 **镜像级节点粘性**：同一镜像后续请求优先复用最近成功的节点
- 💓 **严格健康检查**：按 registry 类型使用不同的测试镜像，避免误判
- 🔒 **手动禁用持久化**：手动禁用的节点在重新拉取后保持禁用
- 📦 **自定义节点持久化**：YAML 声明式配置，重启不丢失
- 📊 **流量统计**：按天、按节点记录拉取流量与拉取历史
- ⚙️ **YAML 配置**：所有参数集中管理，支持 Web 后台在线编辑并自动重载
- 🖥️ **Web 管理**：Vue 3 + Tailwind + ECharts 现代化界面
- 🔑 **Token 缓存**：按 realm/service/scope 缓存上游 token，减少认证请求

---

## 🚀 快速开始

### 方式一：docker-compose 部署（推荐）

创建 `docker-compose.yml`：

```yaml
services:
  dockermirrorflow:
    image: crpi-1tmphkb8hkeahev6.cn-chengdu.personal.cr.aliyuncs.com/1983shake/dockermirrorflow:latest
    container_name: dockermirrorflow
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./config:/app/config
    environment:
      - TZ=Asia/Shanghai
```

首次启动前准备配置：

```bash
mkdir -p config data
```

创建 `config/config.yaml`：

```yaml
app:
  name: "DockerMirrorFlow"
  tagline: "多源聚合，流式加速"

admin:
  user: "admin"
  pass: "change_me"    # ← 修改为你的密码

auto_fetch:
  enabled: true
  interval_minutes: 60

health_check:
  interval_minutes: 30
```

启动：

```bash
docker compose up -d
```

访问 `http://<主机IP>:8000` 进入管理后台。

### 方式二：docker run

```bash
docker run -d --name dockermirrorflow \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config:/app/config \
  -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  crpi-1tmphkb8hkeahev6.cn-chengdu.personal.cr.aliyuncs.com/1983shake/dockermirrorflow:latest
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
mkdir -p config
cp config/config.example.yaml config/config.yaml
vim config/config.yaml      # 修改 admin.pass
python -m app.main
```

---

## 📥 拉取镜像

假设代理服务运行在 `192.168.1.100:8000`。

```bash
# Docker Hub
docker pull 192.168.1.100:8000/library/nginx:latest

# GHCR
docker pull 192.168.1.100:8000/ghcr.io/owner/image:tag

# GCR
docker pull 192.168.1.100:8000/gcr.io/project/image:tag

# Quay
docker pull 192.168.1.100:8000/quay.io/org/image:tag
```

> ⚠️ **NAS 用户注意**：飞牛 / 群晖 / 威联通的 Docker 加速器**只对 Docker Hub 生效**。拉取 GHCR / GCR / Quay 镜像时，必须写成 `<proxy>:8000/ghcr.io/...` 的形式。

---

## ⚠️ HTTP 拉取错误排查

由于本代理默认以 **HTTP** 协议对外提供服务（非 HTTPS），Docker 客户端会拒绝连接，报错如下：

```
Error response from daemon: Get "http://192.168.1.100:8000/v2/": 
http: server gave HTTP response to HTTPS client
```

或：

```
Error response from daemon: Get "https://192.168.1.100:8000/v2/": 
http: server gave HTTP response to HTTPS client
```

### 解决方案：将代理加入 insecure-registries

编辑 Docker daemon 配置 `/etc/docker/daemon.json`：

```json
{
  "insecure-registries": ["192.168.1.100:8000"]
}
```

将 `192.168.1.100:8000` 替换为你的代理地址。保存后重启 Docker：

```bash
sudo systemctl restart docker
```

验证配置已生效：

```bash
docker info | grep -A 5 "Insecure Registries"
```

### NAS 平台配置位置

| 平台 | daemon.json 位置 |
|---|---|
| 群晖 DSM | `/var/packages/Docker/etc/dockerd.json` |
| 威联通 QTS | `/share/CACHEDEV1_DATA/.qpkg/container-station/etc/docker.json` |
| 飞牛 fnOS | 系统设置 → Docker → 高级设置 → insecure-registries |
| OpenMediaVault | `/etc/docker/daemon.json` |

修改后需通过面板重启 Docker 服务。

### 如果是 Docker Swarm / K8s

- **Docker Swarm**：每台节点都要单独配置 `daemon.json`
- **K8s (containerd)**：编辑 `/etc/containerd/config.toml`，添加 `insecure_registry` 或使用 `registries.yaml` 配置镜像源
- **K3s**：编辑 `/etc/rancher/k3s/registries.yaml`

### 为什么会出现这个错误

Docker 出于安全考虑，默认只信任经过 TLS 证书验证的 HTTPS 连接。HTTP 代理属于"明文传输"，必须显式声明为 `insecure-registries` 才会被 Docker 接受。生产环境建议在代理前面加一层 Nginx 反向代理并配置 TLS 证书，即可避免此问题。

---

## ⚙️ 配置说明

配置文件位于 `config/config.yaml`，主要字段：

### 应用与认证

| 字段 | 说明 |
|---|---|
| `app.name` / `app.tagline` | 页面标题与标语 |
| `admin.user` / `admin.pass` | 管理后台账号密码，留空则关闭认证 |

### 代理行为

| 字段 | 说明 |
|---|---|
| `proxy.candidate_count` | 拉取时尝试的候选节点数（默认 5） |
| `proxy.timeout_by_path.probe` | `/v2/` 心跳超时（秒） |
| `proxy.timeout_by_path.manifests` | manifests 请求超时（秒） |
| `proxy.timeout_by_path.blobs` | blobs 请求超时（秒） |
| `proxy.fail_cooldown` | 通用失败熔断时长（秒） |
| `proxy.timeout_fail_cooldown` | 超时类失败熔断时长（秒） |
| `proxy.forbidden_fail_cooldown` | 403 类失败熔断时长（秒） |
| `proxy.server_err_fail_cooldown` | 5xx 类失败熔断时长（秒） |
| `proxy.follow_redirects` | 是否由代理主动跟随 3xx 重定向 |
| `proxy.blob_fail_cooldown` | blob 级失败缓存时长（秒） |
| `proxy.prefer_recent_success` | 是否优先选择最近成功过的节点 |
| `proxy.affinity_window` | 镜像级节点粘性窗口（秒） |
| `proxy.probe_node_window` | 心跳请求的节点粘性窗口（秒） |
| `proxy.realtime_probe` | 是否每次拉取都实时探测延迟 |

### 节点拉取与健康检查

| 字段                                 | 说明                     |
| ---------------------------------- | ---------------------- |
| `auto_fetch.enabled`               | 是否启用自动拉取节点             |
| `auto_fetch.interval_minutes`      | 自动拉取节点间隔（分钟）           |
| `auto_fetch.api_url`               | 上游节点状态 API 地址          |
| `health_check.interval_minutes`    | 健康检查间隔（分钟）             |
| `health_check.test_images_by_type` | 按 registry 类型配置的测试镜像路径 |

### 持久化列表

| 字段 | 说明 |
|---|---|
| `manually_disabled` | 手动禁用节点列表（拉取后保持禁用） |
| `custom_nodes` | 自定义节点列表（重启不丢失） |
| `route_aliases` | 自定义路由别名（留空使用内置默认） |

### 日志

| 字段 | 说明 |
|---|---|
| `logging.level` | 应用日志级别 |
| `logging.third_party_level` | 第三方库日志级别（默认 `WARNING`） |

配置也可以在 Web 后台的「配置文件」按钮中在线编辑、保存并自动重载。

---

## 🖥️ 管理后台功能

访问 `http://<主机IP>:8000`，使用 `admin.user` / `admin.pass` 登录。

- **节点列表**：查看节点状态、延迟、流量、注册表类型与路由前缀
- **获取免费节点**：一键从 `status.anye.xyz` 拉取最新节点
- **测速**：对所有节点或选中节点进行实时测速
- **添加 / 编辑 / 删除**：支持自定义节点，含用户名密码认证
- **手动禁用 / 启用**：禁用的节点在重新拉取后保持禁用
- **批量操作**：勾选多个节点批量禁用 / 启用
- **导入 / 导出**：JSON 格式批量管理节点
- **流量趋势**：ECharts 展示 7 天流量变化
- **拉取记录**：查看最近 200 条镜像拉取历史
- **健康检查日志**：查看每个节点的最近检测结果
- **配置文件编辑**：在线编辑 YAML，保存并自动重载
- **镜像搜索**：搜索 Docker Hub 并一键复制拉取命令

---

## 📁 项目结构

```
dockermirrorflow/
├── app/
│   ├── __init__.py          # 版本号
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # YAML 配置加载
│   ├── database.py          # SQLite 初始化与迁移
│   ├── models.py            # 数据模型
│   ├── services/
│   │   ├── proxy_manager.py # 节点管理、拉取、测速、路由
│   │   ├── traffic_logger.py# 流量与拉取记录
│   │   └── search_service.py# 镜像搜索
│   ├── routers/
│   │   ├── web_ui.py        # 管理后台 API
│   │   └── docker_proxy.py  # Docker Registry 代理
│   └── templates/
│       └── index.html       # 管理后台页面
├── config/
│   ├── config.yaml          # 主配置文件
│   └── config.example.yaml  # 配置示例
├── data/
│   └── dockermirrorflow.db  # SQLite 数据库
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 📝 常见问题

### 拉取镜像报 `http: server gave HTTP response to HTTPS client`

参见上文 [⚠️ HTTP 拉取错误排查](#-http-拉取错误排查)，把代理地址加入 `insecure-registries`。

### 拉取 GHCR / GCR / Quay 镜像失败

NAS 的 Docker 加速器只对 Docker Hub 生效。请使用完整前缀：

```bash
docker pull <proxy>:8000/ghcr.io/owner/image:tag
```

### 某个节点总是失败

可在管理后台手动禁用，或在 `config/config.yaml` 的 `manually_disabled` 中添加：

```yaml
manually_disabled:
  - url: "https://docker.m.daocloud.io"
    reason: "403 Forbidden，对部分镜像不可用"
    disabled_at: "2026-09-13"
```

### 配置修改后不生效

- 通过 Web 后台保存的配置，**立即生效**（除 `server.*` / `logging.*` / 定时任务间隔外）
- 直接编辑 `config.yaml` 文件的配置，需要**重启服务**

### 健康检查误判节点离线

不同 registry 类型使用不同的测试镜像，位于 `health_check.test_images_by_type`。如果某类型对应的镜像不存在，该类型所有节点都会被误判为离线。请修改为对应 registry 上真实存在的公开镜像：

```yaml
health_check:
  test_images_by_type:
    dockerhub: "library/alpine/manifests/latest"
    ghcr:      "stefanprodan/podinfo/manifests/latest"
    gcr:       "distroless/static/manifests/latest"
    quay:      "prometheus/prometheus/manifests/latest"
    mcr:       "hello-world/manifests/latest"
    elastic:   "beats/filebeat/manifests/latest"
    nvcr:      "nvidia/cuda/manifests/latest"
```

某类型留空字符串 `""` 则跳过 manifests 检查，仅用 `/v2/` 判断节点存活。

### 日志太大

将 `logging.third_party_level` 设为 `WARNING`（默认），只打印失败请求，可大幅减小日志体积。

---

## 🙏 致谢

感谢 [xingfeng7788/docker-hub-proxy](https://github.com/xingfeng7788/docker-hub-proxy) 提供思路与源码。

---

## 📄 License

本项目采用 **GPL-3.0** 许可协议发布。详见 [LICENSE](LICENSE)。