"""事件与冻结的持久化访问。

写操作依赖调用方会话已处于写事务（见 ``app.db`` 的 BEGIN IMMEDIATE），
因此 ``seq`` 的"取当前最大值 + 1"分配不会与并发导入互相覆盖。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
        seq=row.seq,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """幂等导入；新事件按提交顺序获得本培养方案内单调递增的序位。"""
    accepted: list[str] = []
    duplicates: list[str] = []

    next_seq = db.execute(
        select(EventModel.seq)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_seq = (next_seq or 0) + 1

    for e in events:
        stmt = sqlite_insert(EventModel).values(
            seq=next_seq,
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
            next_seq += 1
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    """按服务端单调序位读取培养方案的全部事件（含迟到补录）。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.seq.asc(), EventModel.id.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to_seq(
    db: Session, plan_version: str, max_seq: int
) -> list[CoreEvent]:
    """读取截止序位及之前的事件——迟到补录的序位更大，不会渗入。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.seq <= max_seq)
        .order_by(EventModel.seq.asc(), EventModel.id.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def get_cutoff(db: Session, plan_version: str) -> tuple[int | None, str | None]:
    """返回当前最大持久化序位及其业务编号；无事件时为 (None, None)。"""
    row = db.execute(
        select(EventModel.seq, EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.seq.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
    event_cutoff_seq: int | None,
) -> Freeze | None:
    """幂等落库冻结；主键冲突时保留先签发的结果。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
        event_cutoff_seq=event_cutoff_seq,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None
