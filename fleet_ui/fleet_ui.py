"""Streamlit page: control several Tello REST services at once.

Run (from the repository root):  streamlit run fleet_ui/fleet_ui.py
"""
import os
import sys
from pathlib import Path

# ``streamlit run`` puts this file's folder on sys.path, where this very file would shadow the
# ``fleet_ui`` package. Put the repository root first so the package wins.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from fleet_ui.client import DEFAULT_NETWORK, check_ca_bundle, check_drone  # noqa: E402
from fleet_ui.engine import RUNNING, FleetEngine, configure_console_logging  # noqa: E402
from fleet_ui.steps import Step, StepError, parse_row, SYNTAX_HELP  # noqa: E402

DEFAULT_PORT = "8000"
DEFAULT_SCRIPT = ["takeoff", "sleep 3", "land"]
MAX_ROWS = 500


@st.cache_resource
def get_engine() -> FleetEngine:
    """One engine per Streamlit server process: run state must outlive script reruns."""
    configure_console_logging()
    return FleetEngine()


engine = get_engine()
running = engine.is_running


# ----- session state: drone rows -----
def _init_state() -> None:
    if "drone_ids" not in st.session_state:
        st.session_state.drone_ids = [0]
        st.session_state.next_drone_id = 1
        st.session_state.drone_info = {}     # drone_id -> (ip, DroneInfo)


def _add_drone() -> None:
    st.session_state.drone_ids.append(st.session_state.next_drone_id)
    st.session_state.next_drone_id += 1


def _remove_drone(drone_id: int) -> None:
    st.session_state.drone_ids.remove(drone_id)
    st.session_state.drone_info.pop(drone_id, None)
    for prefix in ("ip_", "port_", "key_"):
        st.session_state.pop("{}{}".format(prefix, drone_id), None)


def _drone_label(number: int, drone_id: int, ip: str) -> str:
    """Markdown for 'drone N' plus the last status result (green = flying, red = on the ground)."""
    label = "**drone {}**".format(number)
    entry = st.session_state.drone_info.get(drone_id)
    if entry is None or entry[0] != ip.strip():     # no result yet, or the IP was edited since
        return label
    info = entry[1]
    if not info.reachable:
        return "{} :gray[no answer]".format(label)
    battery = "{}%".format(info.battery) if info.battery is not None else "?%"
    if info.flying:
        return "{} :green[**flying {}**]".format(label, battery)
    return "{} :red[**on ground {}**]".format(label, battery)


def _cell(value) -> str:
    return value if isinstance(value, str) else ""


st.set_page_config(page_title="Tello fleet", layout="wide")
_init_state()
st.session_state["_was_running"] = running

st.title("Tello fleet control")
st.caption("API keys live only in this browser session and are never written to disk or logged. "
           "All calls use HTTPS with certificate validation. This page has no login: keep it on "
           "localhost or a trusted host.")

# ----- drones -----
st.subheader("Drones")
DRONE_COLUMNS = [2.5, 3, 1.5, 4, 1.5]
header = st.columns(DRONE_COLUMNS)
for column, text in zip(header, ("", "IP address ({})".format(DEFAULT_NETWORK), "Port", "API key", "")):
    column.caption(text)

drone_inputs = []
for number, drone_id in enumerate(st.session_state.drone_ids, start=1):
    c_label, c_ip, c_port, c_key, c_remove = st.columns(DRONE_COLUMNS, vertical_alignment="center")
    label_slot = c_label.empty()
    ip = c_ip.text_input("IP address", key="ip_{}".format(drone_id), placeholder="192.168.4.x",
                         label_visibility="collapsed", disabled=running, max_chars=15)
    port = c_port.text_input("Port", key="port_{}".format(drone_id), value=DEFAULT_PORT,
                             label_visibility="collapsed", disabled=running, max_chars=5)
    key = c_key.text_input("API key", key="key_{}".format(drone_id), type="password",
                           placeholder="API key", label_visibility="collapsed",
                           disabled=running, max_chars=256)
    c_remove.button("Remove", key="remove_{}".format(drone_id), on_click=_remove_drone,
                    args=(drone_id,), disabled=running)
    label_slot.markdown(_drone_label(number, drone_id, ip))
    drone_inputs.append((number, drone_id, ip, port, key))

st.button("Add drone", on_click=_add_drone, disabled=running)

configs = []
drone_errors = []
for number, _drone_id, ip, port, key in drone_inputs:
    config, problems = check_drone(ip, port, key)
    if config:
        configs.append(config)
    drone_errors.extend("drone {}: {}".format(number, p) for p in problems)
endpoints = [(c.ip, c.port) for c in configs]
if len(set(endpoints)) != len(endpoints):
    drone_errors.append("two drones use the same IP and port")
if not drone_inputs:
    drone_errors.append("add at least one drone")
for message in drone_errors:
    st.error(message)

ca_bundle = st.text_input(
    "CA bundle (PEM file that signed the drones' certificates; empty = system certificate store)",
    value=os.environ.get("FLEET_UI_CA_BUNDLE", ""), disabled=running, key="ca_bundle")
ca_error = check_ca_bundle(ca_bundle.strip())
if ca_error:
    st.error(ca_error)

# ----- script -----
st.subheader("Script")
st.caption("One instruction per row: `{}`. Blank rows and `#` comments are ignored. "
           "Steps run on all drones at the same time; the next step starts when all have answered.".format(
               SYNTAX_HELP))
edited = st.data_editor(
    pd.DataFrame({"step": DEFAULT_SCRIPT}), num_rows="dynamic", hide_index=True, key="steps_editor",
    disabled=running, width="stretch",
    column_config={"step": st.column_config.TextColumn("Instruction", max_chars=80)})
rows = [_cell(v) for v in edited["step"].tolist()][:MAX_ROWS]

steps = []
check_rows = []
script_errors = []
for number, text in enumerate(rows, start=1):
    parsed = parse_row(number, text)
    if isinstance(parsed, Step):
        steps.append(parsed)
        verdict = "ok"
    elif isinstance(parsed, StepError):
        script_errors.append(parsed)
        verdict = "ERROR: " + parsed.message
    else:
        verdict = "(ignored)"
    check_rows.append({"Row": number, "Instruction": text, "Check": verdict})
if script_errors:
    st.dataframe(pd.DataFrame(check_rows), hide_index=True, width="stretch")
elif not steps:
    st.warning("The script has no instructions.")

# ----- buttons -----
b_execute, b_abort, b_land, b_status, _spacer = st.columns([1, 1, 1, 1, 4])
execute = b_execute.button("Execute", type="primary", disabled=running, width="stretch")
abort = b_abort.button("Abort (hover all)", width="stretch")
land_all = b_land.button("Land all", width="stretch")
status_clicked = b_status.button("Status", width="stretch")

if execute:
    if drone_errors or ca_error or script_errors or not steps:
        st.error("Not started: fix the problems listed above first.")
    elif engine.start(configs, steps, ca_bundle.strip() or None):
        st.rerun()
elif abort or land_all:
    if not configs:
        st.error("No valid drone to send this to.")
    else:
        bundle = None if ca_error else (ca_bundle.strip() or None)
        (engine.abort if abort else engine.land_all)(configs, bundle)
        if drone_errors:
            st.warning("Some drone rows are invalid and were skipped.")
elif status_clicked:
    # Only the status endpoint is called: it reads the service's own view and never moves a drone.
    valid = [(n, did, ip, port, key) for n, did, ip, port, key in drone_inputs
             if check_drone(ip, port, key)[0] is not None]
    if not valid:
        st.error("No valid drone to ask.")
    else:
        valid_configs = [check_drone(ip, port, key)[0] for _n, _d, ip, port, key in valid]
        bundle = None if ca_error else (ca_bundle.strip() or None)
        infos = engine.read_status(valid_configs, bundle, [n for n, *_ in valid])
        for (_n, did, ip, _p, _k), info in zip(valid, infos):
            st.session_state.drone_info[did] = (ip.strip(), info)
        st.rerun()   # redraw the rows with the new colours

# ----- live status and log -----
refresh = 1 if running else None


@st.fragment(run_every=refresh)
def live_panel() -> None:
    state = engine.state()
    now_running = state.status == RUNNING
    if st.session_state.get("_was_running") and not now_running:
        st.session_state["_was_running"] = False
        st.rerun()   # enables the buttons again

    st.subheader("Status")
    if state.total_steps:
        st.progress(state.current_step / state.total_steps,
                    text="{}: step {} of {}{}".format(
                        state.status, state.current_step, state.total_steps,
                        " - " + state.step_text if state.step_text else ""))
    else:
        st.write("idle")
    if state.message:
        st.info(state.message)
    if state.drone_status:
        st.dataframe(pd.DataFrame({"Drone": ["drone {}".format(i + 1) for i in range(len(state.drone_status))],
                                   "Status": list(state.drone_status)}),
                     hide_index=True, width="stretch")

    st.subheader("Log")
    lines = engine.logs()
    st.code("\n".join(reversed(lines)) or "(empty)", language=None, height=320)


live_panel()
st.button("Clear log", on_click=engine.clear_log)
