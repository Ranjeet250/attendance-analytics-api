# DECISIONS.md

**1. Indexes.** employees: unique emp_code, so duplicates become 409. attendance_logs: unique (emp_code, date) gives one record per day and serves the monthly query; (emp_code, punch_in desc) finds the open record at punch-out; (date desc, emp_code) and (status, date desc, emp_code) match the list sort and filters. Rejected: a separate index on status alone. It has only five values, so it filters little, and the compound one already covers it.

**2. Punch-in race.** Both requests build the record and call insert_one. The unique (emp_code, date) index lets MongoDB accept exactly one. The other gets DuplicateKeyError and I return 409. There is no check-then-insert gap.

**3. Ties.** Not implemented in this submission. Plan: rank with $setWindowFields and $rank (1, 2, 2, 4), then return every row with rank <= limit. A tie at the cutoff returns all tied people, so the response can be longer than limit. Order: late minutes, then emp_code.

**4. Headcount.** Not implemented either. Plan: start from employees (joined_on on or before the month end) and $lookup their logs, so people with zero logs still count.

**5. One change for 100x data.** Pre-compute monthly totals per employee instead of aggregating raw logs on every request.
