from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import inspect, text
import logging
import os

logger = logging.getLogger("dockermirrorflow.database")

# 确保数据目录存在
os.makedirs("data", exist_ok=True)

sqlite_file_name = "data/dockermirrorflow.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

# check_same_thread=False 是 SQLite + FastAPI/Threads 所必需的
engine = create_engine(
    sqlite_url,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def upgrade_db():
    """检查缺失的列并添加（自动迁移）。"""
    try:
        inspector = inspect(engine)

        if inspector.has_table("proxynode"):
            columns = [c["name"] for c in inspector.get_columns("proxynode")]
            new_columns = {
                "registry_type": "VARCHAR DEFAULT 'dockerhub'",
                "route_prefix": "VARCHAR",
                "failure_reason": "VARCHAR",
                "download_bytes": "INTEGER NOT NULL DEFAULT 0",
                "is_custom": "BOOLEAN DEFAULT 0",
                "manually_disabled": "BOOLEAN DEFAULT 0",
                "manual_disable_reason": "VARCHAR",
                "manual_disable_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            }
            with engine.connect() as conn:
                for col, col_type in new_columns.items():
                    if col not in columns:
                        logger.info(f"迁移: 添加 {col} 列到 proxynode")
                        conn.execute(
                            text(f"ALTER TABLE proxynode ADD COLUMN {col} {col_type}")
                        )
                conn.commit()

        if not inspector.has_table("healthchecklog"):
            SQLModel.metadata.create_all(engine)

    except Exception as e:
        logger.error(f"迁移失败: {e}")