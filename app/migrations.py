"""旧数据的安全迁移。

事件序位模型上线后，旧库的 ``events`` / ``freezes`` 表缺少
``seq`` / ``event_cutoff_seq`` 列与唯一索引。迁移幂等：

* ``events.seq`` 按事件实际到达服务端的先后（自增主键 ``id``，即行号）
  逐培养方案从 1 连续回填。到达顺序是唯一可靠的因果序：签到区间合并、
  修正量累加都与顺序无关，导师确认在业务上必然晚于其签到，因此旧冻结
  按序位重放结果与当时一致；同时迟到补录（无论编号大小）序位必然更大，
  不会渗入历史截止点。
* ``freezes.event_cutoff_seq`` 取"当时落库快照内出现的事件序位最大值"
  与"截止编号对应事件序位"两者的较大值：前者覆盖以编号形式查不到的
  场景，后者保证截止事件本身（可能是导师确认，不出现在快照明细里）
  被包含。快照 JSON 始终是冻结事实的最终来源。
* 全部回填完成后才建立 (plan_version, seq) 唯一索引。

迁移可以重复执行，也可以与建表同时存在（create_all 已创建同名列/索引时
迁移自动跳过对应步骤）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _add_column_if_missing(conn, table: str, column: str, ddl_type: str) -> bool:  # noqa: ANN001
    cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    if column in cols:
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
    return True


def _seq_for_event_id(conn, plan_version: str, event_id: str | None) -> int:  # noqa: ANN001
    if event_id is None:
        return 0
    value = conn.execute(
        text(
            "SELECT seq FROM events WHERE plan_version = :pv AND event_id = :eid"
        ),
        {"pv": plan_version, "eid": event_id},
    ).scalar_one_or_none()
    return int(value) if value is not None else 0


def _snapshot_event_ids(snapshot: Any) -> set[str]:
    data = json.loads(snapshot) if isinstance(snapshot, str) else dict(snapshot)
    event_ids: set[str] = set()
    for student in data.get("students", []):
        for checkin in student.get("checkins", []):
            event_ids.add(checkin.get("event_id"))
        for adjustment in student.get("adjustments", []):
            event_ids.add(adjustment.get("event_id"))
    event_ids.discard(None)
    return event_ids


def _max_seq_for_ids(conn, plan_version: str, event_ids: set[str]) -> int:  # noqa: ANN001
    if not event_ids:
        return 0
    ids = sorted(event_ids)
    rows = conn.execute(
        text(
            "SELECT MAX(seq) FROM events WHERE plan_version = :pv AND event_id IN ("
            + ",".join(f":e{i}" for i in range(len(ids)))
            + ")"
        ),
        {"pv": plan_version, **{f"e{i}": eid for i, eid in enumerate(ids)}},
    ).first()
    return int(rows[0]) if rows and rows[0] is not None else 0


def run_migrations(engine: Engine) -> None:
    """幂等地把旧库迁移到单调序位模型。"""
    with engine.begin() as conn:
        _add_column_if_missing(conn, "events", "seq", "INTEGER")
        _add_column_if_missing(conn, "freezes", "event_cutoff_seq", "INTEGER")

        # 按事件到达顺序（自增 id / rowid）回填序位；只补尚未分配的行。
        conn.execute(
            text(
                """
                UPDATE events
                SET seq = (
                    SELECT COUNT(*)
                    FROM events AS e2
                    WHERE e2.plan_version = events.plan_version
                      AND e2.id <= events.id
                )
                WHERE events.seq IS NULL
                """
            )
        )

        # 回填冻结截止序位。截止点之后的迟到补录序位更大，永不渗入；
        # 截止点本身（可能是不出现在快照明细里的导师确认）必须包含。
        stale = conn.execute(
            text(
                "SELECT plan_version, freeze_id, event_cutoff_id, snapshot "
                "FROM freezes WHERE event_cutoff_seq IS NULL"
            )
        ).fetchall()
        for plan_version, freeze_id, cutoff_id, snapshot in stale:
            content_seq = _max_seq_for_ids(
                conn, plan_version, _snapshot_event_ids(snapshot)
            )
            cutoff_seq = max(content_seq, _seq_for_event_id(conn, plan_version, cutoff_id))
            conn.execute(
                text(
                    "UPDATE freezes SET event_cutoff_seq = :seq "
                    "WHERE plan_version = :pv AND freeze_id = :fid"
                ),
                {"seq": cutoff_seq, "pv": plan_version, "fid": freeze_id},
            )

        indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(events)"))}
        if "uq_events_plan_seq" not in indexes:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_events_plan_seq "
                    "ON events (plan_version, seq)"
                )
            )
