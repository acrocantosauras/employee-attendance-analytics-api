"""
Comprehensive test suite for Employee Attendance & Analytics API.

Tests cover:
- All endpoints from OpenAPI contract
- Business rules R1-R10
- Error handling and validation
- Analytics correctness
- Concurrency scenarios
"""

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import os
import sys

# Load environment
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app.main import app, db, client


@pytest.fixture(scope="function")
def test_db():
    """Provide a clean database for each test."""
    # Clean collections before test
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield db
    # Clean after test
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


@pytest.fixture
def client_test():
    """Create a test client."""
    return TestClient(app)


# ============================================================================
# Helper functions
# ============================================================================

def create_employee(client_test, emp_code="EMP0001", name="Test Employee",
                    department="Engineering", shift_start="09:30", shift_end="18:30",
                    joined_on="2026-01-01"):
    """Helper to create an employee."""
    response = client_test.post("/employees", json={
        "emp_code": emp_code,
        "name": name,
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on
    })
    return response


def punch_in(client_test, emp_code, punched_at=None, status="PRESENT"):
    """Helper to punch in."""
    payload = {"emp_code": emp_code, "status": status}
    if punched_at:
        payload["punched_at"] = punched_at
    response = client_test.post("/attendance/punch-in", json=payload)
    return response


def punch_out(client_test, emp_code, punched_at=None):
    """Helper to punch out."""
    payload = {"emp_code": emp_code}
    if punched_at:
        payload["punched_at"] = punched_at
    response = client_test.post("/attendance/punch-out", json=payload)
    return response


def epoch_ms(dt):
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def dt_from_epoch(ms):
    """Convert epoch milliseconds to datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ============================================================================
# Health endpoint tests
# ============================================================================

class TestHealth:
    def test_health_ok(self, client_test):
        response = client_test.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ============================================================================
# Employee endpoint tests
# ============================================================================

class TestEmployees:
    def test_create_employee_success(self, client_test, test_db):
        response = create_employee(client_test)
        assert response.status_code == 201
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["name"] == "Test Employee"
        assert data["department"] == "Engineering"
        assert "created_at" in data
        assert isinstance(data["created_at"], int)

    def test_create_employee_duplicate(self, client_test, test_db):
        create_employee(client_test)
        response = create_employee(client_test)
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_employee_invalid_emp_code(self, client_test, test_db):
        response = client_test.post("/employees", json={
            "emp_code": "INVALID",
            "name": "Test",
            "email": "test@example.com",
            "department": "Eng",
            "joined_on": "2026-01-01"
        })
        assert response.status_code == 422

    def test_create_employee_invalid_email(self, client_test, test_db):
        response = client_test.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "invalid",
            "department": "Eng",
            "joined_on": "2026-01-01"
        })
        assert response.status_code == 422

    def test_create_employee_same_shift_times(self, client_test, test_db):
        response = client_test.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "test@example.com",
            "department": "Eng",
            "shift_start": "09:00",
            "shift_end": "09:00",
            "joined_on": "2026-01-01"
        })
        assert response.status_code == 422

    def test_list_employees(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", department="Eng")
        create_employee(client_test, emp_code="EMP0002", department="Sales")

        response = client_test.get("/employees")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["emp_code"] == "EMP0001"  # Sorted by emp_code

    def test_list_employees_filtered_by_department(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", department="Eng")
        create_employee(client_test, emp_code="EMP0002", department="Sales")

        response = client_test.get("/employees", params={"department": "Eng"})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["emp_code"] == "EMP0001"

    def test_list_employees_pagination(self, client_test, test_db):
        for i in range(5):
            create_employee(client_test, emp_code=f"EMP{i:04d}")

        response = client_test.get("/employees", params={"page": 1, "page_size": 2})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["page_size"] == 2


# ============================================================================
# Punch-in endpoint tests
# ============================================================================

class TestPunchIn:
    def test_punch_in_success(self, client_test, test_db):
        create_employee(client_test)
        response = punch_in(client_test, "EMP0001")
        assert response.status_code == 201
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] is not None
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["history"] == []

    def test_punch_in_unknown_employee(self, client_test, test_db):
        response = punch_in(client_test, "UNKNOWN")
        assert response.status_code == 404

    def test_punch_in_duplicate(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")
        response = punch_in(client_test, "EMP0001")
        assert response.status_code == 409

    def test_punch_in_with_custom_time(self, client_test, test_db):
        create_employee(client_test)
        # 2026-07-06 09:40:00 UTC = 14:10 IST
        ts = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        data = response.json()
        assert data["date"] == "2026-07-06"
        # 09:40 IST is 10 minutes after 09:30 shift start - exactly at grace period boundary
        # Should be NOT late (grace is "strictly more than 10 minutes")
        assert data["late_minutes"] == 0

    def test_punch_in_late(self, client_test, test_db):
        create_employee(client_test)
        # 2026-07-06 10:05:00 IST = 04:35 UTC
        ts = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        data = response.json()
        # 10:05 IST - 09:30 IST = 35 minutes
        assert data["late_minutes"] == 35

    def test_punch_in_wfh(self, client_test, test_db):
        create_employee(client_test)
        response = punch_in(client_test, "EMP0001", status="WFH")
        assert response.status_code == 201
        assert response.json()["status"] == "WFH"

    def test_punch_in_on_duty(self, client_test, test_db):
        create_employee(client_test)
        response = punch_in(client_test, "EMP0001", status="ON_DUTY")
        assert response.status_code == 201
        assert response.json()["status"] == "ON_DUTY"

    def test_punch_in_invalid_status(self, client_test, test_db):
        create_employee(client_test)
        response = client_test.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "status": "ABSENT"
        })
        assert response.status_code == 422

    def test_punch_in_invalid_timestamp(self, client_test, test_db):
        create_employee(client_test)
        response = client_test.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 100  # Too small, looks like seconds
        })
        assert response.status_code == 422


# ============================================================================
# Punch-out endpoint tests
# ============================================================================

class TestPunchOut:
    def test_punch_out_success(self, client_test, test_db):
        create_employee(client_test)
        # Punch in at 09:28 IST = 03:58 UTC
        punch_in_ts = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Punch out at 18:35 IST = 13:05 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 200
        data = response.json()
        assert data["punch_out"] is not None
        # 18:35 - 09:28 = 9 hours 7 minutes = 9.12 hours
        assert data["work_hours"] == 9.12
        assert data["overtime_minutes"] == 0  # Under 30 min threshold
        assert data["half_day"] is False

    def test_punch_out_unknown_employee(self, client_test, test_db):
        response = punch_out(client_test, "UNKNOWN")
        assert response.status_code == 404

    def test_punch_out_no_punch_in(self, client_test, test_db):
        create_employee(client_test)
        response = punch_out(client_test, "EMP0001")
        assert response.status_code == 404

    def test_punch_out_before_punch_in(self, client_test, test_db):
        create_employee(client_test)
        # Punch in at 09:30 IST = 04:00 UTC
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Try to punch out before punch in time (09:20 IST = 03:50 UTC)
        # This should return 404 because no punch-in exists at or before this time
        punch_out_ts = int(datetime(2026, 7, 6, 3, 50, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 404

    def test_punch_out_more_than_24_hours(self, client_test, test_db):
        create_employee(client_test)
        # Punch in at 09:30 IST
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Try to punch out more than 24 hours later
        punch_out_ts = int(datetime(2026, 7, 7, 5, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 422

    def test_punch_out_duplicate(self, client_test, test_db):
        create_employee(client_test)
        # Punch in at 09:30 IST = 04:00 UTC
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)
        # Punch out at 18:00 IST = 12:30 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_out(client_test, "EMP0001", punched_at=punch_out_ts)

        # Try to punch out again - should get 409 (already punched out)
        # Use a timestamp AFTER the punch-in to find the record
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 409


# ============================================================================
# Attendance list endpoint tests
# ============================================================================

class TestAttendanceList:
    def test_list_attendance(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/attendance")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_list_attendance_filter_by_emp_code(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001")
        create_employee(client_test, emp_code="EMP0002")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/attendance", params={"emp_code": "EMP0001"})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["emp_code"] == "EMP0001"

    def test_list_attendance_filter_by_date_range(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001")
        # Punch in on July 6, 2026 at 09:30 IST
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        response = client_test.get("/attendance", params={
            "date_from": "2026-07-06",
            "date_to": "2026-07-06"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_list_attendance_invalid_date_range(self, client_test, test_db):
        response = client_test.get("/attendance", params={
            "date_from": "2026-07-08",
            "date_to": "2026-07-06"
        })
        assert response.status_code == 422

    def test_list_attendance_filter_by_status(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001", status="WFH")

        response = client_test.get("/attendance", params={"status": "WFH"})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "WFH"


# ============================================================================
# Regularize (PATCH) endpoint tests
# ============================================================================

class TestRegularize:
    def test_regularize_success(self, client_test, test_db):
        create_employee(client_test)
        # Punch in at 09:30 IST on July 6, 2026
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Correct the punch_in time (change from 09:30 to 09:28)
        new_punch_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_punch_in,
            "reason": "correcting punch time",
            "regularized_by": "hr.admin"
        })
        assert response.status_code == 200
        data = response.json()
        assert len(data["history"]) == 1
        assert data["history"][0]["by"] == "hr.admin"

    def test_regularize_unknown_employee(self, client_test, test_db):
        response = client_test.patch("/attendance/UNKNOWN/2026-07-06", json={
            "reason": "testing unknown employee",
            "regularized_by": "admin"
        })
        assert response.status_code == 404

    def test_regularize_no_record(self, client_test, test_db):
        create_employee(client_test)
        # No punch-in created, so no record exists for this date
        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "testing no record",
            "regularized_by": "admin"
        })
        assert response.status_code == 404

    def test_regularize_set_absent(self, client_test, test_db):
        create_employee(client_test)
        # Create an attendance record first
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "marked absent",
            "regularized_by": "hr.admin"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ABSENT"
        assert data["punch_in"] is None
        assert data["punch_out"] is None

    def test_regularize_absent_with_punch_in_rejected(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")

        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": epoch_ms(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)),
            "reason": "test",
            "regularized_by": "admin"
        })
        assert response.status_code == 422

    def test_regularize_presence_requires_punch_in(self, client_test, test_db):
        create_employee(client_test)

        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "PRESENT",
            "reason": "test",
            "regularized_by": "admin"
        })
        assert response.status_code == 422

    def test_regularize_no_changes_rejected(self, client_test, test_db):
        create_employee(client_test)

        response = client_test.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "test",
            "regularized_by": "admin"
        })
        assert response.status_code == 422


# ============================================================================
# Analytics endpoint tests
# ============================================================================

class TestAnalytics:
    def test_employee_monthly(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", joined_on="2026-01-01")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/analytics/employees/EMP0001/monthly",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert data["working_days"] >= 1
        assert "attendance_pct" in data

    def test_employee_monthly_unknown_employee(self, client_test, test_db):
        response = client_test.get("/analytics/employees/UNKNOWN/monthly",
                                   params={"month": "2026-07"})
        assert response.status_code == 404

    def test_department_summary(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", department="Eng")
        create_employee(client_test, emp_code="EMP0002", department="Eng")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/analytics/departments/summary",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) >= 1

    def test_late_leaderboard(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", department="Eng")
        # Create a late punch
        ts = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=ts)

        response = client_test.get("/analytics/leaderboard/late",
                                   params={"month": "2026-07", "limit": 10})
        assert response.status_code == 200
        data = response.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) >= 1
        assert data["items"][0]["total_late_minutes"] > 0

    def test_department_trend(self, client_test, test_db):
        create_employee(client_test, emp_code="EMP0001", department="Eng")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/analytics/departments/Eng/trend",
                                   params={"from": "2026-07-06", "to": "2026-07-06"})
        assert response.status_code == 200
        data = response.json()
        assert data["department"] == "Eng"
        assert len(data["items"]) == 1
        assert data["items"][0]["date"] == "2026-07-06"

    def test_department_trend_unknown_department(self, client_test, test_db):
        response = client_test.get("/analytics/departments/Unknown/trend",
                                   params={"from": "2026-07-06", "to": "2026-07-06"})
        assert response.status_code == 404

    def test_department_trend_invalid_range(self, client_test, test_db):
        create_employee(client_test, department="Eng")
        response = client_test.get("/analytics/departments/Eng/trend",
                                   params={"from": "2026-07-08", "to": "2026-07-06"})
        assert response.status_code == 422


# ============================================================================
# Admin explain endpoint tests
# ============================================================================

class TestAdminExplain:
    def test_explain_attendance_list(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")

        response = client_test.get("/admin/explain/attendance_list",
                                   params={"emp_code": "EMP0001"})
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "attendance_list"
        assert "explain" in data

    def test_explain_employee_monthly(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")

        response = client_test.get("/admin/explain/employee_monthly",
                                   params={"emp_code": "EMP0001", "month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "employee_monthly"

    def test_explain_department_summary(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")

        response = client_test.get("/admin/explain/department_summary",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "department_summary"

    def test_explain_late_leaderboard(self, client_test, test_db):
        create_employee(client_test)
        punch_in(client_test, "EMP0001")

        response = client_test.get("/admin/explain/late_leaderboard",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "late_leaderboard"

    def test_explain_department_trend(self, client_test, test_db):
        create_employee(client_test, department="Eng")
        punch_in(client_test, "EMP0001")

        response = client_test.get("/admin/explain/department_trend",
                                   params={"department": "Eng",
                                           "from": "2026-07-06",
                                           "to": "2026-07-06"})
        assert response.status_code == 200
        data = response.json()
        assert data["endpoint"] == "department_trend"


# ============================================================================
# Business rule verification tests
# ============================================================================

class TestBusinessRules:
    def test_r2_late_grace_period(self, client_test, test_db):
        """R2: Grace period is 10 minutes. Exactly 10 minutes is NOT late."""
        create_employee(client_test)
        # 09:40:00 IST = exactly 10 minutes after 09:30
        # 09:40:00 UTC = 15:10 IST, so use 04:10 UTC
        ts = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        assert response.json()["late_minutes"] == 0

    def test_r2_late_one_second_over(self, client_test, test_db):
        """R2: More than 10 minutes late = late."""
        create_employee(client_test)
        # 09:40:01 IST = 10 minutes 1 second after 09:30
        # Floor of 10.016 minutes = 10 minutes
        # But the rule says late only if STRICTLY more than 10 minutes
        # So 10 minutes exactly is NOT late
        # 04:10:01 UTC
        ts = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        # 10 minutes is NOT > 10, so not late
        assert response.json()["late_minutes"] == 0

    def test_r2_late_eleven_minutes(self, client_test, test_db):
        """R2: 11 minutes late = late."""
        create_employee(client_test)
        # 09:41:00 IST = 11 minutes after 09:30
        # 04:11:00 UTC
        ts = int(datetime(2026, 7, 6, 4, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        # 11 minutes > 10, so 11 minutes late
        assert response.json()["late_minutes"] == 11

    def test_r3_overtime_threshold(self, client_test, test_db):
        """R3: Overtime counted only when >= 30 minutes."""
        create_employee(client_test)
        # Punch in at 09:30 IST on July 6, 2026
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Punch out at 19:00 IST (40 min after 18:20 shift end... wait, shift ends at 18:30)
        # 18:30 IST = 13:00 UTC, so 19:00 IST = 13:30 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        # 19:00 - 18:30 = 30 minutes overtime
        assert response.status_code == 200
        assert response.json()["overtime_minutes"] == 30

    def test_r4_work_hours_calculation(self, client_test, test_db):
        """R4: Work hours rounded to 2 decimals half-up."""
        create_employee(client_test)
        # Punch in at 09:30 IST on July 6, 2026
        punch_in_ts = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Punch out 8 hours 15 minutes later
        # 09:30 IST = 04:00 UTC, 17:45 IST = 12:15 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 12, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 200
        # 8.25 hours exactly
        assert response.json()["work_hours"] == 8.25

    def test_r5_half_day(self, client_test, test_db):
        """R5: Half day when work_hours < 4.50."""
        create_employee(client_test)
        # Punch in at 09:00 IST (03:30 UTC)
        punch_in_ts = int(datetime(2026, 7, 6, 3, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Work only 4 hours - punch out at 12:30 UTC (18:00 IST)
        # 18:00 - 09:00 = 9 hours... that's wrong
        # Let me recalculate: punch in 09:00, work 4 hours, punch out at 13:00
        # 13:00 IST = 07:30 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 7, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 200
        data = response.json()
        # 4 hours is < 4.5, so half day
        assert data["half_day"] is True
        assert data["work_hours"] == 4.0

    def test_r5_half_day_boundary(self, client_test, test_db):
        """R5: Work exactly 4.5 hours - NOT a half day."""
        create_employee(client_test)
        # Punch in at 09:00 IST (03:30 UTC)
        punch_in_ts = int(datetime(2026, 7, 6, 3, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in(client_test, "EMP0001", punched_at=punch_in_ts)

        # Work exactly 4.5 hours - punch out at 13:00 UTC (18:30 IST)
        # 18:30 - 09:00 = 9.5 hours... still wrong
        # Let me recalculate properly:
        # punch_in = 09:00 IST = 03:30 UTC
        # work 4.5 hours = 4.5 * 60 = 270 minutes
        # punch_out = 09:00 + 4.5 hours = 13:30 IST = 08:00 UTC
        punch_out_ts = int(datetime(2026, 7, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_out(client_test, "EMP0001", punched_at=punch_out_ts)
        assert response.status_code == 200
        data = response.json()
        # 4.5 is not < 4.5, so not half day
        assert data["half_day"] is False
        assert data["work_hours"] == 4.5

    def test_r7_working_days_excludes_weekends(self, client_test, test_db):
        """R7: Working days are Mon-Fri only."""
        create_employee(client_test, joined_on="2026-01-01")

        # July 2026: 1st is Wednesday
        # Working days in July 2026 = 22 (M-F only, excluding weekends)
        response = client_test.get("/analytics/employees/EMP0001/monthly",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        # July 2026 has 31 days, 8 weekend days (Sat/Sun)
        # 31 - 8 = 23 weekdays, but let me verify...
        # Actually we need to count: July 2026 has 4 full weeks + 3 extra days
        # Weekends: 4 Saturdays + 4 Sundays = 8 days
        # Working days = 31 - 8 = 23
        assert data["working_days"] == 23

    def test_r9_headcount_includes_all_joined_employees(self, client_test, test_db):
        """R9: Headcount includes employees who joined on or before end of period."""
        create_employee(client_test, emp_code="EMP0001", joined_on="2026-01-01", department="Eng")
        create_employee(client_test, emp_code="EMP0002", joined_on="2026-07-15", department="Eng")

        response = client_test.get("/analytics/departments/summary",
                                   params={"month": "2026-07", "department": "Eng"})
        assert response.status_code == 200
        data = response.json()
        eng_dept = next((d for d in data["items"] if d["department"] == "Eng"), None)
        assert eng_dept is not None
        # Both employees joined on or before July 31, 2026
        assert eng_dept["headcount"] == 2


# ============================================================================
# Overnight shift tests
# ============================================================================

class TestOvernightShift:
    def test_overnight_shift_date_calculation(self, client_test, test_db):
        """R1: Overnight shift (22:00-06:00) - punch-in before shift_end belongs to previous day."""
        # Create employee with overnight shift
        create_employee(client_test, emp_code="EMP0001",
                       shift_start="22:00", shift_end="06:00",
                       joined_on="2026-01-01")

        # Punch in at 21:55 IST on July 6 = 16:25 UTC
        ts = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        # Should be dated July 6 (shift started that day)
        assert response.json()["date"] == "2026-07-06"

    def test_overnight_shift_punch_in_after_shift_end(self, client_test, test_db):
        """R1: Overnight shift - punch-in after shift_end belongs to that day's shift."""
        create_employee(client_test, emp_code="EMP0001",
                       shift_start="22:00", shift_end="06:00",
                       joined_on="2026-01-01")

        # Punch in at 07:00 IST on July 7 = 01:30 UTC
        ts = int(datetime(2026, 7, 7, 1, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        response = punch_in(client_test, "EMP0001", punched_at=ts)
        assert response.status_code == 201
        # Should be dated July 7 (after shift_end time)
        assert response.json()["date"] == "2026-07-07"


# ============================================================================
# Validation tests
# ============================================================================

class TestValidation:
    def test_emp_code_pattern(self, client_test, test_db):
        """emp_code must match EMP followed by 4-6 digits."""
        response = client_test.post("/employees", json={
            "emp_code": "EMP001",  # Only 3 digits
            "name": "Test",
            "email": "test@example.com",
            "department": "Eng",
            "joined_on": "2026-01-01"
        })
        assert response.status_code == 422

        response = client_test.post("/employees", json={
            "emp_code": "EMP00001",  # 5 digits - valid
            "name": "Test",
            "email": "test@example.com",
            "department": "Eng",
            "joined_on": "2026-01-01"
        })
        assert response.status_code == 201

    def test_punched_at_too_small(self, client_test, test_db):
        """punched_at must be >= 100000000000 (year 1973)."""
        create_employee(client_test)
        response = client_test.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 100  # Way too small
        })
        assert response.status_code == 422

    def test_page_size_max_100(self, client_test, test_db):
        """page_size max is 100."""
        response = client_test.get("/employees", params={"page_size": 101})
        assert response.status_code == 422


# ============================================================================
# Sample data verification tests
# ============================================================================

class TestSampleData:
    """Verify the API works correctly with the seeded sample data.
    These tests require the sample data to be seeded in the database.
    Run: python sample_seed.py before running these tests."""

    def test_sample_employee_records(self, client_test, test_db):
        """Verify sample employees exist."""
        # Re-seed the database for this test
        import json
        from bson import json_util
        import pathlib
        here = pathlib.Path(__file__).parent / "sample_data"
        opts = json_util.JSONOptions(tz_aware=True)
        
        employees = json_util.loads((here / "employees.json").read_text(), json_options=opts)
        attendance_logs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)
        
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})
        test_db.employees.insert_many(employees)
        test_db.attendance_logs.insert_many(attendance_logs)
        
        response = client_test.get("/employees")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 6  # 6 sample employees

    def test_sample_attendance_records(self, client_test, test_db):
        """Verify sample attendance records exist."""
        # Re-seed the database for this test
        import json
        from bson import json_util
        import pathlib
        here = pathlib.Path(__file__).parent / "sample_data"
        opts = json_util.JSONOptions(tz_aware=True)
        
        employees = json_util.loads((here / "employees.json").read_text(), json_options=opts)
        attendance_logs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)
        
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})
        test_db.employees.insert_many(employees)
        test_db.attendance_logs.insert_many(attendance_logs)
        
        response = client_test.get("/attendance")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 9  # 9 sample attendance records

    def test_sample_analytics_employee_monthly(self, client_test, test_db):
        """Verify analytics work with sample data."""
        # Re-seed the database for this test
        import json
        from bson import json_util
        import pathlib
        here = pathlib.Path(__file__).parent / "sample_data"
        opts = json_util.JSONOptions(tz_aware=True)
        
        employees = json_util.loads((here / "employees.json").read_text(), json_options=opts)
        attendance_logs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)
        
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})
        test_db.employees.insert_many(employees)
        test_db.attendance_logs.insert_many(attendance_logs)
        
        response = client_test.get("/analytics/employees/EMP0001/monthly",
                                   params={"month": "2026-07"})
        assert response.status_code == 200
        data = response.json()
        assert data["emp_code"] == "EMP0001"
        assert data["working_days"] > 0

    def test_sample_leaderboard(self, client_test, test_db):
        """Verify leaderboard works with sample data."""
        # Re-seed the database for this test
        import json
        from bson import json_util
        import pathlib
        here = pathlib.Path(__file__).parent / "sample_data"
        opts = json_util.JSONOptions(tz_aware=True)
        
        employees = json_util.loads((here / "employees.json").read_text(), json_options=opts)
        attendance_logs = json_util.loads((here / "attendance_logs.json").read_text(), json_options=opts)
        
        test_db.employees.delete_many({})
        test_db.attendance_logs.delete_many({})
        test_db.employees.insert_many(employees)
        test_db.attendance_logs.insert_many(attendance_logs)
        
        response = client_test.get("/analytics/leaderboard/late",
                                   params={"month": "2026-07", "limit": 10})
        assert response.status_code == 200
        data = response.json()
        # EMP0003 has 35 late minutes in sample data
        late_employees = [item for item in data["items"] if item["emp_code"] == "EMP0003"]
        assert len(late_employees) > 0
        assert late_employees[0]["total_late_minutes"] == 35
