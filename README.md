# Employee Attendance and Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

```
python -m venv .venv
.venv\Scripts\activate          # Windows (on Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Configuration is read from the environment: `MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default `attendance_db`). A local `.env` is loaded for convenience, but real environment variables take priority. All indexes are created at startup and re-running is harmless.

## Implemented

- GET /health
- POST /employees, GET /employees
- POST /attendance/punch-in, POST /attendance/punch-out
- PATCH /attendance/{emp_code}/{date} (corrections, history, recomputed derived fields)
- GET /attendance
- GET /analytics/employees/{emp_code}/monthly (aggregation pipeline)
- GET /admin/explain/{endpoint} for `attendance_list` and `employee_monthly`

## Not implemented (ran out of time)

- GET /analytics/departments/summary
- GET /analytics/leaderboard/late
- GET /analytics/departments/{department}/trend

`/admin/explain/` returns 501 for `department_summary`, `late_leaderboard` and `department_trend`.

## Notes

- Instants are epoch milliseconds in the API and UTC datetimes in MongoDB. Dates and shift times are IST.
- Punch-out closes the most recent record whose punch-in is at or before `punched_at`, so overnight shifts work.
- Concurrent corrections: a write only applies if the record's history has not changed since it was read, otherwise the request gets 409 and can be retried, so no history entry is lost.
- Tested by hand through `/docs` with the sample documents plus a few records I created myself (overnight shift, weekend punch, mid-month joiner).
- I could not finish the 100,000-record check of the explain plans on my machine (it ran out of disk space), so the IXSCAN check was only done on the small sample data.
