# DockerMirrorFlow

> 多源聚合，流式加速 —— 多 Registry 镜像代理加速服务

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0) [![Python](https://img.shields.io/badge/Python-3.11+-green.svg)](https://www.python.org/) [![Docker](https://img.shields.io/badge/Docker-ghcr.io-blue.svg)](https://www.docker.com/)

DockerMirrorFlow 是一个轻量的 Docker Registry 代理，支持 Docker Hub、GHCR、GCR、Quay、MCR 等多种镜像仓库。
自动从公共镜像源拉取可用节点，定时健康检查，按延迟智能路由，为容器拉取提速。

---

## ✨ 特性

- 🚀 **多源聚合**：定时从 `status.anye.xyz` 自动拉取免费镜像节点
- 🎯 **智能路由**：按延迟择优，支持 `ghcr/...` 与 `ghcr.io/...` 两种前缀形式
- 🔄 **自动 fallback**：一个节点失败自动切换下一个，403/5xx 也会自动切换
- 💓 **健康检查**：定时活体检测 + 测速，自动剔除失效节点
- 🔒 **手动禁用持久化**：手动禁用的节点在重新拉取后保持禁用
- 📦 **自定义节点持久化**：YAML 声明式配置，重启不丢失
- 📊 **流量统计**：按天、按节点记录拉取流量与拉取历史
- ⚙️ **YAML 配置**：所有参数集中管理，支持 Web 后台在线编辑
- 🖥️ **Web 管理**：Vue 3 + Tailwind + ECharts 现代化界面

---

## 🚀 快速开始

### 方式一：Docker 一键部署（推荐）

```bash
docker run -d --name dockermirrorflow \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config:/app/config \
  --restart unless-stopped \
  crpi-1tmphkb8hkeahev6.cn-chengdu.personal.cr.aliyuncs.com/1983shake/dockermirrorflow:latest
```

首次启动前，需要准备 `config/config.yaml`：

```bash
mkdir -p config data
# 从仓库复制 config.example.yaml，或直接编辑一个
```

最小可用配置（`config/config.yaml`）：

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

然后访问 `http://localhost:8000` 进入管理后台。

### 方式二：docker-compose

```yaml
# docker-compose.yml
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

```bash
docker compose up -d
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

假设你的代理服务运行在 `192.168.1.100:8000`。

```bash
# Docker Hub
docker pull 192.168.1.100:8000/library/nginx:latest

# GHCR
docker pull 192.168.1.100:8000/ghcr.io/owner/image:tag

# Quay
docker pull 192.168.1.100:8000/quay.io/org/image:tag
```

> ⚠️ **NAS 用户注意**：飞牛/群晖/威联通的 Docker 加速器**只对 Docker Hub 生效**。拉取 GHCR/GCR/Quay 镜像时，必须写成 `<proxy>:8000/ghcr.io/...` 的形式。

---

## ⚙️ 配置说明

配置文件位于 `config/config.yaml`，主要字段：

| 字段 | 说明 |
|---|---|
| `admin.user` / `admin.pass` | 管理后台账号密码，留空则关闭认证 |
| `proxy.candidate_count` | 拉取时的候选节点数（默认 3） |
| `proxy.fail_cooldown` | 节点失败后的熔断时长（秒） |
| `proxy.realtime_probe` | 是否每次拉取实时探测延迟 |
| `auto_fetch.interval_minutes` | 自动拉取节点间隔（分钟） |
| `health_check.interval_minutes` | 健康检查间隔（分钟） |
| `manually_disabled` | 手动禁用节点列表（拉取后保持禁用） |
| `custom_nodes` | 自定义节点列表（重启不丢失） |

配置也可以在 Web 后台的「配置文件」按钮中在线编辑、保存并自动重载。

---

## 📁 项目结构

```
dockermirrorflow/
├── app/
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
│   └── config.yaml          # 主配置文件
├── data/
│   └── dockermirrorflow.db  # SQLite 数据库
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 🙏 致谢

感谢 [xingfeng7788/docker-hub-proxy](https://github.com/xingfeng7788/docker-hub-proxy) 提供思路与源码。

---

## 📄 License

本项目采用 **GPL-3.0** 许可协议发布。详见 [LICENSE](LICENSE)。