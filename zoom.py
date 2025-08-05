import json
import time
import os
import logging
import asyncio
import subprocess
import websockets
import pyautogui
import ctypes
from ctypes import wintypes
from datetime import datetime
from dateutil import parser
from apscheduler.schedulers.blocking import BlockingScheduler
import argparse
import sys
import uuid  # for generating audio task IDs

# ─── Configuration A ────────────────────────────────────────────────
OBS_HOST = os.getenv('OBS_HOST', 'localhost')
OBS_PORT = int(os.getenv('OBS_PORT', 4455))           # OBS écoute ici
OBS_URI = f"ws://{OBS_HOST}:{OBS_PORT}"

# On Windows, we launch Zoom via its zoommtg:// protocol handler (no specific app path needed)
# ZOOM_SCHEDULES_FILE (env) or zoom_schedules.json in this directory is the single source of truth.
SCHEDULES_FILE = os.getenv('ZOOM_SCHEDULES_FILE') or os.path.join(os.path.dirname(__file__), 'zoom_schedules.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
scheduler = BlockingScheduler()
# Path to API's audio task queue
AUDIO_TASKS_FILE = os.path.join(os.path.dirname(__file__), 'audio_schedules.json')

# ─── Window enumeration for Zoom meeting detection ───────────────
user32 = ctypes.WinDLL('user32', use_last_error=True)
EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowTextW = user32.GetWindowTextW
IsWindowVisible = user32.IsWindowVisible

def list_window_titles():
    titles = []
    @EnumWindowsProc
    def foreach(hwnd, lParam):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True
    EnumWindows(foreach, 0)
    return titles

def wait_for_meeting_window(meet_id, timeout_sec=60):
    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        for title in list_window_titles():
            lt = title.lower()
            # Match either the meeting ID or common Zoom window titles
            if (meet_id and meet_id in title) or 'zoom meeting' in lt or lt.endswith('zoom'):
                return True
        time.sleep(1)
    return False

def wait_for_meeting_window_close(meet_id):
    while True:
        found = False
        for title in list_window_titles():
            lt = title.lower()
            if (meet_id and meet_id in title) or 'zoom meeting' in lt or lt.endswith('zoom'):
                found = True
                break
        if not found:
            return
        time.sleep(2)
    
def queue_audio_task(file_path, metadata=None):
    """Append a new local audio processing task to audio_schedules.json, using metadata if provided."""
    try:
        tasks = []
        if os.path.exists(AUDIO_TASKS_FILE):
            with open(AUDIO_TASKS_FILE, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
    except Exception:
        tasks = []
    base, ext = os.path.splitext(os.path.basename(file_path))
    # Determine destination folder: use metadata if available
    if metadata and isinstance(metadata, dict):
        # Example metadata keys: faculty, classText
        fac = metadata.get('faculty')
        cls = metadata.get('classText')
        # Build path under Diploma/Workspace
        destFolder = os.path.join(os.getcwd(), 'Diploma', 'Workspace', fac, cls)
    else:
        destFolder = os.path.dirname(file_path)
    task = {
        'id': f"{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}",
        'url': file_path,
        'fileNameWithoutExt': base,
        'extension': ext,
        'destFolder': destFolder
    }
    # Include metadata if provided
    if metadata:
        task['metadata'] = metadata
    tasks.append(task)
    try:
        with open(AUDIO_TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, indent=2)
        logging.info(f"Queued audio task for recording: {file_path}")
    except Exception as e:
        logging.error(f"Failed to queue audio task: {e}")

def wait_for_file_ready(path, timeout_sec=30):
    """Wait until a file exists and its size has stabilized (no change) or timeout."""
    start = time.time()
    last_size = -1
    while True:
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
            except Exception:
                size = last_size
            if size == last_size:
                return
            last_size = size
        if time.time() - start > timeout_sec:
            logging.warning(f"Timeout waiting for file to finalize: {path}")
            return
        time.sleep(1)


# ─── Fonctions utilitaires WebSocket JSON-RPC v5 pour OBS ───────────
async def _obs_request(request_type: str, request_id: str):
    """
    Connect to OBS-WebSocket, perform JSON-RPC handshake (Hello, Identify),
    send a StartRecord/StopRecord request, and return the request response.
    """
    async with websockets.connect(OBS_URI) as ws:
        # 1) Wait for server Hello (op:0)
        hello = await ws.recv()
        # 2) Send Identify (op:1)
        identify = {"op": 1, "d": {"rpcVersion": 1}}
        await ws.send(json.dumps(identify))
        # 3) Wait for Identify response (op:2)
        while True:
            msg = await ws.recv()
            try:
                obj = json.loads(msg)
            except:
                continue
            if obj.get('op') == 2:
                break
        # 4) Send the actual request (StartRecord or StopRecord)
        payload = {"op": 6, "d": {"requestType": request_type,
                                     "requestData": {},
                                     "requestId": request_id}}
        await ws.send(json.dumps(payload))
        # 5) Wait for the request response (op:7 with matching requestId)
        while True:
            msg = await ws.recv()
            try:
                obj = json.loads(msg)
            except:
                continue
            if obj.get('op') == 7:
                data = obj.get('d', {})
                if data.get('requestId') == request_id:
                    return obj


# ─── Attente que la fenêtre Zoom-A soit ouverte ──────────────────
# Windows: wait for Zoom.exe process to appear
def wait_zoom_process(timeout_sec=30):
    elapsed = 0
    while elapsed < timeout_sec:
        # Check running processes for Zoom.exe
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Zoom.exe"],
                capture_output=True, text=True
            )
            if "Zoom.exe" in result.stdout:
                return True
        except Exception:
            pass
        time.sleep(1)
        elapsed += 1
#   return False

# (macOS-specific window-title polling removed; we rely on process detection on Windows)

def is_zoom_running():
    """Return True if Zoom.exe process is currently running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Zoom.exe"],
            capture_output=True, text=True
        )
        return "Zoom.exe" in result.stdout
    except Exception:
        return False

def start_recording_A(event):
    # Determine URL to launch: convert web join link to zoommtg:// if needed
    raw_url = event.get('zoomUrl', '')
    launch_url = raw_url
    # Extract meeting ID
    meet_id = None
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(raw_url)
        # If web URL, path /j/<id>
        parts = parsed.path.split('/')
        for i, p in enumerate(parts):
            if p.lower() == 'j' and i + 1 < len(parts):
                meet_id = parts[i+1]
                break
        if not meet_id and parts:
            meet_id = parts[-1]
    except Exception:
        pass
    # Convert HTTPS URL to zoommtg:// if needed
    if raw_url.startswith('http'):
        try:
            qs = parse_qs(parsed.query)
            pwd = qs.get('pwd', [None])[0]
            launch_url = f"zoommtg://zoom.us/join?confno={meet_id or ''}"
            if pwd:
                launch_url += f"&pwd={pwd}"
            logging.info(f"Converted web URL to protocol link: {launch_url}")
        except Exception as e:
            logging.warning(f"Could not convert HTTP URL: {e}")
            launch_url = raw_url
    else:
        logging.info(f"Using protocol link: {launch_url}")
    logging.info(f"Launching Zoom meeting URL: {launch_url}")
    # Launch Zoom via default protocol handler
    try:
        os.startfile(launch_url)
    except Exception as e:
        logging.error(f"Failed to launch Zoom meeting URL: {e}")
        return
    # Wait for Zoom client to join the meeting
    time.sleep(10)
    # Press Enter to confirm join dialog if necessary
    try:
        pyautogui.press('enter')
        logging.info("Pressed Enter to confirm Zoom join dialog")
    except Exception as e:
        logging.warning(f"Could not press Enter: {e}")
    # Click at coordinates (400,500) to finalize the join
    try:
        time.sleep(10)
        pyautogui.click(750, 200)
        time.sleep(10)
        pyautogui.click(710, 290)
        logging.info("Clicked at (400,500) to confirm Zoom join")
    except Exception as e:
        logging.warning(f"Could not click to confirm Zoom join: {e}")

    # Optional: wait for the Zoom meeting window to appear before recording
    window_found = False
    if meet_id:
        logging.info(f"Waiting up to 60s for Zoom meeting window (ID={meet_id})...")
        window_found = wait_for_meeting_window(meet_id, timeout_sec=60)
        if window_found:
            logging.info(f"Detected Zoom meeting window for ID {meet_id}")
        else:
            logging.warning(f"Zoom meeting window not detected for ID {meet_id}; will fallback to process exit")
    else:
        logging.warning("No meeting ID extracted; will monitor process exit instead of window")

    # Send StartRecord to OBS
    try:
        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(_obs_request("StartRecord", f"start_{event.get('id')}"))
        finally:
            loop.close()
        logging.info(f"StartRecord response for task {event.get('id')}: {resp}")
    except Exception as e:
        logging.error(f"Error sending StartRecord for task {event.get('id')}: {e}")
        return

    # Decide how to wait: window close or process exit
    if meet_id and window_found:
        logging.info(f"Waiting for Zoom meeting window to close (ID={meet_id})...")
        wait_for_meeting_window_close(meet_id)
        logging.info(f"Meeting window closed; preparing to stop recording (task {event.get('id')})")
    else:
        logging.info(f"Monitoring Zoom.exe; will stop OBS recording when Zoom exits (task {event.get('id')})")
        while is_zoom_running():
            time.sleep(5)
        logging.info(f"Zoom process exited; preparing to stop recording (task {event.get('id')})")
    # Send StopRecord to OBS
    try:
        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(_obs_request("StopRecord", f"stop_{event.get('id')}"))
        finally:
            loop.close()
        logging.info(f"StopRecord response for task {event.get('id')}: {resp}")
        # After recording stops, queue the MP4 for audio processing
        out = resp.get('d', {}).get('responseData', {}).get('outputPath')
        if out:
            # Wait for OBS to finish writing the file (moov atom)
            logging.info(f"Waiting for recording file to finalize: {out}")
            wait_for_file_ready(out)
            # Pass along metadata for audio task
            queue_audio_task(out, event.get('metadata'))
        else:
            logging.warning("No outputPath from OBS response; skipping audio task queue")
    except Exception as e:
        logging.error(f"Error sending StopRecord for task {event.get('id')}: {e}")




if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Zoom scheduler reading zoom_schedules.json')
    parser.add_argument('--id', type=str, help='Run the scheduled task by ID (loads metadata)')
    parser.add_argument('--url', type=str, help='One-off Zoom URL to launch immediately')
    parser.add_argument('--schedules', type=str, default=SCHEDULES_FILE, help='Path to schedules JSON file')
    args = parser.parse_args()

    # Override schedules file path if provided
    # Override schedules file path if provided
    SCHEDULES_FILE = args.schedules

    # If an ID is provided, run that scheduled task (with metadata)
    if args.id:
        try:
            tasks = json.load(open(SCHEDULES_FILE, 'r', encoding='utf-8'))
            task = next((t for t in tasks if t.get('id') == args.id), None)
        except Exception:
            task = None
        if not task:
            print(f"ERROR: no scheduled task with id {args.id}", file=sys.stderr)
            sys.exit(1)
        start_recording_A(task)
        sys.exit(0)
    # One-off URL invocation (no scheduling)
    if args.url:
        start_recording_A({'zoomUrl': args.url, 'id': 'cli'})
        sys.exit(0)

    # Scheduler mode: load tasks from JSON and schedule them
    try:
        tasks = json.load(open(SCHEDULES_FILE, 'r', encoding='utf-8'))
    except Exception:
        tasks = []
    now = datetime.utcnow()
    pending = []
    for task in tasks:
        try:
            run_dt = parser.parse(task.get('scheduleTime'))
            if run_dt > now:
                pending.append((task, run_dt))
        except:
            continue

    # Persist only future tasks
    json.dump([t for t, _ in pending], open(SCHEDULES_FILE, 'w', encoding='utf-8'), indent=2)

    for task, run_dt in pending:
        job_id = task.get('id')
        scheduler.add_job(start_recording_A, 'date', run_date=run_dt, args=[task], id=job_id)

    logging.info(f"Hydrated {len(pending)} pending Zoom tasks from {SCHEDULES_FILE}")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Zoom scheduler stopped.")