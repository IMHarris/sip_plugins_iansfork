from __future__ import print_function
# !/usr/bin/env python
# -*- coding: utf-8 -*-

# Flow SIP addin
import sys
sys.path.insert(0, './plugins/flowhelpers')
import flowhelpers
import ast
from blinker import signal
import datetime
import gv  # Get access to SIP's settings
import io
import queue
import json  # for working with data file
from sip import template_render  #  Needed for working with web.py templates
import threading
import time
from urls import urls  # Get access to SIP's URLs
import web  # web.py framework
from webpages import ProtectedPage, WebPage  # Needed for security
from webpages import showInFooter  # Enable plugin to display station data on timeline

# ========================================
# FLOW SENSOR CONFIGURATION
# ========================================
# Set to True for ESP32C3 (UART), False for Arduino (I2C)
USE_UART_MODE = True

# UART Configuration (for ESP32C3)
UART_PORT = "/dev/ttyAMA0"  # Default UART on Pi. Alternative: "/dev/serial0"
UART_BAUD = 115200

# I2C Configuration (for Arduino)
I2C_CLIENT_ADDR = 0x44

# ========================================
# Mode-specific imports
# ========================================
if USE_UART_MODE:
    try:
        import serial
    except ImportError:
        print(u"ERROR: pyserial not found.")
        print(u"Please install: sudo apt install python3-serial")
        sys.exit(1)

    comm_interface = None  # Will be initialized in main_loop
else:
    try:
        from smbus2 import SMBus, i2c_msg
        bus = SMBus(1)
    except ImportError:
        print(u"ERROR: smbus2 not found.")
        print(u"Please install: sudo apt install python3-smbus2")
        sys.exit(1)

# Global variables
SENSOR_REGISTER = 0x00  # Legacy - kept for compatibility
plugin_initiated = False
fs = flowhelpers.FlowSmoother(5)
settings_b4 = {}
changed_valves = {}
all_pulses = 0  # Calculated pulses since beginning of time
master_sensor_addr = 0
pulse_rate = 0  # holds last captured flow rate
flow_loop_running = False  # Notes if the main loop has started
valve_loop_running = False  # Notes if the valve loop has started
ls = flowhelpers.LocalSettings()
fw = flowhelpers.FlowWindow(ls)
valve_messages = queue.Queue()  # Carries messages from notify_zone_change to the changed_valves_loop

# Variables to note if notification plugins are loaded
email_loaded = False
sms_loaded = False
sms_plugin = ""
voice_loaded = False
voice_plugin = ""

# Legacy variable for backwards compatibility
CLIENT_ADDR = I2C_CLIENT_ADDR

# Add new URLs to access classes in this plugin.
# fmt: off
urls.extend([
    u"/flow-sp", u"plugins.flow.flow",
    u"/flow-save", u"plugins.flow.save_settings",
    u"/flow-data", u"plugins.flow.flowdata",
    u"/flow-settings", u"plugins.flow.settings",
    u"/cfl", u"plugins.flow.clear_log",
    u"/wfl", u"plugins.flow.download_csv",
    u"/wfr", u"plugins.flow.download_flowrate_csv"
    ])

# Add this plugin to the PLUGINS menu ["Menu Name", "URL"], (Optional)
gv.plugin_menu.append([_(u"Flow"), u"/flow-sp"])


def save_prior_settings():
    """
    Save prior settings dictionary to local variable settings_b4
    """
    global settings_b4

    try:
        with open(
            u"./data/flow.json", u"r"
        ) as f:  # Read settings from json file if it exists
            prior_settings = json.load(f)
    except IOError:
        prior_settings = {}
    finally:
        settings_b4 = prior_settings


def print_settings(lpad=2):
    """
    Prints the flow settings
    """
    if USE_UART_MODE:
        print(u"{}Flow sensor mode: UART".format(" " * lpad))
        print(u"{}UART port: {} at {} baud".format(" " * lpad, UART_PORT, UART_BAUD))
    else:
        print(u"{}Flow sensor mode: I2C".format(" " * lpad))
        print(u"{}I2C address: {}".format(" " * lpad, u"0x%02X" % I2C_CLIENT_ADDR))


def set_operation_mode(mode):
    """
    Sets the operation mode on the flow sensor controller

    Args:
        mode: 0x00 for production mode (read real sensor), 0x01 for test mode (generate random data)

    Returns:
        True if successful, False otherwise
    """
    global comm_interface

    if not USE_UART_MODE:
        print(u"ERROR: set_operation_mode() only works in UART mode (ESP32C3)")
        return False

    if comm_interface is None or not comm_interface.is_open:
        print(u"ERROR: UART port is not open")
        return False

    try:
        # Clear any stale data
        comm_interface.reset_input_buffer()

        # Send mode change command: 'M' + mode_byte
        comm_interface.write(b'M')
        comm_interface.write(bytes([mode]))

        # Wait for acknowledgment
        time.sleep(0.1)
        ack = comm_interface.read(1)

        if len(ack) == 1 and ack[0] == ord('A'):
            mode_str = "production" if mode == 0x00 else "test"
            print(u"Operation mode changed to: {} (0x{:02X})".format(mode_str, mode))
            return True
        else:
            print(u"ERROR: Failed to receive acknowledgment from controller")
            return False

    except Exception as e:
        print(u"ERROR: Failed to set operation mode: {}".format(e))
        return False


def list_available_uart_ports():
    """
    List all available serial ports on the system

    Returns:
        List of available port device paths
    """
    try:
        from serial.tools import list_ports
        ports = list_ports.comports()
        print(u"Available serial ports:")
        available = []
        for port in ports:
            print(u"  {} - {}".format(port.device, port.description))
            available.append(port.device)
        return available
    except Exception as e:
        print(u"ERROR: Failed to list serial ports: {}".format(e))
        return []


def find_uart_port():
    """
    Auto-detect the UART port for Raspberry Pi or other systems
    Tries common ports and returns the first available one

    Returns:
        Port path string if found, None otherwise
    """
    import os

    # Common UART ports on Raspberry Pi and other systems
    possible_ports = [
        "/dev/ttyAMA0",    # Pi 3/4/5 (when Bluetooth disabled)
        "/dev/serial0",    # Symlink to primary UART
        "/dev/ttyS0",      # Pi 3/4 (when Bluetooth enabled)
        "/dev/ttyUSB0",    # USB-to-serial adapter
        "/dev/ttyACM0",    # Some USB devices (like ESP32C3 USB-CDC)
    ]

    print(u"Auto-detecting UART port...")

    for port in possible_ports:
        if os.path.exists(port):
            try:
                # Try to open the port briefly to verify it works
                test = serial.Serial(port, UART_BAUD, timeout=0.1)
                test.close()
                print(u"  Found working UART port: {}".format(port))
                return port
            except Exception as e:
                print(u"  Port {} exists but failed to open: {}".format(port, e))
                continue
        else:
            print(u"  Port {} does not exist".format(port))

    # If no standard ports found, try listing all available ports
    print(u"  No standard ports found. Checking all available ports...")
    available = list_available_uart_ports()
    if available:
        # Try the first available port
        try:
            test = serial.Serial(available[0], UART_BAUD, timeout=0.1)
            test.close()
            print(u"  Using first available port: {}".format(available[0]))
            return available[0]
        except:
            pass

    print(u"  ERROR: No working UART port found!")
    print(u"  Please check:")
    print(u"    1. Hardware is connected")
    print(u"    2. Serial console is disabled (raspi-config)")
    print(u"    3. User has permission to access serial ports (add to 'dialout' group)")
    return None


def changed_valves_loop():
    """
    Monitors valve_messages queue for notices that the valve state has changed and takes appropriate action
    This loop runs on its own thread
    """
    global changed_valves
    global fw
    global valve_loop_running

    valve_loop_running = True
    while True:

        while not valve_messages.empty():
            # sleep here to ensure that if multiple valves are closed at the same time,
            # the main program has time to update all the valves in gv.sd
            time.sleep(0.25)
            valve_notice = valve_messages.get()
            if str(gv.srvals) != str(fw.valve_states()):
                capture_time = valve_notice.switch_time
                capture_flow_counter = valve_notice.counter
                i = 0
                fw_new = flowhelpers.FlowWindow(ls)
                fw_new.start_time = capture_time
                fw_new.start_pulses = capture_flow_counter
                vs = fw.valve_states()
                while i < len(vs):
                    if i != gv.sd["mas"] - 1:
                        # Ignore changes in the master valve
                        if vs[i] != gv.srvals[i]:
                            # Determine changed valves
                            if gv.srvals[i] == 1:
                                changed_valves[i] = u"on"
                            else:
                                changed_valves[i] = u"off"
                    i = i + 1
                if fw.valve_open() and not fw_new.valve_open():
                    # All valves are now closed end current flow window
                    fw.end_pulses = capture_flow_counter
                    fw.end_time = capture_time
                    fw.write_log()

                elif not fw.valve_open() and fw_new.valve_open():
                    # Flow has started.  New flow window has already been created above
                    pass

                elif fw.valve_open() and fw_new.valve_open():
                    # Flow is still running but through different valve(s)
                    # End current flow window
                    fw.end_pulses = capture_flow_counter
                    fw.end_time = capture_time
                    fw.write_log()
                fw = fw_new

        time.sleep(0.25)

class clear_log(ProtectedPage):
    """
    Delete all log records
    """
    def GET(self):
        with io.open(u"./data/flowlog.json", u"w") as f:
            f.write(u"")
        raise web.seeother(u"/flow-log")


class download_csv(ProtectedPage):
    """
    Downloads usage log as csv
    """
    def GET(self):
        records = flowhelpers.read_log()
        data = _(u"Date, Start Time, Duration, Stations, Valves, Usage, Units") + u"\n"
        for r in records:
            event = ast.literal_eval(json.dumps(r))
            data += (
                event[u"date"]
                + u', '
                + event[u"start"]
                + u', '
                + event[u"duration"]
                + u', "'
                + event[u"stations"]
                + u'", "'
                + event[u"valves"]
                + u'", '
                + str(event[u"usage"])
                + u', '
                + event[u"measure"]
                + u'\n'
            )

        web.header(u"Content-Type", u"text/csv")
        return data


class settings(ProtectedPage):
    """
    Load an html page for entering plugin settings.
    """
    def GET(self):

        try:
            # Update runtime values based on mode
            if USE_UART_MODE:
                runtime_values = {
                    "sensor-mode": "UART",
                    "sensor-addr": UART_PORT
                }
            else:
                runtime_values = {
                    "sensor-mode": "I2C",
                    "sensor-addr": u"0x%02X" % I2C_CLIENT_ADDR
                }

            if pulse_rate >=0:
                runtime_values.update({"sensor-connected": "yes"})
            else:
                runtime_values.update({"sensor-connected": "no"})
            if email_loaded:
                runtime_values.update({"email-loaded": "yes"})
            else:
                runtime_values.update({"email-loaded": "no"})
            if sms_loaded:
                runtime_values.update({"sms-loaded": "yes"})
                runtime_values.update({"sms-plugin": sms_plugin})
            else:
                runtime_values.update({"sms-loaded": "no"})
                runtime_values.update({"sms-plugin": ""})
            if voice_loaded:
                runtime_values.update({"voice-loaded": "yes"})
                runtime_values.update({"voice-plugin": voice_plugin})
            else:
                runtime_values.update({"voice-loaded": "no"})
                runtime_values.update({"voice-plugin": ""})
            runtime_values.update({"valve-measure-time": str(flowhelpers.IGNORE_INITIAL + flowhelpers.MEASURE_TIME)})

            with open(
                u"./data/flow.json", u"r"
            ) as f:  # Read settings from json file if it exists
                settings = json.load(f)
        except IOError:  # If file does not exist return empty value
            settings = {}

        # Reformat the flow data file
        x = ls.load_avg_flow_data()
        log = []
        for (k, v) in x.items():
            flow_rate = round(v["rate"] / ls.pulses_per_measure, 1)
            flow_rate_str = "{:,.1f} {}/hr".format(flow_rate, ls.volume_measure)
            logline = (
                u'{"'
                + u'valve'
                + u'":"'
                + gv.snames[int(k)]
                + u'","'
                + u'rate'
                + u'":"'
                + flow_rate_str
                + u'","'
                + u"time"
                + u'":"'
                + str(v["time"])
                + u'"}'
            )
            rec = json.loads(logline)
            log.append(rec)

        return template_render.flowsettings(settings, runtime_values, log)  # open flow settings page


class download_flowrate_csv(ProtectedPage):
    """
    Downloads flow rates as csv
    """
    def GET(self):
        x = ls.load_avg_flow_data()
        data = _(u"Station, Rate, Units, Recorded") + u"\n"
        for (k, v) in x.items():
            flow_rate = round(v["rate"] * 3600 / ls.pulses_per_measure, 1)
            data += (
                '"'
                + gv.snames[int(k)]
                + u'", '
                + '{:.1f}'.format(round(flow_rate / ls.pulses_per_measure, 1))
                + u', "'
                + '{}/hr'.format(ls.volume_measure)
                + '", '
                + str(v["time"])
                + '\n'
            )

        web.header(u"Content-Type", u"text/csv")
        return data


class save_settings(ProtectedPage):
    """
    Save user input to json file.
    Will create or update file when SUBMIT button is clicked
    CheckBoxes only appear in qdict if they are checked.
    """

    def GET(self):
        save_prior_settings()
        qdict = (
            web.input()
        )  # Dictionary of values returned as query string from settings page.
        # Clean up and sort the events fields
        if "email-events" in qdict.keys():
            email_events = qdict["email-events"].replace(" ", "")
            email_events_list = email_events.split(",")
            email_events_list2 = []
            for event in email_events_list:
                if len(event) > 0:
                    email_events_list2.append(event)
            qdict["email-events"] = ",".join(sorted(email_events_list2))
        if "sms-events" in qdict.keys():
            sms_events = qdict["sms-events"].replace(" ", "")
            sms_events_list = sms_events.split(",")
            sms_events_list2 = []
            for event in sms_events_list:
                if len(event) > 0:
                    sms_events_list2.append(event)
            qdict["sms-events"] = ",".join(sorted(sms_events_list2))
        if "voice-events" in qdict.keys():
            voice_events = qdict["voice-events"].replace(" ", "")
            voice_events_list = voice_events.split(",")
            voice_events_list2 = []
            for event in voice_events_list:
                if len(event) > 0:
                    voice_events_list2.append(event)
            qdict["voice-events"] = ",".join(sorted(voice_events_list2))
        with open(u"./data/flow.json", u"w") as f:  # Edit: change name of json file
            json.dump(qdict, f)  # save to file
        ls.load_settings()

        raise web.seeother(u"/")  # Return user to home page.


class flowdata(ProtectedPage):
    """
    Return flow values to the web page in JSON form
    """
    global pulse_rate

    def GET(self):
        web.header(b"Access-Control-Allow-Origin", b"*")
        web.header(b"Content-Type", b"application/json")
        web.header(b"Cache-Control", b"no-cache")
        qdict = {u"pulse_rate": pulse_rate}
        qdict.update({u"total_pulses": all_pulses})
        if ls.pulses_per_measure > 0:
            if fs.last_reading() >= 0:
                flow_rate = round(fs.ave_reading() * 3600 / ls.pulses_per_measure, 3)
                flow_rate_raw = round(fs.last_reading() * 3600 / ls.pulses_per_measure, 3)
                qdict.update({u"flow_rate": f'{round(flow_rate, 1):,}'})
                qdict.update({u"flow_rate_raw": f'{round(flow_rate_raw, 1):,}'})
            else:
                qdict.update({u"flow_rate": "N/A"})
                qdict.update({u"flow_rate_raw": "N/A"})
        else:
            qdict.update({u"flow_rate": 0})
            qdict.update({u"flow_rate_raw": 0})
        qdict.update({u"volume_measure": ls.volume_measure + "/hr"})

        # Water usage since beginning of window
        if ls.pulses_per_measure > 0:
            water_use = round((all_pulses - fw.start_pulses) / ls.pulses_per_measure, 1)
        else:
            water_use = 0
        water_use_str = str(water_use) + " " + ls.volume_measure
        qdict.update({u"water_use": water_use_str})

        # Create valve status string
        qdict.update({u"valve_status": fw.valves_status_str()})

        return json.dumps(qdict)


class flow(ProtectedPage):
    """View Log"""

    def GET(self):
        try:
            if USE_UART_MODE:
                runtime_values = {"sensor-addr": UART_PORT}
            else:
                runtime_values = {"sensor-addr": u"0x%02X" % I2C_CLIENT_ADDR}

            if pulse_rate >= 0:
                runtime_values.update({"sensor-connected": "yes"})
            else:
                runtime_values.update({"sensor-connected": "no"})

            with open(
                    u"./data/flow.json", u"r"
            ) as f:  # Read settings from json file if it exists
                settings = json.load(f)
        except IOError:  # If file does not exist return empty value
            settings = {}

        records = flowhelpers.read_log()
        return template_render.flow(settings, runtime_values, records)


class LoopThread (threading.Thread):
    def __init__(self, fn, thread_id, name, counter):
        threading.Thread.__init__(self)
        self.fn = fn
        self.threadID = thread_id
        self.name = name
        self.counter = counter

    def run(self):
        self.fn()


def main_loop():
    """
    **********************************************
    PROGRAM MAIN LOOP
    runs on separate thread
    **********************************************
    """
    global flow_loop_running
    global pulse_rate
    global all_pulses
    global fw
    global comm_interface

    flow_loop_running = True
    print(u"Flow plugin main loop initiated.")

    # Initialize communication interface
    if USE_UART_MODE:
        # Configure GPIO pins 14 and 15 for UART (ALT0 function)
        # This is necessary because SIP may reset pins during startup
        try:
            import subprocess
            subprocess.run(['pinctrl', 'set', '14', 'a0'], check=True)
            subprocess.run(['pinctrl', 'set', '15', 'a0'], check=True)
            print(u"GPIO pins 14 and 15 configured for UART (ALT0)")
        except Exception as e:
            print(u"WARNING: Failed to configure GPIO pins for UART: {}".format(e))
            print(u"You may need to manually run: sudo pinctrl set 14 a0; sudo pinctrl set 15 a0")

        # Try to open the configured port first
        port_to_use = UART_PORT
        try:
            comm_interface = serial.Serial(port_to_use, UART_BAUD, timeout=1)
            print(u"UART initialized: {} at {} baud".format(port_to_use, UART_BAUD))
        except Exception as e:
            print(u"WARNING: Failed to open configured UART port {}: {}".format(port_to_use, e))
            print(u"Attempting auto-detection...")

            # Try auto-detection
            detected_port = find_uart_port()
            if detected_port:
                try:
                    comm_interface = serial.Serial(detected_port, UART_BAUD, timeout=1)
                    print(u"UART initialized on auto-detected port: {} at {} baud".format(detected_port, UART_BAUD))
                    port_to_use = detected_port
                except Exception as e2:
                    print(u"ERROR: Failed to open auto-detected port {}: {}".format(detected_port, e2))
                    comm_interface = None
            else:
                print(u"ERROR: No working UART port found")
                comm_interface = None
    else:
        print(u"I2C initialized at address 0x{:02X}".format(I2C_CLIENT_ADDR))

    start_time = datetime.datetime.now()

    while True:
        try:
            if USE_UART_MODE:
                # ========================================
                # UART MODE (ESP32C3)
                # ========================================
                if comm_interface and comm_interface.is_open:
                    # Clear any stale data
                    comm_interface.reset_input_buffer()

                    # Send read command
                    comm_interface.write(b'R')

                    # Wait for response (4 bytes)
                    time.sleep(0.02)  # 20ms delay for ESP32C3 to respond
                    data = comm_interface.read(4)

                    if len(data) == 4:
                        pulse_rate = int.from_bytes(data, "little")
                    else:
                        pulse_rate = -1
                else:
                    pulse_rate = -1

            else:
                # ========================================
                # I2C MODE (Arduino)
                # ========================================
                msg = i2c_msg.read(I2C_CLIENT_ADDR, 4)
                bus.i2c_rdwr(msg)
                data = list(msg)
                pulse_rate = int.from_bytes(data, "little")

            fs.add_reading(pulse_rate)
            fw.set_pulse_values(pulse_rate, all_pulses)

        except IOError as e:
            print(u"Communication error: {}".format(e))
            pulse_rate = -1
            fs.add_reading(pulse_rate)
        except Exception as e:
            print(u"Error: {}".format(e))
            pulse_rate = -1
            fs.add_reading(pulse_rate)

        if not pulse_rate == -1:
            stop_time = datetime.datetime.now()
            time_elapsed = stop_time - start_time
            all_pulses = all_pulses + time_elapsed.total_seconds() * pulse_rate
            print("*****")
            print("All Pulses", all_pulses)
            print("Start Time", start_time)
            print("Stop Time", stop_time)
            print("Time elapsed/seconds", time_elapsed.total_seconds())
            print("Pulse Rate", pulse_rate)
            print("*****")
            start_time = stop_time

        # Update the application footer with flow information
        rate_footer.unit = u" " + ls.volume_measure + u"/hr"
        if ls.pulses_per_measure == 0:
            rate_footer.val = "N/A"
        elif fs.last_reading() >= 0 and ls.pulses_per_measure > 0:
            rate_footer.val = f'{round(fs.ave_reading() * 3600 / ls.pulses_per_measure, 1):,}'
        else:
            rate_footer.val = "0"

        if ls.pulses_per_measure > 0:
            volume_footer.val = f'{round((all_pulses - fw.start_pulses) / ls.pulses_per_measure, 1):,}'
        else:
            volume_footer.val = "0"
        volume_footer.unit = u" " + ls.volume_measure

        time.sleep(1)

flow_loop = LoopThread(main_loop, 1, "FlowLoop", 1)
valve_loop = LoopThread(changed_valves_loop, 2, "ValveLoop", 2)


"""
Event Triggers
"""
def notify_zone_change(name, **kw):
    """
    This event tells us a valve was turned on or off
    """
    valve_notice = flowhelpers.ValveNotice(datetime.datetime.now(), all_pulses)
    valve_messages.put(valve_notice)


zones = signal(u"zone_change")
zones.connect(notify_zone_change)


def notify_new_day(name, **kw):
    """
    App sends a new_day message after plugins are loaded.
    We'll use this as a trigger to start the threaded loops
    and run any code that has to run after other plugins are loaded
    """
    global email_loaded
    global sms_loaded
    global voice_loaded
    global plugin_initiated
    global fw

    if not plugin_initiated:
        for entry in gv.plugin_menu:
            if entry[0] == "Email settings":
                email_loaded = True

        # Instantiate the first flow window
        fw = flowhelpers.FlowWindow(ls)
        fw.start_time = datetime.datetime.now()
        fw.start_pulses = all_pulses
        plugin_initiated = True

        # Ask notification plugins to check in
        # Enabled notification plugins will respond with a "notification_online" message
        notification_query = signal("notification_checkin")
        notification_query.send(u"Flow.py")

    if not flow_loop_running:
        # This loop watches the flow
        flow_loop.start()
    if not valve_loop_running:
        # This loop watches for valve state changes
        valve_loop.start()


new_day = signal(u"new_day")
new_day.connect(notify_new_day)

def notify_notification_presence(name, **kw):
    """
    Responds to messages from notification plugins advertising their presence
    """
    global sms_loaded
    global voice_loaded
    global sms_plugin
    global voice_plugin
    if kw["txt"] == "sms":
        sms_loaded = True
        if len(name) > 0:
            sms_plugin = name
        else:
            sms_plugin = "?"
        print("Flow plugin is sending sms messages to {}".format(sms_plugin))
    if kw["txt"] == "voice":
        voice_loaded = True
        if len(name) > 0:
            voice_plugin = name
        else:
            voice_plugin = "?"
        print("Flow plugin is sending voice messages to {}".format(voice_plugin))


notification_presence = signal(u"notification_presence")
notification_presence.connect(notify_notification_presence)


"""
Run when plugin is loaded
"""
print_settings()
ls.load_settings()
alarm = signal(u"user_notify")

rate_footer = showInFooter()  # instantiate class to enable data in footer
rate_footer.label = u"Flow rate"
rate_footer.val = "N/A"
rate_footer.unit = u" ?/hr"

volume_footer = showInFooter()  # instantiate class to enable data in footer
volume_footer.label = u"Water usage"
volume_footer.val = 0
volume_footer.unit = u" ?"