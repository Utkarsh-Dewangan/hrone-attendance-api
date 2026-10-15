
from datetime import date, datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, UTC, IST


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_leaderboard_ties_exceeding_limit(client):
    
    for i in range(1, 4):
        code = f"EMP800{i}"
        client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": f"Finance User {i}",
                "email": f"finance_{code}@example.com",
                "department": "Finance",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )
   
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP8001", "EMP8002", "EMP8003"]}})

    db.attendance_logs.insert_one({
        "emp_code": "EMP8001",
        "date": "2026-08-03",
        "status": "PRESENT",
        "punch_in": datetime(2026, 8, 3, 5, 0, 0, tzinfo=UTC),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 60,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
    
    db.attendance_logs.insert_one({
        "emp_code": "EMP8002",
        "date": "2026-08-03",
        "status": "PRESENT",
        "punch_in": datetime(2026, 8, 3, 4, 40, 0, tzinfo=UTC),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 40,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })
   
    db.attendance_logs.insert_one({
        "emp_code": "EMP8003",
        "date": "2026-08-03",
        "status": "PRESENT",
        "punch_in": datetime(2026, 8, 3, 4, 40, 0, tzinfo=UTC),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 40,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    
    res = client.get("/analytics/leaderboard/late?month=2026-08&department=Finance&limit=2")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
   
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP8001" and items[0]["rank"] == 1
    assert items[1]["rank"] == 2 and items[2]["rank"] == 2
 
    assert items[1]["emp_code"] == "EMP8002"
    assert items[2]["emp_code"] == "EMP8003"


def test_headcount_includes_zero_log_employees(client):
  
    db.employees.delete_many({"department": "Security"})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002"]}})

    client.post(
        "/employees",
        json={
            "emp_code": "EMP9001",
            "name": "Guard One",
            "email": "guard1@example.com",
            "department": "Security",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
   
    client.post(
        "/employees",
        json={
            "emp_code": "EMP9002",
            "name": "Guard Two",
            "email": "guard2@example.com",
            "department": "Security",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-09-01",
        },
    )

    res = client.get("/analytics/departments/summary?month=2026-08&department=Security")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    sec_dept = items[0]
    assert sec_dept["department"] == "Security"
    assert sec_dept["headcount"] == 1
    assert sec_dept["present_days"] == 0.0
    assert sec_dept["avg_work_hours"] is None


def test_regularize_absent_leave_rules(client):

    code = "EMP8001"
    
    res_bad = client.patch(
        f"/attendance/{code}/2026-08-03",
        json={
            "reason": "Absent marked by mistake",
            "regularized_by": "hr.lead",
            "status": "ABSENT",
            "punch_in": 1785733200000,
        },
    )
    assert res_bad.status_code == 422

    res_ok = client.patch(
        f"/attendance/{code}/2026-08-03",
        json={
            "reason": "Emp was on unplanned absence",
            "regularized_by": "hr.lead",
            "status": "ABSENT",
        },
    )
    assert res_ok.status_code == 200
    rec = res_ok.json()
    assert rec["status"] == "ABSENT"
    assert rec["punch_in"] is None
    assert rec["punch_out"] is None
    assert rec["late_minutes"] == 0
    assert rec["work_hours"] is None

    
    res_no_change = client.patch(
        f"/attendance/{code}/2026-08-03",
        json={
            "reason": "Unchanged request test",
            "regularized_by": "hr.lead",
            "status": "ABSENT",
        },
    )
    assert res_no_change.status_code == 422


def test_attendance_date_validation(client):
    
    res = client.get("/attendance?date_from=2026-08-10&date_to=2026-08-01")
    assert res.status_code == 422


def test_explain_index_scan(client):
   
    res = client.get("/admin/explain/attendance_list?emp_code=EMP0001")
    assert res.status_code == 200
    data = res.json()
    explain_doc = str(data["explain"])

    assert "IXSCAN" in explain_doc or "FETCH" in explain_doc
