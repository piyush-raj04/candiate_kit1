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

