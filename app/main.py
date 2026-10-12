"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from calendar import monthrange

from fastapi.encoders import jsonable_encoder

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator,model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)],
        name="ix_attendance_status_date",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    local_time = punch_in.astimezone(IST)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    start_date = local_time.date()

    if end_time <= start_time and local_time.time() < end_time:
        start_date -= timedelta(days=1)

    shift_start_at = datetime.combine(start_date, start_time, tzinfo=IST)
    minutes = int((local_time - shift_start_at).total_seconds() // 60)

    return minutes if (local_time - shift_start_at).total_seconds() > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str,
) -> int:
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    shift_date = date.fromisoformat(date_str)

    if end_time <= start_time:
        shift_date += timedelta(days=1)

    shift_end_at = datetime.combine(shift_date, end_time, tzinfo=IST)
    minutes = int((punch_out.astimezone(IST) - shift_end_at).total_seconds() // 60)

    return minutes if minutes >= 30 else 0


def compute_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> date:

    local_time = punch_in.astimezone(IST)

    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    attendance_date = local_time.date()

    if end_time <= start_time and local_time.time() < end_time:
        attendance_date -= timedelta(days=1)

    return attendance_date


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        max_length=120,
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
    )


class RegularizeIn(BaseModel):
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None

    punch_in: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000,
    )
    punch_out: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000,
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_correction(self):
        supplied = self.model_fields_set

        if "status" in supplied and self.status is None:
            raise ValueError("status cannot be null")

        for field in ("punch_in", "punch_out"):
            if field in supplied and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")

        if self.status in ("ABSENT", "LEAVE"):
            if "punch_in" in supplied or "punch_out" in supplied:
                raise ValueError(
                    "ABSENT or LEAVE cannot include punch times"
                )

        return self


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    created_at = datetime.now(timezone.utc)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)

    doc["created_at"] = int(created_at.timestamp() * 1000)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for employee in items:
        if isinstance(employee.get("created_at"), datetime):
            employee["created_at"] = int(
                employee["created_at"].timestamp() * 1000
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)

    attendance_date = compute_attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    ).isoformat()

    late_minutes = compute_late_minutes(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "already punched in for this date",
        )

    doc.pop("_id", None)
    doc["id"] = str(result.inserted_id)
    doc["punch_in"] = int(ts.timestamp() * 1000)

    return doc


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )

    ts = ts.replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(404, "no eligible punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = record["punch_in"].replace(microsecond=0)

    elapsed_seconds = (ts - punch_in_at).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_at, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_in": punch_in_at,
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")

    record["punch_in"] = punch_in_at
    record["punch_out"] = ts
    record["work_hours"] = work_hours
    record["overtime_minutes"] = overtime_minutes
    record["half_day"] = half_day

    record.pop("_id", None)
    record["punch_in"] = int(punch_in_at.timestamp() * 1000)
    record["punch_out"] = int(ts.timestamp() * 1000)

    return record

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: date = Path(..., alias="date"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    date_str = attendance_date.isoformat()
    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date_str}
    )
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    status = body.status if "status" in supplied else record["status"]

    punch_in_at = record.get("punch_in")
    punch_out_at = record.get("punch_out")

    if "punch_in" in supplied:
        punch_in_at = datetime.fromtimestamp(
            body.punch_in / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    if "punch_out" in supplied:
        punch_out_at = datetime.fromtimestamp(
            body.punch_out / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    if status in ("ABSENT", "LEAVE"):
        punch_in_at = None
        punch_out_at = None

    if status in ("PRESENT", "WFH", "ON_DUTY") and punch_in_at is None:
        raise HTTPException(422, "presence status requires punch_in")

    if punch_in_at is not None:
        expected_date = compute_attendance_date(
            punch_in_at, emp["shift_start"], emp["shift_end"]
        )
        if expected_date.isoformat() != date_str:
            raise HTTPException(
                422, "punch_in must belong to the attendance date"
            )

    if punch_out_at is not None:
        if punch_in_at is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (punch_out_at - punch_in_at).total_seconds()
        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )

    late_minutes = (
        compute_late_minutes(
            punch_in_at, emp["shift_start"], emp["shift_end"]
        )
        if punch_in_at is not None else 0
    )

    work_hours = (
        compute_work_hours(punch_in_at, punch_out_at)
        if punch_in_at is not None and punch_out_at is not None
        else None
    )

    overtime_minutes = (
        compute_overtime(
            punch_out_at,
            emp["shift_end"],
            date_str,
            emp["shift_start"],
        )
        if punch_in_at is not None and punch_out_at is not None
        else 0
    )

    half_day = work_hours is not None and work_hours < 4.50

    new_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field)

        if old_value != new_value:
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": (
                        int(old_value.timestamp() * 1000)
                        if old_value is not None else None
                    ),
                    "to": (
                        int(new_value.timestamp() * 1000)
                        if new_value is not None else None
                    ),
                }
            else:
                changes[field] = {
                    "from": old_value,
                    "to": new_value,
                }

    if not changes:
        raise HTTPException(422, "request makes no changes")

    history_changes = {}
    for field, change in changes.items():
        if field in ("punch_in", "punch_out"):
            history_changes[field] = {
                "from": (
                    datetime.fromtimestamp(
                        change["from"] / 1000, tz=timezone.utc
                    ) if change["from"] is not None else None
                ),
                "to": (
                    datetime.fromtimestamp(
                        change["to"] / 1000, tz=timezone.utc
                    ) if change["to"] is not None else None
                ),
            }
        else:
            history_changes[field] = change

    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    old_history = record.get("history", [])
    history_filter = (
        {"history": old_history}
        if "history" in record
        else {"history": {"$exists": False}}
    )

    original_values = {
        field: record.get(field)
        for field in new_values
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            **history_filter,
            **original_values,
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409, "attendance record changed; retry"
        )

    response = {
        "emp_code": emp_code,
        "date": date_str,
        **new_values,
        "history": old_history + [history_entry],
    }

    response["punch_in"] = (
        int(punch_in_at.timestamp() * 1000)
        if punch_in_at is not None else None
    )
    response["punch_out"] = (
        int(punch_out_at.timestamp() * 1000)
        if punch_out_at is not None else None
    )

    for entry in response["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = int(entry["at"].timestamp() * 1000)

        for field in ("punch_in", "punch_out"):
            change = entry.get("changes", {}).get(field)
            if change:
                for side in ("from", "to"):
                    value = change[side]
                    if isinstance(value, datetime):
                        change[side] = int(value.timestamp() * 1000)

    return response


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):

    for field_name, value in (
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if value is not None:
            try:
                parsed_date = date.fromisoformat(value)
                if parsed_date.isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must be a valid YYYY-MM-DD date",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )


    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        date_filter = {}

        if date_from:
            date_filter["$gte"] = date_from

        if date_to:
            date_filter["$lte"] = date_to

        query["date"] = date_filter

    total = db.attendance_logs.count_documents(query)

    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    for doc in docs:
        doc.setdefault("punch_in", None)
        doc.setdefault("punch_out", None)
        doc.setdefault("work_hours", None)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        doc.setdefault("half_day", False)
        doc.setdefault("history", [])

        for field in ("punch_in", "punch_out"):
            value = doc.get(field)

            if isinstance(value, datetime):
                timestamp = int(value.timestamp() * 1000)
                doc[field] = (timestamp // 1000) * 1000
                
        for entry in doc.get("history", []):
            value = entry.get("at")
            if isinstance(value, datetime):
                timestamp = int(value.timestamp() * 1000)
                entry["at"] = (timestamp // 1000) * 1000

            changes = entry.get("changes", {})
            for field in ("punch_in", "punch_out"):
                change = changes.get(field)
                if not isinstance(change, dict):
                    continue

                for key in ("from", "to"):
                    value = change.get(key)
                    if isinstance(value, datetime):
                        timestamp = int(value.timestamp() * 1000)
                        change[key] = (timestamp // 1000) * 1000
                    elif isinstance(value, (int, float)):
                        change[key] = (int(value) // 1000) * 1000


    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


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


# ---------------------------------------------------------------------------
# Analytics and explain endpoints
# Add this section after list_attendance() in app/main.py.
# ---------------------------------------------------------------------------

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def _month_bounds(month: str) -> tuple[str, str, int, int]:
    """Return the inclusive ISO date bounds and year/month for YYYY-MM."""
    try:
        year_text, month_text = month.split("-", 1)
        year, month_number = int(year_text), int(month_text)
        if len(year_text) != 4 or len(month_text) != 2 or not 1 <= month_number <= 12:
            raise ValueError
        last_day = monthrange(year, month_number)[1]
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be a valid YYYY-MM month")

    return (
        f"{year:04d}-{month_number:02d}-01",
        f"{year:04d}-{month_number:02d}-{last_day:02d}",
        year,
        month_number,
    )


def _round_half_up(value, places: int = 2):
    """Round a reported number using the assignment's half-up rule."""
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _working_days(start_date: str, end_date: str) -> int:
    """Count weekdays in a calendar range; this loops over dates, not DB rows."""
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    count = 0
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str):
    """MongoDB aggregation for attendance metrics in one employee-month."""
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }},
        {"$set": {
            "_day_of_week": {
                "$dayOfWeek": {
                    "$dateFromString": {"dateString": "$date"}
                }
            },
            "_is_presence": {"$in": ["$status", PRESENCE_STATUSES]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$_is_presence", {"$in": ["$_day_of_week", [2, 3, 4, 5, 6]]}]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Summarize one employee's attendance for a calendar month."""
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )
    if employee is None:
        raise HTTPException(404, "employee not found")

    month_start, month_end, _, _ = _month_bounds(month)
    joined_on = max(employee["joined_on"], month_start)
    working_days = (
        _working_days(joined_on, month_end) if joined_on <= month_end else 0
    )

    result = next(
        iter(db.attendance_logs.aggregate(
            _employee_monthly_pipeline(emp_code, month_start, month_end)
        )),
        {},
    )
    present_days = _round_half_up(result.get("present_days", 0), 2)
    attendance_pct = (
        _round_half_up((present_days / working_days) * 100, 4)
        if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(result.get("leave_days", 0)),
        "late_count": int(result.get("late_count", 0)),
        "total_late_minutes": int(result.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(result.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


def _department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]):
    """Aggregate employee headcount and their monthly attendance by department."""
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code", "joined_on": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                    {"$gte": ["$date", "$$joined_on"]},
                ]}}},
                {"$set": {
                    "_day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "_is_presence": {"$in": ["$status", PRESENCE_STATUSES]},
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": ["$_is_presence", {"$in": ["$_day_of_week", [2, 3, 4, 5, 6]]}]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0,
                    ]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": ["$_is_presence", {"$isNumber": "$work_hours"}]},
                        "$work_hours", 0,
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": ["$_is_presence", {"$isNumber": "$work_hours"}]}, 1, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "monthly_metrics",
        }},
        {"$unwind": {"path": "$monthly_metrics", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$monthly_metrics.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$monthly_metrics.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$monthly_metrics.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$monthly_metrics.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$monthly_metrics.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$monthly_metrics.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$monthly_metrics.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Summarize headcount and attendance metrics for each department."""
    month_start, month_end, _, _ = _month_bounds(month)
    rows = list(db.employees.aggregate(
        _department_summary_pipeline(month_start, month_end, department)
    ))

    items = []
    for row in rows:
        avg_work_hours = row.get("avg_work_hours")
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": _round_half_up(row.get("present_days", 0), 2),
            "avg_work_hours": _round_half_up(avg_work_hours, 2),
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(month_start: str, month_end: str, department: Optional[str], limit: int):
    """Rank employees by total late minutes; ties use competition ranking."""
    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Rank late employees, keeping ties at the requested rank cutoff."""
    month_start, month_end, _, _ = _month_bounds(month)
    items = list(db.attendance_logs.aggregate(
        _late_leaderboard_pipeline(month_start, month_end, department, limit)
    ))
    return {"month": month, "items": items}


def _department_trend_pipeline(department: str, start_date: date, end_date: date):
    """Create one MongoDB row per day, then calculate daily and rolling metrics."""
    start_datetime = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    day_count = (end_date - start_date).days + 1
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {
            "_id": 0,
            "dates": {"$map": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "in": {"$dateAdd": {
                    "startDate": start_datetime,
                    "unit": "day",
                    "amount": "$$offset",
                }},
            }},
        }},
        {"$unwind": "$dates"},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}},
            "_day_of_week": {"$dayOfWeek": "$dates"},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$project": {"_id": 1}},
            ],
            "as": "active_employees",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$project": {
                    "_id": 0,
                    "status": 1,
                    "half_day": 1,
                    "late_minutes": 1,
                }},
            ],
            "as": "daily_logs",
        }},
        {"$set": {
            "is_working_day": {"$in": ["$_day_of_week", [2, 3, 4, 5, 6]]},
            "headcount": {"$size": "$active_employees"},
            "present_count": {"$reduce": {
                "input": "$daily_logs",
                "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", PRESENCE_STATUSES]},
                    {"$cond": ["$$this.half_day", 0.5, 1]},
                    0,
                ]}]},
            }},
            "late_count": {"$reduce": {
                "input": "$daily_logs",
                "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$gt": ["$$this.late_minutes", 0]}, 1, 0,
                ]}]},
            }},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    """Return daily department attendance for every date in the requested range."""
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")

    rows = list(db.employees.aggregate(
        _department_trend_pipeline(department, from_date, to_date)
    ))
    for row in rows:
        row["present_count"] = _round_half_up(row.get("present_count", 0), 2)
        row["attendance_rate"] = _round_half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = _round_half_up(row.get("moving_avg_7d"), 4)
        row["headcount"] = int(row.get("headcount", 0))
        row["late_count"] = int(row.get("late_count", 0))

    return {"department": department, "items": rows}


@app.on_event("startup")
def create_analytics_indexes():
    """Indexes used by department filters and date-bounded aggregation lookups."""
    db.employees.create_index(
        [("joined_on", 1)], name="ix_employee_joined_on"
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined_on",
    )


def _validate_explain_month(month: Optional[str]) -> tuple[str, str]:
    if month is None:
        raise HTTPException(422, "month is required for this endpoint")
    month_start, month_end, _, _ = _month_bounds(month)
    return month_start, month_end



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    """Explain the same filtered operation used by the selected endpoint."""
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                query["date"]["$lte"] = date_to.isoformat()
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain = db.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain),

        }

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        month_start, month_end = _validate_explain_month(month)
        employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
        if employee is None:
            raise HTTPException(404, "employee not found")
        attendance_start = max(month_start, employee["joined_on"])
        pipeline = _employee_monthly_pipeline(emp_code, attendance_start, month_end)
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        month_start, month_end = _validate_explain_month(month)
        pipeline = _department_summary_pipeline(month_start, month_end, department)
        collection = "employees"

    elif endpoint == "late_leaderboard":
        month_start, month_end = _validate_explain_month(month)
        pipeline = _late_leaderboard_pipeline(month_start, month_end, department, limit)
        collection = "attendance_logs"

    else:  # department_trend
        if not department or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(422, "to must be on or after from")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")
        pipeline = _department_trend_pipeline(department, from_date, to_date)
        collection = "employees"

    explain = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": jsonable_encoder(explain),
    }
