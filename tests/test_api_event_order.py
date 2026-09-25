"""混合编号、迟到事件与并发导入的顺序模型 API 测试。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


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


def test_mixed_event_ids_follow_server_order_not_lexicographic(client):
    """E-10 按字典序排在 E-2 之前；只有服务端序位能决定先后。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                # 实习签到先入库（序位 1），编号恰好是字典序“较大”的 E-2。
                _checkin(
                    "E-2",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T12:00:00+08:00",
                    activity_type="internship",
                ),
                # 导师确认后入库（序位 2），编号 E-10 字典序反而更小。
                {
                    "event_id": "E-10",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-2"},
                },
                # 纯数字迟到补录，字典序最靠前但序位最靠后。
                {
                    "event_id": "1",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 900, "reason": "make-up"},
                },
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["accepted"] == 3

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    # 确认按服务端序位生效：4h 实习被确认 + 15min 修正。
    assert progress["confirmed_seconds"] == 4 * 3600
    assert progress["pending_seconds"] == 0
    assert progress["adjustment_seconds"] == 900
    assert progress["total_seconds"] == 4 * 3600 + 900


def test_late_event_cutoff_uses_seq_and_old_freeze_reproduces(client):
    """冻结点来自序位；迟到补录不改变已签发冻结。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-2",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T12:00:00+08:00",
                    activity_type="internship",
                )
            ]
        },
    )
    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert f1.status_code == 201, f1.text
    f1_body = f1.json()
    assert f1_body["event_cutoff_id"] == "E-2"
    assert f1_body["event_cutoff_seq"] == 1
    assert f1_body["students"][0]["confirmed_seconds"] == 0
    assert f1_body["students"][0]["pending_seconds"] == 4 * 3600

    # 迟到的导师确认（编号 E-10，字典序上还小于 E-2）。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-10",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-2"},
                }
            ]
        },
    )

    # F-01 原样可复现：截止点仍为序位 1，确认不计入。
    f1_replay = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert f1_replay["event_cutoff_id"] == "E-2"
    assert f1_replay["event_cutoff_seq"] == 1
    assert f1_replay["students"][0]["confirmed_seconds"] == 0
    assert f1_replay["students"][0]["pending_seconds"] == 4 * 3600

    # 重复冻结返回同一份已签发结果。
    f1_repost = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert f1_repost["generated_at"] == f1_body["generated_at"]
    assert f1_repost["event_cutoff_seq"] == 1
    assert f1_repost["students"][0]["pending_seconds"] == 4 * 3600

    # 新冻结截止点是“序位 2 上的事件”，其编号恰好是字典序更小的 E-10。
    f2 = client.post(f"/api/plans/{pv}/freezes/F-02", json={}).json()
    assert f2["event_cutoff_seq"] == 2
    assert f2["event_cutoff_id"] == "E-10"
    assert f2["students"][0]["confirmed_seconds"] == 4 * 3600
    assert f2["students"][0]["pending_seconds"] == 0

    diff = client.get(f"/api/plans/{pv}/freezes/F-01/diff/F-02").json()
    assert diff["old_event_cutoff_seq"] == 1
    assert diff["new_event_cutoff_seq"] == 2
    assert diff["students_affected"] == 1
    fields = diff["student_changes"][0]["fields"]
    assert fields["confirmed_seconds"]["before"] == 0
    assert fields["confirmed_seconds"]["after"] == 4 * 3600
    assert fields["pending_seconds"]["before"] == 4 * 3600
    assert fields["pending_seconds"]["after"] == 0


def test_reimport_duplicates_keeps_results_idempotent(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    payload = {
        "events": [
            _checkin(
                "X-100",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            )
        ]
    }
    first = client.post(f"/api/plans/{pv}/events", json=payload).json()
    assert first["accepted"] == 1 and first["duplicates"] == []

    again = client.post(f"/api/plans/{pv}/events", json=payload).json()
    assert again["accepted"] == 0
    assert again["duplicates"] == ["X-100"]

    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    # 第三次重复导入后重放冻结，结果不发生任何变化。
    client.post(f"/api/plans/{pv}/events", json=payload)
    frozen = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert frozen["students"][0]["total_seconds"] == 7200


def test_concurrent_imports_preserve_monotonic_order(client):
    """并发提交不得产生序位冲突或改变最终重放结果。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]

    errors: list[Exception] = []
    accepted_total: list[int] = []
    lock = threading.Lock()
    threads_n = 4
    per_thread = 20

    def _import(slot: int) -> None:
        session = TestSessionLocal()
        try:
            events = [
                {
                    "event_id": f"SYS{slot}-E-{i:04d}",
                    "event_type": "leave_correction",
                    "student_id": f"S{slot}",
                    "payload": {"adjustment_seconds": 60, "reason": "bulk"},
                }
                for i in range(per_thread)
            ]
            from app import services

            result = services.import_events(
                session, plan_version=pv, events=events
            )
            with lock:
                accepted_total.append(result["accepted"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_import, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(accepted_total) == threads_n * per_thread

    # 同一事件被两个线程同时导入时，恰有一方胜出。
    race_errors: list[Exception] = []

    def _import_same() -> None:
        session = TestSessionLocal()
        try:
            from app import services

            services.import_events(
                session,
                plan_version=pv,
                events=[
                    {
                        "event_id": "RACE-1",
                        "event_type": "leave_correction",
                        "student_id": "S9",
                        "payload": {"adjustment_seconds": 300},
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                race_errors.append(exc)
        finally:
            session.close()

    racers = [threading.Thread(target=_import_same) for _ in range(4)]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
    assert race_errors == []

    from sqlalchemy import select

    from app.models import Event as EventModel

    session = TestSessionLocal()
    try:
        seqs = list(
            session.execute(
                select(EventModel.seq).where(EventModel.plan_version == pv)
            ).scalars()
        )
    finally:
        session.close()
    # 序位全部唯一；重复导入只留空洞，绝不复用或撞号。
    assert len(seqs) == threads_n * per_thread + 1
    assert len(set(seqs)) == len(seqs)

    snap = client.get(f"/api/plans/{pv}/snapshot").json()
    by_student = {s["student_id"]: s for s in snap["students"]}
    for slot in range(threads_n):
        assert by_student[f"S{slot}"]["adjustment_seconds"] == per_thread * 60
    assert by_student["S9"]["adjustment_seconds"] == 300
