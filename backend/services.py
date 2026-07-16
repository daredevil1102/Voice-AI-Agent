import datetime
import pytz
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from .models import Clinic, Practitioner, Patient, Appointment, CallSession

# Configurable settings
APPOINTMENT_DURATION = datetime.timedelta(minutes=30)
CLINIC_OPEN_HOUR = 9  # 9:00 AM
CLINIC_CLOSE_HOUR = 17 # 5:00 PM
FEE_POLICY_WINDOW_HOURS = 24
CANCELLATION_FEE = "$25" # Standard fee for late reschedule/cancel

# Timezone: clinic operates in IST (Indian Standard Time, UTC+5:30)
CLINIC_TIMEZONE = pytz.timezone("Asia/Kolkata")

def get_local_now() -> datetime.datetime:
    """Returns the current local (IST) datetime as a naive datetime for DB comparisons."""
    return datetime.datetime.now(CLINIC_TIMEZONE).replace(tzinfo=None)

def get_clinic_working_hours(date: datetime.date):
    """Returns the start and end of working hours for a given date."""
    start = datetime.datetime.combine(date, datetime.time(CLINIC_OPEN_HOUR, 0))
    end = datetime.datetime.combine(date, datetime.time(CLINIC_CLOSE_HOUR, 0))
    return start, end

def get_practitioners_by_specialty(db: Session, specialty: str = None, clinic_id: int = None):
    """Retrieve practitioners filtered by specialty and clinic."""
    query = db.query(Practitioner)
    if specialty:
        query = query.filter(Practitioner.specialty.ilike(specialty))
    if clinic_id:
        query = query.filter(Practitioner.clinic_id == clinic_id)
    return query.all()

def check_practitioner_availability(db: Session, practitioner_id: int, start_time: datetime.datetime):
    """
    Checks if a practitioner is available at a specific start_time.
    Enforces that the time is within working hours, in the future, and does not conflict.
    """
    end_time = start_time + APPOINTMENT_DURATION
    
    # 1. Check working hours
    work_start, work_end = get_clinic_working_hours(start_time.date())
    if start_time < work_start or end_time > work_end:
        return False, "Requested time is outside clinic working hours (9:00 AM - 5:00 PM)."
        
    # 2. Check future time (can't book in the past)
    if start_time < get_local_now():
        return False, "Cannot book appointments in the past."

    # 3. Check overlaps
    # Overlap query: start_time < existing_end_time AND end_time > existing_start_time
    conflicts = db.query(Appointment).filter(
        Appointment.practitioner_id == practitioner_id,
        Appointment.status != "cancelled",
        and_(
            Appointment.start_time < end_time,
            Appointment.end_time > start_time
        )
    ).all()

    if conflicts:
        return False, "Practitioner has a scheduling conflict at this time."

    return True, "Available"

def find_earliest_available_slot(db: Session, specialty: str = None, clinic_id: int = None, start_from: datetime.datetime = None):
    """
    Searches across branches and practitioners to find the earliest available 30-minute slot.
    Returns the practitioner, clinic, and slot start time.
    """
    if start_from is None:
        start_from = get_local_now()
    
    # Round up start_from to the next 30-minute interval
    minutes = (start_from.minute // 30 + 1) * 30
    search_start = start_from.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=minutes)
    
    # Query matching practitioners
    practitioners = get_practitioners_by_specialty(db, specialty, clinic_id)
    if not practitioners:
        return None, "No practitioners matching criteria found."

    # Search day by day for up to 7 days
    for day_offset in range(7):
        current_date = (search_start + datetime.timedelta(days=day_offset)).date()
        work_start, work_end = get_clinic_working_hours(current_date)
        
        # Determine the start time for search on this day
        if day_offset == 0:
            day_search_start = max(search_start, work_start)
        else:
            day_search_start = work_start
            
        # Iterate in 30-minute blocks
        slot_time = day_search_start
        while slot_time + APPOINTMENT_DURATION <= work_end:
            for pract in practitioners:
                is_avail, _ = check_practitioner_availability(db, pract.id, slot_time)
                if is_avail:
                    return {
                        "practitioner_id": pract.id,
                        "practitioner_name": pract.name,
                        "specialty": pract.specialty,
                        "clinic_id": pract.clinic_id,
                        "clinic_name": pract.clinic.name,
                        "start_time": slot_time,
                        "end_time": slot_time + APPOINTMENT_DURATION
                    }, "Found slot"
            slot_time += datetime.timedelta(minutes=30)
            
    return None, "No available slots found within the next 7 days."

def check_reschedule_fee_applies(appointment: Appointment) -> tuple[bool, str]:
    """
    Checks if a cancellation or reschedule fee applies.
    Fee applies if changes are within 24 hours of the appointment start.
    """
    now = get_local_now()
    time_diff = appointment.start_time - now
    if time_diff < datetime.timedelta(hours=FEE_POLICY_WINDOW_HOURS):
        return True, CANCELLATION_FEE
    return False, "$0"

def get_or_create_patient(db: Session, phone_number: str, first_name: str, last_name: str) -> Patient:
    """Gets a patient by phone number or creates a new one."""
    patient = db.query(Patient).filter(
        Patient.phone_number == phone_number,
        Patient.first_name.ilike(first_name),
        Patient.last_name.ilike(last_name)
    ).first()
    if not patient:
        patient = Patient(phone_number=phone_number, first_name=first_name, last_name=last_name)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    return patient

def create_appointment(db: Session, patient_id: int, practitioner_id: int, clinic_id: int, start_time: datetime.datetime, idempotency_key: str = None) -> tuple[Appointment, str]:
    """
    Creates a new appointment. Enforces write-time conflict checks via transactions.
    Supports idempotency.
    """
    # 1. Idempotency Check
    if idempotency_key:
        existing = db.query(Appointment).filter(Appointment.idempotency_key == idempotency_key).first()
        if existing:
            return existing, "Idempotency match: already booked."

    # Start transaction (caller controls transaction, but we lock the practitioner's appointments)
    # Using SELECT FOR UPDATE to block concurrent bookings on the same practitioner
    db.query(Practitioner).filter(Practitioner.id == practitioner_id).with_for_update().first()

    # Re-verify availability
    is_avail, reason = check_practitioner_availability(db, practitioner_id, start_time)
    if not is_avail:
        return None, f"Booking failed: {reason}"

    # Create appointment
    appt = Appointment(
        patient_id=patient_id,
        practitioner_id=practitioner_id,
        clinic_id=clinic_id,
        start_time=start_time,
        end_time=start_time + APPOINTMENT_DURATION,
        status="booked",
        idempotency_key=idempotency_key
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt, "Success"

def reschedule_appointment(db: Session, appointment_id: int, new_start_time: datetime.datetime) -> tuple[Appointment, bool, str, str]:
    """
    Reschedules an existing appointment to a new slot.
    Checks and returns whether a fee applies and the message.
    """
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).with_for_update().first()
    if not appt:
        return None, False, "$0", "Appointment not found."

    if appt.status == "cancelled":
        return None, False, "$0", "Cannot reschedule a cancelled appointment."

    # Check fee policy before modifying state
    fee_applies, fee_amt = check_reschedule_fee_applies(appt)

    # Re-verify availability for the new slot
    is_avail, reason = check_practitioner_availability(db, appt.practitioner_id, new_start_time)
    if not is_avail:
        return None, False, "$0", f"Rescheduling failed: {reason}"

    appt.start_time = new_start_time
    appt.end_time = new_start_time + APPOINTMENT_DURATION
    appt.status = "rescheduled"
    db.commit()
    db.refresh(appt)

    return appt, fee_applies, fee_amt, "Success"

def cancel_appointment(db: Session, appointment_id: int) -> tuple[bool, bool, str, str]:
    """Cancels an existing appointment, returning if success, if fee applies, fee amount, and reason."""
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).with_for_update().first()
    if not appt:
        return False, False, "$0", "Appointment not found."

    if appt.status == "cancelled":
        return False, False, "$0", "Appointment is already cancelled."

    # Check fee policy
    fee_applies, fee_amt = check_reschedule_fee_applies(appt)

    appt.status = "cancelled"
    db.commit()

    return True, fee_applies, fee_amt, "Success"

def update_call_session(db: Session, call_id: str, phone_number: str, context_data: dict, patient_id: int = None, clinic_id: int = None, practitioner_id: int = None, start_time: datetime.datetime = None):
    """Creates or updates a call session with state data."""
    session = db.query(CallSession).filter(CallSession.call_id == call_id).first()
    if not session:
        session = CallSession(call_id=call_id, phone_number=phone_number)
        db.add(session)

    session.phone_number = phone_number
    if context_data is not None:
        session.context_data = context_data
    if patient_id is not None:
        session.patient_id = patient_id
    if clinic_id is not None:
        session.last_clinic_id = clinic_id
    if practitioner_id is not None:
        session.last_practitioner_id = practitioner_id
    if start_time is not None:
        session.last_start_time = start_time

    db.commit()
    return session
