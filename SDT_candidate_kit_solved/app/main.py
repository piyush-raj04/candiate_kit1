"""Employee Attendance & Analytics API."""
import os, re, calendar
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util
from fastapi.responses import JSONResponse

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def utcnow():
    return datetime.now(UTC).replace(microsecond=0)
def dt_from_ms(ms):
    if ms is None: return None
    if type(ms) is not int or ms < 100_000_000_000 or ms > 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)
def ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
def as_utc(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
def round_half_up(n, places=2):
    if n is None: return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(n)).quantize(quantum, rounding=ROUND_HALF_UP))
def parse_date(s):
    try: return date.fromisoformat(s)
    except Exception: raise HTTPException(422, "invalid date; expected YYYY-MM-DD")
def parse_month(s):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y,mn = map(int,s.split("-"))
    return date(y,mn,1), date(y,mn,calendar.monthrange(y,mn)[1])
def epoch_field(v):
    try: return dt_from_ms(v)
    except (ValueError, TypeError, OverflowError): raise HTTPException(422, "timestamps must be integer epoch milliseconds")
def to_api(doc):
    if not doc: return doc
    d = dict(doc); d.pop("_id",None)
    for k in ("created_at","punch_in","punch_out"):
        if k in d: d[k] = ms(d[k])
    hist=[]
    for h in d.get("history",[]) or []:
        hh=dict(h); hh["at"]=ms(hh.get("at"))
        changes={}
        for k,v in (hh.get("changes") or {}).items():
            changes[k]={"from": ms(v.get("from")) if k in ("punch_in","punch_out") else v.get("from"),
                        "to": ms(v.get("to")) if k in ("punch_in","punch_out") else v.get("to")}
        hh["changes"]=changes; hist.append(hh)
    d["history"]=hist
    if "half_day" not in d: d["half_day"]=False
    if "late_minutes" not in d: d["late_minutes"]=0
    if "overtime_minutes" not in d: d["overtime_minutes"]=0
    if "history" not in d: d["history"]=[]
    return d
def compute_late_minutes(punch_in, shift_start, shift_end=None):
    local=as_utc(punch_in).astimezone(IST)
    h,m=map(int,shift_start.split(":"))
    start=local.replace(hour=h,minute=m,second=0,microsecond=0)
    if shift_end is not None and shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        start -= timedelta(days=1)
    # If shift is overnight and the local punch is before shift_end, shift started yesterday.
    return max(0, int((local-start).total_seconds()//60)) if (local-start).total_seconds()>600 else 0
def attendance_date(punch_in, shift_start, shift_end):
    local=as_utc(punch_in).astimezone(IST)
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        return (local.date()-timedelta(days=1)).isoformat()
    return local.date().isoformat()
def shift_end_datetime(punch_in, shift_start, shift_end, attendance_day):
    day=parse_date(attendance_day)
    h,m=map(int,shift_end.split(":"))
    end=datetime(day.year,day.month,day.day,h,m,tzinfo=IST)
    if shift_end <= shift_start: end += timedelta(days=1)
    return end.astimezone(UTC)
def compute_overtime(punch_out, shift_start, shift_end, attendance_day):
    end=shift_end_datetime(punch_out,shift_start,shift_end,attendance_day)
    minutes=int((as_utc(punch_out)-end).total_seconds()//60)
    return minutes if minutes >= 30 else 0
def compute_derived(emp, pi, po, attendance_day):
    late=compute_late_minutes(pi,emp["shift_start"],emp["shift_end"]) if pi else 0
    if pi and po:
        hours=round_half_up((as_utc(po)-as_utc(pi)).total_seconds()/3600,2)
        overtime=compute_overtime(po,emp["shift_start"],emp["shift_end"],attendance_day)
        half=hours < 4.50
    else: hours=None; overtime=0; half=False
    return late,hours,overtime,half
def validate_ms(v):
    if v is not None and (type(v) is not int or v < 100_000_000_000 or v > 4_102_444_800_000):
        raise ValueError("must be epoch milliseconds")
    return v

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str=Field(pattern=r"^EMP\d{4,6}$")
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:str=Field(min_length=1,max_length=50)
    shift_start:str=Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:str=Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls,v): date.fromisoformat(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start==self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="ignore",strict=True)
    emp_code:str
    punched_at:Optional[int]=None
    status:Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v): return validate_ms(v)
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="ignore",strict=True)
    emp_code:str
    punched_at:Optional[int]=None
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v): return validate_ms(v)
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra="ignore",strict=True)
    status:Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in:Optional[int]=None
    punch_out:Optional[int]=None
    reason:str=Field(min_length=5,max_length=200)
    regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def valid_ts(cls,v): return validate_ms(v)

@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code",ASCENDING)],unique=True,name="uq_employee_code")
    db.employees.create_index([("department",ASCENDING),("joined_on",ASCENDING)],name="department_joined")
    db.attendance_logs.create_index([("emp_code",ASCENDING),("date",ASCENDING)],unique=True,name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date",DESCENDING),("emp_code",ASCENDING)],name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code",ASCENDING),("date",ASCENDING),("status",ASCENDING)],name="attendance_emp_date_status")
    db.attendance_logs.create_index([("status",ASCENDING),("date",ASCENDING)],name="attendance_status_date")
    db.attendance_logs.create_index([("date",ASCENDING),("late_minutes",DESCENDING),("emp_code",ASCENDING)],name="attendance_late_date")
@app.get("/health")
def health():
    try: client.admin.command("ping")
    except Exception: raise HTTPException(503,"MongoDB unavailable")
    return {"status":"ok"}
@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=utcnow()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return to_api(doc)
@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=[to_api(d) for d in db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}
@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=epoch_field(body.punched_at) if body.punched_at is not None else utcnow()
    ts=as_utc(ts).replace(microsecond=0)
    day=attendance_date(ts,emp["shift_start"],emp["shift_end"])
    late=compute_late_minutes(ts,emp["shift_start"],emp["shift_end"])
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":late,"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return to_api(doc)
@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=epoch_field(body.punched_at) if body.punched_at is not None else utcnow()
    ts=as_utc(ts).replace(microsecond=0)
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"record already punched out")
    elapsed=(ts-as_utc(rec["punch_in"])).total_seconds()
    if elapsed<=0 or elapsed>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    late,hours,ot,half=compute_derived(emp,rec["punch_in"],ts,rec["date"])
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},
        {"$set":{"punch_out":ts,"work_hours":hours,"overtime_minutes":ot,"half_day":half}},
        return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record already punched out")
    return to_api(updated)
@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None,
                    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    q={}
    if emp_code: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    if status:q["status"]=status
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[to_api(d) for d in docs],"total":total,"page":page,"page_size":page_size}
@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code:str,date_str:str,body:RegularizeIn):
    try: day=parse_date(date_str)
    except: raise HTTPException(422,"date must be YYYY-MM-DD")
    rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":date_str})
    if not rec or not db.employees.find_one({"emp_code":emp_code}): raise HTTPException(404,"employee or attendance record not found")
    emp=db.employees.find_one({"emp_code":emp_code})
    old={k:rec.get(k) for k in ["status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day"]}
    status=body.status if body.status is not None else rec["status"]
    pi=epoch_field(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    po=epoch_field(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if status in {"ABSENT","LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422,"ABSENT/LEAVE cannot include punch times")
        pi=po=None
    elif pi is None: raise HTTPException(422,"presence status requires punch_in")
    if pi is not None and attendance_date(pi,emp["shift_start"],emp["shift_end"])!=date_str: raise HTTPException(422,"punch_in must belong to attendance date")
    if po is not None:
        if pi is None or (as_utc(po)-as_utc(pi)).total_seconds()<=0 or (as_utc(po)-as_utc(pi)).total_seconds()>86400:
            raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")
    late,hours,ot,half=compute_derived(emp,pi,po,date_str) if status in PRESENCE else (0,None,0,False)
    new={"status":status,"punch_in":pi,"punch_out":po,"work_hours":hours,"late_minutes":late,"overtime_minutes":ot,"half_day":half}
    changes={}
    for k,v in new.items():
        ov=old[k]
        if k in ("punch_in","punch_out"): ov=as_utc(ov) if ov else None; v=as_utc(v) if v else None
        if ov!=v:
            changes[k]={"from":old[k],"to":v}
    if not changes: raise HTTPException(422,"request changes nothing")
    hist_entry={"at":utcnow(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Atomic compare-and-swap protects concurrent corrections and their audit histories.
    selector={"_id":rec["_id"]}
    for k in old:
        selector[k]=rec.get(k)
    updated=db.attendance_logs.find_one_and_update(selector,{"$set":new,"$push":{"history":hist_entry}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record changed concurrently; retry")
    return to_api(updated)
def month_records(start,end,emp_code=None):
    q={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
    if emp_code:q["emp_code"]=emp_code
    return list(db.attendance_logs.find(q,{"_id":0}))
def weekdays_between(start,end):
    return sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday()<5)
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    start,end=parse_month(month)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    effective=max(start,parse_date(emp["joined_on"]))
    working=weekdays_between(effective,end) if effective<=end else 0
    docs=month_records(start,end,emp_code)
    present=sum((0.5 if d.get("half_day",False) else 1.0) for d in docs if d.get("status") in PRESENCE and parse_date(d["date"]).weekday()<5)
    leaves=sum(1 for d in docs if d.get("status")=="LEAVE")
    late=[d for d in docs if d.get("late_minutes",0)>0]
    overtime=sum(d.get("overtime_minutes",0) or 0 for d in docs)
    pct=round_half_up(present/working*100,2) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":round_half_up(present,2),"leave_days":leaves,
            "late_count":len(late),"total_late_minutes":sum(d.get("late_minutes",0) or 0 for d in late),"total_overtime_minutes":overtime,"attendance_pct":pct}
@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    start,end=parse_month(month)
    eq={"joined_on":{"$lte":end.isoformat()}}
    if department is not None:eq["department"]=department
    employees=list(db.employees.find(eq,{"_id":0}))
    if not employees:return {"month":month,"items":[]}
    by_dept={}
    for e in employees:by_dept.setdefault(e["department"],[]).append(e)
    docs=month_records(start,end)
    codes={e["emp_code"]:e for e in employees}
    out=[]
    for dep,emps in sorted(by_dept.items()):
        active={e["emp_code"] for e in emps}
        relevant=[d for d in docs if d.get("emp_code") in active]
        present=sum((0.5 if d.get("half_day",False) else 1) for d in relevant if d.get("status") in PRESENCE and parse_date(d["date"]).weekday()<5)
        worked=[d.get("work_hours") for d in relevant if d.get("status") in PRESENCE and d.get("work_hours") is not None]
        late=[d for d in relevant if (d.get("late_minutes",0) or 0)>0]
        out.append({"department":dep,"headcount":len(emps),"present_days":round_half_up(present,2),
          "avg_work_hours":round_half_up(sum(worked)/len(worked),2) if worked else None,"late_count":len(late),
          "total_late_minutes":sum(d.get("late_minutes",0) or 0 for d in late),
          "leave_count":sum(1 for d in relevant if d.get("status")=="LEAVE"),
          "on_duty_count":sum(1 for d in relevant if d.get("status")=="ON_DUTY")})
    return {"month":month,"items":out}
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=parse_month(month)
    empq={"joined_on":{"$lte":end.isoformat()}}
    if department is not None:empq["department"]=department
    emps={e["emp_code"]:e for e in db.employees.find(empq,{"_id":0})}
    totals={}
    for d in month_records(start,end):
        code=d.get("emp_code"); late=d.get("late_minutes",0) or 0
        if code in emps and late>0:
            row=totals.setdefault(code,{"total":0,"count":0});row["total"]+=late;row["count"]+=1
    sorted_rows=sorted(((code,v) for code,v in totals.items() if v["total"]>0),key=lambda x:(-x[1]["total"],x[0]))
    out=[]; rank=0; previous=None
    for pos,(code,v) in enumerate(sorted_rows,1):
        if v["total"]!=previous:rank=pos;previous=v["total"]
        if rank>limit:break
        e=emps[code];out.append({"rank":rank,"emp_code":code,"name":e["name"],"department":e["department"],"total_late_minutes":v["total"],"late_count":v["count"]})
    return {"month":month,"items":out}
@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_date:str=Query(alias="from"),to_date:str=Query(alias="to")):
    start=parse_date(from_date);end=parse_date(to_date)
    if end<start:raise HTTPException(422,"to must be >= from")
    if (end-start).days+1>92:raise HTTPException(422,"range may not exceed 92 days")
    if not db.employees.find_one({"department":department}):raise HTTPException(404,"department not found")
    emps=list(db.employees.find({"department":department},{"_id":0,"emp_code":1,"joined_on":1}))
    codes={e["emp_code"] for e in emps}
    docs=list(db.attendance_logs.find({"emp_code":{"$in":list(codes)},"date":{"$gte":from_date,"$lte":to_date}},{"_id":0}))
    byday={}
    for d in docs:
        row=byday.setdefault(d["date"],{"present":0.0,"late":0})
        if d.get("status") in PRESENCE:row["present"]+=0.5 if d.get("half_day",False) else 1.0
        if (d.get("late_minutes",0) or 0)>0:row["late"]+=1
    rows=[]; rates=[]
    for i in range((end-start).days+1):
        day=start+timedelta(days=i);ds=day.isoformat()
        head=sum(1 for e in emps if e["joined_on"]<=ds)
        wd=day.weekday()<5; agg=byday.get(ds,{"present":0.0,"late":0})
        rate=round_half_up(agg["present"]/head,4) if wd and head>0 else None
        rates.append(rate)
        window=[x for x in rates[max(0,len(rates)-7):] if x is not None]
        moving=round_half_up(sum(window)/len(window),4) if window else None
        rows.append({"date":ds,"is_working_day":wd,"headcount":head,"present_count":round_half_up(agg["present"],2),"late_count":agg["late"],"attendance_rate":rate,"moving_avg_7d":moving})
    return {"department":department,"items":rows}
def explain_command(coll,pipeline_or_query,sort=None,skip=None,limit=None,aggregate=False):
    if aggregate:
        return coll.database.command("explain",{"aggregate":coll.name,"pipeline":pipeline_or_query,"cursor":{}},verbosity="executionStats")
    return coll.database.command("explain",{"find":coll.name,"filter":pipeline_or_query,"sort":sort or {},"skip":skip or 0,"limit":limit or 0},verbosity="executionStats")
@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),
                  date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to"),
                  page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed:raise HTTPException(422,"unknown endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        if status:q["status"]=status
        ex=explain_command(db.attendance_logs,q,{"date":-1,"emp_code":1},(page-1)*page_size,page_size)
        coll="attendance_logs"
    elif endpoint=="employee_monthly":
        if not emp_code or not month:raise HTTPException(422,"emp_code and month are required")
        start,end=parse_month(month);q={"emp_code":emp_code,"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
        ex=explain_command(db.attendance_logs,q);coll="attendance_logs"
    elif endpoint=="department_summary":
        if not month:raise HTTPException(422,"month is required")
        parse_month(month);q={}
        if department:q["department"]=department
        ex=explain_command(db.employees,q);coll="employees"
    elif endpoint=="late_leaderboard":
        if not month:raise HTTPException(422,"month is required")
        start,end=parse_month(month);q={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
        ex=explain_command(db.attendance_logs,q,{"late_minutes":-1,"emp_code":1});coll="attendance_logs"
    else:
        if not department or not from_date or not to_date:raise HTTPException(422,"department, from and to are required")
        parse_date(from_date);parse_date(to_date)
        q={"department":department}
        ex=explain_command(db.employees,q);coll="employees"
    return JSONResponse(content=json_util.loads(json_util.dumps({"endpoint":endpoint,"collection":coll,"explain":ex})))
