from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from supabase import create_client

TABLE = "tempering_log"
CHECKS_TABLE = "rotameter_checks"
TZ = ZoneInfo("Asia/Kolkata")
MIN_TEMPERING_HOURS = 10  # milling shouldn't normally start before this many hours
SECONDS_IN_READING = 30   # the rotameter check window

SHIFT_WINDOWS = {
    # shift: (start_time, start_day_offset, end_time, end_day_offset)
    "A": (dtime(8, 0), 0, dtime(20, 0), 0),
    "B": (dtime(20, 0), 0, dtime(8, 0), 1),
}

DISPLAY_COLUMNS = {
    "bin_no": "Bin No",
    "shift": "Shift",
    "temp_start": "Temp start time",
    "temp_end": "Temp End time",
    "milling_start": "Milling start time",
    "milling_end": "Milling end time",
    "total_tempering_minutes": "Total tempering time",
    "water_lph": "Water in lph",
    "blend": "Blend",
    "status": "Status",
}

CHECK_DISPLAY_COLUMNS = {
    "bin_no": "Bin No",
    "checked_at": "Checked at",
    "water_in_30s": "Water in 30 secs",
    "water_per_hour": "Water per hour",
    "error_value": "Error value",
}

st.set_page_config(page_title="Tempering & Milling Log", layout="centered")

st.markdown("""
<style>
button[kind="primary"], button[kind="secondary"] { min-height: 3em; font-size: 1.1rem; }
div[data-testid="stNumberInput"] input { font-size: 1.3rem; text-align: center; }
div[data-testid="stSelectbox"] div[data-baseweb="select"] { font-size: 1.1rem; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_client():
    return create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])


def datetime_input(label: str, key: str) -> datetime:
    """Date and time pickers side by side, returned as one timezone-aware datetime.
    If session_state already holds a value for this key (e.g. pre-filled from a
    Shift selection), that value wins over the 'now' default."""
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    col_date, col_time = st.columns(2)
    d = col_date.date_input(f"{label} (date)", value=now.date(), key=f"{key}_d", format="DD/MM/YYYY")
    t = col_time.time_input(f"{label} (time)", value=now.time(), key=f"{key}_t", step=60)
    return datetime.combine(d, t, tzinfo=TZ)


def apply_shift_defaults(shift: str):
    """Pre-fill the tempering start/end pickers to match the chosen shift's
    window, only when the shift selection actually changes."""
    if st.session_state.get("_last_shift") == shift:
        return
    st.session_state["_last_shift"] = shift
    if shift in SHIFT_WINDOWS:
        start_t, start_off, end_t, end_off = SHIFT_WINDOWS[shift]
        today = datetime.now(TZ).date()
        st.session_state["ts_d"] = today + timedelta(days=start_off)
        st.session_state["ts_t"] = start_t
        st.session_state["te_d"] = today + timedelta(days=end_off)
        st.session_state["te_t"] = end_t


def fmt_duration(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    return f"{minutes // 60} h {minutes % 60:02d} min"


def fmt_ts(iso_value) -> str:
    if not iso_value:
        return "—"
    return pd.to_datetime(iso_value, utc=True).tz_convert(TZ).strftime("%d-%b-%Y %H:%M")


def fetch_pending_bins() -> list[dict]:
    """Bins that finished tempering but have no milling times logged yet.
    Also used to populate the Rotameter calibration tab's bin picker."""
    return (
        get_client().table(TABLE).select("id,bin_no,temp_start,water_lph")
        .is_("milling_start", "null").order("id").execute().data
    )


def fetch_latest(n: int = 10) -> list[dict]:
    return (
        get_client().table(TABLE).select("*").order("id", desc=True).limit(n).execute().data
    )


def fetch_all() -> list[dict]:
    rows, start = [], 0
    while True:
        page = (
            get_client().table(TABLE).select("*").order("id")
            .range(start, start + 999).execute().data
        )
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def fetch_recent_checks(n: int = 10) -> list[dict]:
    return (
        get_client().table(CHECKS_TABLE).select("*, tempering_log(bin_no)")
        .order("id", desc=True).limit(n).execute().data
    )


def to_table(rows: list[dict]) -> pd.DataFrame:
    """Turn database rows into a display table, times shown in IST."""
    out = []
    for r in rows:
        out.append({
            "bin_no": r["bin_no"],
            "shift": r.get("shift") or "—",
            "temp_start": fmt_ts(r["temp_start"]),
            "temp_end": fmt_ts(r["temp_end"]),
            "milling_start": fmt_ts(r.get("milling_start")),
            "milling_end": fmt_ts(r.get("milling_end")),
            "total_tempering_minutes": fmt_duration(r.get("total_tempering_minutes")),
            "water_lph": r.get("water_lph"),
            "blend": r["blend"],
            "status": "Complete" if r.get("milling_start") else "Awaiting milling",
        })
    return pd.DataFrame(out)[list(DISPLAY_COLUMNS)].rename(columns=DISPLAY_COLUMNS)


def checks_to_table(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        linked = r.get("tempering_log") or {}
        out.append({
            "bin_no": linked.get("bin_no", "—"),
            "checked_at": fmt_ts(r.get("checked_at")),
            "water_in_30s": r.get("water_in_30s"),
            "water_per_hour": r.get("water_per_hour"),
            "error_value": r.get("error_value"),
        })
    return pd.DataFrame(out)[list(CHECK_DISPLAY_COLUMNS)].rename(columns=CHECK_DISPLAY_COLUMNS)


st.title("Tempering & milling log")

tab_temper, tab_mill, tab_rota = st.tabs(
    ["① Start tempering", "② Start milling", "③ Rotameter calibration"]
)

# ---------------------------------------------------------------- tempering
with tab_temper:
    st.caption("Log a bin as soon as tempering begins and ends. Milling is logged later, separately.")

    bin_no = st.selectbox("Bin No", ["Select", "6", "7", "8"])
    shift = st.selectbox("Shift", ["Select", "A", "B"])
    apply_shift_defaults(shift)

    with st.form("temper_entry", clear_on_submit=True):
        blend = st.selectbox("Blend", ["Select", "Select Sbt", "MP"])
        water = st.number_input("Water in lph", min_value=0.0, step=1.0, format="%.1f")

        temp_start = datetime_input("Temp start time", "ts")
        temp_end = datetime_input("Temp end time", "te")

        submitted = st.form_submit_button("Save tempering entry", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if bin_no == "Select":
            errors.append("Please select a Bin No.")
        if shift == "Select":
            errors.append("Please select a Shift.")
        if blend == "Select":
            errors.append("Please select a Blend.")
        if temp_end <= temp_start:
            errors.append("Temp end time must be after temp start time.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                get_client().table(TABLE).insert({
                    "bin_no": bin_no,
                    "shift": shift,
                    "temp_start": temp_start.isoformat(),
                    "temp_end": temp_end.isoformat(),
                    "water_lph": water,
                    "blend": blend,
                }).execute()
                st.success("Tempering entry saved. Total tempering time will show once milling starts.")
            except Exception as exc:
                st.error(f"Not saved: {exc}")

# -------------------------------------------------------------------- milling
with tab_mill:
    st.caption("Select a bin that has finished tempering, then log when milling starts and ends.")
    try:
        pending = fetch_pending_bins()
    except Exception as exc:
        pending = []
        st.error(f"Could not load pending bins: {exc}")

    if not pending:
        st.info("No bins are currently waiting for milling.")
    else:
        options = {
            r["id"]: f"Bin {r['bin_no']} — tempering started {fmt_ts(r['temp_start'])}"
            for r in pending
        }
        pending_by_id = {r["id"]: r for r in pending}

        with st.form("mill_entry", clear_on_submit=True):
            chosen_id = st.selectbox(
                "Bin waiting for milling", list(options.keys()), format_func=lambda i: options[i]
            )
            mill_start = datetime_input("Milling start time", "ms")
            mill_end = datetime_input("Milling end time", "me")
            submitted_mill = st.form_submit_button("Save milling entry", type="primary", use_container_width=True)

        if submitted_mill:
            errors = []
            if mill_end <= mill_start:
                errors.append("Milling end time must be after milling start time.")

            temp_start_dt = pd.to_datetime(pending_by_id[chosen_id]["temp_start"], utc=True).tz_convert(TZ)
            if mill_start < temp_start_dt + timedelta(hours=MIN_TEMPERING_HOURS):
                st.warning(
                    f"Milling is starting less than {MIN_TEMPERING_HOURS} hours after tempering began "
                    f"for this bin. Saving anyway, but double-check the times."
                )

            if errors:
                for e in errors:
                    st.error(e)
            else:
                total_minutes = int((mill_start - temp_start_dt).total_seconds() // 60)
                try:
                    get_client().table(TABLE).update({
                        "milling_start": mill_start.isoformat(),
                        "milling_end": mill_end.isoformat(),
                        "total_tempering_minutes": total_minutes,
                    }).eq("id", chosen_id).execute()
                    st.success(
                        f"Milling entry saved for {options[chosen_id]}. "
                        f"Total tempering time: {fmt_duration(total_minutes)}."
                    )
                except Exception as exc:
                    st.error(f"Not saved: {exc}")

# ------------------------------------------------------------- rotameter check
with tab_rota:
    st.caption(
        "Optional: check the actual water flow against the Water in lph value "
        "set for a bin while it's tempering."
    )
    try:
        checkable = fetch_pending_bins()
    except Exception as exc:
        checkable = []
        st.error(f"Could not load bins: {exc}")

    if not checkable:
        st.info("No bins are currently tempering.")
    else:
        rota_options = {
            r["id"]: f"Bin {r['bin_no']} — tempering started {fmt_ts(r['temp_start'])}"
            for r in checkable
        }
        rota_by_id = {r["id"]: r for r in checkable}

        with st.form("rota_check", clear_on_submit=True):
            rota_id = st.selectbox(
                "Bin to check", list(rota_options.keys()), format_func=lambda i: rota_options[i]
            )
            water_30s = st.number_input(
                "Water in 30 secs", min_value=0.0, step=0.1, format="%.2f"
            )
            submitted_rota = st.form_submit_button(
                "Save calibration check", type="primary", use_container_width=True
            )

        if submitted_rota:
            target_lph = rota_by_id[rota_id].get("water_lph")
            water_per_hour = water_30s * (3600 / SECONDS_IN_READING)
            error_value = (target_lph or 0) - water_per_hour
            try:
                get_client().table(CHECKS_TABLE).insert({
                    "tempering_id": rota_id,
                    "water_in_30s": water_30s,
                    "water_per_hour": water_per_hour,
                    "error_value": error_value,
                }).execute()
                st.success(
                    f"Saved. Water per hour: {water_per_hour:.1f} lph — "
                    f"Error value: {error_value:+.1f} lph (target was {target_lph})."
                )
            except Exception as exc:
                st.error(f"Not saved: {exc}")

    st.subheader("Recent calibration checks")
    try:
        recent_checks = fetch_recent_checks(10)
        if recent_checks:
            st.dataframe(checks_to_table(recent_checks), hide_index=True, use_container_width=True)
        else:
            st.info("No calibration checks logged yet.")
    except Exception as exc:
        st.error(f"Could not load calibration checks: {exc}")

# ------------------------------------------------------------------ history
st.subheader("Latest entries")
try:
    latest = fetch_latest(10)
    if latest:
        st.dataframe(to_table(latest), hide_index=True, use_container_width=True)
    else:
        st.info("No entries yet. Save the first one above.")
except Exception as exc:
    st.error(f"Could not load entries: {exc}")

st.subheader("Backup")
st.caption("The free Supabase plan has no automatic backups. Download a copy regularly.")
if st.button("Prepare CSV of all entries"):
    try:
        all_rows = fetch_all()
        if all_rows:
            csv = to_table(all_rows).to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, file_name="tempering_log.csv", mime="text/csv")
        else:
            st.info("Nothing to download yet.")
    except Exception as exc:
        st.error(f"Could not prepare the file: {exc}")
