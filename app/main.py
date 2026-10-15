
import os
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

@asynccontextmanager
async def lifespan(app: FastAPI):
    
    try:
      
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
       
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    except Exception as exc:
        print(f"Index creation warning: {exc}")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

def truncate_to_seconds(dt: datetime) -> datetime:
    
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:

    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:

    dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    return truncate_to_seconds(dt)


def round_half_up_2(val: Union[float, Decimal, int, str]) -> float:
  
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Union[float, Decimal, int, str]) -> float:
    
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
   
    return shift_end <= shift_start


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    
    punch_in_ist = punch_in.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        h_end, m_end = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (h_end, m_end, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
   
    punch_in_ist = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    shift_date = date.fromisoformat(date_str)
    start_dt_ist = datetime.combine(shift_date, time(h, m, 0), tzinfo=IST)
    delta_seconds = int((punch_in_ist - start_dt_ist).total_seconds())
    if delta_seconds > 600:
        return delta_seconds // 60
    return 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
   
    punch_out_ist = punch_out.astimezone(IST)
    h, m = map(int, shift_end.split(":"))
    shift_date = date.fromisoformat(date_str)
    if is_overnight_shift(shift_start, shift_end):
        shift_date += timedelta(days=1)
    end_dt_ist = datetime.combine(shift_date, time(h, m, 0), tzinfo=IST)
    delta_seconds = int((punch_out_ist - end_dt_ist).total_seconds())
    minutes = delta_seconds // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
   
    delta_seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(delta_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_half_day(work_hours: Optional[float]) -> bool:
  
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def get_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)
    
    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > last_day:
        return 0
    start_date = max(first_day, joined_on)
    
    working_days = 0
    curr = start_date
    while curr <= last_day:
        if curr.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days

def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    serialized_changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_ms(to_val)
        serialized_changes[k] = {"from": from_val, "to": to_val}
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": serialized_changes,
    }


def serialize_attendance(doc: dict) -> dict:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

@app.get("/health")
def health():

    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):

    emp_dict = body.model_dump()
    emp_dict["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(emp_dict)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return serialize_employee(emp_dict)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_ms(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(UTC))

    date_str = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_ms(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(UTC))

    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in record found to punch out")


    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    delta_seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    if delta_seconds <= 0 or delta_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="Punch-out time must be after punch-in and within 24 hours",
        )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"])
    half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": half,
            }
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    new_status = body.status if body.status is not None else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail=f"Punch times cannot be supplied when status is {new_status}",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
    
        if body.punch_in is not None:
            new_punch_in = from_epoch_ms(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail=f"Presence status {new_status} requires a punch_in",
            )

        computed_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date_param:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date_param}",
            )

        if body.punch_out is not None:
            new_punch_out = from_epoch_ms(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            if new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=UTC)
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)
            delta = int((new_punch_out - new_punch_in).total_seconds())
            if delta <= 0 or delta > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_start"], emp["shift_end"], date_param
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date_param)

    changes_mongo: Dict[str, Dict[str, Any]] = {}
    if new_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes_mongo["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes_mongo["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes_mongo["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(status_code=422, detail="No fields were changed")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    current_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification detected on this record. Please retry.",
        )

    return serialize_attendance(updated)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = get_working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},  # Mon=2 in Mongo
                                    {"$lte": ["$day_of_week", 6]},  # Fri=6
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = round_half_up_2(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up_2((present_days / working_days) * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, m_num = map(int, month.split("-"))
    last_day_of_month = f"{month}-{calendar.monthrange(year, m_num)[1]:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": last_day_of_month}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_codes"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$gte": ["$$l.day_of_week", 2]},
                                            {"$lte": ["$$l.day_of_week", 6]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "eligible_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$eligible_work_hours"}, 0]},
                        {"$avg": "$eligible_work_hours.work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        if r["headcount"] == 0:
            continue
        avg_wh = r["avg_work_hours"]
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up_2(r["present_days"]),
            "avg_work_hours": round_half_up_2(avg_wh) if avg_wh is not None else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$regex": f"^{month}"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING,
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    # Check department exists
    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(d_from, time(0, 0, 0), tzinfo=UTC)
    end_dt_exclusive = datetime.combine(d_to + timedelta(days=1), time(0, 0, 0), tzinfo=UTC)

    pipeline = [
        # Start with a single document to generate calendar dates via $densify
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$literal": start_dt},
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt_exclusive],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]},
                    ]
                }
            }
        },

        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"emp_code": 1}},
                ],
                "as": "dept_employees",
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$dept_employees"},
                "dept_emp_codes": "$dept_employees.emp_code",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"current_date": "$date", "codes": "$dept_emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$current_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},

        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for row in raw_items:
        att_rate = row["attendance_rate"]
        mov_avg = row["moving_avg_7d"]
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": round_half_up_2(row["present_count"]),
            "late_count": row["late_count"],
            "attendance_rate": round_half_up_4(att_rate) if att_rate is not None else None,
            "moving_avg_7d": round_half_up_4(mov_avg) if mov_avg is not None else None,
        })

    return {"department": department, "items": items}



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
 
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        q: Dict[str, Any] = {}
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
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        explain_res = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": ["$day_of_week", 2]},
                                        {"$lte": ["$day_of_week", 6]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m_num = map(int, month.split("-"))
        last_day_of_month = f"{month}-{calendar.monthrange(year, m_num)[1]:02d}"
        match_emp = {"joined_on": {"$lte": last_day_of_month}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"dept_codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$dept_codes"]},
                                        {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = [
            {
                "$match": {
                    "date": {"$regex": f"^{month}"},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
            {"$match": {"rank": {"$lte": limit}}},
        ])
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
        start_dt = datetime.combine(d_from, time(0, 0, 0), tzinfo=UTC)
        end_dt_exclusive = datetime.combine(d_to + timedelta(days=1), time(0, 0, 0), tzinfo=UTC)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "date_dt": {"$literal": start_dt}}},
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_dt_exclusive],
                    },
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    raise HTTPException(status_code=422, detail=f"Unsupported explain endpoint: {endpoint}")
