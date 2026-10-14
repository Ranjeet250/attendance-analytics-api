"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional , Literal , Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query
from pydantic import BaseModel , Field , StrictInt , ConfigDict
from pymongo import MongoClient
from contextlib import asynccontextmanager
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000, tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    yield
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers

# --------------------------------------------------------------------------- #

def to_ms(dt):
    return round(dt.timestamp() * 1000) if dt else None


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



def instant(ms):
    if ms is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def compute_work_hours(punch_in, punch_out):
    secs = int((punch_out - punch_in).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end_dt):
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def half_up(value, places="0.01"):
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


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


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    pipeline = [
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
    rows = list(db.attendance_logs.aggregate(pipeline))
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
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception :
        raise HTTPException(503, "database not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(timezone.utc)
    doc["created_at"] = datetime.now(timezone.utc)
    try:
       db.employees.insert_one(doc)
    except DuplicateKeyError:
       raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = int(now.timestamp()*1000)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1,ge=1), page_size: int = Query(20,ge=1,le=100),):
    q = {}
    if department:
        q["department"] = department
    skip = (page-1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code",1).skip(skip).limit(page_size))
    for e in items:
        e["created_at"] = to_ms(e.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = (
    datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc)
    if body.punched_at
    else datetime.now(timezone.utc).replace(microsecond=0)
)
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
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
