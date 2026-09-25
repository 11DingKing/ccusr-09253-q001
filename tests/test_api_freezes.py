"""服务端业务模块。"""

from __future__ import annotations

import threading

from tests.conftest import NY_PLAN, SHANGHAI_PLAN


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
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


def test_frozen_snapshot_immutable_after_late_correction(client):
    """执行确定性的业务处理。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )
    # Freeze F-01.
    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert f1.status_code == 201, f1.text
    f1_body = f1.json()
    assert f1_body["freeze_id"] == "F-01"
    assert f1_body["event_cutoff_id"] == "E-01"
    f1_total = f1_body["students"][0]["total_seconds"]
    assert f1_total == 7200

    # A late correction E-09 arrives (adds one hour).
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-09",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {
                        "adjustment_seconds": 3600,
                        "reason": "approved make-up session",
                    },
                }
            ]
        },
    )

    # F-01 is unchanged.
    f1_again = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert f1_again["students"][0]["total_seconds"] == 7200
    assert f1_again["event_cutoff_id"] == "E-01"

    # The live snapshot, however, reflects E-09.
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 7200 + 3600

    # Re-freezing F-01 is idempotent and still returns the old value.
    f1_repost = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert f1_repost["students"][0]["total_seconds"] == 7200


def test_new_revision_includes_late_event_and_diff_explains_change(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-09",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": 3600, "reason": "make-up"},
                }
            ]
        },
    )
    # Freeze a second revision F-02.
    f2 = client.post(f"/api/plans/{pv}/freezes/F-02", json={}).json()
    assert f2["event_cutoff_id"] == "E-09"
    assert f2["students"][0]["total_seconds"] == 7200 + 3600

    diff = client.get(f"/api/plans/{pv}/freezes/F-01/diff/F-02").json()
    assert diff["students_affected"] == 1
    change = diff["student_changes"][0]
    assert change["student_id"] == "S1"
    assert change["fields"]["total_seconds"]["before"] == 7200
    assert change["fields"]["total_seconds"]["after"] == 10800
    assert change["fields"]["lesson_units"]["before"] == 2
    assert change["fields"]["lesson_units"]["after"] == 4


def test_explain_frozen_student_returns_breakdown(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                {
                    "event_id": "E-02",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": -900, "reason": "late"},
                },
            ]
        },
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    explanation = client.get(
        f"/api/plans/{pv}/freezes/F-01/explain/S1"
    ).json()
    assert explanation["student_id"] == "S1"
    assert explanation["confirmed_seconds"] == 7200
    assert explanation["adjustment_seconds"] == -900
    assert explanation["total_seconds"] == 7200 - 900
    assert len(explanation["checkins"]) == 1
    assert len(explanation["adjustments"]) == 1
    assert explanation["checkins"][0]["status"] == "CONFIRMED"


def test_concurrent_freeze_only_one_wins(client):
    """执行确定性的业务处理。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )

    from app import services
    from tests.conftest import TestSessionLocal

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _freeze():
        session = TestSessionLocal()
        try:
            _, created = services.freeze_semester(
                session, plan_version=pv, freeze_id="F-CONCURRENT"
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_freeze) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread created the freeze; the rest saw the existing row.
    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 3

    stored = client.get(f"/api/plans/{pv}/freezes/F-CONCURRENT").json()
    assert stored["freeze_id"] == "F-CONCURRENT"
    assert stored["students"][0]["total_seconds"] == 7200


def _correction(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def test_concurrent_imports_cannot_change_issued_freeze(db, client):
    """并发导入与冻结交错时，已签发结果必须与截止序位重放一致。"""
    from app.core.snapshot import build_snapshot
    from app import services
    from app.repository import load_events_up_to_seq
    from tests.conftest import TestSessionLocal

    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    # 初始事件，保证冻结非空。
    client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_checkin("BASE-1", "S1",
                                  "2024-03-15T08:00:00+08:00",
                                  "2024-03-15T10:00:00+08:00")]},
    )

    barrier = threading.Barrier(5)
    issued: list[tuple[int, int]] = []  # (cutoff_seq, total_seconds)
    lock = threading.Lock()

    def _import_worker(idx: int):
        session = TestSessionLocal()
        barrier.wait()
        try:
            services.import_events(
                session,
                plan_version=pv,
                events=[_correction(f"LATE-{idx}", "S1", 60, f"c{idx}")],
            )
        finally:
            session.close()

    def _freeze_worker(idx: int):
        session = TestSessionLocal()
        barrier.wait()
        try:
            snap, _created = services.freeze_semester(
                session, plan_version=pv, freeze_id=f"F-RACE-{idx}"
            )
            with lock:
                issued.append((snap.event_cutoff_seq,
                               snap.students[0]["total_seconds"]))
        finally:
            session.close()

    workers = [threading.Thread(target=_import_worker, args=(i,)) for i in range(2)]
    workers += [threading.Thread(target=_freeze_worker, args=(i,)) for i in range(3)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    plan = services.get_plan_plain(db, pv)
    for cutoff_seq, total in issued:
        assert cutoff_seq is not None
        events = load_events_up_to_seq(db, pv, cutoff_seq)
        rebuilt = build_snapshot(
            events,
            plan_version=pv,
            timezone_name=plan["iana_timezone"],
            required_seconds=plan["required_seconds"],
            event_cutoff_seq=cutoff_seq,
        )
        assert rebuilt.students[0]["total_seconds"] == total

    # 每个冻结各自只签发一次；重复返回相同结果。
    for i in range(3):
        snap, created = services.freeze_semester(
            db, plan_version=pv, freeze_id=f"F-RACE-{i}"
        )
        assert created is False
        assert any(
            seq == snap.event_cutoff_seq and total == snap.students[0]["total_seconds"]
            for seq, total in issued
        )


def test_new_york_dst_fallback_via_api(client):
    """纽约 DST 回拨夜按真实经过时长计学时。"""
    plan = dict(NY_PLAN)
    plan["required_seconds"] = 7200
    client.post("/api/plans", json=plan)
    pv = plan["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S2",
                    "2024-11-03T01:30:00-04:00",
                    "2024-11-03T02:30:00-05:00",
                )
            ]
        },
    )
    progress = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert progress["confirmed_seconds"] == 7200
    assert progress["total_seconds"] == 7200
    assert progress["lesson_units"] == 2
    assert progress["meets_requirement"] is True
