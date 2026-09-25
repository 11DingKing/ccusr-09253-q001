"""服务端应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import engine
from .migrations import run_migrations
from .models import Base
from .routers import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 重启后先建表再执行幂等迁移，旧库安全补齐单调序位列与索引。
    Base.metadata.create_all(engine)
    run_migrations(engine)
    yield


app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay in server-assigned monotonic sequence order and "
        "can be frozen into an immutable snapshot."
    ),
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
