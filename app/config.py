import logging
import yaml
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator

logger = logging.getLogger("dockermirrorflow.config")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"

# ============================================================
#  固定应用元信息
#
#  ⚠️ 硬编码于此，不可修改：
#     config.yaml 中的 app.name / app.tagline、
#     Web 后台「配置管理」表单、
#     Docker 镜像内预置或挂载的任何配置文件，
#     都会被强制覆盖为下面这两个值。
# ============================================================
APP_NAME = "DockerMirrorFlow"
APP_TAGLINE = "多源聚合，流式加速"


class AppMeta(BaseModel):
    """应用元信息（固定值，外部配置无法覆盖）"""

    name: str = APP_NAME
    tagline: str = APP_TAGLINE

    @field_validator("name", mode="before")
    @classmethod
    def _lock_name(cls, _value):
        return APP_NAME

    @field_validator("tagline", mode="before")
    @classmethod
    def _lock_tagline(cls, _value):
        return APP_TAGLINE


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

    # ⚠️ candidate_count 已取消，节点全部按速度排序后依次尝试

    fail_cooldown: int = 60
    timeout_fail_cooldown: int = 300
    forbidden_fail_cooldown: int = 600
    server_err_fail_cooldown: int = 180

    probe_timeout: float = 2.0

    follow_redirects: bool = True
    follow_redirects_max: int = 5
    follow_redirect_fail_cooldown: int = 300

    blob_fail_cooldown: int = 600

    # ============================================================
    #  blob 流式读取超时（秒）
    #
    #  - None / 0 / 负数：不限制读取超时（推荐，默认）
    #  - 正数：两次 chunk 之间的最大空闲时间，超时抛 ReadTimeout
    #
    #  说明：
    #    httpx 的 read timeout 作用于「两次 recv 之间」的空闲等待。
    #    blob 拉取是大文件流式传输，上游 CDN（Cloudflare / S3 / Fastly 等）
    #    经常出现几秒到几十秒的分块停顿，若和 connect 用同一档短超时，
    #    会在传输中途抛 httpx.ReadTimeout 导致拉取中断、Docker 反复重试。
    #    因此这里默认不限制；如担心长连接占用，可设置一个较宽松的值
    #    （例如 300）。
    # ============================================================
    blob_read_timeout: Optional[float] = None

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
    interval_minutes: int = 1440  # 默认 24 小时
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
    """活体检测：只探测 /v2/，不做 manifests 校验"""

    interval_minutes: int = 60  # 默认 1 小时
    timeout_seconds: float = 5.0
    latency_threshold: float = 500.0
    disable_threshold: float = 9999.0
    concurrent_batch: int = 5
    auto_recover: bool = True
    recover_after_minutes: int = 120


DEFAULT_SPEED_TEST_IMAGES: dict[str, str] = {
    "dockerhub": "library/alpine",
    "ghcr": "stefanprodan/podinfo",
    "gcr": "distroless/static",
    "quay": "prometheus/prometheus",
    "mcr": "hello-world",
    "elastic": "beats/filebeat",
    "nvcr": "nvidia/cuda",
}


class SpeedTestConfig(BaseModel):
    """速度测试：固定时长内下载 layer，计算 bytes/sec"""

    enabled: bool = True
    interval_minutes: int = 720  # 默认 12 小时
    duration_seconds: float = 5.0  # 固定下载时长（秒），不宜过长
    tag: str = "latest"
    concurrent_batch: int = 5
    test_images_by_type: dict[str, str] = DEFAULT_SPEED_TEST_IMAGES.copy()

    @field_validator("test_images_by_type", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/dockermirrorflow.log"
    max_bytes: int = 10485760
    backup_count: int = 5
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
    speed_test: SpeedTestConfig = SpeedTestConfig()
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

    # 应用名称与标语固定，忽略配置文件中可能存在的任何自定义值
    data["app"] = {"name": APP_NAME, "tagline": APP_TAGLINE}

    for key in ("custom_nodes", "manually_disabled"):
        if data.get(key) is None:
            data[key] = []
    if data.get("route_aliases") is None:
        data["route_aliases"] = {}
    if data.get("search") is None:
        data["search"] = {}
    if data.get("speed_test") is None:
        data["speed_test"] = {}

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
