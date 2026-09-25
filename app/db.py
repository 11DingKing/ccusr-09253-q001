"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL

_connect_args: dict[str, object] = {}
if DATABASE_URL.startswith("sqlite"):
    # Wait on contended writes (concurrent event imports / freezes) instead
    # of failing immediately with "database is locked".
    _connect_args = {"timeout": 30}

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
