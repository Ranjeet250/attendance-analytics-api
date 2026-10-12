# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| #   | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
| --- | ----------------------- | ------------- | --------------------------------------------- | ---------------- |

| 1 | 'list_employees', the 'total' line|'count_documents({})' ignores the department filter | 'GET /employees? daparment=Sales' returns total 6, but only 2 employess are in sales |Passed the filter 'q' to 'count_documents' |

| 2 | list*employees, skip line | skip = page * page*size treats page as 0-based, so page 1 skips the first page_size records | GET /employees?page=1&page_size=20 returns empty items while total is 6 | Changed to skip = (page - 1) * page_size |

| 3 | list_employees, find call | No sort, so the order is not guaranteed and pages can repeat or miss employees | GET /employees?page_size=2 pages are not guaranteed to be in emp_code order as the contract requires | Added .sort("emp_code", 1) before skip and limit |

| 4 | list_employees, page and page_size parameters | No bounds on page or page_size, so page=0 gives a negative skip and a 500, and page_size=0 or 1000 is accepted although R10 caps it at 100 | GET /employees?page=0 returns a server error, GET /employees?page_size=1000 is accepted | Used Query(1, ge=1) for page and Query(20, ge=1, le=100) for page_size so bad values return 422 |

| 5 | EmployeeIn model | No format checks on email, shift_start, shift_end, joined_on, so garbage is saved | POST /employees with email abc, shift_start 25:99, joined_on hello returns 201 and is stored | Added Field(pattern=...) for each so invalid values return 422 |

| 6 | create_employee, created_at | datetime.now() is naive local time (IST), PyMongo stores it as if it were UTC, so created_at is 5h30m ahead of real UTC | created_at showed 2026-10-12T00:18 UTC while the response Date header said 18:52 GMT on 11 Oct | Changed to datetime.now(timezone.utc) |

| 7 | health | Always returns ok without touching MongoDB, so it is 200 even when the database is down | Stop MongoDB and call GET /health, it still returns 200 instead of 503 | Ping the database with client.admin.command ping and return 503 on failure |

| 8 | create_employee, duplicate check | find_one then insert_one is check-then-act, so two concurrent requests with the same emp_code can both succeed | Send two parallel POST /employees with the same emp_code, two documents can be stored | Unique index on emp_code created at startup, insert directly and map DuplicateKeyError to 409 |

| 9 | create_employee, response | created_at is returned as an ISO string, but the contract says every instant is integer epoch milliseconds | POST /employees returns created_at as "2026-10-12T00:18:18.379000" instead of a 13-digit integer | Keep the datetime in MongoDB, convert to int(now.timestamp() \* 1000) in the response |

| 10 | punch_in, the employee lookup | The result of find_one is never checked. For an unknown emp_code it is None and the code crashes later on emp["Shift_start"]. The contract wants 404. | POST /attendance/punch-in with emp_code EMP9999 returns a 500 error (TypeError). | Return HTTPException 404 right after the lookup when the employee is not found. |

| 11 | PunchInIn model, the status field | status is a free string, so any value is accepted and stored. Punch-in may only create PRESENT, WFH or ON_DUTY. ABSENT and LEAVE come from seeded data or corrections. | POST /attendance/punch-in with status BANANA returned 201 and saved BANANA. | Changed the type to Literal["PRESENT", "WFH", "ON_DUTY"], so anything else returns 422. |

| 12 | PunchInIn model, the punched_at field | A plain int accepts numeric strings and floats, and there is no range check. A value in seconds is read as milliseconds and lands in January 1970. | POST /attendance/punch-in with punched_at 1791747715 (seconds) returned 201 with a date in 1970. | Used StrictInt with ge=100_000_000_000, so floats, strings and seconds-sized values return 422. |

| 13 | punch_in, the duplicate check | find_one followed by insert_one is check-then-act. Two simultaneous punch-ins for the same employee and date can both pass the check and both be inserted, giving two records for one day. | Send two parallel identical POST /attendance/punch-in requests. Both can return 201 and two documents for the same (emp_code, date) are stored. | Created a unique index on (emp_code, date) at startup, removed the find_one check and insert directly. DuplicateKeyError is mapped to 409, so the database decides who wins and exactly one request gets 201.
