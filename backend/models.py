import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Clinic(Base):
    __tablename__ = "clinics"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    location = Column(String, nullable=False)

    practitioners = relationship("Practitioner", back_populates="clinic")
    appointments = relationship("Appointment", back_populates="clinic")


class Practitioner(Base):
    __tablename__ = "practitioners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    specialty = Column(String, nullable=False)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)

    clinic = relationship("Clinic", back_populates="practitioners")
    appointments = relationship("Appointment", back_populates="practitioner")


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    phone_number = Column(String, index=True, nullable=False)

    appointments = relationship("Appointment", back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=False)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, default="booked", nullable=False)  # "booked", "rescheduled", "cancelled"
    idempotency_key = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)

    patient = relationship("Patient", back_populates="appointments")
    practitioner = relationship("Practitioner", back_populates="appointments")
    clinic = relationship("Clinic", back_populates="appointments")


class CallSession(Base):
    __tablename__ = "call_sessions"

    call_id = Column(String, primary_key=True, index=True)
    phone_number = Column(String, nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    last_clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    last_practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=True)
    last_start_time = Column(DateTime, nullable=True)
    context_data = Column(JSON, nullable=True)  # Store conversational state, current flow, slots offered, etc.
    is_active = Column(Integer, default=1, nullable=False) # 1 for active/recent, 0 for ended
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
