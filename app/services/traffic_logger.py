import logging
from datetime import datetime, timedelta
from sqlmodel import Session, select, func

from app.database import engine
from app.models import (
    TrafficStats,
    PullHistory,
    ProxyNode,
    get_shanghai_time,
)

logger = logging.getLogger("dockermirrorflow.traffic")

# 内存缓存
_recent_pulls: dict[str, datetime] = {}
_MAX_RECENT_PULLS = 500
_DEDUP_WINDOW = 60

# 待定拉取：manifest 成功登记，等 blob 到达后提升为正式记录
# key = f"{client_ip}_{norm_image}"
_pending_pulls: dict[str, dict] = {}
_PENDING_TTL = 300  # 5 分钟：仅探测、无 blob，自动过期丢弃
_INFLIGHT_TTL = 3600  # 1 小时：已提升，等待整次拉取的所有 blob 完成


def _normalize_image(image: str) -> str:
    """规范化镜像名，去掉常见 registry 前缀，便于去重比较。"""
    if not image:
        return image
    for prefix in (
        "ghcr.io/",
        "ghcr/",
        "docker.io/",
        "docker/",
        "library/",
        "quay.io/",
        "quay/",
        "gcr.io/",
        "gcr/",
        "registry.k8s.io/",
        "k8s.gcr.io/",
        "mcr.microsoft.com/",
        "mcr/",
        "nvcr.io/",
        "nvcr/",
        "docker.elastic.co/",
        "elastic/",
    ):
        if image.startswith(prefix):
            return image[len(prefix) :]
    return image


def log_traffic(bytes_downloaded: int = 0, bytes_uploaded: int = 0, node_id: int = None):
    if bytes_downloaded <= 0 and bytes_uploaded <= 0 and node_id is None:
        return

    today_str = get_shanghai_time().date().isoformat()

    with Session(engine) as session:
        stats = session.exec(select(TrafficStats).where(TrafficStats.date == today_str)).first()

        if not stats:
            stats = TrafficStats(date=today_str)
            session.add(stats)

        stats.download_bytes += max(0, bytes_downloaded)
        stats.upload_bytes += max(0, bytes_uploaded)
        stats.request_count += 1

        if node_id is not None and bytes_downloaded > 0:
            node = session.get(ProxyNode, node_id)
            if node:
                node.download_bytes += bytes_downloaded
                session.add(node)

        session.commit()


# ============================================================
#  待定拉取管理
# ============================================================


def _cleanup_pending(now: datetime = None):
    now = now or datetime.now()
    for k in list(_pending_pulls.keys()):
        v = _pending_pulls[k]
        ttl = _INFLIGHT_TTL if v.get("pull_id") else _PENDING_TTL
        if (now - v["ts"]).total_seconds() > ttl:
            _pending_pulls.pop(k, None)
            logger.debug(f"[pull-pending] 过期丢弃: {k}")


def mark_pending_pull(
    image: str,
    tag: str,
    client_ip: str,
    node_id: int = None,
    node_name: str = None,
):
    """
    manifest 请求成功时调用。写入内存待定区，等 blob 流量确认拉取。
    """
    if not image or not client_ip:
        return
    norm = _normalize_image(image)
    if not norm:
        return

    key = f"{client_ip}_{norm}"
    _pending_pulls[key] = {
        "image": image,
        "tag": tag,
        "client_ip": client_ip,
        "node_id": node_id,
        "node_name": node_name,
        "ts": datetime.now(),
        "pull_id": None,
    }
    _cleanup_pending()
    logger.debug(f"[pull-pending] 记录待定拉取: {key} tag={tag}")


def ensure_pull_record(image: str, client_ip: str) -> int | None:
    """
    确保存在一条拉取记录：
      - 首次调用：创建 PullHistory，记下 pull_id
      - 后续调用：返回同一个 pull_id（用于累加字节）

    该函数内没有 await，在 asyncio 单线程中整体原子执行，天然并发安全。
    """
    if not image or not client_ip:
        return None
    norm = _normalize_image(image)
    if not norm:
        return None

    key = f"{client_ip}_{norm}"
    pending = _pending_pulls.get(key)
    if not pending:
        logger.debug(f"[pull-pending] 未命中待定条目: {key}")
        return None

    # 已提升 → 复用
    if pending.get("pull_id"):
        pending["ts"] = datetime.now()
        return pending["pull_id"]

    # 首次提升 → 创建记录
    with Session(engine) as session:
        pull = PullHistory(
            image=pending["image"],
            tag=pending["tag"],
            client_ip=pending["client_ip"],
            node_id=pending["node_id"],
            node_name=pending["node_name"],
            status="success",
        )
        session.add(pull)
        session.commit()
        session.refresh(pull)

    pending["pull_id"] = pull.id
    pending["ts"] = datetime.now()
    logger.info(f"[pull] 待定拉取提升为记录 id={pull.id} " f"image={pending['image']} tag={pending['tag']} " f"client={pending['client_ip']}")
    return pull.id


def add_bytes_to_pull(pull_id: int, bytes_added: int):
    """按 pull_id 精确累加字节，每个 blob 请求一次。"""
    if not pull_id or bytes_added <= 0:
        return
    try:
        with Session(engine) as session:
            pull = session.get(PullHistory, pull_id)
            if pull:
                pull.download_bytes = (pull.download_bytes or 0) + bytes_added
                session.add(pull)
                session.commit()
    except Exception as e:
        logger.error(f"累加拉取字节失败: {e}")


# ============================================================
#  直接记录（用于失败等场景的兼容路径）
# ============================================================


def log_pull(
    image: str,
    tag: str,
    client_ip: str,
    node_id: int = None,
    node_name: str = None,
    status: str = "success",
    error_message: str = None,
) -> int | None:
    """
    直接写一条拉取历史（带内存 + 数据库双重去重）。
    常规成功路径请使用 mark_pending_pull + ensure_pull_record。
    """
    now = datetime.now()

    if status == "success":
        if len(_recent_pulls) > _MAX_RECENT_PULLS:
            cutoff = now - timedelta(seconds=_DEDUP_WINDOW * 2)
            for k in [k for k, v in _recent_pulls.items() if v < cutoff]:
                _recent_pulls.pop(k, None)

        norm_image = _normalize_image(image) or image
        cache_key = f"{client_ip}_{norm_image}_{tag}"

        last_seen = _recent_pulls.get(cache_key)
        if last_seen is not None:
            if (now - last_seen).total_seconds() < _DEDUP_WINDOW:
                return None

        try:
            with Session(engine) as session:
                cutoff_db = get_shanghai_time() - timedelta(seconds=_DEDUP_WINDOW)
                existing = session.exec(
                    select(PullHistory)
                    .where(PullHistory.image.contains(norm_image))
                    .where(PullHistory.tag == tag)
                    .where(PullHistory.client_ip == client_ip)
                    .where(PullHistory.status == "success")
                    .where(PullHistory.request_time > cutoff_db)
                    .order_by(PullHistory.request_time.desc())
                    .limit(1)
                ).first()
                if existing:
                    _recent_pulls[cache_key] = now
                    return existing.id
        except Exception as e:
            logger.error(f"拉取去重检查失败: {e}")

        _recent_pulls[cache_key] = now

    with Session(engine) as session:
        pull = PullHistory(
            image=image,
            tag=tag,
            client_ip=client_ip,
            node_id=node_id,
            node_name=node_name,
            status=status,
            error_message=error_message,
        )
        session.add(pull)
        session.commit()
        session.refresh(pull)
        logger.info(f"[pull] 新增记录 id={pull.id} image={image} tag={tag} " f"client={client_ip} status={status}")
        return pull.id


# ============================================================
#  查询 / 清理
# ============================================================


def get_pull_history(limit: int = 100):
    with Session(engine) as session:
        return session.exec(select(PullHistory).order_by(PullHistory.request_time.desc()).limit(limit)).all()


def get_total_pull_count() -> int:
    with Session(engine) as session:
        return session.exec(select(func.count(PullHistory.id))).one()


def get_pull_stats() -> dict:
    with Session(engine) as session:
        total = session.exec(select(func.count(PullHistory.id))).one()
        success = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "success")).one()
        failed = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "failed")).one()
        cancelled = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "cancelled")).one()
    return {
        "total": total or 0,
        "success": success or 0,
        "failed": failed or 0,
        "cancelled": cancelled or 0,
    }


def get_traffic_stats(days: int = 30):
    with Session(engine) as session:
        return session.exec(select(TrafficStats).order_by(TrafficStats.date.desc()).limit(days)).all()


def clear_pull_history():
    _recent_pulls.clear()
    _pending_pulls.clear()
    with Session(engine) as session:
        session.exec(PullHistory.__table__.delete())
        session.commit()
