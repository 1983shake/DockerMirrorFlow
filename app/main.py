import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import config
from app.database import create_db_and_tables, upgrade_db
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

# ========== 第三方库日志降噪 ==========
# 这些库默认把每次 HTTP 请求都打 INFO 日志，造成日志膨胀
# 可通过 config.logging.third_party_level 调整
#   可选值: "DEBUG" / "INFO" / "WARNING" / "ERROR" / "CRITICAL"
#   默认 "WARNING"，只打印失败请求
_third_level = getattr(
    logging,
    str(config.logging.third_party_level).upper(),
    logging.WARNING,
)
for _noisy_logger in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
    logging.getLogger(_noisy_logger).setLevel(_third_level)

logger = logging.getLogger("dockermirrorflow")

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info(f"  {config.app.name}  {config.app.tagline}")
    logger.info("=" * 60)

    logger.info("初始化数据库...")
    create_db_and_tables()
    upgrade_db()

    logger.info("加载配置中的自定义节点...")
    proxy_manager.init_proxies()

    logger.info("执行初始健康检查...")
    try:
        await proxy_manager.run_health_check()
    except Exception as e:
        logger.error(f"初始健康检查失败: {e}")

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
    scheduler.add_job(
        proxy_manager.cleanup_caches,
        "interval",
        minutes=10,
        id="cleanup_caches",
        replace_existing=True,
    )
    scheduler.start()

    yield

    logger.info("关闭调度器...")
    scheduler.shutdown()


app = FastAPI(
    title=config.app.name,
    description=f"{config.app.tagline} —— 多 Registry 镜像代理加速服务",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(web_ui.router)
app.include_router(docker_proxy.router)


if __name__ == "__main__":
    import uvicorn

    # ⚠️ 强制单 worker：
    #   1. SQLite 不支持多进程并发写
    #   2. APScheduler 是进程内调度器，多 worker 会重复执行定时任务
    #   3. 内存熔断/粘性状态多进程不同步
    _FORCED_WORKERS = 1

    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
        workers=_FORCED_WORKERS,
    )
