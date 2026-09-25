"""测试夹具。"""
from __future__ import annotations

import os
from collections.abc import Iterator

# 必须在导入任何 app.* 模块前指定测试库：app.db 在导入时按 DATABASE_URL 建引擎。
os.environ.setdefault("DATABASE_URL", "sqlite:///./practice_hours_test.db")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db, make_sqlite_engine
from app.main import app
from app.migrations import run_migrations
from app.models import Base

test_engine = make_sqlite_engine(
    "sqlite:///./practice_hours_test.db",
    check_same_thread=False,
)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)

@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    # 与生产启动路径一致：先建表，再执行幂等迁移（重复执行必须安全）。
    Base.metadata.create_all(test_engine)
    run_migrations(test_engine)
    run_migrations(test_engine)
    yield
    Base.metadata.drop_all(test_engine)

@pytest.fixture
def db() -> Iterator[Session]:
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

SHANGHAI_PLAN = {"plan_version": "P-SH-2024", "iana_timezone": "Asia/Shanghai", "required_seconds": 10800}
NY_PLAN = {"plan_version": "P-NY-2024", "iana_timezone": "America/New_York", "required_seconds": 3600}
