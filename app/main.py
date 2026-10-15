"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=2000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
ATT_SORT = [("date", -1), ("emp_code", 1)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Indexes are created at startup; create_index is idempotent.
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ms(dt):
    return round(dt.timestamp() * 1000) if dt else None


def instant(ms):
    """Epoch milliseconds -> UTC-aware datetime truncated to whole seconds (R1)."""
    if ms is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def half_up(value, places="0.01"):
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def history_out(h):
    changes = {}
    for field, ch in h.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            ch = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
        changes[field] = ch
    return {**h, "at": to_ms(h["at"]), "changes": changes}


def attendance_out(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = to_ms(out.get("punch_in"))
    out["punch_out"] = to_ms(out.get("punch_out"))
    out["half_day"] = out.get("half_day", False)
    out["history"] = [history_out(h) for h in out.get("history", [])]
    return out


def shift_info(ts, shift_start, shift_end):
    """ts is a UTC-aware datetime. Returns (IST date string, shift start in IST, shift end in IST)."""
    local = ts.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    day = local.date()
    if overnight and (local.hour, local.minute, local.second) < (eh, em, 0):
        day = day - timedelta(days=1)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end_day = day + timedelta(days=1) if overnight else day
    end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return day.isoformat(), start, end


def late_minutes_for(ts, start):
    secs = int((ts - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in, punch_out):
    secs = int((punch_out - punch_in).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end_dt):
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= 30 else 0


def working_days_in(month, joined_on):
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    d = max(first, date.fromisoformat(joined_on))
    n = 0
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def check_dates(date_from, date_to):
    for v in (date_from, date_to):
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise HTTPException(422, "invalid date")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


def attendance_filter(emp_code, date_from, date_to, status):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def monthly_pipeline(emp_code, month):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$gte": ["$_dow", 2]},
                    {"$lte": ["$_dow", 6]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_real_date(cls, v):
        date.fromisoformat(v)  # raises ValueError for 2026-13-45 -> 422
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(timezone.utc)
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for e in items:
        e["created_at"] = to_ms(e.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = instant(body.punched_at)
    d, shift_start_dt, _ = shift_info(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_for(ts, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = instant(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    _, _, shift_end_dt = shift_info(rec["punch_in"], emp["shift_start"], emp["shift_end"])
    hours = compute_work_hours(rec["punch_in"], ts)
    changes = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(ts, shift_end_dt),
        "half_day": hours < 4.50,
    }
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None}, {"$set": changes}
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(changes)
    return attendance_out(rec)


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    if new_status in PRESENCE:
        new_in = instant(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = instant(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_in = new_out = None

    new = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if new_in is not None:
        att_date, start_dt, end_dt = shift_info(new_in, emp["shift_start"], emp["shift_end"])
        if body.punch_in is not None and att_date != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        new["late_minutes"] = late_minutes_for(new_in, start_dt)
        if new_out is not None:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            hours = compute_work_hours(new_in, new_out)
            new["work_hours"] = hours
            new["overtime_minutes"] = compute_overtime(new_out, end_dt)
            new["half_day"] = hours < 4.50

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "nothing to change")

    entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    hist = rec.get("history", [])
    # Optimistic guard: only write if nobody appended to history (or punched out) since we read.
    guard = {
        "_id": rec["_id"],
        "punch_out": rec.get("punch_out"),
        "history": {"$size": len(hist)} if "history" in rec else {"$exists": False},
    }
    res = db.attendance_logs.update_one(guard, {"$set": new, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")
    rec.update(new)
    rec["history"] = hist + [entry]
    return attendance_out(rec)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_dates(date_from, date_to)
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = (
        db.attendance_logs.find(q)
        .sort(ATT_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {}
    present = float(s.get("present_days", 0))
    wd = working_days_in(month, emp["joined_on"])
    pct = half_up(Decimal(str(present)) * 100 / Decimal(wd)) if wd else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        check_dates(date_from, date_to)
        inner = {
            "find": "attendance_logs",
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        inner = {
            "aggregate": "attendance_logs",
            "pipeline": monthly_pipeline(emp_code, month),
            "cursor": {},
        }
    else:
        raise HTTPException(501, f"{endpoint} is not implemented in this submission")
    raw = db.command({"explain": inner, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": json.loads(json_util.dumps(raw)),
    }


# --------------------------------------------------------------------------- #
# Not implemented in this submission (see README.md):
#   GET /analytics/departments/summary
#   GET /analytics/leaderboard/late
#   GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #