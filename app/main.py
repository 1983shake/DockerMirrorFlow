import logging
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

    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
        workers=config.server.workers if not config.server.debug else 1,
    )