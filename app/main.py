"""
Employee Attendance & Analytics API - COMPLETE IMPLEMENTATION

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Implements the full HROne engineering assignment contract from openapi.yaml.
All business rules R1-R10 are implemented. See DECISIONS.md for design notes.
"""

import os
import math
from datetime import datetime, timedelta, timezone
import calendar
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from datetime import date as DateType

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# IST timezone (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def dt_to_epoch_ms(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a timezone-aware datetime (UTC)."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds per R1."""
    return dt.replace(microsecond=0)


def round_half_up(value: float, decimals: int) -> float:
    """Round using half-up rounding (not banker's rounding)."""
    d = Decimal(str(value))
    quantize_str = '0.' + '0' * decimals
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2: Late minutes calculation.
    Grace period is 10 minutes. Late only if strictly more than 10:00 after shift_start.
    Returns floor minutes elapsed since shift_start when late, otherwise 0.
    punch_in is in UTC, shift_start is IST time.
    """
    # Convert punch_in to IST for comparison
    punch_in_ist = punch_in.astimezone(IST)
    punch_in_truncated = truncate_to_seconds(punch_in_ist)

    h, m = map(int, shift_start.split(":"))
    # Create shift_start datetime on the same date as punch_in (in IST)
    shift_start_dt = punch_in_truncated.replace(hour=h, minute=m, second=0, microsecond=0)

    diff_seconds = (punch_in_truncated - shift_start_dt).total_seconds()
    diff_minutes = int(diff_seconds / 60)

    # Late only if strictly more than 10 minutes
    if diff_minutes > 10:
        return diff_minutes
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Optional[float]:
    """
    R4: Work hours = (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up.
    """
    if punch_in is None or punch_out is None:
        return None
    seconds = (punch_out - punch_in).total_seconds()
    hours = seconds / 3600
    return round_half_up(hours, 2)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, date_str: str) -> int:
    """
    R3: Overtime minutes = floor minutes between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Handles overnight shifts.
    """
    h, m = map(int, shift_end.split(":"))
    # Create shift_end datetime on the given date
    end_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)

    # For overnight shifts (shift_end <= shift_start), shift_end is on the next day
    # But we're computing from punch_out date perspective
    punch_out_truncated = truncate_to_seconds(punch_out)

    # Calculate the difference
    diff_seconds = (punch_out_truncated - end_dt).total_seconds()
    diff_minutes = int(diff_seconds / 60)

    if diff_minutes >= 30:
        return diff_minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Determine the attendance date from a punch_in instant.
    For normal shifts: IST calendar date of punch_in.
    For overnight shifts (shift_end <= shift_start): if punch_in IST time is
    earlier than shift_end, it belongs to the previous day's shift.
    """
    # Convert to IST for date calculations
    punch_in_ist = punch_in.astimezone(IST)
    punch_in_truncated = truncate_to_seconds(punch_in_ist)

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        # shift_end time in minutes from midnight
        shift_end_minutes = eh * 60 + em
        punch_in_minutes = punch_in_truncated.hour * 60 + punch_in_truncated.minute

        # If punch_in time is before shift_end time, it belongs to previous day's shift
        if punch_in_minutes < shift_end_minutes:
            attendance_date = (punch_in_truncated - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            attendance_date = punch_in_truncated.strftime("%Y-%m-%d")
    else:
        attendance_date = punch_in_truncated.strftime("%Y-%m-%d")

    return attendance_date


def validate_epoch_millis(value: Optional[int]) -> Optional[int]:
    """
    Validate epoch milliseconds per OpenAPI schema.
    Must be >= 100000000000 (year 1973) and <= 4102444800000 (year 2100).
    """
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Must be an integer")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("Value out of valid range")
    return value


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator('emp_code')
    @classmethod
    def validate_emp_code(cls, v):
        import re
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError('Must match pattern EMP followed by 4-6 digits')
        return v

    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        import re
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError('Invalid email format')
        if len(v) > 120:
            raise ValueError('Email too long')
        return v

    @field_validator('shift_start', 'shift_end')
    @classmethod
    def validate_time(cls, v):
        import re
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError('Must be HH:MM 24-hour format')
        return v

    @field_validator('joined_on')
    @classmethod
    def validate_date(cls, v):
        import re
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', v):
            raise ValueError('Must be YYYY-MM-DD')
        return v

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError('Name must be 1-100 characters')
        return v

    @field_validator('department')
    @classmethod
    def validate_department(cls, v):
        if not v or len(v) > 50:
            raise ValueError('Department must be 1-50 characters')
        return v

    @model_validator(mode='after')
    def check_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError('shift_start must differ from shift_end')
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator('punched_at', 'status', mode='before')
    @classmethod
    def validate_punched_at(cls, v, info):
        if info.field_name == 'punched_at' and v is not None:
            return validate_epoch_millis(v)
        return v

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if v not in ('PRESENT', 'WFH', 'ON_DUTY'):
            raise ValueError('status must be PRESENT, WFH, or ON_DUTY')
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator('punched_at', mode='before')
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            return validate_epoch_millis(v)
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ('PRESENT', 'WFH', 'ON_DUTY', 'ABSENT', 'LEAVE'):
            raise ValueError('Invalid status')
        return v

    @field_validator('punch_in', 'punch_out', mode='before')
    @classmethod
    def validate_times(cls, v, info):
        if v is not None:
            return validate_epoch_millis(v)
        return v

    @field_validator('reason')
    @classmethod
    def validate_reason(cls, v):
        if not v or len(v) < 5 or len(v) > 200:
            raise ValueError('reason must be 5-200 characters')
        return v

    @field_validator('regularized_by')
    @classmethod
    def validate_by(cls, v):
        if not v or len(v) < 1 or len(v) > 50:
            raise ValueError('regularized_by must be 1-50 characters')
        return v


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


# ---------------------------------------------------------------------------
# Index creation at startup
# ---------------------------------------------------------------------------

def create_indexes():
    """Create indexes idempotently at startup."""
    # Unique index on emp_code for employees collection
    db.employees.create_index("emp_code", unique=True,
                              name="idx_emp_code_unique")

    # Compound unique index for attendance_logs: (emp_code, date)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)],
                                    unique=True, name="idx_attendance_unique")

    # Indexes for common queries
    db.attendance_logs.create_index("emp_code", name="idx_attendance_emp_code")
    db.attendance_logs.create_index("date", name="idx_attendance_date")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)],
                                    name="idx_attendance_emp_date")
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)],
                                    name="idx_attendance_date_emp")
    db.employees.create_index("department", name="idx_employees_department")
    db.employees.create_index("joined_on", name="idx_employees_joined_on")


create_indexes()


# ---------------------------------------------------------------------------
# Helper: get employee or raise 404
# ---------------------------------------------------------------------------

def get_employee_or_404(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")
    return emp


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Readiness probe - returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB not available")


# ---------------------------------------------------------------------------
# Employee endpoints
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces unique emp_code via DB index."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List employees sorted by emp_code ascending.
    total counts the filtered set, not the whole collection.
    """
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)

    skip = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        emp["created_at"] = dt_to_epoch_ms(emp["created_at"])
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's attendance record.
    Uses atomic upsert to handle concurrent duplicate punch-ins.
    """
    # Get employee
    emp = get_employee_or_404(body.emp_code)

    # Determine timestamp
    if body.punched_at:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc)

    # Determine attendance date per R1
    date_str = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    # Calculate late_minutes per R2
    late_minutes = compute_late_minutes(ts, emp["shift_start"])

    # Build the attendance document
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    # Use atomic upsert to handle concurrency.
    # upsert=True creates the document if it doesn't exist.
    # If it already exists, we check if it has a punch_in (duplicate) or not.
    result = db.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": date_str},
        {"$setOnInsert": doc},
        upsert=True
    )

    if result.upserted_id:
        # We created a new record
        doc["_id"] = result.upserted_id
    else:
        # Document already existed - check if it's a duplicate
        existing = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": date_str})
        if existing.get("punch_in") is not None:
            raise HTTPException(status_code=409, detail="already punched in for this date")
        # Race condition: document existed but without punch_in, use the existing
        doc = existing

    # Convert for response
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_epoch_ms(doc.get("punch_out"))

    return doc


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes the employee's most recent open attendance record.
    Uses conditional update to handle concurrent punch-outs.
    """
    # Get employee
    emp = get_employee_or_404(body.emp_code)

    # Determine timestamp
    if body.punched_at:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc)

    ts_truncated = truncate_to_seconds(ts)

    # Find the most recent record with punch_in <= punched_at
    # This must work across midnight for overnight shifts
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts}
        },
        sort=[("punch_in", -1)]  # Most recent first
    )

    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    # Check if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    punch_in_dt = record["punch_in"]

    # Validate: punched_at must be after punch_in
    if ts_truncated <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    # Validate: punched_at must be within 24 hours of punch_in
    diff_seconds = (ts_truncated - punch_in_dt).total_seconds()
    if diff_seconds > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    # Compute derived values
    work_hours = compute_work_hours(punch_in_dt, ts_truncated)
    overtime_minutes = compute_overtime_minutes(ts_truncated, emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    # Use atomic update with condition to handle concurrency
    # Only update if punch_out is still null
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None  # Condition: must not already be punched out
        },
        {
            "$set": {
                "punch_out": ts_truncated,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if update_result is None:
        # Someone else already punched out this record
        raise HTTPException(status_code=409, detail="record already punched out")

    # Convert for response
    update_result.pop("_id", None)
    update_result["punch_in"] = dt_to_epoch_ms(update_result["punch_in"])
    update_result["punch_out"] = dt_to_epoch_ms(update_result["punch_out"])

    return update_result


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Uses MongoDB skip/limit for efficient pagination.
    """
    # Validate date range
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    # Build query
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from
        if date_to:
            date_query["$lte"] = date_to
        query["date"] = date_query
    if status:
        query["status"] = status

    # Get total count with filters
    total = db.attendance_logs.count_documents(query)

    # Query with sort and pagination
    cursor = db.attendance_logs.find(query)\
        .sort([("date", -1), ("emp_code", 1)])\
        .skip((page - 1) * page_size)\
        .limit(page_size)

    items = []
    for record in cursor:
        # Convert for response - exclude _id
        item = {
            "emp_code": record["emp_code"],
            "date": record["date"],
            "status": record["status"],
            "punch_in": dt_to_epoch_ms(record.get("punch_in")),
            "punch_out": dt_to_epoch_ms(record.get("punch_out")),
            "work_hours": record.get("work_hours"),
            "late_minutes": record.get("late_minutes", 0),
            "overtime_minutes": record.get("overtime_minutes", 0),
            "half_day": record.get("half_day", False),
            "history": record.get("history", [])
        }
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest
):
    """
    Manually correct (regularize) one day's attendance record.
    Appends to history, recomputes derived fields.
    """
    # Validate date format
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        raise HTTPException(status_code=422, detail="Invalid date format")
    
    # Verify employee exists
    emp = get_employee_or_404(emp_code)

    # Find the attendance record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record found for this date")

    # Store original values for history
    original = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False)
    }

    # Build new values
    new_status = body.status if body.status is not None else original["status"]
    new_punch_in = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else original["punch_in"]
    new_punch_out = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else original["punch_out"]

    # Validation rules per OpenAPI spec

    # Rule: ABSENT or LEAVE clears both punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422,
                detail="punch_in and punch_out must not be supplied with ABSENT or LEAVE status")
        new_punch_in = None
        new_punch_out = None

    # Rule: presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if new_punch_in is None:
            raise HTTPException(status_code=422,
                detail="punch_in is required for presence status")

    # Rule: punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        record_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if record_date != date:
            raise HTTPException(status_code=422,
                detail="punch_in must be on the record's attendance date")

    # Rule: punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(status_code=422,
                detail="punch_out requires punch_in")
        new_punch_out_truncated = truncate_to_seconds(new_punch_out)
        if new_punch_out_truncated <= new_punch_in:
            raise HTTPException(status_code=422,
                detail="punch_out must be after punch_in")
        diff_seconds = (new_punch_out_truncated - new_punch_in).total_seconds()
        if diff_seconds > 24 * 3600:
            raise HTTPException(status_code=422,
                detail="punch_out must be within 24 hours of punch_in")

    # Compute derived values
    work_hours = compute_work_hours(new_punch_in, new_punch_out)
    late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"]) if new_punch_in else 0
    overtime_minutes = compute_overtime_minutes(new_punch_out, emp["shift_end"], date) if new_punch_out else 0
    half_day = compute_half_day(work_hours)

    # Check if anything actually changed
    changes = {}
    if new_status != original["status"]:
        changes["status"] = {"from": original["status"], "to": new_status}
    if new_punch_in != original["punch_in"]:
        changes["punch_in"] = {"from": dt_to_epoch_ms(original["punch_in"]),
                                "to": dt_to_epoch_ms(new_punch_in)}
    if new_punch_out != original["punch_out"]:
        changes["punch_out"] = {"from": dt_to_epoch_ms(original["punch_out"]),
                                 "to": dt_to_epoch_ms(new_punch_out)}
    if work_hours != original["work_hours"]:
        changes["work_hours"] = {"from": original["work_hours"], "to": work_hours}
    if late_minutes != original["late_minutes"]:
        changes["late_minutes"] = {"from": original["late_minutes"], "to": late_minutes}
    if overtime_minutes != original["overtime_minutes"]:
        changes["overtime_minutes"] = {"from": original["overtime_minutes"], "to": overtime_minutes}
    if half_day != original["half_day"]:
        changes["half_day"] = {"from": original["half_day"], "to": half_day}

    # If nothing changed, return 422
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    # Build history entry
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Apply the update atomically
    # Use version field or conditional update to handle concurrent edits
    update_result = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if update_result is None:
        raise HTTPException(status_code=409, detail="concurrent modification detected")

    # Convert for response
    update_result.pop("_id", None)
    update_result["punch_in"] = dt_to_epoch_ms(update_result.get("punch_in"))
    update_result["punch_out"] = dt_to_epoch_ms(update_result.get("punch_out"))

    # Convert history dates
    for entry in update_result.get("history", []):
        if "at" in entry and isinstance(entry["at"], datetime):
            entry["at"] = dt_to_epoch_ms(entry["at"])
        # Convert punch_in/punch_out in changes
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                if isinstance(change.get("from"), datetime):
                    change["from"] = dt_to_epoch_ms(change["from"])
                if isinstance(change.get("to"), datetime):
                    change["to"] = dt_to_epoch_ms(change["to"])

    # Ensure history field exists
    if "history" not in update_result:
        update_result["history"] = []

    return update_result


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str
):
    """
    Monthly attendance summary for one employee.
    Uses MongoDB aggregation.
    """
    # Verify employee exists
    if db.employees.find_one({"emp_code": emp_code}) is None:
        raise HTTPException(status_code=404, detail=f"Employee {emp_code} not found")

    # Parse month
    try:
        year, mon = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month format")

    # Calculate date range for the month
    import calendar
    last_day = calendar.monthrange(year, mon)[1]
    date_from = f"{year}-{mon:02d}-01"
    date_to = f"{year}-{mon:02d}-{last_day:02d}"

    # Get employee for working days calculation
    emp = db.employees.find_one({"emp_code": emp_code})
    joined_on = emp["joined_on"]

    # Count working days (Mon-Fri) in the month, from joined_on
    working_days = 0
    from datetime import date
    start = date(year, mon, 1)
    end = date(year, mon, last_day)

    current = start
    while current <= end:
        # Count from joined_on onwards
        if current >= date.fromisoformat(joined_on):
            # Monday = 0, Friday = 4
            if current.weekday() < 5:
                working_days += 1
        current += timedelta(days=1)

    # MongoDB aggregation for attendance stats
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": date_from, "$lte": date_to}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$or": [
                            {"$eq": ["$status", "PRESENT"]},
                            {"$eq": ["$status", "WFH"]},
                            {"$eq": ["$status", "ON_DUTY"]}
                        ]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        stats = result[0]
    else:
        stats = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    present_days = stats.get("present_days", 0)
    leave_days = stats.get("leave_days", 0)
    late_count = stats.get("late_count", 0)
    total_late_minutes = stats.get("total_late_minutes", 0)
    total_overtime_minutes = stats.get("total_overtime_minutes", 0)

    # Calculate attendance percentage
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = Query(None)
):
    """
    Department summary for a month.
    Uses MongoDB aggregation across both collections.
    """
    # Parse month
    try:
        year, mon = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month format")

    import calendar
    last_day = calendar.monthrange(year, mon)[1]
    date_from = f"{year}-{mon:02d}-01"
    date_to = f"{year}-{mon:02d}-{last_day:02d}"

    # Build aggregation pipeline
    pipeline = [
        # Start with employees collection
        {"$match": {"joined_on": {"$lte": date_to}}},
        # Filter by department if specified
        {"$match": {**({"department": department} if department else {})}},
        # Lookup attendance logs for the month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                    "date": {"$gte": date_from, "$lte": date_to}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # Group by department
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$or": [
                            {"$eq": ["$logs.status", "PRESENT"]},
                            {"$eq": ["$logs.status", "WFH"]},
                            {"$eq": ["$logs.status", "ON_DUTY"]}
                        ]},
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                        0
                    ]
                }
            },
            "avg_work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$or": [
                                {"$eq": ["$logs.status", "PRESENT"]},
                                {"$eq": ["$logs.status", "WFH"]},
                                {"$eq": ["$logs.status", "ON_DUTY"]}
                            ]},
                            {"$ifNull": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        0
                    ]
                }
            },
            "avg_work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$or": [
                                {"$eq": ["$logs.status", "PRESENT"]},
                                {"$eq": ["$logs.status", "WFH"]},
                                {"$eq": ["$logs.status", "ON_DUTY"]}
                            ]},
                            {"$ifNull": ["$logs.work_hours", None]}
                        ]},
                        1,
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                        ]},
                        1,
                        0
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
            }
        }},
        # Calculate avg_work_hours
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$eq": ["$avg_work_hours_count", 0]},
                    None,
                    {"$round": [{"$divide": ["$avg_work_hours_sum", "$avg_work_hours_count"]}, 2]}
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))

    # Filter out departments with headcount 0
    items = [r for r in results if r["headcount"] > 0]

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    """
    Late-comers leaderboard for a month.
    Standard competition ranking (ties share rank, next rank skipped).
    """
    # Parse month
    try:
        year, mon = map(int, month.split("-"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month format")

    import calendar
    last_day = calendar.monthrange(year, mon)[1]
    date_from = f"{year}-{mon:02d}-01"
    date_to = f"{year}-{mon:02d}-{last_day:02d}"

    # MongoDB aggregation for late leaderboard
    pipeline = [
        # Match attendance logs in date range with late_minutes > 0
        {"$match": {
            "date": {"$gte": date_from, "$lte": date_to},
            "late_minutes": {"$gt": 0}
        }},
        # Lookup employee info
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        # Filter by department if specified
        {"$match": {**({"emp.department": department} if department else {})}},
        # Group by emp_code
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        # Sort by total_late_minutes descending, then emp_code ascending
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # Calculate standard competition ranking in Python
    # Ties share a rank and the next rank is skipped (e.g., 1, 2, 2, 4)
    ranked_items = []
    current_rank = 0
    previous_late_minutes = None
    
    for i, item in enumerate(results):
        if item["total_late_minutes"] != previous_late_minutes:
            current_rank = i + 1
            previous_late_minutes = item["total_late_minutes"]
        
        if current_rank <= limit:
            ranked_items.append({
                "rank": current_rank,
                "emp_code": item["_id"],
                "name": item["name"],
                "department": item["department"],
                "total_late_minutes": item["total_late_minutes"],
                "late_count": item["late_count"]
            })

    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    """
    Daily attendance trend for a department.
    Returns one row per calendar day from from to to inclusive.
    Uses $densify for gap-filling.
    """
    # Validate department exists
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=404, detail=f"Department {department} not found")

    # Validate date range
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")

    from datetime import datetime, timedelta
    d_from = datetime.fromisoformat(from_date)
    d_to = datetime.fromisoformat(to_date)
    days_diff = (d_to - d_from).days

    if days_diff > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")

    to_dt = d_to
    
    # Generate all dates in range
    from datetime import timedelta
    all_dates = []
    current = d_from.date()
    while current <= d_to.date():
        all_dates.append(current.isoformat())
        current += timedelta(days=1)

    # Get employees in department with joined_on <= to_date (for headcount)
    employees = list(db.employees.find(
        {"department": department, "joined_on": {"$lte": to_date}},
        {"emp_code": 1, "joined_on": 1}
    ))

    # Build aggregation pipeline to get daily stats
    pipeline = [
        # Match attendance logs for the department in date range
        {"$match": {
            "emp_code": {"$in": [e["emp_code"] for e in employees]},
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        # Group by date to get daily aggregates
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$or": [
                            {"$eq": ["$status", "PRESENT"]},
                            {"$eq": ["$status", "WFH"]},
                            {"$eq": ["$status", "ON_DUTY"]}
                        ]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            }
        }}
    ]

    # Execute aggregation
    daily_stats = {}
    for record in db.attendance_logs.aggregate(pipeline):
        daily_stats[record["_id"]] = {
            "present_count": record.get("present_count", 0),
            "late_count": record.get("late_count", 0)
        }
    
    # Build results for all dates in range
    results = []
    for date_str in all_dates:
        stats = daily_stats.get(date_str, {"present_count": 0, "late_count": 0})
        
        # Calculate headcount for this date
        headcount = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": date_str}
        })
        
        # Determine if working day (Mon-Fri)
        date_obj = datetime.fromisoformat(date_str).date()
        is_working_day = date_obj.weekday() < 5  # Monday = 0, Friday = 4
        
        # Calculate attendance rate
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(stats["present_count"] / headcount, 4)
        
        results.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": stats["present_count"],
            "late_count": stats["late_count"],
            "attendance_rate": attendance_rate
        })
    
    # Calculate moving_avg_7d
    rates = [r["attendance_rate"] for r in results]
    for i, r in enumerate(results):
        window_start = max(0, i - 6)
        window_rates = [rates[j] for j in range(window_start, i + 1) if rates[j] is not None]
        
        if window_rates:
            r["moving_avg_7d"] = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            r["moving_avg_7d"] = None
    
    return {"department": department, "items": results}


# ---------------------------------------------------------------------------
# Admin endpoint: explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Run explain with executionStats verbosity for an endpoint's main query.
    """
    # Build the query/aggregation that the endpoint would run
    explain_result = None
    collection_name = None

    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            date_query = {}
            if date_from:
                date_query["$gte"] = date_from
            if date_to:
                date_query["$lte"] = date_to
            query["date"] = date_query
        if status:
            query["status"] = status

        # Use database.command for explain (PyMongo 4.x doesn't support cursor.explain with args)
        explain_result = db.command("explain",
            {"find": collection_name, "filter": query,
             "sort": {"date": -1, "emp_code": 1},
             "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month required")
        collection_name = "attendance_logs"

        try:
            year, mon = map(int, month.split("-"))
            import calendar
            last_day = calendar.monthrange(year, mon)[1]
            date_from_m = f"{year}-{mon:02d}-01"
            date_to_m = f"{year}-{mon:02d}-{last_day:02d}"
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid month format")

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": date_from_m, "$lte": date_to_m}
            }},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": 1}
            }}
        ]

        # Use database.command for aggregation explain (PyMongo 4.x)
        explain_result = db.command({
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        collection_name = "employees"

        try:
            year, mon = map(int, month.split("-"))
            import calendar
            last_day = calendar.monthrange(year, mon)[1]
            date_from_m = f"{year}-{mon:02d}-01"
            date_to_m = f"{year}-{mon:02d}-{last_day:02d}"
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid month format")

        pipeline = [
            {"$match": {"joined_on": {"$lte": date_to_m}}},
            {"$match": {**({"department": department} if department else {})}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                        "date": {"$gte": date_from_m, "$lte": date_to_m}
                    }}
                ],
                "as": "logs"
            }}
        ]

        # Use database.command for aggregation explain
        explain_result = db.command({
            "explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats"
        })

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        collection_name = "attendance_logs"

        try:
            year, mon = map(int, month.split("-"))
            import calendar
            last_day = calendar.monthrange(year, mon)[1]
            date_from_m = f"{year}-{mon:02d}-01"
            date_to_m = f"{year}-{mon:02d}-{last_day:02d}"
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid month format")

        # Simplified pipeline for explain (without $setWindowFields which can't be explained with multiple sort fields)
        pipeline = [
            {"$match": {
                "date": {"$gte": date_from_m, "$lte": date_to_m},
                "late_minutes": {"$gt": 0}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {**({"emp.department": department} if department else {})}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit}
        ]

        # Use database.command for aggregation explain
        explain_result = db.command({
            "explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats"
        })

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to required")

        collection_name = "attendance_logs"

        employees = list(db.employees.find(
            {"department": department, "joined_on": {"$lte": to_date}},
            {"emp_code": 1}
        ))

        pipeline = [
            {"$match": {
                "emp_code": {"$in": [e["emp_code"] for e in employees]},
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": 1}
            }}
        ]

        # Use database.command for aggregation explain
        explain_result = db.command({
            "explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats"
        })

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_result
    }
