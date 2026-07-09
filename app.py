"""
Hexagon Service Assistant - Service Centre Appointment Booking
==============================================================
Book / reschedule / cancel service appointments, with an AI chat assistant
and a password-protected admin view (day / week / month).

Storage:   Google Sheets (service account creds in Streamlit Secrets)
AI layer:  Anthropic Claude (chat assistant only - core booking is
           deterministic and works even if the API is down)
Slots:     Mon-Fri, 09:00-17:00, hourly. Indian public holidays blocked.
"""

from __future__ import annotations

import base64
import json
import re
import smtplib
import time
import uuid
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import gspread
import holidays
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configuration (edit these lists without touching the rest of the code)
# ---------------------------------------------------------------------------

BRANDING = {
    "navy_bg": "#0C2C40",
    "navy_panel": "#123B54",
    "lime": "#C9DD28",
    "cyan": "#6FD6FF",
    "accent": "#0096D6",
}

SERVICE_TYPES = [
    "Calibration",
    "Repair",
    "Preventive Maintenance",
    "Installation & Commissioning",
    "AMC Visit",
    "Software / Firmware Update",
]

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Andaman & Nicobar", "Chandigarh",
    "Dadra & Nagar Haveli and Daman & Diu", "Delhi", "Jammu & Kashmir",
    "Ladakh", "Lakshadweep", "Puducherry",
]

# Hourly slots, Mon-Fri 09:00-17:00 (last slot 16:00-17:00)
SLOTS = [f"{h:02d}:00 - {h + 1:02d}:00" for h in range(9, 17)]
SLOT_CAPACITY = 2            # bookings allowed per slot (service bays/engineers)
BOOKING_WINDOW_DAYS = 60     # how far ahead bookings are allowed
MIN_LEAD_DAYS = 1            # earliest bookable day = tomorrow

# National holidays via the `holidays` library; add state/company-specific
# closures here as ISO date strings, e.g. "2026-11-12"
HOLIDAY_EXTRA: set[str] = set()

IN_HOLIDAYS = holidays.country_holidays("IN", years=range(2025, 2031))

SHEET_COLUMNS = [
    "appointment_id", "status", "name", "mobile", "company", "email",
    "state", "city", "serial_number", "service_type", "date", "time_slot",
    "created_at", "updated_at",
]

CLAUDE_MODEL = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# Page config + Hexagon branding
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Hexagon Service Assistant",
    page_icon="⬡",
    layout="wide",
)

BG = BRANDING["navy_bg"]
PANEL = BRANDING["navy_panel"]
LIME = BRANDING["lime"]
CYAN = BRANDING["cyan"]
ACCENT = BRANDING["accent"]
INPUT_BG = "#1D465E"
TEXT = "#D7E3EC"

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@300;400;500;600;700;800&display=swap');

    html, body, [class*="css"], .stApp, p, li, label, input, textarea, button, select {{
        font-family: 'Hanken Grotesk', sans-serif !important;
    }}
    /* Material Symbols exception - removing this breaks Streamlit's icons */
    [data-testid="stIconMaterial"], .material-symbols-rounded, .material-symbols-outlined {{
        font-family: 'Material Symbols Rounded', 'Material Symbols Outlined' !important;
    }}

    .stApp {{
        background:
            radial-gradient(1100px 500px at 80% -10%, rgba(0,150,214,0.25), transparent 60%),
            radial-gradient(900px 500px at -10% 110%, rgba(201,221,40,0.10), transparent 55%),
            {BG};
        color: {TEXT};
    }}
    .stApp::before {{
        content: "";
        position: fixed; top: 0; left: 0; right: 0; height: 4px; z-index: 1000;
        background: linear-gradient(90deg, {LIME}, {CYAN}, {LIME});
        background-size: 200% 100%;
        animation: hexbar 6s linear infinite;
    }}
    @keyframes hexbar {{ 0% {{background-position: 0% 0;}} 100% {{background-position: 200% 0;}} }}

    h1, h2, h3, h4 {{ color: #FFFFFF !important; }}
    .hex-title {{
        font-size: 2rem; font-weight: 800; margin-bottom: 0;
        background: linear-gradient(90deg, #FFFFFF 30%, {CYAN});
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }}
    .hex-sub {{ color: {TEXT}; opacity: 0.85; margin-top: 0.2rem; }}

    /* Solid opaque inputs - never inherit the viewer's OS theme */
    .stTextInput input, .stTextArea textarea, [data-baseweb="input"] input,
    [data-baseweb="base-input"] input {{
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
        background: {INPUT_BG} !important;
        background-color: {INPUT_BG} !important;
        caret-color: {LIME};
        min-height: 2.6rem;
    }}
    [data-baseweb="input"], [data-baseweb="input"] > div {{
        background: {INPUT_BG} !important;
    }}
    .stTextInput input::placeholder {{
        color: rgba(215,227,236,0.55) !important;
        -webkit-text-fill-color: rgba(215,227,236,0.55) !important;
    }}
    [data-baseweb="select"] * {{
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }}
    [data-baseweb="select"] > div {{ background: {INPUT_BG} !important; }}
    [data-baseweb="popover"] [role="listbox"], [data-baseweb="menu"] {{ background: {PANEL} !important; }}
    [data-baseweb="popover"] [role="option"] {{ color: #FFFFFF !important; }}
    [data-baseweb="popover"] [role="option"]:hover,
    [data-baseweb="popover"] [role="option"][aria-selected="true"] {{
        background: rgba(201,221,40,0.16) !important;
    }}
    .stDateInput input {{ color: #FFFFFF !important; -webkit-text-fill-color: #FFFFFF !important;
        background: {INPUT_BG} !important; }}

    .stButton > button {{
        background: {LIME}; color: {BG} !important; font-weight: 700;
        border: none; border-radius: 8px;
    }}
    .stButton > button p {{ color: {BG} !important; }}
    .stButton > button:hover {{ background: {CYAN}; }}

    [data-testid="stSidebar"] {{ background: {PANEL}; }}
    [data-testid="stSidebar"] * {{ color: {TEXT}; }}

    .slot-free {{ color: {LIME}; font-weight: 600; }}
    .slot-full {{ color: rgba(215,227,236,0.35); text-decoration: line-through; }}

    #MainMenu, footer, [data-testid="stToolbar"] {{ visibility: hidden; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Google Sheets storage
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@st.cache_resource(show_spinner=False)
def get_worksheet():
    """Connect to the Google Sheet and return the appointments worksheet."""
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    sh = gc.open(st.secrets.get("SHEET_NAME", "Hexagon Service Appointments"))
    ws = sh.sheet1
    # Ensure the header row exists exactly once
    first_row = ws.row_values(1)
    if first_row != SHEET_COLUMNS:
        if not any(first_row):
            ws.update("A1", [SHEET_COLUMNS])
        else:
            raise RuntimeError(
                "Sheet header row doesn't match the expected columns. "
                "Clear the sheet or fix the header."
            )
    return ws


@st.cache_data(ttl=20, show_spinner=False)
def load_appointments() -> pd.DataFrame:
    ws = get_worksheet()
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=SHEET_COLUMNS)
    df = pd.DataFrame(records)
    for col in SHEET_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[SHEET_COLUMNS].astype(str)


def refresh_data():
    load_appointments.clear()


def append_booking(row: dict):
    ws = get_worksheet()
    ws.append_row([row.get(c, "") for c in SHEET_COLUMNS],
                  value_input_option="USER_ENTERED")
    refresh_data()


def update_booking(appointment_id: str, changes: dict) -> bool:
    """Update a booking row in place. Returns True if found."""
    ws = get_worksheet()
    records = ws.get_all_records()
    for i, rec in enumerate(records):
        if str(rec.get("appointment_id", "")).strip().upper() == appointment_id.strip().upper():
            row_num = i + 2  # +1 header, +1 1-indexing
            merged = {**rec, **changes,
                      "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            ws.update(f"A{row_num}:N{row_num}",
                      [[merged.get(c, "") for c in SHEET_COLUMNS]],
                      value_input_option="USER_ENTERED")
            refresh_data()
            return True
    return False


# ---------------------------------------------------------------------------
# Booking helpers
# ---------------------------------------------------------------------------

def gen_appointment_id(df: pd.DataFrame) -> str:
    existing = set(df["appointment_id"].str.upper()) if not df.empty else set()
    while True:
        cand = "HEX-" + uuid.uuid4().hex[:6].upper()
        if cand not in existing:
            return cand


def valid_mobile(m: str) -> bool:
    m = re.sub(r"\D", "", m)
    return len(m) == 10 and m[0] in "6789"


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match(e.strip()))


def is_business_day(d: date) -> tuple[bool, str]:
    """Returns (ok, reason_if_not)."""
    if d.weekday() >= 5:
        return False, "Weekends are not available - the service centre works Monday to Friday."
    if d in IN_HOLIDAYS:
        return False, f"That's a public holiday ({IN_HOLIDAYS.get(d)}) - the service centre is closed."
    if d.isoformat() in HOLIDAY_EXTRA:
        return False, "The service centre is closed on that date."
    return True, ""


def slot_counts(df: pd.DataFrame, d: date) -> dict:
    day = df[(df["date"] == d.isoformat()) & (df["status"] == "Confirmed")]
    return day["time_slot"].value_counts().to_dict()


def available_slots(df: pd.DataFrame, d: date) -> list[str]:
    counts = slot_counts(df, d)
    return [s for s in SLOTS if counts.get(s, 0) < SLOT_CAPACITY]


def make_ics(row: dict) -> bytes:
    start_h = int(row["time_slot"].split(":")[0])
    d = row["date"].replace("-", "")
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Hexagon//Service//EN\n"
        "BEGIN:VEVENT\n"
        f"UID:{row['appointment_id']}@hexagon\n"
        f"DTSTART:{d}T{start_h:02d}0000\n"
        f"DTEND:{d}T{start_h + 1:02d}0000\n"
        f"SUMMARY:Hexagon Service - {row['service_type']} ({row['appointment_id']})\n"
        f"DESCRIPTION:Machine S/N {row['serial_number']} - {row['name']} - {row['mobile']}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    ).encode()


def try_send_email(row: dict, kind: str = "confirmed") -> str:
    """Send a notification email if the visitor gave an address and SMTP is
    configured in secrets. Returns a short status string for the UI."""
    to_addr = str(row.get("email", "")).strip()
    if not to_addr:
        return "No email provided - showing on-screen confirmation only."
    if not valid_email(to_addr):
        return f"The email address '{to_addr}' looks invalid, so no mail was sent."
    sender = str(st.secrets.get("EMAIL_SENDER", "")).strip()
    app_pw = st.secrets.get("EMAIL_APP_PASSWORD", "")
    if not (sender and app_pw):
        return "Email notifications not configured yet (EMAIL_SENDER / EMAIL_APP_PASSWORD missing in secrets)."
    subject = {
        "confirmed": f"Hexagon Service Appointment Confirmed - {row['appointment_id']}",
        "rescheduled": f"Hexagon Service Appointment Rescheduled - {row['appointment_id']}",
        "cancelled": f"Hexagon Service Appointment Cancelled - {row['appointment_id']}",
    }[kind]
    body = (
        f"Dear {row['name']},\n\n"
        f"Your Hexagon service appointment has been {kind}.\n\n"
        f"Appointment ID: {row['appointment_id']}\n"
        f"Service: {row['service_type']}\n"
        f"Machine S/N: {row['serial_number']}\n"
        f"Date: {row['date']}\n"
        f"Time: {row['time_slot']}\n"
        f"Location: {row['city']}, {row['state']}\n\n"
        "Please keep the Appointment ID handy to reschedule or cancel.\n\n"
        "Hexagon Service Centre"
    )
    try:
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, sender, to_addr
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as srv:
            srv.login(sender, app_pw)
            srv.sendmail(sender, [to_addr], msg.as_string())
        return f"Confirmation email sent to {to_addr}."
    except smtplib.SMTPRecipientsRefused as e:
        detail = "; ".join(f"{r}: {c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                           for r, (c, m) in e.recipients.items())
        return f"Booking saved, but Gmail refused the recipient - {detail}"
    except smtplib.SMTPAuthenticationError:
        return ("Booking saved, but the email login failed - check EMAIL_SENDER and "
                "EMAIL_APP_PASSWORD in secrets (app password, not the normal password).")
    except Exception as e:  # noqa: BLE001 - never crash a booking over email
        return f"Booking saved, but the email could not be sent ({type(e).__name__}: {str(e)[:120]})."


# ---------------------------------------------------------------------------
# AI assistant (Claude) - optional layer on top of deterministic booking
# ---------------------------------------------------------------------------

def get_claude():
    try:
        import anthropic
        return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    except Exception:
        return None


_BOOKING_FIELDS = {
    "name": {"type": "string", "description": "Customer full name"},
    "mobile": {"type": "string", "description": "10-digit Indian mobile number"},
    "company": {"type": "string", "description": "Company name, empty string if not given"},
    "email": {"type": "string", "description": "Email for confirmation mail, empty if not given"},
    "state": {"type": "string", "description": "Indian state"},
    "city": {"type": "string", "description": "City"},
    "serial_number": {"type": "string", "description": "Machine serial number"},
    "service_type": {"type": "string", "enum": SERVICE_TYPES},
    "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
    "time_slot": {"type": "string", "enum": SLOTS},
}

AGENT_TOOLS = [
    {
        "name": "check_availability",
        "description": ("Check which time slots are free on a given date. ALWAYS call "
                        "this before offering, agreeing to, or proposing any slot."),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "find_bookings",
        "description": ("Look up confirmed bookings by Appointment ID (e.g. HEX-1A2B3C) "
                        "or 10-digit mobile number. Call this before any reschedule or "
                        "cancellation."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "propose_booking",
        "description": ("Propose a NEW booking. Shows the user a Confirm button - the "
                        "system books only after they press it. Call only when every "
                        "required field is collected AND check_availability showed the "
                        "slot free."),
        "input_schema": {
            "type": "object",
            "properties": _BOOKING_FIELDS,
            "required": ["name", "mobile", "state", "city", "serial_number",
                          "service_type", "date", "time_slot"],
        },
    },
    {
        "name": "propose_reschedule",
        "description": ("Propose moving an existing booking to a new date/slot. Shows a "
                        "Confirm button. Call find_bookings and check_availability first."),
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "new_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "new_time_slot": {"type": "string", "enum": SLOTS},
            },
            "required": ["appointment_id", "new_date", "new_time_slot"],
        },
    },
    {
        "name": "propose_cancellation",
        "description": ("Propose cancelling an existing booking. Shows a Confirm button. "
                        "Call find_bookings first to identify the booking."),
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "string"}},
            "required": ["appointment_id"],
        },
    },
]

AGENT_SYSTEM = """You are the Hexagon Service Agent. You operate the Hexagon service
centre appointment system on the customer's behalf, using your tools.

TODAY is {today} ({weekday}).

FACTS:
- Service centre: Monday-Friday only, hourly slots 09:00-17:00. Closed weekends
  and Indian public holidays.
- Bookings open from tomorrow up to {window} days ahead. Up to {capacity}
  bookings per slot.
- Service types: {services}.
- Required for a booking: full name, 10-digit Indian mobile, state, city,
  machine serial number, service type, date, time slot. Optional: company,
  email (an email gets a confirmation mail).

RULES:
- NEVER invent availability. Always call check_availability before mentioning
  or choosing a slot. Only offer slots the tool returned.
- For reschedule or cancellation, always call find_bookings first.
- The propose_* tools only SHOW the user a Confirm button. Never say a booking
  is confirmed, moved, or cancelled - say it is ready and ask them to press
  Confirm. A [SYSTEM NOTE] in the user's message reports what actually
  happened after they pressed it - trust that note.
- Resolve relative dates ("tomorrow", "next Tuesday") from TODAY and say the
  resolved date back to the user.
- Ask for at most two missing details per message. Warm, brief, professional
  Indian business register. No emojis.
- Only help with service centre matters."""


def _agent_system() -> str:
    today = date.today()
    return AGENT_SYSTEM.format(
        today=today.isoformat(), weekday=today.strftime("%A"),
        window=BOOKING_WINDOW_DAYS, capacity=SLOT_CAPACITY,
        services=", ".join(SERVICE_TYPES),
    )


def _find_confirmed(df: pd.DataFrame, query: str) -> pd.DataFrame:
    q = query.strip().upper()
    qm = re.sub(r"\D", "", query)
    mask = df["appointment_id"].str.upper() == q
    if len(qm) == 10:
        mask = mask | (df["mobile"] == qm)
    return df[(df["status"] == "Confirmed") & mask]


def execute_tool(name: str, args: dict) -> dict:
    """Run one agent tool. Reads execute immediately; writes only create a
    pending proposal that the user must confirm in the UI."""
    df = load_appointments()

    if name == "check_availability":
        try:
            d = date.fromisoformat(str(args.get("date", "")))
        except ValueError:
            return {"error": "Invalid date - use YYYY-MM-DD."}
        if d < date.today() + timedelta(days=MIN_LEAD_DAYS):
            return {"date": d.isoformat(), "available": False,
                    "reason": "Bookings start from tomorrow onwards."}
        if d > date.today() + timedelta(days=BOOKING_WINDOW_DAYS):
            return {"date": d.isoformat(), "available": False,
                    "reason": f"Bookings are open only {BOOKING_WINDOW_DAYS} days ahead."}
        ok, why = is_business_day(d)
        if not ok:
            return {"date": d.isoformat(), "available": False, "reason": why}
        free = available_slots(df, d)
        return {"date": d.isoformat(), "available": bool(free), "free_slots": free,
                **({} if free else {"reason": "All slots are booked that day."})}

    if name == "find_bookings":
        found = _find_confirmed(df, str(args.get("query", "")))
        return {"bookings": [
            {"appointment_id": r.appointment_id, "date": r.date,
             "time_slot": r.time_slot, "service_type": r.service_type,
             "name": r.name, "serial_number": r.serial_number}
            for r in found.head(5).itertuples()
        ]}

    if name == "propose_booking":
        problems = validate_booking(df, args)
        if problems:
            return {"accepted": False, "problems": problems}
        st.session_state.agent_pending = {"kind": "create", "data": dict(args)}
        return {"accepted": True,
                "status": "Proposal shown to the user with a Confirm button. "
                          "Ask them to review it and press Confirm."}

    if name == "propose_reschedule":
        apt = str(args.get("appointment_id", "")).strip().upper()
        row = _find_confirmed(df, apt)
        if row.empty:
            return {"accepted": False, "problems": ["No confirmed booking with that Appointment ID."]}
        current = row.iloc[0].to_dict()
        try:
            nd = date.fromisoformat(str(args.get("new_date", "")))
        except ValueError:
            return {"accepted": False, "problems": ["Invalid new date - use YYYY-MM-DD."]}
        ok, why = is_business_day(nd)
        if not ok:
            return {"accepted": False, "problems": [why]}
        if not (date.today() + timedelta(days=MIN_LEAD_DAYS) <= nd
                <= date.today() + timedelta(days=BOOKING_WINDOW_DAYS)):
            return {"accepted": False, "problems": ["New date is outside the booking window."]}
        ns = args.get("new_time_slot", "")
        free = available_slots(df, nd)
        same = (nd.isoformat() == current["date"] and ns == current["time_slot"])
        if ns not in SLOTS or (ns not in free and not same):
            return {"accepted": False,
                    "problems": ["That slot is not available.", f"Free that day: {free}"]}
        st.session_state.agent_pending = {
            "kind": "reschedule", "current": current,
            "data": {"appointment_id": current["appointment_id"],
                     "new_date": nd.isoformat(), "new_time_slot": ns}}
        return {"accepted": True,
                "status": "Reschedule proposal shown - ask the user to press Confirm."}

    if name == "propose_cancellation":
        apt = str(args.get("appointment_id", "")).strip().upper()
        row = _find_confirmed(df, apt)
        if row.empty:
            return {"accepted": False, "problems": ["No confirmed booking with that Appointment ID."]}
        st.session_state.agent_pending = {"kind": "cancel", "current": row.iloc[0].to_dict(),
                                          "data": {"appointment_id": apt}}
        return {"accepted": True,
                "status": "Cancellation proposal shown - ask the user to press Confirm."}

    return {"error": f"Unknown tool: {name}"}


def run_agent(client, api_history: list, display_log: list, max_iters: int = 8):
    """Agentic loop: call Claude, execute requested tools, feed results back,
    repeat until it answers in plain text."""
    retried_empty = False
    for _ in range(max_iters):
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=3000,
            system=_agent_system(), tools=AGENT_TOOLS, messages=api_history,
        )
        blocks = []
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                blocks.append({"type": "text", "text": b.text})
                display_log.append({"role": "assistant", "kind": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name,
                               "input": b.input})
                display_log.append({"role": "assistant", "kind": "tool",
                                    "text": f"{b.name} → {json.dumps(b.input, default=str)[:140]}"})
        if not blocks:  # adaptive-thinking starvation guard (Agent 9 lesson)
            if retried_empty:
                display_log.append({"role": "assistant", "kind": "text",
                                    "text": "Sorry, I had trouble responding - please try again, "
                                            "or use the Book appointment tab."})
                return
            retried_empty = True
            time.sleep(1)
            continue
        api_history.append({"role": "assistant", "content": blocks})
        tool_calls = [b for b in blocks if b["type"] == "tool_use"]
        if resp.stop_reason == "tool_use" and tool_calls:
            results = []
            for tc in tool_calls:
                out = execute_tool(tc["name"], tc["input"])
                results.append({"type": "tool_result", "tool_use_id": tc["id"],
                                "content": json.dumps(out, default=str)})
            api_history.append({"role": "user", "content": results})
            continue
        return
    display_log.append({"role": "assistant", "kind": "text",
                        "text": "I've reached my step limit for this request - "
                                "please rephrase or use the form tabs."})


def validate_booking(df: pd.DataFrame, b: dict) -> list[str]:
    problems = []
    if not b.get("name", "").strip():
        problems.append("Full name is required.")
    if not valid_mobile(b.get("mobile", "")):
        problems.append("A valid 10-digit Indian mobile number is required.")
    if b.get("email", "").strip() and not valid_email(b["email"]):
        problems.append("That email address doesn't look valid - please correct it "
                        "or leave the field empty.")
    if not b.get("state", "").strip() or not b.get("city", "").strip():
        problems.append("State and city are required.")
    if not b.get("serial_number", "").strip():
        problems.append("Machine serial number is required.")
    if b.get("service_type") not in SERVICE_TYPES:
        problems.append("Please choose a valid service type.")
    try:
        d = date.fromisoformat(b.get("date", ""))
        if d < date.today() + timedelta(days=MIN_LEAD_DAYS):
            problems.append("Bookings start from tomorrow onwards.")
        elif d > date.today() + timedelta(days=BOOKING_WINDOW_DAYS):
            problems.append(f"Bookings are open only {BOOKING_WINDOW_DAYS} days ahead.")
        else:
            ok, why = is_business_day(d)
            if not ok:
                problems.append(why)
            elif b.get("time_slot") not in SLOTS:
                problems.append("Please choose a valid time slot.")
            elif b["time_slot"] not in available_slots(df, d):
                problems.append("That slot has just filled up - please pick another.")
    except ValueError:
        problems.append("Please choose a valid date.")
    return problems


def do_book(df: pd.DataFrame, b: dict) -> dict:
    row = {c: str(b.get(c, "")).strip() for c in SHEET_COLUMNS}
    row["appointment_id"] = gen_appointment_id(df)
    row["status"] = "Confirmed"
    row["mobile"] = re.sub(r"\D", "", row["mobile"])
    row["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    row["updated_at"] = row["created_at"]
    append_booking(row)
    return row


def confirmation_panel(row: dict, email_status: str):
    st.success(f"Appointment confirmed - your Appointment ID is **{row['appointment_id']}**. "
               "Please save it to reschedule or cancel later.")
    st.markdown(
        f"**{row['service_type']}** for machine **{row['serial_number']}**  \n"
        f"{row['date']} · {row['time_slot']}  \n"
        f"{row['name']} · {row['mobile']} · {row['city']}, {row['state']}"
    )
    st.caption(email_status)
    st.download_button("Add to calendar (.ics)", data=make_ics(row),
                       file_name=f"{row['appointment_id']}.ics", mime="text/calendar")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _logo_html() -> str:
    """Embed logo.png from the repo root; fall back to the ⬡ mark."""
    p = Path(__file__).parent / "logo.png"
    if p.exists():
        b64 = base64.b64encode(p.read_bytes()).decode()
        return (f'<img src="data:image/png;base64,{b64}" '
                'style="height:52px;width:auto;" alt="Hexagon"/>')
    return '<span class="hex-title">⬡</span>'


@st.dialog("Admin access")
def admin_login_dialog():
    pw = st.text_input("Admin password", type="password")
    if st.button("Unlock", use_container_width=True):
        if pw and pw == st.secrets.get("ADMIN_PASSWORD", ""):
            st.session_state.admin_ok = True
            st.rerun()
        else:
            st.error("Incorrect password.")


head_l, head_r = st.columns([6, 1])
with head_l:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:14px;">{_logo_html()}'
        '<span class="hex-title">Hexagon Service Assistant</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<p class="hex-sub">Welcome to the Hexagon Service Centre - book, reschedule '
                'or cancel your service appointment.</p>', unsafe_allow_html=True)
with head_r:
    st.write("")
    if st.session_state.get("admin_ok"):
        if st.button("Exit admin", use_container_width=True):
            st.session_state.admin_ok = False
            st.rerun()
    elif st.button("Admin", use_container_width=True):
        admin_login_dialog()

# Fail fast with a friendly message if storage isn't configured
try:
    df_all = load_appointments()
except Exception as e:  # noqa: BLE001
    st.error("Could not connect to the appointments store. Check the Google Sheets "
             f"settings in Streamlit Secrets. ({type(e).__name__})")
    st.stop()

# Admin mode replaces the customer view entirely
if st.session_state.get("admin_ok"):
    st.subheader("Admin - appointments overview")
    data = df_all.copy()
    if data.empty:
        st.info("No appointments yet.")
    else:
        data["date_dt"] = pd.to_datetime(data["date"], errors="coerce")
        show_cancelled = st.toggle("Include cancelled", value=False)
        if not show_cancelled:
            data = data[data["status"] == "Confirmed"]

        view = st.radio("View", ["Day", "Week", "Month"], horizontal=True)
        if view == "Day":
            d = st.date_input("Day", value=date.today(), format="DD/MM/YYYY",
                              key="adm_day")
            sel = data[data["date_dt"].dt.date == d].sort_values("time_slot")
            st.metric("Appointments", len(sel))
        elif view == "Week":
            anchor = st.date_input("Any date in the week", value=date.today(),
                                   format="DD/MM/YYYY", key="adm_week")
            monday = anchor - timedelta(days=anchor.weekday())
            friday = monday + timedelta(days=4)
            st.caption(f"Week: {monday.strftime('%d %b')} - {friday.strftime('%d %b %Y')}")
            sel = data[(data["date_dt"].dt.date >= monday)
                       & (data["date_dt"].dt.date <= friday)]
            st.metric("Appointments this week", len(sel))
            if not sel.empty:
                per_day = sel.groupby(sel["date_dt"].dt.strftime("%a %d %b")).size()
                st.bar_chart(per_day)
            sel = sel.sort_values(["date", "time_slot"])
        else:  # Month
            months = pd.date_range(date.today().replace(day=1) - pd.DateOffset(months=2),
                                   periods=6, freq="MS")
            label = st.selectbox("Month", [m.strftime("%B %Y") for m in months],
                                 index=2)
            m_start = datetime.strptime(label, "%B %Y")
            sel = data[(data["date_dt"].dt.year == m_start.year)
                       & (data["date_dt"].dt.month == m_start.month)]
            st.metric("Appointments this month", len(sel))
            if not sel.empty:
                per_day = sel.groupby(sel["date_dt"].dt.day).size()
                st.bar_chart(per_day)
            sel = sel.sort_values(["date", "time_slot"])

        st.dataframe(
            sel[["appointment_id", "status", "date", "time_slot", "name",
                 "mobile", "company", "city", "state", "serial_number",
                 "service_type"]],
            use_container_width=True, hide_index=True)
        st.download_button(
            "Download this view (CSV)",
            data=sel.drop(columns=["date_dt"]).to_csv(index=False).encode(),
            file_name="hexagon_appointments.csv", mime="text/csv")
    st.stop()

tab_ai, tab_book, tab_manage = st.tabs(
    ["AI Agent", "Book appointment", "Manage booking"]
)

# ---------------------------------------------------------------------------
# 1) Book appointment
# ---------------------------------------------------------------------------

with tab_book:
    left, right = st.columns([3, 2], gap="large")

    with left:
        st.subheader("Your details")
        name = st.text_input("Full name *")
        mobile = st.text_input("Mobile number *", placeholder="10-digit Indian mobile")
        company = st.text_input("Company name (optional)")
        email = st.text_input("Email (optional)", placeholder="For a confirmation email")
        c1, c2 = st.columns(2)
        with c1:
            state = st.selectbox("State *", INDIAN_STATES,
                                 index=INDIAN_STATES.index("Haryana"))
        with c2:
            city = st.text_input("City *")
        serial = st.text_input("Machine serial number *")
        service = st.selectbox("Type of service *", SERVICE_TYPES)

    with right:
        st.subheader("Pick a date & time")
        st.caption("Service centre hours: Monday-Friday, 09:00-17:00. "
                   "Weekends and Indian public holidays are closed.")
        min_d = date.today() + timedelta(days=MIN_LEAD_DAYS)
        max_d = date.today() + timedelta(days=BOOKING_WINDOW_DAYS)
        chosen_date = st.date_input("Preferred date *", value=None,
                                    min_value=min_d, max_value=max_d,
                                    format="DD/MM/YYYY")
        chosen_slot = None
        if chosen_date:
            ok, why = is_business_day(chosen_date)
            if not ok:
                st.warning(why + " Please pick another date.")
            else:
                free = available_slots(df_all, chosen_date)
                counts = slot_counts(df_all, chosen_date)
                if not free:
                    st.warning("All slots are booked on this date - please pick another day.")
                else:
                    chosen_slot = st.radio(
                        "Available time slots *", free, horizontal=True,
                        help=f"Up to {SLOT_CAPACITY} bookings per slot.")
                    booked_out = [s for s in SLOTS if s not in free]
                    if booked_out:
                        st.markdown("Fully booked: " + " · ".join(
                            f'<span class="slot-full">{s}</span>' for s in booked_out),
                            unsafe_allow_html=True)

        st.divider()
        if st.button("Confirm booking", use_container_width=True):
            booking = {
                "name": name, "mobile": mobile, "company": company,
                "email": email, "state": state, "city": city,
                "serial_number": serial, "service_type": service,
                "date": chosen_date.isoformat() if chosen_date else "",
                "time_slot": chosen_slot or "",
            }
            problems = validate_booking(df_all, booking)
            if problems:
                for p in problems:
                    st.error(p)
            else:
                row = do_book(df_all, booking)
                email_status = try_send_email(row, "confirmed")
                confirmation_panel(row, email_status)

# ---------------------------------------------------------------------------
# 2) Reschedule / cancel
# ---------------------------------------------------------------------------

with tab_manage:
    st.subheader("Find your booking")
    lookup = st.text_input("Appointment ID or mobile number",
                           placeholder="e.g. HEX-4F2A1B or 9876543210")
    if lookup.strip():
        q = lookup.strip().upper()
        qm = re.sub(r"\D", "", lookup)
        mine = df_all[
            (df_all["status"] == "Confirmed")
            & (
                (df_all["appointment_id"].str.upper() == q)
                | (df_all["mobile"] == qm) if qm else (df_all["appointment_id"].str.upper() == q)
            )
        ]
        if mine.empty:
            st.info("No confirmed appointment found for that ID / mobile number.")
        else:
            options = {
                f"{r.appointment_id} · {r.service_type} · {r.date} {r.time_slot}": r.appointment_id
                for r in mine.itertuples()
            }
            picked = st.selectbox("Select the appointment", list(options.keys()))
            apt_id = options[picked]
            current = mine[mine["appointment_id"] == apt_id].iloc[0].to_dict()

            action = st.radio("What would you like to do?",
                              ["Reschedule", "Cancel"], horizontal=True)

            if action == "Reschedule":
                new_date = st.date_input(
                    "New date", value=None,
                    min_value=date.today() + timedelta(days=MIN_LEAD_DAYS),
                    max_value=date.today() + timedelta(days=BOOKING_WINDOW_DAYS),
                    format="DD/MM/YYYY", key="res_date")
                new_slot = None
                if new_date:
                    ok, why = is_business_day(new_date)
                    if not ok:
                        st.warning(why)
                    else:
                        free = available_slots(df_all, new_date)
                        # allow keeping the same slot on the same date
                        if (new_date.isoformat() == current["date"]
                                and current["time_slot"] not in free):
                            free = free + [current["time_slot"]]
                        if not free:
                            st.warning("No free slots that day - try another date.")
                        else:
                            new_slot = st.radio("New time slot", free, horizontal=True,
                                                key="res_slot")
                if st.button("Confirm reschedule"):
                    if not (new_date and new_slot):
                        st.error("Pick a new date and slot first.")
                    else:
                        ok, why = is_business_day(new_date)
                        if not ok:
                            st.error(why)
                        elif update_booking(apt_id, {"date": new_date.isoformat(),
                                                     "time_slot": new_slot}):
                            updated = {**current, "date": new_date.isoformat(),
                                       "time_slot": new_slot}
                            st.success(f"Rescheduled to {new_date.isoformat()} · {new_slot}.")
                            st.caption(try_send_email(updated, "rescheduled"))
                        else:
                            st.error("Could not update the booking - please try again.")

            else:  # Cancel
                st.warning(f"Cancel appointment **{apt_id}** "
                           f"({current['date']} · {current['time_slot']})?")
                if st.button("Yes, cancel this appointment"):
                    if update_booking(apt_id, {"status": "Cancelled"}):
                        st.success("Appointment cancelled. The slot is now free for others.")
                        st.caption(try_send_email(current, "cancelled"))
                    else:
                        st.error("Could not cancel - please try again.")

# ---------------------------------------------------------------------------
# 3) AI assistant
# ---------------------------------------------------------------------------

with tab_ai:
    st.subheader("Hexagon Service Agent")
    st.caption("The agent can check availability, book, reschedule and cancel - "
               "all through conversation. It operates the same booking engine as "
               "the forms, and every change still needs your Confirm.")
    client = get_claude()
    if client is None:
        st.info("The AI agent needs ANTHROPIC_API_KEY in Streamlit Secrets. "
                "Booking via the form tabs works without it.")
    else:
        st.session_state.setdefault("agent_api", [])
        st.session_state.setdefault("agent_log", [])
        st.session_state.setdefault("agent_pending", None)
        st.session_state.setdefault("agent_note", "")

        for entry in st.session_state.agent_log:
            with st.chat_message(entry["role"]):
                if entry.get("kind") == "tool":
                    st.caption(f"🔧 {entry['text']}")
                else:
                    st.markdown(entry["text"])

        pending = st.session_state.agent_pending
        if pending:
            kind, data = pending["kind"], pending["data"]
            if kind == "create":
                st.info(f"**Ready to book:** {data.get('service_type')} · "
                        f"{data.get('date')} · {data.get('time_slot')}  \n"
                        f"{data.get('name')} · {data.get('mobile')} · "
                        f"{data.get('city')}, {data.get('state')} · "
                        f"S/N {data.get('serial_number')}")
            elif kind == "reschedule":
                cur = pending["current"]
                st.info(f"**Reschedule {data['appointment_id']}:** "
                        f"{cur['date']} · {cur['time_slot']} → "
                        f"**{data['new_date']} · {data['new_time_slot']}**")
            else:
                cur = pending["current"]
                st.info(f"**Cancel {data['appointment_id']}** "
                        f"({cur['service_type']} · {cur['date']} · {cur['time_slot']})?")

            cc1, cc2 = st.columns(2)
            if cc1.button("Confirm", use_container_width=True, key="agent_confirm"):
                fresh = load_appointments()
                if kind == "create":
                    problems = validate_booking(fresh, data)
                    if problems:
                        note = "Could not book: " + " ".join(problems)
                    else:
                        row = do_book(fresh, data)
                        note = (f"Booked! Appointment ID **{row['appointment_id']}** - "
                                f"{row['date']} · {row['time_slot']}. "
                                + try_send_email(row, "confirmed"))
                elif kind == "reschedule":
                    ok = update_booking(data["appointment_id"],
                                        {"date": data["new_date"],
                                         "time_slot": data["new_time_slot"]})
                    if ok:
                        updated = {**pending["current"], "date": data["new_date"],
                                   "time_slot": data["new_time_slot"]}
                        note = (f"Rescheduled **{data['appointment_id']}** to "
                                f"{data['new_date']} · {data['new_time_slot']}. "
                                + try_send_email(updated, "rescheduled"))
                    else:
                        note = "Could not update the booking - please try again."
                else:
                    ok = update_booking(data["appointment_id"], {"status": "Cancelled"})
                    note = (f"Cancelled **{data['appointment_id']}**. The slot is free again. "
                            + try_send_email(pending["current"], "cancelled")
                            ) if ok else "Could not cancel - please try again."
                st.session_state.agent_log.append(
                    {"role": "assistant", "kind": "text", "text": note})
                st.session_state.agent_note = re.sub(r"\*", "", note)
                st.session_state.agent_pending = None
                st.rerun()
            if cc2.button("Discard", use_container_width=True, key="agent_discard"):
                st.session_state.agent_note = "The user discarded the proposal without confirming."
                st.session_state.agent_pending = None
                st.rerun()

        col_reset, _ = st.columns([1, 4])
        if st.session_state.agent_log and col_reset.button("Start over"):
            for k in ("agent_api", "agent_log", "agent_pending", "agent_note"):
                st.session_state.pop(k, None)
            st.rerun()

        if prompt := st.chat_input(
                "e.g. Book a calibration for my RTC360 next Tuesday, or "
                "move HEX-1A2B3C to Friday"):
            content = prompt
            if st.session_state.agent_note:
                content = f"[SYSTEM NOTE: {st.session_state.agent_note}]\n\n{prompt}"
                st.session_state.agent_note = ""
            st.session_state.agent_api.append({"role": "user", "content": content})
            st.session_state.agent_log.append(
                {"role": "user", "kind": "text", "text": prompt})
            with st.spinner("Working…"):
                try:
                    run_agent(client, st.session_state.agent_api,
                              st.session_state.agent_log)
                except Exception as e:  # noqa: BLE001
                    st.session_state.agent_log.append(
                        {"role": "assistant", "kind": "text",
                         "text": f"Something went wrong ({type(e).__name__}). "
                                 "Please try again or use the form tabs."})
            st.rerun()
