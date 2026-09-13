import logging
import yaml
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator

logger = logging.getLogger("dockermirrorflow.config")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"

# 各 registry 类型的默认测试镜像（完整 manifests 路径）
# 格式: {image_path}/manifests/{tag}，会拼到 {registry}/v2/ 后面
# 这些镜像都是公开、稳定、长期存在的
DEFAULT_TEST_IMAGES: dict[str, str] = {
    "dockerhub": "library/alpine/manifests/latest",
    "ghcr": "stefanprodan/podinfo/manifests/latest",
    "gcr": "distroless/static/manifests/latest",
    "quay": "prometheus/prometheus/manifests/latest",
    "mcr": "hello-world/manifests/latest",
    "elastic": "beats/filebeat/manifests/latest",
    "nvcr": "nvidia/cuda/manifests/latest",
}


class AppMeta(BaseModel):
    name: str = "DockerMirrorFlow"
    tagline: str = "多源聚合，流式加速"


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


class AdminConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user: str = ""
    pass_: str = Field(default="", alias="pass")


class TimeoutByPath(BaseModel):
    """按路径类型区分的上游超时（秒）"""

    probe: float = 3.0
    manifests: float = 5.0
    blobs: float = 10.0


class ProxyConfig(BaseModel):
    timeout: float = 10.0
    timeout_by_path: TimeoutByPath = TimeoutByPath()

    max_redirects: int = 5
    stream_chunk_size: int = 1048576
    candidate_count: int = 5

    fail_cooldown: int = 60
    timeout_fail_cooldown: int = 300
    forbidden_fail_cooldown: int = 600
    server_err_fail_cooldown: int = 180

    realtime_probe: bool = False
    probe_timeout: float = 2.0

    follow_redirects: bool = True
    follow_redirects_max: int = 5
    follow_redirect_fail_cooldown: int = 300

    blob_fail_cooldown: int = 600

    prefer_recent_success: bool = True
    recent_success_window: int = 300

    affinity_window: int = 300
    probe_node_window: int = 600


class AccessConfig(BaseModel):
    ip_whitelist: list[str] = []
    image_whitelist_regex: str = ""
    image_blacklist_regex: str = ""

    @field_validator("ip_whitelist", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class AutoFetchConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 60
    api_url: str = "https://status.anye.xyz"
    registry_types: list[str] = ["hub", "ghcr", "quay", "mcr", "gcr", "elastic", "nvcr"]
    filters: dict[str, Any] = {"selectable": True, "access": "public"}

    @field_validator("registry_types", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("filters", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class HealthCheckConfig(BaseModel):
    interval_minutes: int = 30
    timeout_seconds: float = 5.0
    latency_threshold: float = 500.0
    disable_threshold: float = 9999.0
    concurrent_batch: int = 5
    auto_recover: bool = True
    recover_after_minutes: int = 120

    # 按 registry 类型的测试镜像路径（完整，含 /manifests/{tag}）
    # 会拼到 {registry}/v2/ 后面，例如:
    #   https://ghcr.nju.edu.cn/v2/stefanprodan/podinfo/manifests/latest
    # 要求对应 registry 上真实存在该镜像，404 会被视为节点故障
    # 某类型留空字符串则跳过 manifests 检查，仅用 /v2/ 判断存活
    test_images_by_type: dict[str, str] = DEFAULT_TEST_IMAGES.copy()

    @field_validator("test_images_by_type", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/dockermirrorflow.log"
    max_bytes: int = 10485760
    backup_count: int = 5

    # 第三方库日志级别（httpx / httpcore / apscheduler / uvicorn.access）
    # 可选值：
    #   "DEBUG"    - 打印所有 HTTP 请求（生产环境噪音巨大，仅调试用）
    #   "INFO"     - 打印所有 HTTP 请求（httpx 默认级别）
    #   "WARNING"  - 只打印失败请求（推荐，避免日志膨胀）
    #   "ERROR"    - 只打印错误
    #   "CRITICAL" - 几乎不打印
    third_party_level: str = "WARNING"


class CustomNode(BaseModel):
    name: str
    url: str
    registry_type: str = "dockerhub"
    route_prefix: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True


class ManuallyDisabledNode(BaseModel):
    url: str
    reason: str = ""
    disabled_at: str = ""


class SearchUpstream(BaseModel):
    name: str
    url: str


class SearchConfig(BaseModel):
    enabled: bool = True
    page_size: int = 25
    timeout: float = 10.0
    upstreams: list[SearchUpstream] = []

    @field_validator("upstreams", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppMeta = AppMeta()
    server: ServerConfig = ServerConfig()
    admin: AdminConfig = AdminConfig()
    proxy: ProxyConfig = ProxyConfig()
    access: AccessConfig = AccessConfig()
    auto_fetch: AutoFetchConfig = AutoFetchConfig()
    health_check: HealthCheckConfig = HealthCheckConfig()
    logging: LoggingConfig = LoggingConfig()
    custom_nodes: list[CustomNode] = []
    manually_disabled: list[ManuallyDisabledNode] = []
    route_aliases: dict[str, list[str]] = {}
    search: SearchConfig = SearchConfig()

    @field_validator("custom_nodes", "manually_disabled", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("route_aliases", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    for key in ("custom_nodes", "manually_disabled"):
        if data.get(key) is None:
            data[key] = []
    if data.get("route_aliases") is None:
        data["route_aliases"] = {}
    if data.get("search") is None:
        data["search"] = {}

    return AppConfig(**data)


def reload_config(path: Path = CONFIG_PATH) -> AppConfig:
    new_config = load_config(path)

    for field_name in AppConfig.model_fields.keys():
        try:
            setattr(config, field_name, getattr(new_config, field_name))
        except Exception as e:
            logger.error(f"重载字段 {field_name} 失败: {e}")

    logger.info(f"配置已重载: {path}")
    return config


config = load_config()
