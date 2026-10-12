# Employee Attendance & Analytics API

A FastAPI application for tracking employee attendance with punch-in/punch-out operations, manual corrections with audit trail, and analytics aggregations using MongoDB.

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (tested with 8.0)

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env  # Edit MONGO_URI and MONGO_DB as needed

# Seed sample data (optional, for testing)
python sample_seed.py

# Start the server
uvicorn app.main:app --port 8000
```

## MongoDB Configuration

Environment variables:
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

## API Documentation

Once the server is running, interactive API docs are available at:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Running Tests

```bash
# Install test dependencies
pip install pytest httpx

# Run tests
pytest test_app.py -v
```

## Implemented Features

### Employees
- `POST /employees` - Create employee (unique emp_code enforced by DB index)
- `GET /employees` - List employees with pagination and department filter

### Attendance
- `POST /attendance/punch-in` - Record punch-in with late calculation (R1-R2)
- `POST /attendance/punch-out` - Record punch-out with work hours, overtime, half-day (R3-R5)
- `GET /attendance` - List attendance records with filters and pagination
- `PATCH /attendance/{emp_code}/{date}` - Manual correction with audit trail

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Monthly summary per employee
- `GET /analytics/departments/summary` - Department summary with headcount
- `GET /analytics/leaderboard/late` - Late-comers leaderboard with competition ranking
- `GET /analytics/departments/{department}/trend` - Daily attendance trend

### Admin
- `GET /admin/explain/{endpoint}` - MongoDB query explain output

## Business Rules Implemented

- **R1**: IST date calculation with overnight shift support
- **R2**: Late calculation with 10-minute grace period
- **R3**: Overtime calculation (>= 30 minutes threshold)
- **R4**: Work hours rounded half-up to 2 decimals
- **R5**: Half-day when work_hours < 4.50
- **R6**: Presence status determination
- **R7**: Working days = Mon-Fri, counted from joined_on
- **R8**: Half-up rounding for reported numbers
- **R9**: Headcount includes all employees joined on/before period end
- **R10**: Pagination with page_size max 100

## Interview Walkthrough

### 1. Request-to-Database Flow

```
HTTP Request → FastAPI Route → Pydantic Validation → Business Logic →
MongoDB Operation → Response Serialization → HTTP Response
```

- All datetime values are converted to/from epoch milliseconds at the API boundary
- MongoDB stores datetimes in UTC; IST conversions happen in business logic
- Derived fields (work_hours, late_minutes, etc.) are computed by the API, not stored from client input

### 2. Attendance State Transitions

```
No Record → [Punch In] → Open Record (punch_in set, punch_out null)
Open Record → [Punch Out] → Closed Record (both times set, derived fields computed)
Any Record → [PATCH regularize] → Updated Record (history entry appended)
```

- Status values: PRESENT, WFH, ON_DUTY (require punch_in), ABSENT, LEAVE (no punch times)
- ABSENT/LEAVE are never created by punch endpoints, only by seeded data or corrections

### 3. Duplicate and Concurrent Operations

**Punch-in duplicates**: Unique compound index on `(emp_code, date)` in MongoDB ensures only one record per employee per day. When two requests arrive simultaneously:
- First request creates the document via upsert
- Second request finds document exists with punch_in set → returns 409

**Punch-out duplicates**: Find record, check if already punched out, conditional update with `punch_out: None` check → 409 if already punched out.

**Regularize concurrency**: `find_one_and_update` with `$push` to history ensures atomic append.

### 4. Manual Corrections and Audit Trail

The `history` array is append-only:

```json
{
  "at": 1783312800000,  // epoch ms
  "by": "hr.admin",     // who made the change
  "reason": "biometric glitch",
  "changes": {
    "punch_in": {"from": 1783312500000, "to": 1783310280000},
    "late_minutes": {"from": 35, "to": 0}
  }
}
```

- Only fields that actually changed are recorded
- Derived fields are recomputed after correction
- Punch-in/punch-out never write to history (only corrections do)

### 5. Analytics Aggregation Pipeline Example (Employee Monthly)

```javascript
// Stage 1: Match records for employee in date range
{ "$match": { "emp_code": "EMP0001", "date": { "$gte": "2026-07-01", "$lte": "2026-07-31" } } }

// Stage 2: Group and calculate stats
{ "$group": {
    "_id": null,
    "present_days": { "$sum": {
        "$cond": [
            { "$or": [
                { "$eq": ["$status", "PRESENT"] },
                { "$eq": ["$status", "WFH"] },
                { "$eq": ["$status", "ON_DUTY"] }
            ]},
            { "$cond": [{ "$ifNull": ["$half_day", false] }, 0.5, 1.0] },
            0
        ]
    }},
    "leave_days": { "$sum": { "$cond": [{ "$eq": ["$status", "LEAVE"] }, 1, 0] } },
    "late_count": { "$sum": { "$cond": [{ "$gt": [{ "$ifNull": ["$late_minutes", 0] }, 0] }, 1, 0] } },
    "total_late_minutes": { "$sum": { "$ifNull": ["$late_minutes", 0] } }
}}
```

Working days are calculated in Python (Mon-Fri count from joined_on to month end).

### 6. Key Trade-offs and Limitations

- **Department trend**: Uses Python loop for date generation instead of MongoDB `$densify` for compatibility. Could be optimized with `$densify` on MongoDB 5.1+.
- **Moving average**: Calculated in Python after aggregation for clarity; MongoDB window functions could do this in-database.
- **Single file**: All code in `app/main.py` per the assessment requirements. In production, would split into routers, services, models.
- **No caching**: Analytics recalculated on each request. Could cache monthly results.

## Known Limitations

- Department trend's moving average uses Python post-processing
- No rate limiting or request throttling
- No backup/retention policies implemented

## License

Assessment submission - all rights reserved by candidate.
