
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import (
    app,
    db,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    get_attendance_date,
    get_working_days_in_month,
    IST,
    UTC,
)


@pytest.fixture(scope="session")
def client():
   
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reload_seed_data():
   
    pass

def test_r1_overnight_shift_and_date():
    
    dt1 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC)
    assert get_attendance_date(dt1, "09:30", "18:30") == "2026-07-06"

    dt2 = datetime(2026, 7, 6, 16, 35, 0, tzinfo=UTC)
    assert get_attendance_date(dt2, "22:00", "06:00") == "2026-07-06"

    dt3 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=UTC)
    assert get_attendance_date(dt3, "22:00", "06:00") == "2026-07-06"

    dt4 = datetime(2026, 7, 7, 0, 29, 59, tzinfo=UTC)
    assert get_attendance_date(dt4, "22:00", "06:00") == "2026-07-06"

    dt5 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC)
    assert get_attendance_date(dt5, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes():
 
    dt1 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC)
    assert compute_late_minutes(dt1, "09:30", "2026-07-06") == 0

    dt2 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC)
    assert compute_late_minutes(dt2, "09:30", "2026-07-06") == 10

    dt3 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC)
    assert compute_late_minutes(dt3, "09:30", "2026-07-06") == 45


def test_r3_overtime():

    dt1 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC)
    assert compute_overtime(dt1, "09:30", "18:30", "2026-07-06") == 0

    dt2 = datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC)
    assert compute_overtime(dt2, "09:30", "18:30", "2026-07-06") == 0

    dt3 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=UTC)
    assert compute_overtime(dt3, "09:30", "18:30", "2026-07-06") == 30

    dt4 = datetime(2026, 7, 7, 1, 10, 0, tzinfo=UTC)
    assert compute_overtime(dt4, "22:00", "06:00", "2026-07-06") == 40


def test_r4_work_hours_half_up():
    t1 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)

    t2 = t1 + timedelta(seconds=9090)
    assert compute_work_hours(t1, t2) == 2.53


def test_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.0) is False
    assert compute_half_day(None) is False


def test_r7_working_days_mid_month_joiner():

    assert get_working_days_in_month("2026-07", "2026-01-01") == 23

    assert get_working_days_in_month("2026-07", "2026-07-13") == 15

    assert get_working_days_in_month("2026-07", "2026-08-01") == 0

def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud(client):
  
    code = f"EMP{int(datetime.now().timestamp()) % 100000:05d}"
    payload = {
        "emp_code": code,
        "name": "Test Tester",
        "email": f"test_{code}@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == code
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    bad_payload = dict(payload, emp_code="EMP9999", shift_start="09:00", shift_end="09:00")
    res_bad = client.post("/employees", json=bad_payload)
    assert res_bad.status_code == 422

    res_list = client.get("/employees?department=QA&page=1&page_size=20")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] >= 1
    assert any(e["emp_code"] == code for e in list_data["items"])


def test_punch_in_and_punch_out_flow(client):

    code = f"EMP{int(datetime.now().timestamp()) % 90000 + 10000}"
    client.post(
        "/employees",
        json={
            "emp_code": code,
            "name": "Punch Tester",
            "email": f"punch_{code}@example.com",
            "department": "Operations",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    t_in = datetime(2026, 8, 3, 4, 10, 1, tzinfo=UTC)
    ms_in = int(t_in.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms_in})
    assert res_in.status_code == 201
    rec = res_in.json()
    assert rec["emp_code"] == code
    assert rec["date"] == "2026-08-03"
    assert rec["late_minutes"] == 10
    assert rec["work_hours"] is None
    assert rec["half_day"] is False
    assert "_id" not in rec

    res_dup = client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms_in + 1000})
    assert res_dup.status_code == 409

    t_out = datetime(2026, 8, 3, 13, 40, 0, tzinfo=UTC)
    ms_out = int(t_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms_out})
    assert res_out.status_code == 200
    out_rec = res_out.json()
    assert out_rec["punch_out"] == ms_out
    assert out_rec["overtime_minutes"] == 40

    assert out_rec["work_hours"] == 9.50
    assert out_rec["half_day"] is False

    res_out_again = client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms_out + 1000})
    assert res_out_again.status_code == 409


def test_regularize_attendance_and_history(client):

    code = f"EMP{int(datetime.now().timestamp()) % 80000 + 20000}"
    client.post(
        "/employees",
        json={
            "emp_code": code,
            "name": "Reg Tester",
            "email": f"reg_{code}@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    t_in = datetime(2026, 7, 20, 3, 58, 0, tzinfo=UTC)
    ms_in = int(t_in.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms_in})

    t_out = datetime(2026, 7, 20, 13, 5, 0, tzinfo=UTC)
    ms_out = int(t_out.timestamp() * 1000)
    client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms_out})

    new_in = datetime(2026, 7, 20, 4, 45, 0, tzinfo=UTC)
    new_in_ms = int(new_in.timestamp() * 1000)

    patch_res = client.patch(
        f"/attendance/{code}/2026-07-20",
        json={
            "reason": "Traffic jam on outer ring road",
            "regularized_by": "manager.bob",
            "punch_in": new_in_ms,
        },
    )
    assert patch_res.status_code == 200
    reg_rec = patch_res.json()
    assert reg_rec["punch_in"] == new_in_ms
    assert reg_rec["late_minutes"] == 45
    assert len(reg_rec["history"]) == 1

    last_history = reg_rec["history"][-1]
    assert last_history["by"] == "manager.bob"
    assert "punch_in" in last_history["changes"]
    assert "late_minutes" in last_history["changes"]


def test_employee_monthly_analytics(client):

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] >= 1.0
    assert isinstance(data["attendance_pct"], float)


def test_department_summary_analytics(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) > 0
    for dept in data["items"]:
        assert dept["headcount"] > 0
        assert "present_days" in dept
        assert "late_count" in dept
        assert "leave_count" in dept
        assert "on_duty_count" in dept


def test_late_leaderboard_competition_ranking(client):
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)
    if data["items"]:

        assert data["items"][0]["rank"] == 1
        assert data["items"][0]["total_late_minutes"] > 0


def test_department_trend_gap_filling_and_moving_avg(client):

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 10

    dates = [item["date"] for item in data["items"]]
    expected_dates = [f"2026-07-{d:02d}" for d in range(1, 11)]
    assert dates == expected_dates
    for item in data["items"]:
        assert "is_working_day" in item
        assert "headcount" in item
        assert "attendance_rate" in item
        assert "moving_avg_7d" in item


def test_admin_explain_endpoints(client):
    for ep in [
        "attendance_list",
        "employee_monthly?emp_code=EMP0001&month=2026-07",
        "department_summary?month=2026-07",
        "late_leaderboard?month=2026-07",
        "department_trend?department=Engineering&from=2026-07-01&to=2026-07-07",
    ]:
        ep_name = ep.split("?")[0]
        url = f"/admin/explain/{ep}"
        res = client.get(url)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep_name
        assert "explain" in data
