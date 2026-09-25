"""混合编号、迟到补录与冻结截止序位的 API 测试。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN
from app.repository import load_events


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    return SHANGHAI_PLAN["plan_version"]


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def test_mixed_event_ids_follow_server_sequence_not_lexicographic_order(client, db):
    """短数字、业务前缀混排时，顺序只认服务端序位。

    字典序下 "A-CONFIRM" < "EVENT-10" < "EVENT-9"，导师确认会先于其
    签到被处理而失效；按服务端序位则确认正常生效。
    """
    pv = _create_plan(client)
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "EVENT-9",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T12:00:00+08:00",
                    activity_type="internship",
                ),
                _checkin(
                    "EVENT-10",
                    "S1",
                    "2024-03-15T13:00:00+08:00",
                    "2024-03-15T14:00:00+08:00",
                ),
                {
                    "event_id": "A-CONFIRM",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "EVENT-9"},
                },
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["accepted"] == 3

    # 服务端序位严格按提交次序分配，与编号格式无关。
    events = load_events(db, pv)
    assert [e.event_id for e in events] == ["EVENT-9", "EVENT-10", "A-CONFIRM"]
    assert [e.seq for e in events] == [1, 2, 3]

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["confirmed_seconds"] == 5 * 3600
    assert progress["pending_seconds"] == 0


def test_short_numeric_and_prefixed_ids_are_all_accepted_and_idempotent(client):
    pv = _create_plan(client)
    payload = {
        "events": [
            _checkin(
                "9",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T09:00:00+08:00",
                activity_type="internship",
            ),
            {
                "event_id": "1",
                "event_type": "mentor_confirm",
                "student_id": "S1",
                "payload": {"checkin_event_id": "9"},
            },
            _checkin(
                "HR-2024-0002",
                "S1",
                "2024-03-15T10:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
            ),
        ]
    }
    resp = client.post(f"/api/plans/{pv}/events", json=payload)
    assert resp.json()["accepted"] == 3
    # 字典序下 "1" < "9"，旧模型会漏掉这次确认；序位模型下照常生效。
    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["confirmed_seconds"] == 2 * 3600
    assert progress["pending_seconds"] == 0

    resp = client.post(f"/api/plans/{pv}/events", json=payload)
    body = resp.json()
    assert body["accepted"] == 0
    assert set(body["duplicates"]) == {"9", "1", "HR-2024-0002"}


def test_late_low_id_event_never_leaks_into_prior_freeze_and_diff_tracks_seq(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-2",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )
    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert f1["event_cutoff_id"] == "E-2"
    assert f1["event_cutoff_seq"] == 1
    assert f1["students"][0]["total_seconds"] == 7200

    # 迟到补录，编号字典序反而排在 E-2 之前。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-1",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 3600, "reason": "late make-up"},
                }
            ]
        },
    )

    # 历史冻结不变：截止点是持久化序位而非编号。
    stored = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert stored["event_cutoff_id"] == "E-2"
    assert stored["event_cutoff_seq"] == 1
    assert stored["students"][0]["total_seconds"] == 7200

    # 实时视图包含迟到事件。
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 10800

    # 新冻结以更大的序位为截止点（注意其编号 E-1 字典序更小）。
    f2 = client.post(f"/api/plans/{pv}/freezes/F-02", json={}).json()
    assert f2["event_cutoff_id"] == "E-1"
    assert f2["event_cutoff_seq"] == 2
    assert f2["students"][0]["total_seconds"] == 10800

    diff = client.get(f"/api/plans/{pv}/freezes/F-01/diff/F-02").json()
    assert diff["old_event_cutoff_id"] == "E-2"
    assert diff["new_event_cutoff_id"] == "E-1"
    assert diff["old_event_cutoff_seq"] == 1
    assert diff["new_event_cutoff_seq"] == 2
    change = diff["student_changes"][0]["fields"]["total_seconds"]
    assert change == {"before": 7200, "after": 10800}

    # 重复冻结不改变已签发结果，重复幂等导入不推进序位。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-1",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 3600},
                }
            ]
        },
    )
    f1_repost = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert f1_repost["event_cutoff_seq"] == 1
    assert f1_repost["students"][0]["total_seconds"] == 7200
    f3 = client.post(f"/api/plans/{pv}/freezes/F-03", json={}).json()
    assert f3["event_cutoff_seq"] == 2
    assert f3["students"][0]["total_seconds"] == 10800
