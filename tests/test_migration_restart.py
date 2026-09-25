"""旧库迁移与重启读取测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import make_sqlite_engine
from app.migrations import run_migrations
from app.models import Base
from app.core.replay import Event, EventType
from app.core.snapshot import build_snapshot
from app import services


PV = "P-LEGACY"

# 上线前的旧表结构（无 seq / event_cutoff_seq，无 plan+seq 唯一索引）。
_LEGACY_PLANS_DDL = """
CREATE TABLE plans (
    plan_version VARCHAR(128) PRIMARY KEY,
    iana_timezone VARCHAR(64) NOT NULL,
    required_seconds INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_LEGACY_EVENTS_DDL = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(128) NOT NULL,
    plan_version VARCHAR(128) NOT NULL,
    student_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_events_event_id_plan UNIQUE (event_id, plan_version)
)
"""
_LEGACY_FREEZES_DDL = """
CREATE TABLE freezes (
    plan_version VARCHAR(128) NOT NULL,
    freeze_id VARCHAR(128) NOT NULL,
    snapshot JSON NOT NULL,
    event_cutoff_id VARCHAR(128),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_version, freeze_id)
)
"""


def _checkin_payload() -> dict:
    return {
        "activity_id": "A1",
        "activity_type": "regular",
        "check_in_at": "2024-03-15T08:00:00+08:00",
        "check_out_at": "2024-03-15T10:00:00+08:00",
    }


def _build_legacy_db(path: str) -> None:
    raw = create_engine(f"sqlite:///{path}", future=True)
    with raw.begin() as conn:
        conn.execute(text(_LEGACY_PLANS_DDL))
        conn.execute(text(_LEGACY_EVENTS_DDL))
        conn.execute(text(_LEGACY_FREEZES_DDL))
        conn.execute(
            text(
                "CREATE INDEX ix_events_plan_student ON events "
                "(plan_version, student_id)"
            )
        )
        conn.execute(text("CREATE INDEX ix_events_plan_version ON events (plan_version)"))
        conn.execute(text("CREATE INDEX ix_events_student_id ON events (student_id)"))
        conn.execute(
            text(
                "INSERT INTO plans (plan_version, iana_timezone, required_seconds, created_at)"
                " VALUES (:pv, :tz, :req, :now)"
            ),
            {"pv": PV, "tz": "Asia/Shanghai", "req": 10800,
             "now": datetime.now(timezone.utc)},
        )
        # 冻结时只存在 E-02（2 小时签到）。
        conn.execute(
            text(
                "INSERT INTO events (event_id, plan_version, student_id, event_type, payload)"
                " VALUES ('E-02', :pv, 'S1', 'checkin', :payload)"
            ),
            {"pv": PV, "payload": json.dumps(_checkin_payload())},
        )
        old_event = Event(
            event_id="E-02",
            plan_version=PV,
            event_type=EventType.CHECKIN,
            student_id="S1",
            payload=_checkin_payload(),
            created_at=datetime.now(timezone.utc),
        )
        snap = build_snapshot(
            [old_event],
            plan_version=PV,
            timezone_name="Asia/Shanghai",
            required_seconds=10800,
            freeze_id="F-OLD",
            event_cutoff_id="E-02",
        ).to_dict()
        # 旧服务落库的快照没有 event_cutoff_seq 字段。
        snap.pop("event_cutoff_seq", None)
        conn.execute(
            text(
                "INSERT INTO freezes (plan_version, freeze_id, snapshot, event_cutoff_id)"
                " VALUES (:pv, 'F-OLD', :snapshot, 'E-02')"
            ),
            {"pv": PV, "snapshot": json.dumps(snap)},
        )
        # 冻结之后才有迟到补录 E-01（字典序反而在前）。
        conn.execute(
            text(
                "INSERT INTO events (event_id, plan_version, student_id, event_type, payload)"
                " VALUES ('E-01', :pv, 'S1', 'leave_correction', :payload)"
            ),
            {"pv": PV,
             "payload": json.dumps({"adjustment_seconds": 3600, "reason": "late"})},
        )
    raw.dispose()


def test_legacy_database_migrates_safely_and_history_stays_reproducible(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(str(db_path))

    engine = make_sqlite_engine(f"sqlite:///{db_path}", check_same_thread=False)
    Base.metadata.create_all(engine)
    # 迁移必须幂等：连续执行两次结果一致。
    run_migrations(engine)
    run_migrations(engine)

    NewSession = sessionmaker(bind=engine, future=True)
    with NewSession() as db:
        # 序位按到达顺序（自增 id）回填：E-02 先到为 1，迟到的 E-01 为 2。
        seqs = dict(
            db.execute(text("SELECT event_id, seq FROM events ORDER BY seq")).all()
        )
        assert seqs == {"E-02": 1, "E-01": 2}

        frozen = services.get_frozen_snapshot(db, PV, "F-OLD")
        assert frozen.event_cutoff_id == "E-02"
        assert frozen.event_cutoff_seq == 1
        # 已签发结果稳定：迟到的 E-01（字典序反而在前）没有渗入。
        assert frozen.students[0]["total_seconds"] == 7200

        live = services.current_snapshot(db, PV)
        assert live.students[0]["total_seconds"] == 10800

        # 新事件在旧库迁移后正常分配序位（不受回填影响）。
        result = services.import_events(
            db,
            plan_version=PV,
            events=[
                {
                    "event_id": "SYS-X-42",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 1800, "reason": "new"},
                }
            ],
        )
        assert result["accepted"] == 1
        seq = db.execute(
            text("SELECT seq FROM events WHERE event_id = 'SYS-X-42'")
        ).scalar_one()
        assert seq == 3

        # 唯一索引生效：手工写入重复序位必须失败。
        with pytest.raises(Exception):
            db.execute(
                text(
                    "INSERT INTO events (seq, event_id, plan_version, student_id,"
                    " event_type, payload) VALUES (1, 'BOGUS', :pv, 'S1',"
                    " 'leave_correction', '{}')"
                ),
                {"pv": PV},
            )
            db.commit()
        db.rollback()

    engine.dispose()

    # 模拟服务重启：新引擎读取同一文件，建表/迁移均为无操作，序位保持稳定。
    engine2 = make_sqlite_engine(f"sqlite:///{db_path}", check_same_thread=False)
    Base.metadata.create_all(engine2)
    run_migrations(engine2)
    Session2 = sessionmaker(bind=engine2, future=True)
    with Session2() as db:
        seqs = dict(
            db.execute(text("SELECT event_id, seq FROM events ORDER BY seq")).all()
        )
        assert seqs == {"E-02": 1, "E-01": 2, "SYS-X-42": 3}
        frozen = services.get_frozen_snapshot(db, PV, "F-OLD")
        assert frozen.students[0]["total_seconds"] == 7200
        # 重启后签发的新冻结可复现截止序位语义。
        snap, created = services.freeze_semester(db, plan_version=PV, freeze_id="F-NEW")
        assert created is True
        assert snap.event_cutoff_seq == 3
        assert snap.event_cutoff_id == "SYS-X-42"
        assert snap.students[0]["total_seconds"] == 7200 + 3600 + 1800
    engine2.dispose()


def test_empty_plan_freeze_migrates_with_cutoff_zero(tmp_path):
    db_path = tmp_path / "empty_legacy.db"
    raw = create_engine(f"sqlite:///{db_path}", future=True)
    with raw.begin() as conn:
        conn.execute(text(_LEGACY_PLANS_DDL))
        conn.execute(text(_LEGACY_EVENTS_DDL))
        conn.execute(text(_LEGACY_FREEZES_DDL))
        conn.execute(
            text(
                "INSERT INTO plans (plan_version, iana_timezone, required_seconds, created_at)"
                " VALUES ('P0', 'Asia/Shanghai', 0, :now)"
            ),
            {"now": datetime.now(timezone.utc)},
        )
        conn.execute(
            text(
                "INSERT INTO freezes (plan_version, freeze_id, snapshot, event_cutoff_id)"
                " VALUES ('P0', 'F0', :snapshot, NULL)"
            ),
            {"snapshot": json.dumps(
                build_snapshot(
                    [], plan_version="P0", timezone_name="Asia/Shanghai",
                    required_seconds=0, freeze_id="F0",
                ).to_dict()
            )},
        )
    raw.dispose()

    engine = make_sqlite_engine(f"sqlite:///{db_path}", check_same_thread=False)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    NewSession = sessionmaker(bind=engine, future=True)
    with NewSession() as db:
        cutoff = db.execute(
            text("SELECT event_cutoff_seq FROM freezes")
        ).scalar_one()
        assert cutoff == 0
        frozen = services.get_frozen_snapshot(db, "P0", "F0")
        assert frozen.event_cutoff_seq == 0
        assert frozen.students == []
    engine.dispose()
