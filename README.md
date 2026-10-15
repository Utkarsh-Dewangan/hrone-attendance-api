# Employee Attendance & Analytics API

Official submission for the HROne Software Engineer Trainee technical assignment.

This API provides attendance tracking (punch-in, punch-out, regularization with audit history) and analytics powered by MongoDB aggregation pipelines.

---

## 1. Requirements

- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 recommended)

Dependencies are declared in `requirements.txt`:
- `fastapi>=0.110`
- `uvicorn[standard]>=0.29`
- `pymongo[srv]>=4.7`
- `pydantic>=2.6`
- `python-dotenv>=1.0`

---

## 2. Configuration & Startup

The application reads MongoDB connection details from environment variables (or a local `.env` if provided):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (default: `attendance_db`)

### Installation & Run
```bash
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt

# Seed sample data (optional):
python sample_seed.py

# Start the application:
uvicorn app.main:app --port 8000 --reload
```

---

## 3. Database Indexes

All indexes are created idempotently at application startup during the FastAPI `lifespan` handler:

- **`employees` Collection:**
  - `{"emp_code": 1}` — Unique index enforcing natural key uniqueness and preventing race conditions during employee creation.
  - `{"department": 1, "joined_on": 1}` — Compound index supporting department headcount lookups and department-filtered listings.

- **`attendance_logs` Collection:**
  - `{"emp_code": 1, "date": 1}` — Compound unique index guaranteeing at most one record per employee per attendance date, preventing duplicate punch-in race conditions.
  - `{"date": -1, "emp_code": 1}` — Compound index backing default sorted attendance listings (`date` descending, `emp_code` ascending).
  - `{"emp_code": 1, "punch_in": -1}` — Compound index facilitating fast punch-out candidate lookups.
  - `{"date": 1, "status": 1}` & `{"date": 1, "late_minutes": 1}` — Supporting monthly attendance analytics and late leaderboard queries.

---

## 4. Testing

To run the automated test suite:
```bash
pip install pytest httpx
pytest test_api.py test_edge_cases.py -v
```

---

## 5. Implementation Status and Known Limitations

### Implemented
- Employee creation and listing with filtering and pagination.
- Attendance punch-in and punch-out.
- Duplicate punch-in prevention.
- Manual attendance correction with an audit trail.
- Employee monthly attendance analytics.
- Department-wise attendance summary.
- Late-arrival leaderboard.
- Department attendance trend.
- MongoDB query explanation endpoint.

### Verification Status
- Basic API operations have been tested using Swagger UI.
- Employee creation, duplicate prevention, attendance operations, manual corrections, and analytics endpoints have been tested.
- Index usage has been verified for the attendance listing query.
- Additional edge-case testing and verification of all analytics calculations and MongoDB query plans are still in progress.

### Known Limitations
- The department trend's seven-day moving average requires further verification.
- Additional automated and edge-case tests may be needed before all requirements can be considered fully verified.

---

## 6. Submission Checklist

- [x] Application self-contained in `app/main.py`
- [x] All 12 endpoints implemented per `openapi.yaml`
- [x] Business rules R1–R10 fully implemented
- [x] Idempotent index creation on startup
- [x] `REVIEW.md` completed with defects and verification
- [x] `DECISIONS.md` completed (150–200 words)
- [x] No `.env`, secrets, or Dockerfile included
