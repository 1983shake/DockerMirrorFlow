import asyncio
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from app.config import config
from app.database import create_db_and_tables, upgrade_db, engine
from app.models import ProxyNode
from app.services import proxy_manager
from app.routers import web_ui, docker_proxy

# ========== 日志配置 ==========
handlers = [logging.StreamHandler()]
try:
    os.makedirs(os.path.dirname(config.logging.file) or "data", exist_ok=True)
    handlers.append(
        RotatingFileHandler(
            config.logging.file,
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
    )
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, config.logging.level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)


# ========== 屏蔽高频轮询接口的访问日志 ==========
class _AccessLogFilter(logging.Filter):
    """
    过滤 uvicorn.access 中的高频噪声请求日志。

    目前屏蔽：
      - /api/tasks/status（前端约 1.5s 轮询一次，纯状态查询）

    后续如有其他高频内部接口，只需追加到 _SUPPRESS_PATHS。
    """

    _SUPPRESS_PATHS: tuple[str, ...] = ("/api/tasks/status",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for path in self._SUPPRESS_PATHS:
            if path in msg:
                return False
        return True


# ========== 第三方库日志降噪 ==========
_third_level = getattr(
    logging,
    str(config.logging.third_party_level).upper(),
    logging.WARNING,
)
for _noisy_logger in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
    logging.getLogger(_noisy_logger).setLevel(_third_level)

# 无论级别如何，都不打印高频轮询日志
logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())

logger = logging.getLogger("dockermirrorflow")

scheduler = AsyncIOScheduler()
_bg_task: asyncio.Task | None = None


def _get_node_count() -> int:
    """统计数据库中的节点数量。"""
    with Session(engine) as session:
        return len(session.exec(select(ProxyNode)).all())


async def _background_startup():
    """后台执行首次拉取、在线检测、速度测试，不阻塞 Web 页面启动。"""
    try:
        # ---------- 首次节点拉取（仅数据库为空时） ----------
        node_count = _get_node_count()
        if node_count == 0:
            if config.auto_fetch.enabled:
                logger.info("数据库无节点，执行首次节点拉取...")
                try:
                    added = await proxy_manager.fetch_and_update_proxies()
                    logger.info(f"首次节点拉取完成，新增 {added} 个节点")
                except Exception as e:
                    logger.error(f"首次节点拉取失败: {e}")
            else:
                logger.warning("数据库无节点，但 auto_fetch.enabled=false，跳过拉取")
        else:
            logger.info(f"数据库已有 {node_count} 个节点，跳过首次拉取")

        # ---------- 初始在线检测 ----------
        logger.info("执行初始在线检测...")
        try:
            await proxy_manager.run_health_check()
        except Exception as e:
            logger.error(f"初始在线检测失败: {e}")

        # ---------- 初始速度测试 ----------
        if config.speed_test.enabled:
            logger.info("执行初始速度测试...")
            try:
                await proxy_manager.run_speed_test()
            except Exception as e:
                logger.error(f"初始速度测试失败: {e}")
        else:
            logger.info("速度测试已禁用，跳过初始速度测试")
    except asyncio.CancelledError:
        logger.info("后台初始化任务被取消")
        raise
    except Exception as e:
        logger.error(f"后台初始化任务异常: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task

    logger.info("=" * 60)
    logger.info(f"  {config.app.name}  {config.app.tagline}")
    logger.info("=" * 60)

    # ---------- 1. 初始化数据库（同步，快速） ----------
    logger.info("初始化数据库...")
    create_db_and_tables()
    upgrade_db()

    # ---------- 2. 加载自定义节点（同步，快速） ----------
    logger.info("加载配置中的自定义节点...")
    proxy_manager.init_proxies()

    # ---------- 3. 启动定时任务调度器 ----------
    logger.info("启动定时任务调度器...")

    if config.auto_fetch.enabled:
        scheduler.add_job(
            proxy_manager.fetch_and_update_proxies,
            "interval",
            minutes=config.auto_fetch.interval_minutes,
            id="auto_fetch",
            replace_existing=True,
        )
    scheduler.add_job(
        proxy_manager.run_health_check,
        "interval",
        minutes=config.health_check.interval_minutes,
        id="health_check",
        replace_existing=True,
    )
    if config.speed_test.enabled:
        scheduler.add_job(
            proxy_manager.run_speed_test,
            "interval",
            minutes=config.speed_test.interval_minutes,
            id="speed_test",
            replace_existing=True,
        )
    scheduler.add_job(
        proxy_manager.cleanup_caches,
        "interval",
        minutes=10,
        id="cleanup_caches",
        replace_existing=True,
    )
    scheduler.start()

    # ---------- 4. 后台执行初始化（不阻塞服务启动） ----------
    _bg_task = asyncio.create_task(_background_startup())

    logger.info("服务已就绪，Web 页面可访问")

    yield

    logger.info("关闭调度器...")
    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
        try:
            await _bg_task
        except (asyncio.CancelledError, Exception):
            pass
    scheduler.shutdown()


app = FastAPI(
    title=config.app.name,
    description=f"{config.app.tagline} —— 多 Registry 镜像代理加速服务",
    version="1.2.0",
    lifespan=lifespan,
)

app.include_router(web_ui.router)
app.include_router(docker_proxy.router)


if __name__ == "__main__":
    import uvicorn

    _FORCED_WORKERS = 1

    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
        workers=_FORCED_WORKERS,
    )
