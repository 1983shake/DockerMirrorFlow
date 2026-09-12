感谢[xingfeng7788/docker-hub-proxy](https://github.com/xingfeng7788/docker-hub-proxy)思路提供。

# DockerMirrorFlow

> 多源聚合，流式加速 —— 多 Registry 镜像代理加速服务

DockerMirrorFlow 是一个高性能 Docker Registry 代理，支持 Docker Hub、GHCR、GCR、Quay、MCR 等多种镜像仓库，
通过自动拉取、活体检测、智能路由和流量统计，为你的容器拉取加速。

## ✨ 特性

- 🚀 **多源聚合**：自动从 `status.anye.xyz` 拉取免费镜像节点（YAML 可配置）
- 🎯 **智能路由**：按延迟择优，支持域名风格前缀匹配（`ghcr.io/xxx`）
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