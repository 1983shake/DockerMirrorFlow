# DockerMirrorFlow

> 多源聚合，流式加速 —— 多 Registry 镜像代理加速服务

DockerMirrorFlow 是一个高性能 Docker Registry 代理，支持 Docker Hub、GHCR、GCR、Quay、MCR 等多种镜像仓库。  
通过自动拉取、活体检测、智能路由和流量统计，为你的容器拉取加速。

## ✨ 特性

- 🚀 **多源聚合**：自动从 Docker 监控站拉取免费镜像节点（YAML 可配置）
- 🎯 **智能路由**：按延迟择优，支持域名风格前缀匹配（如 `ghcr.io/xxx`）
- 🔄 **自动切换**：拉取失败自动 fallback 到次优节点，失败节点熔断 60 秒
- ⚡ **实时探测**（可选）：每次拉取前实时探测节点延迟
- 💓 **活体检测**：定时健康检查 + 速度检测，自动剔除失效节点
- 🔒 **手动禁用持久化**：手动禁用的节点在重新拉取后保持禁用
- 📦 **自定义节点持久化**：YAML 声明式配置，重启不丢失
- 📊 **流量统计**：按天、按节点记录拉取流量
- 📝 **健康日志**：记录每次健康检查的结果与延迟
- 🖥️ **Web 管理**：Vue 3 + Tailwind + ECharts 现代化界面
- ⚙️ **YAML 配置**：所有参数集中管理

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
mkdir -p config
cp config/config.example.yaml config/config.yaml
vim config/config.yaml
```

### 3. 启动

```bash
python -m app.main
```

启动后访问 [http://localhost:8000](http://localhost:8000) 进入管理后台。

### 4. 使用

```bash
docker pull your-server:8000/library/nginx:latest
docker pull your-server:8000/ghcr.io/owner/image:tag
docker pull your-server:8000/quay.io/org/image:tag
```

## 🐳 Docker 部署

```bash
docker compose up -d
```

## 📖 配置说明

详见 `config/config.example.yaml` 中的注释。

### 关键配置项

| 配置项 | 说明 |
| --- | --- |
| `proxy.candidate_count` | 拉取时尝试的候选节点数（默认 `3`） |
| `proxy.fail_cooldown` | 节点失败后熔断时长（默认 `60` 秒） |
| `proxy.realtime_probe` | 是否每次拉取实时探测延迟（默认 `false`） |
| `auto_fetch.interval_minutes` | 自动拉取间隔（分钟） |
| `health_check.interval_minutes` | 健康检查间隔（分钟） |
| `custom_nodes` | 自定义节点列表（重启不丢失） |
| `manually_disabled` | 手动禁用节点列表（拉取后保持禁用） |

### 默认用户名和密码

根据 `config/config.example.yaml` 中的默认配置：

```yaml
admin:
  user: "admin"
  pass: "change_me"
```

> ⚠️ 首次部署后请及时修改默认密码。

## NAS 用户必读

NAS（飞牛 / 群晖 / 威联通）的 Docker 加速器设置 **只对 Docker Hub 生效**。

若需要走代理，需要将原地址：

```text
ghcr.io/<owner>/<image>:<tag>
```

改成：

```text
ghcr/<owner>/<image>:<tag>
```

拉取 `ghcr.io`、`quay.io`、`gcr.io` 等其他仓库的镜像时，必须显式加上代理地址前缀：

```bash
# Docker Hub
docker pull <proxy>:8000/library/nginx:latest
docker pull <proxy>:8000/docker.io/library/nginx:latest

# GitHub Container Registry (GHCR)
docker pull <proxy>:8000/ghcr.io/owner/image:tag
docker pull <proxy>:8000/ghcr/owner/image:tag

# Google Container Registry (GCR)
docker pull <proxy>:8000/gcr.io/project/image:tag
docker pull <proxy>:8000/registry.k8s.io/project/image:tag

# Quay.io
docker pull <proxy>:8000/quay.io/org/image:tag
```

### ⚠️ 注意事项

1. 不要直接使用 `docker pull ghcr.io/owner/image:tag`，这会绕过代理直连上游，无法享受加速。
2. 必须在镜像名前加上代理地址前缀。
3. 代理地址前不加 `http://` 或 `https://`。

## 🙏 致谢

感谢 [xingfeng7788/docker-hub-proxy](https://github.com/xingfeng7788/docker-hub-proxy) 提供思路与源码。