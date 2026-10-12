# REVIEW.md

## Defects found in starter `app/main.py`

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Did not convert punch_in to IST before comparing with shift_start. Shift times are IST but punch_in is UTC. | Punch-in at 10:05 IST with 09:30 shift would compute wrong late_minutes. | Added IST conversion: `punch_in_ist = punch_in.astimezone(IST)` before comparison. |
| 2 | `punch_in` endpoint | Used `update_one` with `upsert=True` but didn't properly handle the case where document already exists with punch_in set. Returned 201 instead of 409 for duplicate punch-ins. | Two concurrent punch-in requests for same employee/date could both succeed. | Rewrote to check `result.upserted_id` and re-fetch existing document to determine if it's a duplicate. |
| 3 | `punch_out` endpoint | Query included `"punch_out": None` condition, preventing finding already-punched-out records for duplicate detection. | Second punch-out attempt returned 404 instead of 409. | Removed `punch_out: None` from initial query; added separate check after finding record. |
| 4 | `list_attendance` endpoint | Returned MongoDB documents with `_id` field, causing serialization errors. | API returned ValueError on response serialization. | Explicitly construct response dict excluding `_id`, converting datetime fields to epoch ms. |
| 5 | `regularize_attendance` endpoint | Missing validation for date format in path parameter. | Invalid date strings passed to endpoint. | Added regex validation for YYYY-MM-DD format. |
| 6 | Helper functions | `compute_overtime_minutes` used IST datetime but compared with UTC punch_out without proper conversion. | Overnight shift overtime calculated incorrectly. | Ensured consistent timezone handling throughout. |
| 7 | Missing indexes | No indexes created for `emp_code`, `date`, or compound keys. | Queries on large datasets would be slow; concurrent punch-ins not properly prevented. | Added unique index on `(emp_code, date)` for attendance_logs and `emp_code` for employees. |
| 8 | Pagination | Used Python slicing on full result set instead of MongoDB skip/limit. | Would load entire collection into memory for large datasets. | Changed to use `.skip().limit()` in MongoDB queries. |
| 9 | `list_employees` | `total` counted entire collection instead of filtered set. | Filtered queries returned wrong total count. | Changed `count_documents` to use the same query filter. |
| 10 | Date handling | Used `datetime.now()` without timezone for punch-in default, causing inconsistent IST date calculation. | Punch-in records dated incorrectly depending on server timezone. | Used `datetime.now(timezone.utc)` and proper IST conversion. |

## Not defects (intentional or acceptable)

- **Simple structure**: Single file is acceptable for a small assessment; no need to over-engineer.
- **Basic error handling**: FastAPI's default error responses are acceptable per the contract.
- **No authentication**: Spec explicitly states "no authentication in this assignment."

## Key business rule clarifications

- **R2 Late calculation**: Must convert punch_in to IST, truncate to seconds, then compare with shift_start.
- **R1 Date calculation**: For overnight shifts, punch_in before shift_end time belongs to previous day's shift.
- **R5 Half day**: `work_hours < 4.50` (strictly less than), not `<= 4.50`.
