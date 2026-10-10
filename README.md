# Secure People Analytics

A small demo of HR self-service where every read and write is authorized, scoped and audited
in the backend, with PTO and overtime analytics served from ClickHouse.

> **All data is synthetic.** The 20 employees, contact details, salaries, leave requests,
> succession and investigation records in `data/` are fictional (555 phone numbers,
> "Example Avenue" addresses). No real person's data is included.

## Watch the demo

▶️ **[Watch the demo video](https://drive.google.com/file/d/1-cLKclYJyUilZCqgp5Dp9xmr5DUZ5p9I/view?usp=sharing)** (Google Drive) 

## Run locally
After starting the app on your computer, open:
[Open the local app](http://localhost:8501/)

The video walks through the app using synthetic data only:

- **Employee self-service:** an employee views and edits their own contact details and checks their PTO.
- **Manager-scoped analytics:** a manager sees PTO and overtime dashboards for their direct reports only.
- **Blocked unauthorized actions:** attempts to see or change other people's data, edit PTO, approve
  another team's leave or export data are refused by the backend.
- **Audit logging:** every allowed and denied attempt appears in the activity log.

## The problem

HR data mixes information with very different sensitivity: an employee's own phone number,
a team's PTO balances, salaries, investigations. People need self-service, and managers need
team analytics, but each person should see and change only what their role allows, and every
attempt, allowed or denied, should leave an audit trail. Analytics should come from a scalable
store without widening what anyone can see.

## What is implemented

**Employee** (e.g. `U_EMP01`)
- View and edit their own address and phone. Values are validated, each change writes change
  history, and the audit event is written before the change is made.
- View their own monthly PTO: opening, accrued, used, closing, and hours reserved by approved leave.
- See their own activity log and change history.

**Manager** (`M001` manages E001–E010 in Operations; `M002` manages E011–E020 in Customer Success)
- Team overview for direct reports only: average available PTO, overtime, two charts and tables.
- Approve or deny leave requests assigned to them for their own direct reports. Approval reserves
  hours against PTO; monthly records are never edited, so hours are not deducted twice. Each
  decision records the approver and a fingerprint of the exact request.

**Both roles** have a "Try a blocked action" tab whose buttons call real backend functions with
disallowed targets or actions (another person's data, editing PTO, approving another team's
leave, exporting). The backend denies each one with a reason code and logs it.

Not built (default deny): payroll, HRBP, auditor and ER screens; salary changes; sharing/export;
succession permissions.

## ClickHouse integration

- `python -m people_app.analytics upload` copies **only** synthetic workforce metrics to
  `people_analytics.workforce_monthly`: employee ID, department, manager ID, month, four PTO
  columns and overtime hours. Names, contact details, salary, succession and investigation data
  are never read by the upload.
- Uploads are idempotent: only `(employee_id, month)` keys not already present are inserted, and
  the `ReplacingMergeTree` key collapses any duplicate.
- The employee PTO view and the manager team overview read metrics from ClickHouse. Authorization
  runs first; the query is then filtered server-side to exactly the employee IDs that identity is
  allowed to see, and any row outside that set is discarded.
- Results are cached for 60 s per (user ID, role, permitted employee IDs), so one identity can
  never be served another's cached rows.
- The page shows **"Analytics source: ClickHouse"** only after a successful query. If ClickHouse is
  unreachable it shows **"local fallback (ClickHouse unavailable)"** and uses local seed data with
  the same scoping. Without a `.env` it says **"local data (ClickHouse not configured)"**.
- Contact edits, leave decisions, reservations and audit logs stay local.

## Architecture

```
Streamlit UI (app.py)              passes only the selected user id
        │
        ▼
PeopleService (people_app/service.py)
  ├─ Policy (policy/access_policy.json)   default deny; role + resource + action + scope
  ├─ scope checks                          self / direct_team / assigned request
  ├─ audit log  ─────────────► runtime/access_events.jsonl   (every allow and deny)
  ├─ writes     ─────────────► runtime/contact_details.json, leave_requests.json,
  │                            approvals.jsonl, change_history.jsonl
  └─ metrics (after authorization, authorized ids only)
        └─ ClickHouseWorkforce (people_app/analytics.py) ──► ClickHouse Cloud over TLS
                └─ fallback: data/workforce_monthly.json
```

- `data/` holds clean synthetic seeds and is never modified (a test checks this).
- `runtime/` is created from the seeds on first run. Existing runtime files are never overwritten,
  so restarting keeps edits, decisions and audit logs. It is git-ignored.
- Tables use `st.table` and charts are static SVG images, so the UI shows no built-in download,
  "show data" or fullscreen controls.

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/7chordszither-netizen/secure-people-analytics.git
cd secure-people-analytics
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest            # ClickHouse tests are skipped until .env is configured
streamlit run app.py        # http://localhost:8501
```

The app works without ClickHouse, using local data. To enable ClickHouse:

```bash
cp .env.example .env        # then edit .env and set host and password; never commit it
python -m people_app.clickhouse_db      # runs SELECT 1, prints OK or a sanitized error
python -m people_app.analytics upload   # safe to repeat
python -m people_app.analytics count
python -m pytest            # now also runs the ClickHouse verification tests
```

To start again from the seeds, stop the app and delete `runtime/`. It is recreated on the next run.

## Security demo

See [docs/demo.md](docs/demo.md) for a five-minute walkthrough. In short: pick `U_EMP01`, try the
blocked actions, then check *My activity*; pick `M001`, approve L001, and try to approve L002
(another team's request).

## Limitations — read before reusing

- **Simulated identities, not authentication.** Anyone can pick any identity from the sidebar.
  A real deployment needs SSO, with the backend taking the identity from the verified session.
- **Authorization is enforced in the application.** It is tested, default-deny and server-side,
  but the database does not enforce it.
- **ClickHouse admin connection.** The app connects as the ClickHouse `default` admin user, which
  can read every row. Anyone with that password can bypass the app's scoping. Production should
  use a read-only user limited to this table, ideally with row policies.
- **Hidden controls are not data-loss prevention.** Users can still screenshot or copy what is on
  screen.
- **No AI agent runs inside the app.** It makes no model calls. `tests/malicious_document.json` is
  a prompt-injection test fixture kept for future model integration.
- Local JSON files with a process-level lock suit a single-process demo, not concurrent servers.

## Tests

`python -m pytest` runs the permission, manager, UI and analytics tests. With ClickHouse
configured, it also checks: the uploaded row count matches the source; a repeat upload adds
nothing; E001 cannot retrieve E011; M001 sees only E001–E010; M002 sees only E011–E020; and
switching users never returns another user's cached rows.
