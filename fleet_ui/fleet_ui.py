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

from fleet_ui import storage  # noqa: E402
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
    """First run of a browser session: start from the saved setup file, if there is one."""
    if "drone_ids" in st.session_state:
        return
    setup, load_error = storage.load_setup(storage.drones_file())
    drones = setup.drones if setup and setup.drones else [("", DEFAULT_PORT)]
    st.session_state.drone_ids = list(range(len(drones)))
    st.session_state.next_drone_id = len(drones)
    for drone_id, (ip, port) in enumerate(drones):
        st.session_state["ip_{}".format(drone_id)] = ip
        st.session_state["port_{}".format(drone_id)] = port or DEFAULT_PORT
    st.session_state["ca_bundle"] = (setup.ca_bundle if setup else "") or os.environ.get("FLEET_UI_CA_BUNDLE", "")
    st.session_state.script_base = setup.script if setup and setup.script else list(DEFAULT_SCRIPT)
    st.session_state.load_error = load_error
    _remember_encrypted_keys(setup)


def _remember_encrypted_keys(setup) -> None:
    st.session_state.encrypted_keys = setup.encrypted_keys if setup else None
    st.session_state.saved_ips = [ip.strip() for ip, _port in setup.drones] if setup else []


def _add_drone() -> None:
    drone_id = st.session_state.next_drone_id
    st.session_state.drone_ids.append(drone_id)
    st.session_state["port_{}".format(drone_id)] = DEFAULT_PORT
    st.session_state.next_drone_id += 1


def _unlock_keys() -> None:
    """Button callback: decrypt the saved API keys and fill them into the matching drone rows."""
    passphrase = st.session_state.get("passphrase", "")
    st.session_state["passphrase"] = ""              # do not keep the passphrase around
    try:
        keys = storage.decrypt_keys(st.session_state.encrypted_keys, passphrase)
    except storage.WrongPassphrase:
        st.session_state["_notice"] = ("error", "Wrong passphrase (or the saved keys were modified).")
        return
    except storage.StorageError as exc:
        st.session_state["_notice"] = ("error", "Saved keys cannot be used: {}".format(exc))
        return
    key_by_ip = dict(zip(st.session_state.saved_ips, keys))
    filled = 0
    for drone_id in st.session_state.drone_ids:
        key = key_by_ip.get(st.session_state.get("ip_{}".format(drone_id), "").strip())
        if key is not None:
            st.session_state["key_{}".format(drone_id)] = key
            filled += 1
    st.session_state["_notice"] = ("success", "API keys filled in for {} drone(s).".format(filled))


def _remove_drone(drone_id: int) -> None:
    st.session_state.drone_ids.remove(drone_id)
    for prefix in ("ip_", "port_", "key_"):
        st.session_state.pop("{}{}".format(prefix, drone_id), None)


def _drone_label(number: int, ip: str, port: str) -> str:
    """Markdown for 'drone N' plus its last known status (green = flying, red = on the ground).

    The status comes from the engine: it is updated by the Status button and before every
    script row during a run. It is looked up by (ip, port), so editing the IP hides a stale result."""
    label = "**drone {}**".format(number)
    info = engine.info_for(ip, port)
    if info is None:
        return label
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
st.caption("API keys live in this browser session and are never logged; they are written to disk only "
           "if you save with a passphrase (encrypted). All calls use HTTPS with certificate validation. "
           "This page has no login: keep it on localhost or a trusted host.")
if st.session_state.load_error:
    st.warning("Saved setup not loaded: {}".format(st.session_state.load_error))

# ----- drones -----
st.subheader("Drones")
DRONE_COLUMNS = [2.5, 3, 1.5, 4, 1.5]
header = st.columns(DRONE_COLUMNS)
for column, text in zip(header, ("", "IP address ({})".format(DEFAULT_NETWORK), "Port", "API key", "")):
    column.caption(text)

drone_inputs = []
label_slots = []     # (number, ip, port, placeholder) so the live panel can refresh the labels
for number, drone_id in enumerate(st.session_state.drone_ids, start=1):
    c_label, c_ip, c_port, c_key, c_remove = st.columns(DRONE_COLUMNS, vertical_alignment="center")
    label_slot = c_label.empty()
    ip = c_ip.text_input("IP address", key="ip_{}".format(drone_id), placeholder="192.168.4.x",
                         label_visibility="collapsed", disabled=running, max_chars=15)
    port = c_port.text_input("Port", key="port_{}".format(drone_id),
                             label_visibility="collapsed", disabled=running, max_chars=5)
    key = c_key.text_input("API key", key="key_{}".format(drone_id), type="password",
                           placeholder="API key", label_visibility="collapsed",
                           disabled=running, max_chars=256)
    c_remove.button("Remove", key="remove_{}".format(drone_id), on_click=_remove_drone,
                    args=(drone_id,), disabled=running)
    label_slot.markdown(_drone_label(number, ip, port))
    label_slots.append((number, ip, port, label_slot))
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
    disabled=running, key="ca_bundle", max_chars=storage.MAX_PATH_CHARS)
ca_error = check_ca_bundle(ca_bundle.strip())
if ca_error:
    st.error(ca_error)

# ----- script -----
st.subheader("Script")
st.caption("One instruction per row: `{}`. Blank rows and `#` comments are ignored. "
           "Steps run on all drones at the same time; the next step starts when all have answered.".format(
               SYNTAX_HELP))
edited = st.data_editor(
    pd.DataFrame({"step": st.session_state.script_base}), num_rows="dynamic", hide_index=True,
    key="steps_editor",
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
        engine.read_status(valid_configs, bundle, [n for n, *_ in valid])
        st.rerun()   # redraw the rows with the new colours

# ----- save / load -----
st.subheader("Save setup")
st.caption("Saves IP, port, CA bundle path and the script to `{}`; it is loaded again when the page "
           "starts. API keys are saved only with a passphrase (AES-256-GCM, key derived with scrypt); "
           "without one they are left out. The passphrase is not stored.".format(storage.drones_file()))
c_pass, c_save, c_unlock, _gap = st.columns([4, 1.5, 1.5, 3], vertical_alignment="bottom")
c_pass.text_input("Passphrase for the API keys (8+ characters)", type="password", key="passphrase",
                  max_chars=storage.MAX_PASSPHRASE_CHARS)
save_clicked = c_save.button("Save", width="stretch")
c_unlock.button("Unlock keys", width="stretch", on_click=_unlock_keys, disabled=running or not st.session_state.encrypted_keys)
if st.session_state.encrypted_keys:
    st.caption("The saved file holds encrypted API keys: enter the passphrase and press **Unlock keys**.")

notice = st.session_state.pop("_notice", None)
if notice:
    (st.success if notice[0] == "success" else st.error)(notice[1])

if save_clicked:
    passphrase = st.session_state.get("passphrase", "")
    had_keys = bool(st.session_state.encrypted_keys)
    try:
        with_keys = storage.save_setup(
            storage.drones_file(),
            [(ip.strip(), port.strip(), key) for _n, _d, ip, port, key in drone_inputs],
            ca_bundle.strip(), rows, passphrase)
    except storage.StorageError as exc:
        st.error("Not saved: {}".format(exc))
    except OSError:
        storage.logger.exception("could not write %s", storage.drones_file())
        st.error("Not saved: the file could not be written (see the terminal).")
    else:
        saved, _error = storage.load_setup(storage.drones_file())
        _remember_encrypted_keys(saved)
        if with_keys:
            st.success("Saved to {} (API keys encrypted).".format(storage.drones_file()))
        elif had_keys:
            st.warning("Saved to {}. No passphrase given, so the API keys were NOT saved and the "
                       "previously saved keys were removed from the file.".format(storage.drones_file()))
        else:
            st.success("Saved to {} (API keys not saved: no passphrase given).".format(storage.drones_file()))

# ----- live status and log -----
refresh = 1 if running else None


@st.fragment(run_every=refresh)
def live_panel() -> None:
    state = engine.state()
    now_running = state.status == RUNNING
    if st.session_state.get("_was_running") and not now_running:
        st.session_state["_was_running"] = False
        st.rerun()   # enables the buttons again and redraws the final drone labels

    # During a run the engine asks every drone for its status before every script row;
    # refresh the green/red "flying NN%" labels next to "drone N" with the newest answers.
    for number, ip, port, slot in label_slots:
        slot.markdown(_drone_label(number, ip, port))

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
