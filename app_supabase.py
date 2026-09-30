from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from supabase import create_client

TABLE = "tempering_log"
TZ = ZoneInfo("Asia/Kolkata")
MIN_TEMPERING_HOURS = 10  # milling shouldn't normally start before this many hours

DISPLAY_COLUMNS = {
    "bin_no": "Bin No",
    "temp_start": "Temp start time",
    "temp_end": "Temp End time",
    "milling_start": "Milling start time",
    "milling_end": "Milling end time",
    "total_tempering_minutes": "Total tempering time",
    "water_lph": "Water in lph",
    "blend": "Blend",
    "status": "Status",
}

st.set_page_config(page_title="Tempering & Milling Log", layout="centered")


@st.cache_resource
def get_client():
    return create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])


def datetime_input(label: str, key: str) -> datetime:
    """Date and time pickers side by side, returned as one timezone-aware datetime."""
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    col_date, col_time = st.columns(2)
    d = col_date.date_input(f"{label} (date)", value=now.date(), key=f"{key}_d", format="DD/MM/YYYY")
    t = col_time.time_input(f"{label} (time)", value=now.time(), key=f"{key}_t", step=60)
    return datetime.combine(d, t, tzinfo=TZ)


def fmt_duration(minutes: int) -> str:
    return f"{minutes // 60} h {minutes % 60:02d} min"


def fmt_ts(iso_value) -> str:
    if not iso_value:
        return "—"
    return pd.to_datetime(iso_value, utc=True).tz_convert(TZ).strftime("%d-%b-%Y %H:%M")


def fetch_pending_bins() -> list[dict]:
    """Bins that finished tempering but have no milling times logged yet."""
    return (
        get_client().table(TABLE).select("id,bin_no,temp_start,temp_end")
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


def to_table(rows: list[dict]) -> pd.DataFrame:
    """Turn database rows into a display table, times shown in IST."""
    out = []
    for r in rows:
        out.append({
            "bin_no": r["bin_no"],
            "temp_start": fmt_ts(r["temp_start"]),
            "temp_end": fmt_ts(r["temp_end"]),
            "milling_start": fmt_ts(r.get("milling_start")),
            "milling_end": fmt_ts(r.get("milling_end")),
            "total_tempering_minutes": fmt_duration(r["total_tempering_minutes"]),
            "water_lph": r["water_lph"],
            "blend": r["blend"],
            "status": "Complete" if r.get("milling_start") else "Awaiting milling",
        })
    return pd.DataFrame(out)[list(DISPLAY_COLUMNS)].rename(columns=DISPLAY_COLUMNS)


st.title("Tempering & milling log")

tab_temper, tab_mill = st.tabs(["① Start tempering", "② Start milling"])

# ---------------------------------------------------------------- tempering
with tab_temper:
    st.caption("Log a bin as soon as tempering begins and ends. Milling is logged later, separately.")
    with st.form("temper_entry", clear_on_submit=True):
        bin_no = st.selectbox("Bin No", ["Select", "6", "7", "8"])
        blend = st.selectbox("Blend", ["Select", "Select Sbt", "MP"])
        water = st.number_input("Water in lph", min_value=0.0, step=1.0, format="%.1f")

        temp_start = datetime_input("Temp start time", "ts")
        temp_end = datetime_input("Temp end time", "te")

        submitted = st.form_submit_button("Save tempering entry", type="primary", use_container_width=True)

    if submitted:
        errors = []
        if bin_no == "Select":
            errors.append("Please select a Bin No.")
        if blend == "Select":
            errors.append("Please select a Blend.")
        if temp_end <= temp_start:
            errors.append("Temp end time must be after temp start time.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            minutes = int((temp_end - temp_start).total_seconds() // 60)
            try:
                get_client().table(TABLE).insert({
                    "bin_no": bin_no,
                    "temp_start": temp_start.isoformat(),
                    "temp_end": temp_end.isoformat(),
                    "total_tempering_minutes": minutes,
                    "water_lph": water,
                    "blend": blend,
                }).execute()
                st.success(f"Tempering entry saved. Total tempering time: {fmt_duration(minutes)}.")
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
            r["id"]: f"Bin {r['bin_no']} — tempering ended {fmt_ts(r['temp_end'])}"
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
                try:
                    get_client().table(TABLE).update({
                        "milling_start": mill_start.isoformat(),
                        "milling_end": mill_end.isoformat(),
                    }).eq("id", chosen_id).execute()
                    st.success(f"Milling entry saved for {options[chosen_id]}.")
                except Exception as exc:
                    st.error(f"Not saved: {exc}")

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