# REVIEW.md

| # | Where | What is wrong | How to notice | Fix applied |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Subtracts the shift start on the punch-in's calendar date, so overnight early-morning punches are compared with the wrong day. | Punch in at 01:00 for a 22:00–06:00 shift. | Calculate the start instant on the previous day when appropriate; apply the 10-minute grace rule strictly. |
| 2 | `compute_work_hours` / rounding | Native `round()` uses bankers' rounding and float arithmetic. | Use a value exactly halfway at two decimal places. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Does not handle overnight shifts and does not enforce the 30-minute minimum. | Punch out at 06:20 for a 22:00–06:00 shift should yield zero overtime. | Build the end instant on the correct calendar day and apply the 30-minute threshold. |
| 4 | Employee creation | A read-before-insert duplicate check races; shift start and end can be equal; server time is naive. | Send concurrent creates for the same code, or submit equal shift times. | Unique database index, catch duplicate-key errors, validate distinct shifts, and store UTC instants. |
| 5 | Employee listing | Pagination skips the first page incorrectly; total ignores the department filter; results are not sorted. | Request page 1 or filter by department. | Use `(page - 1) * page_size`, count the filtered query, and sort by employee code. |
| 6 | Punch-in | Unknown employee dereferences `None`; timestamp is interpreted in the machine's local timezone; date can be wrong for IST/overnight shifts; check-then-insert is racy; invalid status accepted. | Unknown code, concurrent requests, UTC/IST boundary, or overnight punch. | Validate employee/status/timestamp, calculate IST attendance date, and rely on a unique `(emp_code, date)` index. |
| 7 | Attendance listing | Loads the entire collection and sorts/slices in Python; leaks `_id`; omits date/employee tie-break sort; pagination validation is missing. | Large dataset or page > 1. | Apply filters, sort, skip and limit in MongoDB; convert timestamps and hide `_id`. |
| 8 | Missing contract endpoints | Punch-out, regularization, monthly analytics, department summary, leaderboard, trend and explain endpoints are absent. | Request any documented route. | Implement the documented routes and response shapes. |
| 9 | API timestamp serialization | Mongo dates are returned as datetimes instead of epoch milliseconds; nested history time values need conversion too. | Inspect create/punch/history responses. | Convert all API instants, including history changes, at the boundary. |
| 10 | Health check and startup | Health returns success without checking MongoDB; required indexes are not created. | Stop MongoDB or inspect indexes. | Ping MongoDB and create idempotent indexes at startup. |

## Considered but not defects

- `date` and `joined_on` are stored as `YYYY-MM-DD` strings: this is intentional because they represent calendar dates, not instants.
- Missing `history`, `half_day`, `late_minutes`, and `overtime_minutes` in legacy records are valid and should be treated as `[]`, `false`, `0`, and `0`.
- A day with no attendance document is not the same as an `ABSENT` record.
