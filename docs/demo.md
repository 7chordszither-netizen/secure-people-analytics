# Demo walkthrough (about 5 minutes)

All people and records are synthetic. Picking an identity simulates a login; it is not authentication.

Start the app with `streamlit run app.py` and open http://localhost:8501.

## 1. Employee self-service — `U_EMP01`

1. In the sidebar, choose **U_EMP01 — Demo Employee 01 (employee)**.
2. **My details**: change the phone number and click **Save changes**. The change is validated and recorded.
3. Note the PTO table and the caption under it: **Analytics source: ClickHouse** (or the labelled local fallback).
4. **Try a blocked action**: click each button. Every one is refused by the backend with a reason code:
   - View or edit another employee's contact or PTO → `OUT_OF_SCOPE`
   - Set my PTO to 100h → `PTO_SYSTEM_MAINTAINED`
   - Try unauthorized leave approval → `NO_MATCHING_RULE`
   - Export my data → `SHARING_DISABLED`
5. **My activity**: the phone change and every denied attempt appear in the log. Values are never logged, only field names.

## 2. Manager — `M001`

1. Choose **M001 — M001 (manager)**.
2. **Team overview**: only E001–E010 appear, with the ClickHouse source caption. The charts and
   tables have no download, "show data" or fullscreen controls.
3. **Leave requests**: approve or deny **L001** (E001). It changes once; a second decision is rejected.
4. **Try a blocked action**: approve **L002** (M002's team), view a non-report's PTO or overtime,
   edit overtime, view a report's contact details, export team data. All are denied and logged.

## 3. Isolation check — `M002`

Choose **M002**. Only E011–E020 appear. Switch back and forth between M001, M002 and an employee:
each identity only ever sees its own scope, including from the analytics cache.

## Reset

Stop the app and delete the `runtime/` folder to return to the seed data. It is recreated on the next start.
