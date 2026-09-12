from typing import Optional
from datetime import datetime, timezone, timedelta
from sqlmodel import Field, SQLModel
import enum


def get_shanghai_time() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


class NodeStatus(str, enum.Enum):
    ONLINE = "online"
    SLOW = "slow"
    OFFLINE = "offline"
    DISABLED = "disabled"


class ProxyNode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str = Field(unique=True, index=True)
    registry_type: str = Field(default="dockerhub")
    route_prefix: Optional[str] = Field(default=None, index=True)
    enabled: bool = True
    latency: float = Field(default=9999.0)
    last_check: Optional[datetime] = None
    is_default: bool = False
    is_custom: bool = False
    manually_disabled: bool = False
    manual_disable_reason: Optional[str] = None
    manual_disable_at: Optional[datetime] = None
    username: Optional[str] = None
    password: Optional[str] = None
    failure_reason: Optional[str] = None
    download_bytes: int = Field(default=0)
    created_at: datetime = Field(default_factory=get_shanghai_time)
    updated_at: datetime = Field(default_factory=get_shanghai_time)

    @property
    def status(self) -> NodeStatus:
        if self.manually_disabled or not self.enabled:
            return NodeStatus.DISABLED
        if self.latency >= 9999:
            return NodeStatus.OFFLINE
        if self.latency >= 500:
            return NodeStatus.SLOW
        return NodeStatus.ONLINE


class TrafficStats(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True)
    download_bytes: int = Field(default=0)
    upload_bytes: int = Field(default=0)
    request_count: int = Field(default=0)


class PullHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_time: datetime = Field(default_factory=get_shanghai_time, index=True)
    image: str = Field(index=True)
    tag: str
    client_ip: str
    node_id: Optional[int] = Field(default=None, foreign_key="proxynode.id")
    node_name: Optional[str] = None


class HealthCheckLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="proxynode.id", index=True)
    node_name: str
    check_time: datetime = Field(default_factory=get_shanghai_time, index=True)
    success: bool
    latency: float
    error_message: Optional[str] = None