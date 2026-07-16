import datetime
import logging
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel

from .database import init_db, get_db
from .models import Clinic, Practitioner, Patient, Appointment, CallSession
from . import services

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clinic-voice-agent")

app = FastAPI(title="Voice AI Clinic Receptionist Backend")

# Enable CORS for local testing/webchat
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Welcome to Aarohan Clinic Voice AI Backend!",
        "documentation": "/docs"
    }

# Pydantic Schemas for inputs
class IdentifyCallerRequest(BaseModel):
    phone_number: Optional[str] = None

class GetPractitionersRequest(BaseModel):
    specialty: Optional[str] = None
    clinic_id: Optional[int] = None

class CheckAvailabilityRequest(BaseModel):
    practitioner_id: int
    start_time: str

class SearchEarliestSlotRequest(BaseModel):
    specialty: Optional[str] = None
    clinic_id: Optional[int] = None
    start_from: Optional[str] = None

class BookAppointmentRequest(BaseModel):
    first_name: str
    last_name: str
    practitioner_id: int
    clinic_id: int
    start_time: str
    idempotency_key: Optional[str] = None

class RescheduleAppointmentRequest(BaseModel):
    appointment_id: int
    new_start_time: str

class CancelAppointmentRequest(BaseModel):
    appointment_id: int


@app.on_event("startup")
def startup_event():
    logger.info("Initializing database...")
    init_db()
    logger.info("Database initialized successfully.")

# Helper to extract call metadata from Retell request
async def get_call_metadata(request: Request) -> tuple[str, str]:
    """
    Extracts call_id and phone_number from Retell's request body or headers.
    Retell payload structure for custom tool calls contains a 'call' object:
    {
      "call": {
        "call_id": "call_abc123",
        "from_number": "+15550199", ...
      },
      "arguments": { ... }
    }
    """
    call_id = "unknown_call"
    phone_number = "unknown_phone"
    
    try:
        body = await request.json()
        if "call" in body and isinstance(body["call"], dict):
            call_id = body["call"].get("call_id", call_id)
            phone_number = body["call"].get("from_number", phone_number)
    except Exception:
        pass
        
    # Fallback to headers
    x_call_id = request.headers.get("X-Call-Id")
    if x_call_id:
        call_id = x_call_id
    x_phone = request.headers.get("X-Phone-Number")
    if x_phone:
        phone_number = x_phone
        
    return call_id, phone_number

# Webhook for Retell events
@app.post("/webhook")
async def retell_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handles Retell events like call_started and call_ended.
    """
    try:
        payload = await request.json()
        logger.info(f"Received webhook payload: {payload}")
        
        event = payload.get("event")
        call_data = payload.get("call", {})
        call_id = call_data.get("call_id")
        phone_number = call_data.get("from_number")
        
        if not call_id or not phone_number:
            return {"status": "ignored", "reason": "missing call metadata"}
            
        if event == "call_started":
            # Check if there is an active session for this phone number that was dropped
            logger.info(f"Call started: {call_id} from {phone_number}")
            services.update_call_session(db, call_id, phone_number, context_data={})
            
        elif event == "call_ended":
            logger.info(f"Call ended: {call_id} from {phone_number}")
            # Mark call session as inactive
            session = db.query(CallSession).filter(CallSession.call_id == call_id).first()
            if session:
                session.is_active = 0
                db.commit()
                
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        return {"status": "error", "message": str(e)}

# Tool Endpoints

@app.post("/tools/identify_caller")
async def identify_caller(
    request: Request, 
    payload: Optional[IdentifyCallerRequest] = None,
    db: Session = Depends(get_db)
):
    """
    Identifies if the caller is new, returning, or returning after a dropped call.
    Also handles family line disambiguation.
    """
    req_call_id, req_phone = await get_call_metadata(request)
    
    phone = None
    if payload:
        phone = payload.phone_number
        
    # Use phone number from body parameter or fallback to request metadata
    phone = phone or req_phone
    if not phone or phone == "unknown_phone":
        return {
            "status": "unknown_phone",
            "message": "I could not automatically detect your phone number. Could you please tell me your phone number?"
        }
        
    logger.info(f"Identifying caller for phone: {phone}")

    # 1. Check for a dropped call recovery (active session in last 10 minutes)
    ten_minutes_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
    recent_session = db.query(CallSession).filter(
        CallSession.phone_number == phone,
        CallSession.is_active == 1,
        CallSession.updated_at >= ten_minutes_ago,
        CallSession.context_data != {}
    ).order_by(CallSession.updated_at.desc()).first()

    # Only trigger recovery if we have actual booking context (start_time or queried_start_time) in progress
    if recent_session and recent_session.context_data and ("queried_start_time" in recent_session.context_data or "start_time" in recent_session.context_data):
        ctx = recent_session.context_data
        logger.info(f"Found recently dropped call session for {phone}: {ctx}")
        # Copy context to current call session
        services.update_call_session(db, req_call_id, phone, context_data=ctx)
        
        # Format a friendly resumption message
        appt_time_str = ctx.get("start_time")
        doc_name = ctx.get("practitioner_name", "your doctor")
        clinic_name = ctx.get("clinic_name", "the clinic")
        
        return {
            "status": "dropped_call_recovery",
            "patient_name": ctx.get("patient_name"),
            "context": ctx,
            "message": f"Welcome back. It looks like we got cut off. Would you like to pick up where we left off and continue booking your appointment with {doc_name} at {clinic_name} for {appt_time_str}?"
        }

    # 2. Check patient database
    patients = db.query(Patient).filter(Patient.phone_number == phone).all()
    
    if not patients:
        return {
            "status": "new_patient",
            "message": "Welcome! It looks like you're calling us for the first time. Can I start by getting your full name, please?"
        }
        
    if len(patients) > 1:
        # Family line sharing one phone number
        names = [f"{p.first_name} {p.last_name}" for p in patients]
        logger.info(f"Family line detected for phone {phone}: {names}")
        
        # Save state to current session that we are disambiguating
        services.update_call_session(db, req_call_id, phone, context_data={"disambiguating_patients": names})
        
        return {
            "status": "family_line_shared",
            "names": names,
            "message": "I see multiple profiles registered under this phone number. To make sure I access the correct record, could you please tell me your full name?"
        }

    # Exactly one returning patient
    patient = patients[0]
    patient_name = f"{patient.first_name} {patient.last_name}"
    logger.info(f"Returning patient identified: {patient_name}")
    
    # Save patient context
    services.update_call_session(db, req_call_id, phone, context_data={"patient_id": patient.id, "patient_name": patient_name})
    
    return {
        "status": "returning_patient",
        "patient_name": patient_name,
        "message": f"Welcome back, {patient.first_name}! How can I help you today? (Note: If you are booking an appointment, I will still confirm your full name at the end for security.)"
    }


@app.get("/practitioners")
def list_practitioners(specialty: Optional[str] = None, clinic_id: Optional[int] = None, db: Session = Depends(get_db)):
    """List practitioners filtered by specialty or clinic."""
    practitioners = services.get_practitioners_by_specialty(db, specialty, clinic_id)
    return [
        {
            "id": p.id,
            "name": p.name,
            "specialty": p.specialty,
            "clinic_id": p.clinic_id,
            "clinic_name": p.clinic.name,
            "clinic_location": p.clinic.location
        }
        for p in practitioners
    ]


@app.post("/tools/get_practitioners")
def get_practitioners_tool(payload: Optional[GetPractitionersRequest] = None, db: Session = Depends(get_db)):
    """List practitioners filtered by specialty or clinic for Retell custom function."""
    specialty = payload.specialty if payload else None
    clinic_id = payload.clinic_id if payload else None
    practitioners = services.get_practitioners_by_specialty(db, specialty, clinic_id)
    return [
        {
            "id": p.id,
            "name": p.name,
            "specialty": p.specialty,
            "clinic_id": p.clinic_id,
            "clinic_name": p.clinic.name,
            "clinic_location": p.clinic.location
        }
        for p in practitioners
    ]


@app.post("/tools/check_availability")
async def check_availability(
    request: Request,
    payload: CheckAvailabilityRequest,
    db: Session = Depends(get_db)
):
    """Checks if a specific doctor is available at a given time."""
    req_call_id, req_phone = await get_call_metadata(request)
    practitioner_id = payload.practitioner_id
    start_time = payload.start_time
    
    try:
        dt = datetime.datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        # Strip timezone info for database comparison if we store naive UTC datetimes
        dt_naive = dt.replace(tzinfo=None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}. Use ISO 8601 format.")

    is_avail, reason = services.check_practitioner_availability(db, practitioner_id, dt_naive)
    
    # Retrieve practitioner details
    pract = db.query(Practitioner).filter(Practitioner.id == practitioner_id).first()
    pract_name = pract.name if pract else "Unknown Practitioner"
    clinic_name = pract.clinic.name if pract else "Unknown Clinic"
    clinic_id = pract.clinic_id if pract else None

    # Update session context with what they are querying (state tracking)
    session = db.query(CallSession).filter(CallSession.call_id == req_call_id).first()
    if session:
        ctx = session.context_data or {}
        ctx.update({
            "queried_practitioner_id": practitioner_id,
            "queried_practitioner_name": pract_name,
            "queried_clinic_id": clinic_id,
            "queried_clinic_name": clinic_name,
            "queried_start_time": dt_naive.isoformat(),
            "availability_status": is_avail
        })
        services.update_call_session(db, req_call_id, req_phone, context_data=ctx)

    return {
        "practitioner_id": practitioner_id,
        "practitioner_name": pract_name,
        "start_time": start_time,
        "is_available": is_avail,
        "reason": reason
    }


@app.post("/tools/search_earliest_slot")
async def search_earliest_slot(
    request: Request,
    payload: Optional[SearchEarliestSlotRequest] = None,
    db: Session = Depends(get_db)
):
    """Searches across branches and practitioners to find the earliest slot."""
    req_call_id, req_phone = await get_call_metadata(request)
    
    specialty = payload.specialty if payload else None
    clinic_id = payload.clinic_id if payload else None
    start_from = payload.start_from if payload else None
    
    start_dt = None
    if start_from:
        try:
            start_dt = datetime.datetime.fromisoformat(start_from.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass

    slot, msg = services.find_earliest_available_slot(db, specialty, clinic_id, start_dt)
    
    if not slot:
        return {"success": False, "message": msg}

    # Update call session state with search result
    session = db.query(CallSession).filter(CallSession.call_id == req_call_id).first()
    if session:
        ctx = session.context_data or {}
        ctx.update({
            "queried_practitioner_id": slot["practitioner_id"],
            "queried_practitioner_name": slot["practitioner_name"],
            "queried_clinic_id": slot["clinic_id"],
            "queried_clinic_name": slot["clinic_name"],
            "queried_start_time": slot["start_time"].isoformat(),
            "availability_status": True
        })
        services.update_call_session(db, req_call_id, req_phone, context_data=ctx)

    # Format datetime response cleanly
    slot_formatted = slot.copy()
    slot_formatted["start_time"] = slot["start_time"].isoformat()
    slot_formatted["end_time"] = slot["end_time"].isoformat()
    
    return {
        "success": True,
        "slot": slot_formatted,
        "message": f"Found slot with {slot['practitioner_name']} ({slot['specialty']}) at {slot['clinic_name']} on {slot['start_time'].strftime('%Y-%m-%d')} at {slot['start_time'].strftime('%I:%M %p')}"
    }


@app.post("/tools/book_appointment")
async def book_appointment(
    request: Request,
    payload: BookAppointmentRequest,
    db: Session = Depends(get_db)
):
    """Books a new appointment. Verification of full name is required before booking."""
    req_call_id, req_phone = await get_call_metadata(request)
    first_name = payload.first_name
    last_name = payload.last_name
    practitioner_id = payload.practitioner_id
    clinic_id = payload.clinic_id
    start_time = payload.start_time
    idempotency_key = payload.idempotency_key
    
    if not first_name.strip() or not last_name.strip():
        raise HTTPException(status_code=400, detail="Booking requires both first and last name.")

    try:
        dt = datetime.datetime.fromisoformat(start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Enforce write-time transaction locks & availability check inside services
    patient = services.get_or_create_patient(db, req_phone, first_name.strip(), last_name.strip())
    appt, status = services.create_appointment(db, patient.id, practitioner_id, clinic_id, dt, idempotency_key)
    
    if not appt:
        return {"success": False, "message": status}

    # Clear/update call session context so we don't prompt recovery for this booking anymore
    session = db.query(CallSession).filter(CallSession.call_id == req_call_id).first()
    if session:
        # Keep basic patient details but remove active booking state
        services.update_call_session(db, req_call_id, req_phone, context_data={
            "patient_id": patient.id,
            "patient_name": f"{patient.first_name} {patient.last_name}"
        })

    return {
        "success": True,
        "appointment_id": appt.id,
        "patient_name": f"{patient.first_name} {patient.last_name}",
        "practitioner_name": appt.practitioner.name,
        "clinic_name": appt.clinic.name,
        "start_time": appt.start_time.isoformat(),
        "status": appt.status,
        "message": f"Appointment booked successfully for {patient.first_name} {patient.last_name} with {appt.practitioner.name} at {appt.clinic.name} on {appt.start_time.strftime('%Y-%m-%d')} at {appt.start_time.strftime('%I:%M %p')}."
    }


@app.post("/tools/reschedule_appointment")
async def reschedule_appointment(
    request: Request,
    payload: RescheduleAppointmentRequest,
    db: Session = Depends(get_db)
):
    """Reschedules an existing appointment to a new slot."""
    req_call_id, req_phone = await get_call_metadata(request)
    appointment_id = payload.appointment_id
    new_start_time = payload.new_start_time
    
    try:
        dt = datetime.datetime.fromisoformat(new_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    appt, fee_applies, fee_amt, status = services.reschedule_appointment(db, appointment_id, dt)
    
    if not appt:
        return {"success": False, "message": status}

    res = {
        "success": True,
        "appointment_id": appt.id,
        "new_start_time": appt.start_time.isoformat(),
        "fee_applies": fee_applies,
        "fee_amount": fee_amt,
        "message": f"Appointment rescheduled successfully to {appt.start_time.strftime('%Y-%m-%d')} at {appt.start_time.strftime('%I:%M %p')}."
    }

    if fee_applies:
        res["message"] += f" Please note that a late rescheduling fee of {fee_amt} applies as this change is made within 24 hours of the appointment."

    return res


@app.post("/tools/cancel_appointment")
async def cancel_appointment(
    request: Request,
    payload: CancelAppointmentRequest,
    db: Session = Depends(get_db)
):
    """Cancels an appointment."""
    appointment_id = payload.appointment_id
    success, fee_applies, fee_amt, status = services.cancel_appointment(db, appointment_id)
    
    if not success:
        return {"success": False, "message": status}

    res = {
        "success": True,
        "appointment_id": appointment_id,
        "fee_applies": fee_applies,
        "fee_amount": fee_amt,
        "message": "Appointment cancelled successfully."
    }

    if fee_applies:
        res["message"] += f" Please note that a late cancellation fee of {fee_amt} applies as this cancellation is made within 24 hours of the scheduled time."

    return res
