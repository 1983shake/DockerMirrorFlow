import yaml
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


class AppMeta(BaseModel):
    name: str = "DockerMirrorFlow"
    tagline: str = "多源聚合，流式加速"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 2
    debug: bool = False


class AdminConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user: str = ""
    pass_: str = Field(default="", alias="pass")


class ProxyConfig(BaseModel):
    timeout: float = 10.0
    max_redirects: int = 5
    stream_chunk_size: int = 1048576
    candidate_count: int = 3
    fail_cooldown: int = 60
    realtime_probe: bool = False
    probe_timeout: float = 2.0


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


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/dockermirrorflow.log"
    max_bytes: int = 10485760
    backup_count: int = 5


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


class AppConfig(BaseModel):
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
    # ✅ 新增：路由别名（可选，留空则用内置默认）
    route_aliases: dict[str, list[str]] = {}

    # 把 None 转为 []
    @field_validator("custom_nodes", "manually_disabled", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    # 把 None 转为 {}
    @field_validator("route_aliases", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 顶层空值规范化，防止 YAML 里写成 `custom_nodes:` 解析成 None
    for key in ("custom_nodes", "manually_disabled"):
        if data.get(key) is None:
            data[key] = []
    if data.get("route_aliases") is None:
        data["route_aliases"] = {}

    return AppConfig(**data)


config = load_config()