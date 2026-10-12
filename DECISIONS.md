# DECISIONS.md

## 1. Indexes

Created the following indexes at startup:

- **`employees.emp_code` (unique)**: Enforces unique employee codes per spec. Prevents duplicate employee creation at database level.
- **`attendance_logs.(emp_code, date)` (unique)**: Enforces one record per employee per day. Critical for preventing duplicate punch-ins via database constraint.
- **`attendance_logs.emp_code`**: Supports employee-specific queries (punch-out lookup, attendance listing).
- **`attendance_logs.date`**: Supports date-range filtering for attendance list and analytics.
- **`attendance_logs.(emp_code, date)` (compound)**: Optimizes "find most recent record" queries for punch-out.
- **`employees.department`**: Supports department filtering in analytics.
- **`employees.joined_on`**: Supports headcount calculations in department analytics.

**Rejected**: A text index on name/email - not needed for the API's query patterns.

## 2. Punch-in race condition

When two identical punch-in requests arrive simultaneously for the same employee/date:

1. Both requests call `update_one` with `upsert=True` and `$setOnInsert`.
2. MongoDB's unique index on `(emp_code, date)` ensures only one document is created.
3. The first request gets `result.upserted_id` set and returns 201.
4. The second request finds `result.upserted_id` is None, re-fetches the document, sees `punch_in` is already set, and returns 409.

This relies on the unique index as the ultimate arbiter, not application-level locking.

## 3. Late leaderboard ties

Standard competition ranking (1, 2, 2, 4) is implemented in Python after aggregation:

- Sort by `total_late_minutes` descending, then `emp_code` ascending.
- Iterate through results, incrementing rank only when `total_late_minutes` changes.
- Filter to `rank <= limit` after ranking.

This ensures ties share a rank and the next rank is skipped. The response can contain more than `limit` rows when employees tie at the cutoff.

## 4. Department summary headcount

For each department:

1. Query all employees with `joined_on <= end_of_month`.
2. Use `$lookup` to get their attendance logs for the month.
3. Group by department, counting employees (`headcount`) and summing present days.

**Key**: Employees with zero logs are included because we start from the `employees` collection and `$lookup` attendance. The `headcount` is the count of employees, not logs.

## 5. Scaling to 100x data

If this needed to serve 100x the data:

- **Department trend**: Replace Python date generation + per-date headcount queries with a single `$densify` aggregation (available in MongoDB 5.1+). Currently using Python loop for simplicity.
- **Pagination**: Current skip/limit approach works but could be optimized with keyset pagination for very large result sets.
- **Caching**: Analytics results could be cached for a given month since they're deterministic.
- **Read replicas**: Analytics queries are read-heavy; MongoDB read preferences could distribute load.
- **Pre-computation**: Daily stats could be pre-computed via MongoDB triggers or cron jobs.

## Other decisions

- **Aggregation vs Python**: Used MongoDB aggregations where they provide clear benefits (employee_monthly, department_summary, late_leaderboard). Used Python post-processing for department_trend due to `$densify` compatibility concerns and moving average calculation complexity.
- **UTC storage, IST display**: All instants stored as UTC in MongoDB, converted to/from epoch milliseconds at API boundary. IST used only for date/ruling calculations.
- **Half-up rounding**: Implemented using Python's `Decimal` with `ROUND_HALF_UP` for deterministic rounding.
