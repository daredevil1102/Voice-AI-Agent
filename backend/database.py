import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

from .models import Base, Clinic, Practitioner, Patient, Appointment, CallSession

load_dotenv()

# Determine database URL. If not provided, default to a SQLite file in the workspace
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
    seed_data()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def seed_data():
    db = SessionLocal()
    try:
        # Check if clinics already exist
        if db.query(Clinic).first() is not None:
            return

        # Create branches
        downtown = Clinic(name="Downtown Health Center", location="123 Main St, Downtown")
        westside = Clinic(name="Westside Family Clinic", location="456 West Ave, Westside")
        db.add_all([downtown, westside])
        db.commit()

        # Create practitioners
        dr_ramesh = Practitioner(
            name="Dr. Ramesh Sharma", 
            specialty="General Medicine", 
            clinic_id=downtown.id
        )
        dr_priya = Practitioner(
            name="Dr. Priya Patel", 
            specialty="Pediatrics", 
            clinic_id=downtown.id
        )
        dr_amit = Practitioner(
            name="Dr. Amit Verma", 
            specialty="Dermatology", 
            clinic_id=westside.id
        )
        dr_sneha = Practitioner(
            name="Dr. Sneha Rao", 
            specialty="General Medicine", 
            clinic_id=westside.id
        )
        db.add_all([dr_ramesh, dr_priya, dr_amit, dr_sneha])
        
        # Seed a returning patient for testing
        test_patient = Patient(
            first_name="Rajesh",
            last_name="Kumar",
            phone_number="+15550199" # Test phone number
        )
        db.add(test_patient)

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()
