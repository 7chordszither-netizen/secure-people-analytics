"""Streamlit UI for employee and manager self-service. All checks happen in people_app.service."""
import base64
from html import escape

import pandas as pd
import streamlit as st

from people_app.analytics import SOURCE_CLICKHOUSE, ClickHouseWorkforce
from people_app.clickhouse_db import ConfigError, load_settings
from people_app.service import AccessDenied, AuditFailure, ConflictError, PeopleService, ValidationError

st.set_page_config(page_title="Secure People Data", layout="wide")

HANDLED = (AccessDenied, AuditFailure, ConflictError, ValidationError)

# Tables use st.table and charts are plain SVG via st.html. Neither renders Streamlit's element
# toolbar, so there is no download, "Show data", spec copy or fullscreen control to remove.
# st.dataframe and st.*_chart would bring those controls back.


def show_table(data):
    st.table(pd.DataFrame(data).style.hide(axis="index").format(precision=1))


def show_bar_chart(labels, values, x_label: str, y_label: str):
    """Static SVG bar chart shown as an <img>. st.html's sanitizer strips inline <svg>, so the SVG is
    embedded as a data URI. Text is mid-grey so it reads on light and dark themes."""
    labels, values = [str(v) for v in labels], [float(v) for v in values]
    w, h, left, bottom, top = 480, 260, 44, 44, 16
    plot_w, plot_h = w - left - 8, h - top - bottom
    top_value = max(values + [1.0])
    step = plot_w / max(len(values), 1)
    parts = []
    for i in range(5):
        v = top_value * i / 4
        y = top + plot_h - plot_h * i / 4
        parts.append(f'<line x1="{left}" x2="{w - 8}" y1="{y:.1f}" y2="{y:.1f}" stroke="#808495" '
                     f'stroke-opacity="0.15"/><text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{v:.0f}</text>')
    for i, (label, v) in enumerate(zip(labels, values)):
        bh = plot_h * max(v, 0.0) / top_value
        x = left + i * step + step * 0.15
        cx = left + i * step + step / 2
        parts.append(f'<rect x="{x:.1f}" y="{top + plot_h - bh:.1f}" width="{step * 0.7:.1f}" height="{bh:.1f}" '
                     f'fill="#3b82c4" rx="2"/>')
        parts.append(f'<text x="{cx:.1f}" y="{top + plot_h + 14}" text-anchor="middle">{escape(label)}</text>')
    parts.append(f'<text x="{left + plot_w / 2:.1f}" y="{h - 6}" text-anchor="middle">{escape(x_label)}</text>')
    parts.append(f'<text transform="translate(12 {top + plot_h / 2:.1f}) rotate(-90)" '
                 f'text-anchor="middle">{escape(y_label)}</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
           f'font-family="sans-serif" font-size="10" fill="#808495">'
           + "".join(parts) + "</svg>")
    alt = f"{y_label} by {x_label}: " + ", ".join(f"{l} {v:g}" for l, v in zip(labels, values))
    st.html(f'<img src="data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}" '
            f'alt="{escape(alt)}" style="width: 100%; max-width: {w}px; height: auto">')


@st.cache_resource
def get_service() -> PeopleService:
    # PTO/overtime metrics come from ClickHouse when .env is complete; everything else stays local.
    try:
        load_settings()
    except ConfigError:
        return PeopleService()
    return PeopleService(analytics=ClickHouseWorkforce())


def show_source(source: str):
    """Claim ClickHouse only when this request's rows actually came from a successful query."""
    if source == SOURCE_CLICKHOUSE:
        st.caption("Analytics source: ClickHouse")
    elif source == "local":
        st.caption("Analytics source: local data (ClickHouse not configured)")
    else:
        st.warning(f"Analytics source: {source}. Showing local seed data.")


svc = get_service()
identities = svc.demo_identities()
all_employee_ids = [a.employee_id for a in identities if a.role == "employee"]

st.title("Secure People Data")
st.caption("Demo only. Picking an identity simulates a login; it is NOT authentication.")

user_id = st.sidebar.selectbox(
    "Simulated identity",
    [a.user_id for a in identities],
    format_func=lambda uid: f"{uid} — {svc.get_actor(uid).display_name} ({svc.get_actor(uid).role})",
)
# The UI passes only the user id; the backend resolves role, employee id and scope.
actor = svc.get_actor(user_id)
st.sidebar.markdown(f"**Role:** {actor.role}  \n**Employee ID:** {actor.employee_id or '—'}")


def show_error(err: Exception):
    if isinstance(err, AccessDenied):
        st.error(f"Denied by backend: `{err.reason_code}`. This attempt was logged.")
    elif isinstance(err, AuditFailure):
        st.error("Blocked: audit log unavailable, so no change was made.")
    else:
        st.warning(str(err))


def blocked_buttons(attempts: list[tuple[str, callable]]):
    st.write("These buttons call the real backend functions with targets or actions you are not allowed. "
             "They exist to show that the backend, not the UI, blocks them.")
    cols = st.columns(len(attempts))
    for col, (label, fn) in zip(cols, attempts):
        if col.button(label, use_container_width=True):
            try:
                fn()
                st.error("Unexpected: action was allowed!")
            except HANDLED as e:
                show_error(e)


def activity_tab():
    st.caption("You only see your own activity. Full logs are limited to the security auditor; "
               "values are never written to the activity log.")
    st.subheader("My activity log")
    try:
        ev = svc.view_activity_log(actor, 100)
        if ev:
            show_table(ev)
        else:
            st.info("No events yet.")
    except HANDLED as e:
        show_error(e)
    st.subheader("Change history" + (" for my record" if actor.role == "employee" else " — my decisions"))
    try:
        hist = svc.view_change_history(actor)
        if hist:
            show_table(hist)
        else:
            st.info("No changes yet.")
    except HANDLED as e:
        show_error(e)


# ---------------- employee ----------------

def employee_view():
    tab_me, tab_try, tab_activity = st.tabs(["My details", "Try a blocked action", "My activity"])
    with tab_me:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("My contact details")
            try:
                contact = svc.view_contact(actor, actor.employee_id)
                st.write(f"**Address:** {contact['address']}")
                st.write(f"**Phone:** {contact['phone']}")
                with st.form("edit_contact"):
                    new_address = st.text_input("Address", contact["address"])
                    new_phone = st.text_input("Phone", contact["phone"])
                    if st.form_submit_button("Save changes"):
                        changed = False
                        for field, value in (("address", new_address), ("phone", new_phone)):
                            if value.strip() == contact[field]:
                                continue
                            try:
                                changed |= svc.edit_contact(actor, actor.employee_id, field, value)["changed"]
                            except HANDLED as e:
                                show_error(e)
                        if changed:
                            st.rerun()
            except HANDLED as e:
                show_error(e)
        with col2:
            st.subheader("My PTO (read-only)")
            try:
                pto = svc.view_pto(actor, actor.employee_id)
                a, b = st.columns(2)
                a.metric("Available hours", pto["available_hours"])
                b.metric("Reserved by approved leave", pto["reserved_hours"])
                show_table(pto["months"])
                show_source(pto["source"])
                st.caption("PTO is maintained by the system. Approved leave reserves hours; it is not deducted twice.")
            except HANDLED as e:
                show_error(e)
    with tab_try:
        others = [e for e in all_employee_ids if e != actor.employee_id]
        target = st.selectbox("Other employee", others)
        blocked_buttons([
            ("View their contact", lambda: svc.view_contact(actor, target)),
            ("View their PTO", lambda: svc.view_pto(actor, target)),
            ("Edit their phone", lambda: svc.edit_contact(actor, target, "phone", "+1-202-555-0000")),
            ("Set my PTO to 100h", lambda: svc.edit_pto(actor, actor.employee_id, "pto_closing_hours", 100)),
            ("Try unauthorized leave approval", lambda: svc.decide_leave(actor, "L001", "approve")),
            ("Export my data", lambda: svc.share(actor, actor.employee_id, "contact")),
        ])
    with tab_activity:
        activity_tab()


# ---------------- manager ----------------

def manager_view():
    tab_team, tab_leave, tab_try, tab_activity = st.tabs(
        ["Team overview", "Leave requests", "Try a blocked action", "My activity"])

    with tab_team:
        try:
            team = svc.view_team_analytics(actor)
            df = pd.DataFrame(team["rows"])
            show_source(team["source"])
        except HANDLED as e:
            show_error(e)
            df = None
        if df is not None and not df.empty:
            latest_month = df["month"].max()
            latest = df[df["month"] == latest_month].copy()
            latest["available_hours"] = latest["pto_closing_hours"] - latest["reserved_hours"]
            try:
                pending = sum(r["status"] == "pending" for r in svc.list_leave_requests(actor))
            except HANDLED:
                pending = "—"

            st.subheader(f"Direct reports — {latest_month}")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Direct reports", len(latest))
            m2.metric("Avg available PTO (h)", f"{latest['available_hours'].mean():.1f}")
            m3.metric(f"Team overtime {latest_month} (h)", int(latest["overtime_hours"].sum()))
            m4.metric("Pending leave requests", pending)

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Available PTO hours by employee** (latest closing − approved leave)")
                show_bar_chart(latest["employee_id"], latest["available_hours"], "Employee", "Hours")
            with c2:
                st.markdown("**Team overtime hours by month**")
                monthly = df.groupby("month", as_index=False)["overtime_hours"].sum()
                show_bar_chart(monthly["month"], monthly["overtime_hours"], "Month", "Hours")

            st.markdown("**Latest month by employee**")
            show_table(latest[["employee_id", "display_name", "pto_closing_hours", "reserved_hours",
                               "available_hours", "overtime_hours"]])
            with st.expander("All monthly rows"):
                show_table(df)
            st.caption("Read-only. PTO and overtime are system-maintained; there are no direct edits.")

    with tab_leave:
        st.subheader("Leave requests assigned to you")
        try:
            requests = svc.list_leave_requests(actor)
        except HANDLED as e:
            show_error(e)
            requests = []
        if not requests:
            st.info("No leave requests assigned to you.")
        for r in requests:
            with st.container(border=True):
                info, act = st.columns([3, 2])
                info.markdown(f"**{r['request_id']}** — {r['display_name']} ({r['employee_id']})  \n"
                              f"Start {r['start_date']} · {r['hours']} hours · status **{r['status']}**")
                if r["status"] == "pending":
                    b1, b2 = act.columns(2)
                    for col, decision in ((b1, "approve"), (b2, "deny")):
                        if col.button(decision.capitalize(), key=f"{decision}-{r['request_id']}",
                                      use_container_width=True):
                            try:
                                svc.decide_leave(actor, r["request_id"], decision)
                                st.rerun()
                            except HANDLED as e:
                                show_error(e)
        st.caption("Approving reserves the hours against the employee's PTO. The monthly PTO "
                   "records are not edited, so hours are never deducted twice.")

    with tab_try:
        team = set(actor.scope_ids)
        others = [e for e in all_employee_ids if e not in team]
        target = st.selectbox("Employee on another team", others)
        own_member = sorted(team)[0]
        foreign_request = "L002" if actor.user_id == "M001" else "L001"
        blocked_buttons([
            ("View their PTO", lambda: svc.view_pto(actor, target)),
            ("View their overtime", lambda: svc.view_overtime(actor, target)),
            (f"Approve {foreign_request} (other team)", lambda: svc.decide_leave(actor, foreign_request, "approve")),
            (f"Set {own_member} PTO to 200h", lambda: svc.edit_pto(actor, own_member, "pto_closing_hours", 200)),
            (f"Edit {own_member} overtime", lambda: svc.edit_workforce(actor, own_member, "overtime_hours", 0)),
            (f"View {own_member} contact", lambda: svc.view_contact(actor, own_member)),
            ("Export team data", lambda: svc.share(actor, "direct_team", "workforce")),
        ])

    with tab_activity:
        activity_tab()


if actor.role == "employee":
    employee_view()
elif actor.role == "manager":
    manager_view()
else:
    st.info("This role has no screens yet.")
