import logging
from datetime import datetime, timedelta
from sqlmodel import Session, select, func

from app.database import engine
from app.models import TrafficStats, PullHistory, ProxyNode, get_shanghai_time

logger = logging.getLogger("dockermirrorflow.traffic")

_recent_pulls: dict[str, datetime] = {}


def log_traffic(bytes_downloaded: int = 0, bytes_uploaded: int = 0, node_id: int = None):
    today_str = get_shanghai_time().date().isoformat()

    with Session(engine) as session:
        stats = session.exec(select(TrafficStats).where(TrafficStats.date == today_str)).first()

        if not stats:
            stats = TrafficStats(date=today_str)
            session.add(stats)

        stats.download_bytes += bytes_downloaded
        stats.upload_bytes += bytes_uploaded
        stats.request_count += 1

        if node_id is not None:
            node = session.get(ProxyNode, node_id)
            if node:
                node.download_bytes += bytes_downloaded
                session.add(node)

        session.commit()


def log_pull(
    image: str,
    tag: str,
    client_ip: str,
    node_id: int = None,
    node_name: str = None,
    status: str = "success",
    error_message: str = None,
):
    """
    记录一条镜像拉取历史。
    status: success / failed / cancelled
    """
    now = datetime.now()

    # 去重：成功记录在 15 秒内不重复写
    if status == "success":
        if len(_recent_pulls) > 1000:
            cutoff = now - timedelta(seconds=60)
            for k in [k for k, v in _recent_pulls.items() if v < cutoff]:
                del _recent_pulls[k]

        cache_key_exact = f"{client_ip}_{image}_{tag}"
        cache_key_image = f"{client_ip}_{image}"

        if cache_key_exact in _recent_pulls:
            if (now - _recent_pulls[cache_key_exact]).total_seconds() < 15:
                return

        if tag.startswith("sha256:"):
            if cache_key_image in _recent_pulls:
                if (now - _recent_pulls[cache_key_image]).total_seconds() < 15:
                    return

        _recent_pulls[cache_key_exact] = now
        _recent_pulls[cache_key_image] = now

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


def get_pull_history(limit: int = 100):
    with Session(engine) as session:
        return session.exec(select(PullHistory).order_by(PullHistory.request_time.desc()).limit(limit)).all()


def get_total_pull_count() -> int:
    with Session(engine) as session:
        return session.exec(select(func.count(PullHistory.id))).one()


def get_pull_stats() -> dict:
    """按状态统计拉取次数"""
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
    with Session(engine) as session:
        session.exec(PullHistory.__table__.delete())
        session.commit()
