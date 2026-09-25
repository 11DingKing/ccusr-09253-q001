"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, PlanEventCounter


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


def _reserve_seq_block(db: Session, plan_version: str, count: int) -> int:
    """Atomically reserve ``count`` positions; return the first new seq.

    ``next_seq`` always holds the next value to allocate (one past the high
    water mark). A single UPSERT takes SQLite's write lock, serializing
    concurrent importers so no two transactions can reserve the same seq.
    """
    stmt = sqlite_insert(PlanEventCounter).values(
        plan_version=plan_version, next_seq=1 + count
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={"next_seq": PlanEventCounter.next_seq + count},
    ).returning(PlanEventCounter.next_seq)
    next_to_allocate = db.execute(stmt).scalar_one()
    return int(next_to_allocate) - count


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """导入事件并按提交顺序分配服务端单调序位。

    重复事件（同 plan + event_id）幂等忽略，且不会挤占后续序位之外的任何
    既有结果；预留给重复事件的序位允许出现空洞，但绝不复用。
    """
    accepted: list[str] = []
    duplicates: list[str] = []
    if not events:
        return accepted, duplicates

    first_seq = _reserve_seq_block(db, plan_version, len(events))

    for offset, e in enumerate(events):
        stmt = sqlite_insert(EventModel).values(
            seq=first_seq + offset,
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.event_id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(inserted_id)
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.seq, EventModel.id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to_seq(
    db: Session, plan_version: str, cutoff_seq: int
) -> list[CoreEvent]:
    """读取序位不超过截止点的全部事件（服务端持久化顺序）。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.seq <= cutoff_seq)
        .order_by(EventModel.seq, EventModel.id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def current_cutoff(db: Session, plan_version: str) -> tuple[int, str] | None:
    """返回当前最高序位及其业务编号；无事件时为 None。"""
    stmt = (
        select(EventModel.seq, EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.seq.desc())
        .limit(1)
    )
    row = db.execute(stmt).first()
    if row is None:
        return None
    return int(row[0]), row[1]


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
    """保存冻结快照；同一冻结编号重复提交时保持既有结果不变。"""
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
