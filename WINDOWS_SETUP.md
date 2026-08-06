# Windows Setup & Verification (PowerShell)

Run every command from the `backend` directory.

## 1. Create and activate the virtual environment

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\.venv\Scripts\Activate.ps1
```
Your prompt should now start with `(.venv)`.

## 2. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## 3. Seed and migrate the database

`seed.py` creates `dalton_solar.db`, applies migrations 001-003, and seeds four
test users. It is **destructive** — it drops and recreates the database.

```powershell
python seed.py
```

To apply migrations to an existing database WITHOUT wiping it:
```powershell
python -m db.migrate
```

## 4. Run the full test suite

```powershell
python -m pytest
```

## 5. Run each standalone validation suite

```powershell
python test\test_perch_milestone2.py
python test\e2e_scenario.py
python test\verify_frontend_integration.py
```

Each resets the database on start, so run them one at a time, not in parallel.

## 6. Launch the application

```powershell
python app.py
```
Then open http://localhost:5000 and sign in as
`charlie@daltonsolar.com` / `RepPass1!`.

Stop with `Ctrl+C`. Deactivate the venv with `deactivate`.

## Notes

- **Do not set any `PERCH_*` variables.** Unset means `PERCH_API_MODE=mock`,
  which is what every suite expects. Staging is not ready until Friday.
- **Tesseract is not required** for any test — all fixtures are PDFs, handled by
  `pdfplumber`. It is only needed to OCR *photo* uploads at runtime.
- If `python` maps to Python 2 or is missing, use `py -3` instead
  (e.g. `py -3 -m venv .venv`).
