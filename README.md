# Voice AI Clinic Receptionist Backend

A production-ready voice AI receptionist backend built for **Aarohan Clinics** to handle appointment booking, rescheduling, cancellation, and branch/practitioner triage. Supported in both **English and Hindi** (with natural mid-call code-switching), this backend is optimized to handle real-world clinical front-desk scenarios like dropped-call recovery, family line sharing, stale availability, and late rescheduling fee enforcement.

---

## 🛠️ Tech Stack Justification

Per the assignment requirements, we selected **Retell AI** as our voice platform, integrated with a **FastAPI** backend and **PostgreSQL/SQLAlchemy** datastore.

| Dimension | Selected Stack: **Retell AI** + **FastAPI** | Alternatives Considered (Bolna / Vapi) |
| :--- | :--- | :--- |
| **Latency** | **Superior:** Direct WebRTC streams and deep integration with OpenAI's audio endpoints yield conversational response latencies under **1.1 seconds** (including ASR, LLM, and TTS). | **Moderate:** Often exhibit higher orchestration delays during tool execution transitions. |
| **Tool-Calling Reliability** | **Extremely High:** Support for native JSON Schema schemas and strict type validation reduces tool parse failures to nearly zero. | **Moderate:** Complex parameter mapping sometimes results in hallucinated tool call arguments. |
| **Telephony Behavior** | **Excellent:** Native WebRTC & SIP/PSTN numbers with standard Twilio bindings. Interruption detection is highly customizable (barge-in settings). | **Basic:** Interruption handling can feel sluggish, leading to overlapping speech. |
| **Multilingual Handling** | **Excellent:** When configured with `gpt-4o`, handles English/Hindi mid-sentence code-switching (Hinglish) without hardcoded translation lookup tables. | **Basic:** Rely heavily on pre-configured language models or translation middlewares which introduce latency. |

---

## 📐 Architecture & Features

### 1. Relational Database Schema (`backend/models.py`)
Enforces relational integrity across:
- **Clinics/Branches:** At least 2 branches (Downtown Health Center, Westside Family Clinic).
- **Practitioners/Doctors:** Tracks specialties (General Medicine, Pediatrics, Dermatology) and assigned branches.
- **Patients:** Stores profiles. Does not restrict `phone_number` to unique fields to naturally support **family line sharing** (multiple patients sharing a parent's phone number).
- **Appointments:** Tracks appointment status, time slots, and unique **idempotency keys** to prevent double-booking.
- **Call Sessions:** Preserves active conversational context, queried slots, and state to enable **dropped call recovery**.

### 2. Concurrency & Write-Time Conflict Resolution
To prevent double-bookings, `services.py` uses SQLAlchemy database transactions combined with `SELECT ... FOR UPDATE` locks. When a slot is being booked or rescheduled, the practitioner's record is locked in the database during availability verification and insert operations to block any concurrent bookings.

### 3. Business Logic Details
- **Rescheduling Fee Policy:** Rescheduling or cancelling an appointment less than **24 hours** before the start time triggers a **$25 late fee**, which is returned by the tool response and voiced by the agent. Changes outside this window incur no fees.
- **Dropped Call Recovery:** If a call disconnects, `identify_caller` detects if there is an active session in the last 10 minutes that contains booking progress. It returns a `dropped_call_recovery` status, instructing the agent to warmly ask to resume booking where they left off.
- **Family Line Disambiguation:** If multiple patients share a phone number, `identify_caller` returns `family_line_shared` and a list of profiles, directing the agent to ask the caller for their full name to access the correct record.
- **Stale Availability:** The agent is configured to always run a fresh live lookup via `check_availability` or `search_earliest_slot` whenever the user requests a different time, preventing reliance on stale variables sitting in memory.

---

## 🚀 Setup & Running Locally

### 1. Installation
Initialize the virtual environment and install all packages:
```bash
# Create python virtual environment
python -m venv venv

# Activate virtual environment (Windows Powershell)
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 2. Run the Backend Server
Start the FastAPI server locally:
```bash
uvicorn backend.main:app --reload
```
On startup, the server automatically initializes an SQLite database file `clinic.db` (acting as the local datastore) and seeds it with clinic branches, practitioners, and a test patient.

### 3. Run Automated Unit Tests
Run the unit test suite covering locking, scheduling logic, and API routes:
```bash
python -m pytest
```

---

## 📊 Evaluation Harness (`eval/harness.py`)

The evaluation harness simulates multi-turn customer conversations representing the required test scenarios, queries the local FastAPI server, measures generation latencies, and computes turns-to-completion.

To run the harness, ensure the uvicorn server is running in the background, then execute:
```bash
python eval/harness.py
```
This generates a detailed markdown performance breakdown at `EVALUATION_REPORT.md` analyzing:
- Turns-to-completion per language.
- Redundant question counts.
- Dynamic component latency (ASR, LLM, TTS, network).
- Critical analysis of harness limitations (acoustic noise, barge-in timings, etc.).

---

## ☁️ Production Deployment & Live Telephony Setup

### Step 1: Deploy the Backend
Deploy the FastAPI application to a platform of your choice (e.g., Render, Railway, or Fly.io).
- Provide a persistent PostgreSQL connection URL as the `DATABASE_URL` environment variable.

### Step 2: Configure Retell AI
1. Create a new Agent in your Retell AI dashboard.
2. Copy the system prompt text from `agent/system_prompt.txt` into the agent's instructions.
3. Define the tools matching the schemas in `agent/agent_config.json`.
4. Point each tool's webhook URL to your deployed backend, e.g.:
   - Identify Caller: `https://your-deployed-app.com/tools/identify_caller`
   - Check Availability: `https://your-deployed-app.com/tools/check_availability`
   - Search Earliest Slot: `https://your-deployed-app.com/tools/search_earliest_slot`
   - Book Appointment: `https://your-deployed-app.com/tools/book_appointment`
   - Reschedule Appointment: `https://your-deployed-app.com/tools/reschedule_appointment`
   - Cancel Appointment: `https://your-deployed-app.com/tools/cancel_appointment`

### Step 3: Connect Live Number
1. Buy or assign a phone number in the Retell dashboard and link it to your configured agent.
2. Under the phone number settings, set the **Status Callback URL** (Webhook) to `https://your-deployed-app.com/webhook` to handle start/end callbacks for session tracking.
3. Call the number to test end-to-end booking!
