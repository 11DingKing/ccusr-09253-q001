"""旧库迁移与重启读取测试：基于真实 SQLite 文件验证安全升级。"""

from __future__ import annotations

import json

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models import Base
from app.repository import current_cutoff, insert_events, load_events
from app.services import freeze_semester, get_frozen_snapshot

LEGACY_SCHEMA = """
CREATE TABLE plans (
    plan_version VARCHAR(128) NOT NULL PRIMARY KEY,
    iana_timezone VARCHAR(64) NOT NULL,
    required_seconds INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_plans_required_nonneg CHECK (required_seconds >= 0)
);
CREATE TABLE events (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(128) NOT NULL,
    plan_version VARCHAR(128) NOT NULL,
    student_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_events_event_id_plan UNIQUE (event_id, plan_version)
);
CREATE INDEX ix_events_plan_student ON events (plan_version, student_id);
CREATE TABLE freezes (
    plan_version VARCHAR(128) NOT NULL,
    freeze_id VARCHAR(128) NOT NULL,
    snapshot JSON NOT NULL,
    event_cutoff_id VARCHAR(128),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (plan_version, freeze_id)
);
"""

PLAN = ("P-LEGACY", "Asia/Shanghai", 3600)

# 历史库中先入库 E-10（rowid 1），后入库 E-2（rowid 2）——旧代码按字典序
# 会把 E-10 排在 E-2 之前，而服务端真实顺序恰好相反。
LEGACY_EVENTS = [
    (
        "E-10",
        "S1",
        "checkin",
        {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": "2024-03-15T08:00:00+08:00",
            "check_out_at": "2024-03-15T09:00:00+08:00",
        },
    ),
    (
        "E-2",
        "S1",
        "checkin",
        {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": "2024-03-15T09:00:00+08:00",
            "check_out_at": "2024-03-15T10:00:00+08:00",
        },
    ),
]

# 旧版本签发的冻结：只有 event_cutoff_id，没有 seq 字段。
LEGACY_FREEZE_SNAPSHOT = {
    "plan_version": "P-LEGACY",
    "freeze_id": "F-OLD",
    "timezone": "Asia/Shanghai",
    "required_seconds": 3600,
    "generated_at": "2024-06-30T00:00:00Z",
    "event_cutoff_id": "E-2",
    "students": [
        {
            "student_id": "S1",
            "confirmed_seconds": 7200,
            "pending_seconds": 0,
            "adjustment_seconds": 0,
            "total_seconds": 7200,
            "lesson_units": 2,
            "pending_lesson_units": 0,
            "meets_requirement": True,
            "daily": [{"academic_day": "2024-03-15", "seconds": 7200}],
            "checkins": [],
            "adjustments": [],
        }
    ],
}


def _build_legacy_db(path) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(text(statement))
        conn.execute(
            text(
                "INSERT INTO plans (plan_version, iana_timezone, required_seconds,"
                " created_at) VALUES (:pv, :tz, :req, '2024-01-01 00:00:00')"
            ),
            {"pv": PLAN[0], "tz": PLAN[1], "req": PLAN[2]},
        )
        for event_id, student_id, event_type, payload in LEGACY_EVENTS:
            conn.execute(
                text(
                    "INSERT INTO events (event_id, plan_version, student_id,"
                    " event_type, payload, created_at) VALUES (:eid, :pv, :sid,"
                    " :etype, :payload, '2024-03-20 00:00:00')"
                ),
                {
                    "eid": event_id,
                    "pv": PLAN[0],
                    "sid": student_id,
                    "etype": event_type,
                    "payload": json.dumps(payload),
                },
            )
        conn.execute(
            text(
                "INSERT INTO freezes (plan_version, freeze_id, snapshot,"
                " event_cutoff_id, created_at) VALUES (:pv, :fid, :snap, :cut,"
                " '2024-06-30 00:00:00')"
            ),
            {
                "pv": PLAN[0],
                "fid": "F-OLD",
                "snap": json.dumps(LEGACY_FREEZE_SNAPSHOT),
                "cut": "E-2",
            },
        )
    engine.dispose()


def _open(path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    return engine


def test_legacy_database_migrates_and_survives_restart(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path)

    # 第一次“启动”：迁移旧库。
    engine = _open(db_path)
    Base.metadata.create_all(engine)
    run_migrations(engine)

    with Session(engine) as session:
        events = load_events(session, PLAN[0])
        # 序位按历史入库顺序回填：E-10 -> 1，E-2 -> 2。
        assert [(e.event_id, e.seq) for e in events] == [("E-10", 1), ("E-2", 2)]
        assert current_cutoff(session, PLAN[0]) == (2, "E-2")

        # 历史冻结原样可复现，且补上了数值截止序位。
        frozen = get_frozen_snapshot(session, PLAN[0], "F-OLD")
        assert frozen.event_cutoff_id == "E-2"
        assert frozen.students[0]["total_seconds"] == 7200
        assert frozen.generated_at == "2024-06-30T00:00:00Z"

        # 迁移后新事件继续递增，不复用历史序位。
        accepted, duplicates = insert_events(
            session,
            plan_version=PLAN[0],
            events=[
                {
                    "event_id": "E-3",
                    "student_id": "S1",
                    "event_type": "leave_correction",
                    "payload": {"adjustment_seconds": 600, "reason": "late"},
                }
            ],
        )
        assert accepted == ["E-3"] and duplicates == []
        events = load_events(session, PLAN[0])
        assert [(e.event_id, e.seq) for e in events] == [
            ("E-10", 1),
            ("E-2", 2),
            ("E-3", 3),
        ]

        # 新冻结使用数值截止点。
        snap, created = freeze_semester(session, plan_version=PLAN[0], freeze_id="F-NEW")
        assert created is True
        assert snap.event_cutoff_seq == 3
        assert snap.event_cutoff_id == "E-3"
        assert snap.students[0]["student_id"] == "S1"
        assert snap.students[0]["total_seconds"] == 7200 + 600
    engine.dispose()

    # 模拟进程重启：全新引擎重新打开同一文件。
    engine2 = _open(db_path)
    Base.metadata.create_all(engine2)
    run_migrations(engine2)  # 幂等：重复执行不改变任何数据
    with Session(engine2) as session:
        events = load_events(session, PLAN[0])
        assert [(e.event_id, e.seq) for e in events] == [
            ("E-10", 1),
            ("E-2", 2),
            ("E-3", 3),
        ]
        frozen_old = get_frozen_snapshot(session, PLAN[0], "F-OLD")
        assert frozen_old.event_cutoff_id == "E-2"
        assert frozen_old.students[0]["total_seconds"] == 7200
        frozen_new = get_frozen_snapshot(session, PLAN[0], "F-NEW")
        assert frozen_new.event_cutoff_seq == 3
        assert frozen_new.students[0]["total_seconds"] == 7800

        # 重启后继续导入，序位从 4 开始。
        accepted, _ = insert_events(
            session,
            plan_version=PLAN[0],
            events=[
                {
                    "event_id": "E-4",
                    "student_id": "S1",
                    "event_type": "leave_correction",
                    "payload": {"adjustment_seconds": 60},
                }
            ],
        )
        assert accepted == ["E-4"]
        assert current_cutoff(session, PLAN[0]) == (4, "E-4")
    engine2.dispose()


def test_migration_is_idempotent_on_fresh_database(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = _open(db_path)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    run_migrations(engine)  # 第二次执行无副作用
    with Session(engine) as session:
        accepted, _ = insert_events(
            session,
            plan_version="P-FRESH",
            events=[
                {
                    "event_id": "E-1",
                    "student_id": "S1",
                    "event_type": "leave_correction",
                    "payload": {"adjustment_seconds": 1},
                }
            ],
        )
        assert accepted == ["E-1"]
        assert current_cutoff(session, "P-FRESH") == (1, "E-1")
    engine.dispose()
