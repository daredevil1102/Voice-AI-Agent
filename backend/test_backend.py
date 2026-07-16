import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models import Base, Clinic, Practitioner, Patient, Appointment, CallSession
from backend import services

from sqlalchemy.pool import StaticPool

# In-memory SQLite for testing database operations
DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(name="db")
def db_fixture():
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    
    # Seed mock clinic and doctors
    db = TestingSessionLocal()
    
    # Branches
    downtown = Clinic(name="Downtown Health Center", location="Downtown")
    westside = Clinic(name="Westside Family Clinic", location="Westside")
    db.add_all([downtown, westside])
    db.commit()

    # Doctors
    dr_ramesh = Practitioner(name="Dr. Ramesh Sharma", specialty="General Medicine", clinic_id=downtown.id)
    dr_priya = Practitioner(name="Dr. Priya Patel", specialty="Pediatrics", clinic_id=downtown.id)
    dr_amit = Practitioner(name="Dr. Amit Verma", specialty="Dermatology", clinic_id=westside.id)
    dr_sneha = Practitioner(name="Dr. Sneha Rao", specialty="General Medicine", clinic_id=westside.id)
    db.add_all([dr_ramesh, dr_priya, dr_amit, dr_sneha])
    db.commit()

    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_availability_and_double_booking(db):
    dr_ramesh = db.query(Practitioner).filter(Practitioner.name == "Dr. Ramesh Sharma").first()
    clinic = db.query(Clinic).filter(Clinic.name == "Downtown Health Center").first()
    
    # 1. Target time is 10:00 AM tomorrow
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    slot_time = datetime.datetime.combine(tomorrow, datetime.time(10, 0))

    # Should be available
    is_avail, reason = services.check_practitioner_availability(db, dr_ramesh.id, slot_time)
    assert is_avail is True

    # 2. Book patient A
    patient_a = services.get_or_create_patient(db, "+11112222", "Aarav", "Sharma")
    appt, status = services.create_appointment(db, patient_a.id, dr_ramesh.id, clinic.id, slot_time)
    assert appt is not None
    assert status == "Success"

    # Should now be unavailable for Ramesh
    is_avail, reason = services.check_practitioner_availability(db, dr_ramesh.id, slot_time)
    assert is_avail is False

    # Still available for Dr. Priya Patel (same clinic) or Dr. Sneha Rao (other clinic)
    dr_priya = db.query(Practitioner).filter(Practitioner.name == "Dr. Priya Patel").first()
    is_avail_priya, _ = services.check_practitioner_availability(db, dr_priya.id, slot_time)
    assert is_avail_priya is True

    # Try booking Patient B for Ramesh at same time - should fail
    patient_b = services.get_or_create_patient(db, "+33334444", "Kabir", "Mehta")
    appt_b, status_b = services.create_appointment(db, patient_b.id, dr_ramesh.id, clinic.id, slot_time)
    assert appt_b is None
    assert "conflict" in status_b.lower()


def test_earliest_slot_search(db):
    downtown = db.query(Clinic).filter(Clinic.name == "Downtown Health Center").first()
    
    # Block Dr. Ramesh Sharma's earliest slots tomorrow at 9:00 AM and 9:30 AM
    dr_ramesh = db.query(Practitioner).filter(Practitioner.name == "Dr. Ramesh Sharma").first()
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    
    patient = services.get_or_create_patient(db, "+11112222", "Aarav", "Sharma")
    
    slot1 = datetime.datetime.combine(tomorrow, datetime.time(9, 0))
    slot2 = datetime.datetime.combine(tomorrow, datetime.time(9, 30))
    
    services.create_appointment(db, patient.id, dr_ramesh.id, downtown.id, slot1)
    services.create_appointment(db, patient.id, dr_ramesh.id, downtown.id, slot2)

    # Search earliest slot starting tomorrow 9:00 AM for General Medicine at Downtown Clinic (clinic_id = 1)
    start_search = datetime.datetime.combine(tomorrow, datetime.time(9, 0))
    res, msg = services.find_earliest_available_slot(db, specialty="General Medicine", clinic_id=downtown.id, start_from=start_search)
    
    assert res is not None
    # Earliest slot should be tomorrow at 10:00 AM (or Dr. Sneha Rao at Westside if we didn't specify clinic_id, but here clinic_id is Downtown)
    # Wait, Dr. Sneha Rao is at Westside. Downtown General Medicine is Dr. Ramesh. Since 9:00 and 9:30 are blocked, 10:00 AM is the earliest slot.
    assert res["practitioner_name"] == "Dr. Ramesh Sharma"
    assert res["start_time"] == datetime.datetime.combine(tomorrow, datetime.time(10, 0))


def test_idempotency_booking(db):
    dr_ramesh = db.query(Practitioner).filter(Practitioner.name == "Dr. Ramesh Sharma").first()
    clinic = db.query(Clinic).filter(Clinic.name == "Downtown Health Center").first()
    patient = services.get_or_create_patient(db, "+11112222", "Aarav", "Sharma")
    
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    slot_time = datetime.datetime.combine(tomorrow, datetime.time(11, 0))
    
    # Book with idempotency key
    ikey = "test-idempotency-123"
    appt1, status1 = services.create_appointment(db, patient.id, dr_ramesh.id, clinic.id, slot_time, idempotency_key=ikey)
    assert appt1 is not None
    assert status1 == "Success"

    # Try booking again with same key
    appt2, status2 = services.create_appointment(db, patient.id, dr_ramesh.id, clinic.id, slot_time, idempotency_key=ikey)
    assert appt2 is not None
    assert appt2.id == appt1.id
    assert "Idempotency match" in status2


def test_reschedule_and_cancellation_fee_policy(db, monkeypatch):
    # Mock check_practitioner_availability to avoid failures due to clinic hours constraints
    monkeypatch.setattr(services, "check_practitioner_availability", lambda *args: (True, "Available"))

    dr_ramesh = db.query(Practitioner).filter(Practitioner.name == "Dr. Ramesh Sharma").first()
    clinic = db.query(Clinic).filter(Clinic.name == "Downtown Health Center").first()
    patient = services.get_or_create_patient(db, "+11112222", "Aarav", "Sharma")
    
    # Scenario A: > 24 hours (No fee)
    far_date = datetime.datetime.utcnow() + datetime.timedelta(days=3)
    appt_far, _ = services.create_appointment(db, patient.id, dr_ramesh.id, clinic.id, far_date)
    
    new_far_date = far_date + datetime.timedelta(hours=2)
    res_appt, fee_applies, fee_amt, status = services.reschedule_appointment(db, appt_far.id, new_far_date)
    assert res_appt is not None
    assert fee_applies is False
    assert fee_amt == "$0"

    # Scenario B: < 24 hours (Fee applies)
    near_date = datetime.datetime.utcnow() + datetime.timedelta(hours=4)
    appt_near, _ = services.create_appointment(db, patient.id, dr_ramesh.id, clinic.id, near_date)
    
    new_near_date = near_date + datetime.timedelta(hours=1)
    res_appt_near, fee_applies_near, fee_amt_near, status_near = services.reschedule_appointment(db, appt_near.id, new_near_date)
    assert res_appt_near is not None
    assert fee_applies_near is True
    assert fee_amt_near == "$25"


def test_api_workflows(db, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app, get_db
    
    # Mock availability for testing API layer easily
    monkeypatch.setattr(services, "check_practitioner_availability", lambda *args: (True, "Available"))
    
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    
    # 0. Simulate call started webhook
    resp = client.post("/webhook", json={
        "event": "call_started",
        "call": {
            "call_id": "call-test-123",
            "from_number": "+9876543210"
        }
    })
    assert resp.status_code == 200

    # 1. Identify caller: New Patient
    resp = client.post(
        "/tools/identify_caller", 
        json={"phone_number": "+1234567890"},
        headers={"X-Call-Id": "call-test-new", "X-Phone-Number": "+1234567890"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "new_patient"
    
    # 2. Identify caller: Returning Patient
    p1 = Patient(first_name="Ramesh", last_name="Prasad", phone_number="+9876543210")
    db.add(p1)
    db.commit()
    
    resp = client.post(
        "/tools/identify_caller", 
        json={"phone_number": "+9876543210"},
        headers={"X-Call-Id": "call-test-123", "X-Phone-Number": "+9876543210"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "returning_patient"
    assert resp.json()["patient_name"] == "Ramesh Prasad"

    # 3. Identify caller: Family Line
    p2 = Patient(first_name="Sita", last_name="Prasad", phone_number="+9876543210")
    db.add(p2)
    db.commit()
    
    resp = client.post(
        "/tools/identify_caller", 
        json={"phone_number": "+9876543210"},
        headers={"X-Call-Id": "call-test-123", "X-Phone-Number": "+9876543210"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "family_line_shared"
    assert "Ramesh Prasad" in resp.json()["names"]
    assert "Sita Prasad" in resp.json()["names"]

    # 4. Check availability
    dr = db.query(Practitioner).first()
    resp = client.post(
        "/tools/check_availability", 
        json={
            "practitioner_id": dr.id,
            "start_time": "2026-12-13T10:00:00"
        },
        headers={"X-Call-Id": "call-test-123", "X-Phone-Number": "+9876543210"}
    )
    assert resp.status_code == 200
    assert resp.json()["is_available"] is True

    # 5. Book Appointment
    resp = client.post(
        "/tools/book_appointment", 
        json={
            "first_name": "Ramesh",
            "last_name": "Prasad",
            "practitioner_id": dr.id,
            "clinic_id": dr.clinic_id,
            "start_time": "2026-12-13T10:00:00"
        }, 
        headers={"X-Phone-Number": "+9876543210", "X-Call-Id": "call-test-123"}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # 6. Dropped call recovery test
    # Simulate a call drop by having an active CallSession that was not ended
    session = db.query(CallSession).filter(CallSession.call_id == "call-test-123").first()
    assert session is not None
    session.context_data = {
        "patient_name": "Ramesh Prasad",
        "practitioner_name": dr.name,
        "clinic_name": dr.clinic.name,
        "start_time": "2026-12-13T10:00:00"
    }
    db.commit()

    # Call identify_caller again - should trigger dropped_call_recovery
    resp = client.post(
        "/tools/identify_caller", 
        json={"phone_number": "+9876543210"},
        headers={"X-Call-Id": "call-test-123", "X-Phone-Number": "+9876543210"}
    )
    assert resp.status_code == 200
    assert "status" in resp.json()
    assert resp.json()["status"] == "dropped_call_recovery"
    assert "cut off" in resp.json()["message"].lower()

    app.dependency_overrides.clear()

