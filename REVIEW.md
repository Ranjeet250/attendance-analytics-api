# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| #   | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
| --- | ----------------------- | ------------- | --------------------------------------------- | ---------------- |

| 1 | 'list_employees', the 'total' line|'count_documents({})' ignores the department filter | 'GET /employees? daparment=Sales' returns total 6, but only 2 employess are in sales |Passed the filter 'q' to 'count_documents' |

| 2 | list*employees, skip line | skip = page * page*size treats page as 0-based, so page 1 skips the first page_size records | GET /employees?page=1&page_size=20 returns empty items while total is 6 | Changed to skip = (page - 1) * page_size |

| 3 | list_employees, find call | No sort, so the order is not guaranteed and pages can repeat or miss employees | GET /employees?page_size=2 pages are not guaranteed to be in emp_code order as the contract requires | Added .sort("emp_code", 1) before skip and limit |

| 4 | list_employees, page and page_size parameters | No bounds on page or page_size, so page=0 gives a negative skip and a 500, and page_size=0 or 1000 is accepted although R10 caps it at 100 | GET /employees?page=0 returns a server error, GET /employees?page_size=1000 is accepted | Used Query(1, ge=1) for page and Query(20, ge=1, le=100) for page_size so bad values return 422 |

| 5 | EmployeeIn model | No format checks on email, shift_start, shift_end, joined_on, so garbage is saved | POST /employees with email abc, shift_start 25:99, joined_on hello returns 201 and is stored | Added Field(pattern=...) for each so invalid values return 422 |
Added a field validator that parses joined_on as a real date, so 2026-13-45 also returns 422.

| 6 | create_employee, created_at | datetime.now() is naive local time (IST), PyMongo stores it as if it were UTC, so created_at is 5h30m ahead of real UTC | created_at showed 2026-10-12T00:18 UTC while the response Date header said 18:52 GMT on 11 Oct | Changed to datetime.now(timezone.utc) |

| 7 | health | Always returns ok without touching MongoDB, so it is 200 even when the database is down | Stop MongoDB and call GET /health, it still returns 200 instead of 503 | Ping the database with client.admin.command ping and return 503 on failure |

| 8 | create_employee, duplicate check | find_one then insert_one is check-then-act, so two concurrent requests with the same emp_code can both succeed | Send two parallel POST /employees with the same emp_code, two documents can be stored | Unique index on emp_code created at startup, insert directly and map DuplicateKeyError to 409 |

| 9 | create_employee, response | created_at is returned as an ISO string, but the contract says every instant is integer epoch milliseconds | POST /employees returns created_at as "2026-10-12T00:18:18.379000" instead of a 13-digit integer | Keep the datetime in MongoDB, convert to int(now.timestamp() \* 1000) in the response |

| 10 | punch_in, the employee lookup | The result of find_one is never checked. For an unknown emp_code it is None and the code crashes later on emp["Shift_start"]. The contract wants 404. | POST /attendance/punch-in with emp_code EMP9999 returns a 500 error (TypeError). | Return HTTPException 404 right after the lookup when the employee is not found. |

| 11 | PunchInIn model, the status field | status is a free string, so any value is accepted and stored. Punch-in may only create PRESENT, WFH or ON_DUTY. ABSENT and LEAVE come from seeded data or corrections. | POST /attendance/punch-in with status BANANA returned 201 and saved BANANA. | Changed the type to Literal["PRESENT", "WFH", "ON_DUTY"], so anything else returns 422. |

| 12 | PunchInIn model, the punched_at field | A plain int accepts numeric strings and floats, and there is no range check. A value in seconds is read as milliseconds and lands in January 1970. | POST /attendance/punch-in with punched_at 1791747715 (seconds) returned 201 with a date in 1970. | Used StrictInt with ge=100_000_000_000, so floats, strings and seconds-sized values return 422. |

| 13 | punch_in, the duplicate check | find_one followed by insert_one is check-then-act. Two simultaneous punch-ins for the same employee and date can both pass the check and both be inserted, giving two records for one day. | Send two parallel identical POST /attendance/punch-in requests. Both can return 201 and two documents for the same (emp_code, date) are stored. | Created a unique index on (emp_code, date) at startup, removed the find_one check and insert directly. DuplicateKeyError is mapped to 409, so the database decides who wins and exactly one request gets 201.

| 14 | punch_in, the ts line | datetime.fromtimestamp and datetime.now return naive local time (IST) and keep the milliseconds. PyMongo stores a naive value as if it were UTC, and R1 says instants are truncated to whole seconds. | POST /attendance/punch-in with a known punched_at stores a punch_in that is 5h30m off from the real UTC instant and keeps the sub-second part. | Build the instant as timezone-aware UTC, use integer division by 1000 so it is truncated to seconds, and use datetime.now(timezone.utc).replace(microsecond=0) for the default. |

| 15 | MongoClient creation | The client is not tz_aware, so datetimes read back from MongoDB are naive and converting them to epoch milliseconds gives wrong values. | A document read from MongoDB has a naive punch_in, and its timestamp is shifted by the local UTC offset. | Created the client with tz_aware=True. |

| 16 | punch_in, date and compute_late_minutes | The attendance date came from ts.date(), which is the server or UTC calendar day, but R1 says it is the IST calendar day, with a special rule for overnight shifts. compute_late_minutes also applied the shift time on the punch-in's own date and floored the minutes before comparing, so 09:40:30 gave 0 instead of 10, and overnight shifts were not handled. | Punch in at 01:30 IST on 13 Oct (20:00 UTC on 12 Oct): the record gets date 2026-10-12 instead of 2026-10-13. Punch in EMP0001 at 09:40:01 IST and the late check depends on rounding order. | Wrote shift_info, which converts to IST, applies the overnight rule and returns the date with the IST shift start and end. Wrote late_minutes_for, which tests total seconds greater than 600 and then floors to minutes. |

| 17 | punch_in, the response | The response added an id field built from the Mongo \ \_id, which the contract says must never be exposed, and returned punch_in as an ISO string although every instant must be integer epoch milliseconds. | POST /attendance/punch-in returned "id": "6acceed3fc1d700312c9ea2d" and "punch_in": "2026-10-12T03:58:00+00:00". | Added attendance_out, which drops \ \_id, converts punch_in, punch_out and history times to milliseconds and defaults half_day and history for seeded records. |

| 18 | list_employees, created_at | created_at was returned as a datetime, so it was serialized as an ISO string instead of integer milliseconds. | GET /employees showed created_at as a string. | Convert it with to_ms before returning. |

| 19 | compute_work_hours | round() on a float is not half-up (R4 and R8), and the instants were not truncated to whole seconds. | 16218 seconds is 4.505 hours: round(16218 / 3600, 2) gives 4.5, half-up gives 4.51. | Rewrote it with Decimal and ROUND_HALF_UP on whole seconds. |

| 20 | compute_overtime | Counted any positive minutes instead of only 30 or more (R3), and built the shift end from the record date as a naive-looking value, which breaks overnight shifts where the shift ends on the next day. | Punch out 20 minutes after shift end gives overtime 20 instead of 0. For EMP0005 (22:00-06:00) the shift end lands on the wrong day. | Rewrote it to take the IST shift end from shift_info, floor to minutes, and return 0 below 30. |

| 21 | list_attendance, the query | The whole collection matching the filter was loaded with list(find(q)), sorted in Python and sliced, so memory and time grow with the data. The contract says do not load the collection into memory. | With about 100,000 records every page request reads all matching documents. | Count with count_documents and let MongoDB do sort, skip and limit. |

| 22 | list_attendance, parameters and sort | Nothing validated page, page_size, dates or status, there was no tie-break in the sort (the contract says date descending, then emp_code ascending), and date_from after date_to was accepted. | GET /attendance?page=0, ?page_size=1000, ?status=BANANA and ?date_from=2026-07-10&date_to=2026-07-01 all returned 200. | Query bounds with ge and le, a regex plus a real date check, a Literal for status, a 422 when date_from is after date_to, and sort by date descending then emp_code ascending. |

| 23 | list_attendance, response and indexes | The response exposed an id built from \_id, returned datetimes instead of epoch milliseconds, did not default half_day and history for seeded records, and no index backed the filters or the sort, so the plan is a collection scan. | GET /attendance returned id and ISO time strings. Explain on the filter shows COLLSCAN. | Responses go through attendance_out, and two compound indexes are created at startup: (date desc, emp_code) and (status, date desc, emp_code). |

| 24 | lifespan, index creation | Two of the index creation lines were written after yield, so they would only run at shutdown and the indexes would never exist at startup. | Compass showed only the \_id and (emp_code, date) indexes after the app started. | Moved every create_index call before yield, so all indexes are created at startup and re-running is harmless. |
