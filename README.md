# AchintERP

In-house ERP for a manufacturing company: clients, purchase orders, invoices,
dispatch tracking, payment allocation, audit log, and AI-assisted PDF extraction
of invoices and purchase orders.

## Stack

- **Backend**: Python 3.11, [FastAPI](https://fastapi.tiangolo.com/), SQLAlchemy 2.x, SQLite (PostgreSQL-ready via `APP_DATABASE_URL`)
- **Frontend**: Single-page app (`public/index.html`) + AG Grid for the dispatch matrix
- **AI**: Google Gemini via the new `google-genai` SDK (with a compatibility shim for legacy callers)
- **Auth**: JWT (`localStorage`-cached for cross-refresh persistence) + PBKDF2 password hashing
- **Schema migrations**: Alembic (baseline in `alembic/`)
- **Tests**: `pytest` (regression / contracts / audit / reconciliation / restore drill / infra) + Playwright (`tests/e2e`)

## Quick start

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium     # only needed once for e2e tests

# Copy the env template and edit the values you want to override.
copy .env.example .env

python main.py        # prod-like: no auto-reload

# Windows hot-reload: prefer this over raw `uvicorn --reload`; the latter sends
# Ctrl+C to the worker on each reload and can spam KeyboardInterrupt tracebacks
# while stdlib/third-party imports finish. This wrapper debounces reloads and
# ignores tests/backups/__pycache__ noise.
.\scripts\run-dev.ps1
```

The SPA is served at <http://127.0.0.1:3000/> by the same process.

## Tests

```powershell
# All backend + e2e tests (current baseline: 49/49 in ~40s)
.\venv\Scripts\python.exe -m pytest

# Only the API regression suite (fast)
.\venv\Scripts\python.exe -m pytest tests/test_regression_api.py
```

Continuous integration runs the same command on every push and pull request via
[GitHub Actions](.github/workflows/ci.yml). PRs cannot be merged unless CI is
green.

## Operations

- **Backups** are written to `db_backups/erp_database_*.sqlite` once a day; old
  files are auto-rotated based on `KEEP_BACKUPS_DAYS` (default 30, set to 0 to
  disable).
- **Restore drill** validates the latest backup is actually restorable + the
  app boots cleanly against it. Run unattended:
  ```powershell
  python restore_drill.py                                 # PASS / FAIL summary
  python restore_drill.py --user admin --password "..."   # also exercises auth
  ```
- **Health probe**: `GET /api/healthz` (DB ping, disk free, last backup age).
- **Metrics**: `GET /api/metrics` (row counts, uptime, backup config).

## Pre-commit hooks (optional)

```powershell
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

Hooks are check-only by default - they never rewrite files; a human always
decides what changes.

## Documentation

- `alembic/README` - schema migration workflow
- `.env.example` - every supported env knob with sensible defaults
- `restore_drill.py --help` - drill CLI reference

## Upgrade roadmap status

The platform is being modernised in tiers. Completed so far:

| Tier   | Workstream                                                     | Status |
| ------ | -------------------------------------------------------------- | ------ |
| **1**  | Pin deps, kill deprecations, structured logs, security headers | done   |
| **1**  | `.env`-only host/port, `/api/healthz` + `/api/metrics`         | done   |
| **1**  | Backup rotation (`KEEP_BACKUPS_DAYS`)                          | done   |
| **2G** | GitHub Actions CI gate (`pytest` + Playwright)                 | done   |
| **2A** | Modular backend (start: `routers/audit`, `health`, `recon`)    | partial - see `routers/` |
| **2B** | Modular SPA (start: `public/js/audit.js`, `recon.js`)          | partial - see `public/js/` |

`main.py` still owns the bulk of the endpoints + helpers; further router
carve-outs (clients, purchase orders, invoices, payments, dispatch, uploads)
are tracked as Tier 2A continuation. The pattern is documented at the bottom
of `main.py` (search for `Domain routers (Workstream 2A carve-out)`).
