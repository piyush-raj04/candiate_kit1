# Candidate kit — completed implementation

The starter API has been expanded in `app/main.py`. All application code remains in that single file as required by the assignment.

## Run
```bash
python -m venv .venv
# activate the environment
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db
python sample_seed.py
uvicorn app.main:app --port 8000
```
On Windows PowerShell, use `$env:MONGO_URI="mongodb://localhost:27017"` and `$env:MONGO_DB="attendance_db"`.

Read `PROBLEM_STATEMENT.docx`, `openapi.yaml`, and `DATA_MODEL.md` for the complete requirements and API contract. `REVIEW.md` documents starter defects; `DECISIONS.md` records the implementation decisions.

**Before submission:** run the assignment's tests against MongoDB 7.0. In particular, validate the analytics and `/admin/explain/{endpoint}` routes against the hidden-data edge cases and the requirement for MongoDB aggregation execution plans; explain output and performance criteria can depend on the grader's indexes and dataset.
