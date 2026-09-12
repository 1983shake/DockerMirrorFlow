# ============================================================
#  DockerMirrorFlow 配置文件
#  多源聚合，流式加速 —— 多 Registry 镜像代理加速服务
#
#  修改后可通过 Web 管理后台的「配置文件」按钮保存并重载，
#  部分字段需要重启服务才能生效（详见文末说明）。
# ============================================================


# ------------------------------------------------------------
# 应用元信息（显示在页面标题与页头）
# ------------------------------------------------------------
app:
  name: "DockerMirrorFlow"
  tagline: "多源聚合，流式加速"


# ------------------------------------------------------------
# 服务器配置（修改后需要重启服务）
# ------------------------------------------------------------
server:
  host: "0.0.0.0"
  port: 8000
  workers: 2
  debug: false


# ------------------------------------------------------------
# 管理后台认证（留空则关闭认证）
# 生产环境务必设置，否则任何人都能访问 /api/config
# ------------------------------------------------------------
admin:
  user: "admin"
  pass: "change_me"


# ------------------------------------------------------------
# 代理行为配置
# ------------------------------------------------------------
proxy:
  timeout: 10.0               # 上游请求超时（秒）
  max_redirects: 5            # 跟随重定向最大次数
  stream_chunk_size: 1048576  # 流式返回 chunk 大小（字节，默认 1MB）

  # 拉取时的候选节点数（fallback 链长度）
  candidate_count: 3

  # 节点失败后的熔断时长（秒）
  # 熔断期内该节点不会被选中，冷却结束后自动恢复
  fail_cooldown: 60

  # 是否每次拉取都实时探测候选节点的延迟
  # true  = 每次拉取都重新测速（更准，但每次多 100~300ms）
  # false = 使用健康检查缓存的延迟（更快，但可能选到已变慢的节点）
  realtime_probe: false

  # 实时探测的超时时间（秒），仅在 realtime_probe=true 时生效
  probe_timeout: 2.0


# ------------------------------------------------------------
# 访问控制
# ------------------------------------------------------------
access:
  # IP 白名单，空列表表示不限制
  # 示例: ["192.168.1.0/24", "10.0.0.1", "192.168.99.61"]
  ip_whitelist: []

  # 镜像白名单正则（留空表示不限制）
  # 仅对包含 /manifests/ 或 /blobs/ 的请求生效
  # 示例: "^library/.*|^ghcr\\.io/.*"
  image_whitelist_regex: ""

  # 镜像黑名单正则（留空表示不限制）
  # 优先级高于白名单
  # 示例: ".*:latest$|^private/.*"
  image_blacklist_regex: ""


# ------------------------------------------------------------
# 节点自动拉取配置
# ------------------------------------------------------------
auto_fetch:
  enabled: true
  # 拉取间隔（分钟），修改后需重启
  interval_minutes: 60

  # 上游 API 地址
  api_url: "https://status.anye.xyz"

  # 需要拉取的 registry 类型
  # 支持: hub, ghcr, quay, mcr, gcr, elastic, nvcr
  registry_types:
    - hub       # Docker Hub
    - ghcr      # GitHub Container Registry
    - quay      # Quay.io
    - mcr       # Microsoft Container Registry
    - gcr       # Google Container Registry
    - elastic   # Elastic Registry
    - nvcr      # NVIDIA Container Registry

  # 拉取筛选条件
  filters:
    selectable: true          # 仅拉取 selectable=true 的节点
    access: "public"          # 仅拉取 public 访问的节点


# ------------------------------------------------------------
# 活体检测与测速配置
# ------------------------------------------------------------
health_check:
  # 检测间隔（分钟），修改后需重启
  interval_minutes: 30

  # 单节点检测超时（秒）
  timeout_seconds: 5

  # 延迟阈值（毫秒）
  # < latency_threshold          → 在线（绿）
  # latency_threshold ~ disable  → 缓慢（黄）
  # >= disable_threshold         → 离线（灰）
  latency_threshold: 500
  disable_threshold: 9999

  # 并发检测数量（一次同时检测多少个节点）
  concurrent_batch: 5

  # 自动恢复：被自动禁用的节点在冷却后是否重新尝试
  auto_recover: true

  # 禁用后多久重新尝试（分钟）
  recover_after_minutes: 120


# ------------------------------------------------------------
# 手动禁用节点列表
# 手动禁用的节点在自动拉取后依旧保持禁用
# 不会参与路由选择与健康检查
# ------------------------------------------------------------
manually_disabled: []
# 示例：
# manually_disabled:
#   - url: "https://docker.m.daocloud.io"
#     reason: "403 Forbidden，拉取失败"
#     disabled_at: "2026-09-12"
#   - url: "https://mirror.example.com"
#     reason: "速度太慢"
#     disabled_at: "2026-09-11"


# ------------------------------------------------------------
# 自定义节点
# 手动添加的节点，重启后不会丢失
# 会自动同步到数据库并标记为 is_custom=true
# ------------------------------------------------------------
custom_nodes: []
# 示例：
# custom_nodes:
#   - name: "My Private Docker Hub Mirror"
#     url: "https://my-mirror.example.com"
#     registry_type: "dockerhub"
#     route_prefix: null
#     username: null
#     password: null
#     enabled: true
#
#   - name: "GHCR Mirror"
#     url: "https://ghcr-mirror.example.com"
#     registry_type: "ghcr"
#     route_prefix: "ghcr"
#     enabled: true
#
#   - name: "私有仓库（带认证）"
#     url: "https://private-registry.example.com"
#     registry_type: "other"
#     route_prefix: "private"
#     username: "user"
#     password: "pass"
#     enabled: true


# ------------------------------------------------------------
# 路由别名
# 用于把请求路径前缀映射到对应 registry 类型的节点
# 留空则使用内置默认（见下）
#
# 内置默认：
#   dockerhub: ["docker.io", "registry-1.docker.io", "index.docker.io"]
#   ghcr:      ["ghcr.io", "ghcr"]
#   gcr:       ["gcr.io", "k8s.gcr.io", "registry.k8s.io", "gcr"]
#   quay:      ["quay.io", "quay"]
#   mcr:       ["mcr.microsoft.com", "mcr"]
#   nvcr:      ["nvcr.io", "nvcr"]
#   elastic:   ["docker.elastic.co", "elastic"]
# ------------------------------------------------------------
route_aliases: {}
# 如需覆盖，请完整写出所有类型（未写出的类型会失去默认别名）：
# route_aliases:
#   dockerhub: ["docker.io", "registry-1.docker.io", "index.docker.io"]
#   ghcr:      ["ghcr.io", "ghcr"]
#   gcr:       ["gcr.io", "k8s.gcr.io", "registry.k8s.io", "gcr"]
#   quay:      ["quay.io", "quay"]
#   mcr:       ["mcr.microsoft.com", "mcr"]
#   nvcr:      ["nvcr.io", "nvcr"]
#   elastic:   ["docker.elastic.co", "elastic"]


# ------------------------------------------------------------
# 搜索配置（Web UI 的「搜索 & 拉取 Docker 镜像」功能）
#
# 前端会优先让浏览器直接访问 hub.docker.com 搜索。
# 如果浏览器所在网络也访问不了，会调用后端 /api/search。
# 后端会依次尝试下面 upstreams 里的 URL。
#
# ⚠️ 注意：Docker 镜像加速节点（DaoCloud、南大等）只代理
#    /v2/ registry 协议，不提供搜索 API。所以要在这里填
#    一个能访问 hub.docker.com 的搜索代理地址。
#
# 常见可选 upstream:
#   - 内网 HTTP 代理（公司/机房出网代理）
#   - 自建 nginx 反代: http://your-nginx/hub-search
#   - 能访问 Docker Hub 的第三方搜索 API
# ------------------------------------------------------------
search:
  enabled: true
  page_size: 25
  timeout: 10.0
  upstreams: []
# 示例：
# search:
#   enabled: true
#   page_size: 25
#   timeout: 10.0
#   upstreams:
#     - name: "内网出网代理"
#       url: "http://192.168.1.10:8080/proxy/hub-search"
#     - name: "备用镜像搜索"
#       url: "https://some-search-mirror.example.com/v2/search/repositories/"


# ------------------------------------------------------------
# 日志配置（修改后需要重启服务）
# ------------------------------------------------------------
logging:
  level: "INFO"                                # DEBUG / INFO / WARNING / ERROR
  file: "data/dockermirrorflow.log"            # 日志文件路径
  max_bytes: 10485760                          # 单文件最大字节数（默认 10MB）
  backup_count: 5                              # 保留的滚动日志文件数量


# ============================================================
# 生效说明
# ============================================================
# 立即生效（保存后自动重载）：
#   - admin.*
#   - proxy.*
#   - access.*
#   - custom_nodes
#   - manually_disabled
#   - route_aliases
#   - search.*
#
# 需要重启服务：
#   - server.*
#   - logging.*
#   - auto_fetch.interval_minutes
#   - health_check.interval_minutes
#
# 修改方式：
#   1. Web 管理后台 → 加速节点 → 「配置文件」按钮
#   2. 直接编辑本文件后重启服务
#
# 备份：
#   每次通过 Web 保存前，旧配置自动备份到 config.yaml.bak
# ============================================================