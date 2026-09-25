"""数据库引擎与会话管理。

SQLite 下所有写事务都以 ``BEGIN IMMEDIATE`` 开启：导入事件与签发冻结因此
串行化，冻结在同一个写事务内读取截止序位、重放并落库，避免并发导入改变
已签发结果。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL


def make_sqlite_engine(url: str, *, check_same_thread: bool) -> Engine:
    """创建带写锁与迁移钩子的 SQLite 引擎。"""
    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args={"timeout": 30, "check_same_thread": check_same_thread},
    )

    @event.listens_for(engine, "connect")
    def _disable_driver_autobegin(dbapi_connection, _record):  # noqa: ANN001
        # 让 SQLAlchemy 自己控制事务边界，避免驱动隐式 BEGIN 与下面的
        # BEGIN IMMEDIATE 冲突。
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn) -> None:  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


engine: Engine = make_sqlite_engine(DATABASE_URL, check_same_thread=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：提供一个请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
