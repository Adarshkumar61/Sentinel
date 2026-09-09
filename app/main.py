import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .database import add_event, clear_events, clear_zone, initialise, load_zone, recent_events, save_zone, total_events
from .vision import VisionState

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)
CAPTURES = ROOT / "static" / "captures"
CAPTURES.mkdir(exist_ok=True)
app = FastAPI(title="AI Surveillance Dashboard")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
state = VisionState()
# state holds the current AI system state.
latest_jpeg = None
stream_lock = threading.Lock()
worker = None
active_capture = None
capture_lock = threading.Lock()
source_lock = threading.RLock()
stream_generation = 0
server_shutdown = threading.Event()
camera_error = None
source_kind = "webcam"
source_key = "webcam:0"
source_status = "Offline"
# Used only when a source has no saved zone yet.  Coordinates are normalized
# and therefore keep the same physical area at every webcam/video resolution.
DEFAULT_RESTRICTED_ZONE = [[0.62, 0.12], [0.94, 0.12], [0.94, 0.82], [0.62, 0.82]]


class Settings(BaseModel):
    crowd_threshold: int = Field(ge=1, le=100)
    restricted_zone: list[list[float]]
    alert_cooldown: float = Field(default=4, ge=1, le=3600)


class RTSPCamera(BaseModel):
    url: str
    name: str = Field(default="IP Camera", min_length=1, max_length=80)
    username: str = ""
    password: str = ""


def rtsp_url(camera: RTSPCamera):
    """Add optional credentials without retaining them anywhere except this request."""
    if not camera.url.lower().startswith("rtsp://"):
        raise HTTPException(400, "RTSP URL must begin with rtsp://")
    if camera.username and "@" not in camera.url.split("://", 1)[1]:
        from urllib.parse import quote
        return "rtsp://" + quote(camera.username, safe="") + ":" + quote(camera.password, safe="") + "@" + camera.url.split("://", 1)[1]
    return camera.url


def open_capture(source):
    """Open a video file or find the first Windows camera that yields real frames."""
    if isinstance(source, str):
        capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            return None, None
        # A container can open successfully while its codec is unsupported.
        # Verify that OpenCV can decode a real frame before replacing a live feed.
        for _ in range(20):
            ok, frame = capture.read()
            if ok and frame is not None and frame.size:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return capture, None
            time.sleep(.02)
        capture.release()
        return None, None

    # Laptop cameras are not always index 0 (virtual cameras often take it).
    # Probe briefly and accept only a camera that actually returns a non-empty frame.
    # DirectShow is typically much more dependable on Windows webcams; MSMF
    # remains as a fallback for cameras whose drivers require it.
    backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
    for index in range(4):
        for backend in backends:
            capture = cv2.VideoCapture(index, backend)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(8):
                ok, frame = capture.read()
                # Some blocked/virtual camera drivers report successful reads but
                # return an all-black buffer. Do not present that as a live feed.
                if ok and frame is not None and frame.size and (frame.mean() > 2 or frame.std() > 1):
                    # to avoid treating a black frame as a working camera
                    return capture, index
                time.sleep(.04)
            capture.release()
    return None, None


def processing_loop(session_id, source, kind):
    global latest_jpeg, active_capture, camera_error, source_status
    capture, camera_index = open_capture(source)
    if capture is None:
        if session_id == stream_generation:
            camera_error = "RTSP connection failed." if kind == "rtsp" else "No usable camera found. Check Windows camera permission or close apps using the camera."
            source_status = "Disconnected"
            state.running = False
        return
    if camera_index is not None:
        state.source_name = f"Camera {camera_index + 1}"
    with capture_lock:
        if session_id != stream_generation:
            capture.release()
            return
        active_capture = capture
    source_status = "Streaming / AI analysis active"

    def is_active():
        return state.running and session_id == stream_generation and not server_shutdown.is_set()
    # A bounded deque is a O(1) queue. It keeps only the newest frame: when
    # inference is slower than the camera, old frames are dropped instead of
    # building latency. This is the key difference between a lagging and live feed.
    frame_queue = deque(maxlen=1)
    queue_lock = threading.Lock()
    capture_done = threading.Event()

    def capture_frames():
        global camera_error
        while is_active():
            ok, frame = capture.read()
            if not ok:
                if kind == "video" and is_active():
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    # to code video ko beginning par le jaata hai:
                    continue
                if session_id == stream_generation:
                    camera_error = "Camera disconnected. Use Reconnect to try again." if kind == "rtsp" else "No frames received from camera. Try another camera or restart it."
                    source_status = "Disconnected"
                break
            with queue_lock:
                frame_queue.append(frame)
                # Now the newest frame is available to the AI processing loop.
        capture_done.set()

    grabber = threading.Thread(target=capture_frames, daemon=True)
    grabber.start()
    while is_active() and not capture_done.is_set():
        with queue_lock:
            frame = frame_queue.pop() if frame_queue else None
        if frame is None:
            time.sleep(.003)
            continue
        annotated, events = state.process(frame)
        for event_type, details, severity, face_image, body_image in events:
            image_path = save_face_capture(face_image) if face_image is not None else None
            body_image_path = save_body_capture(body_image) if body_image is not None else None
            add_event(event_type, state.source_name, details, severity, image_path, body_image_path)
#   Convert processed frame to JPEG:
        ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            with stream_lock:
                latest_jpeg = encoded.tobytes()
        # do not sleep here. The next deque item is always the freshest frame
    capture.release()
    with capture_lock:
        if active_capture is capture:
            active_capture = None
    if session_id == stream_generation:
        state.running = False
        state.person_count = 0


def release_active_capture():
    global active_capture
    with capture_lock:
        capture, active_capture = active_capture, None
    if capture is not None:
        capture.release()


def start_source(source, name, kind="webcam", key="webcam:0"):
    global worker, latest_jpeg, stream_generation, camera_error, source_kind, source_key, source_status
    # Serialise source changes. Double-clicking Start or uploading while a
    # webcam is opening otherwise leaves two readers fighting for the device.
    with source_lock:
        stream_generation += 1
        state.running = False
        release_active_capture()
        if worker and worker.is_alive():
            worker.join(timeout=2)
        with stream_lock:
            latest_jpeg = None
        state.source, state.source_name, state.running = source, name, True
        # Do not erase the working default zone just because this source has
        # not been saved in SQLite before. A persisted zone always wins.
        saved_zone = load_zone(key)


        state.restricted_zone = saved_zone or [point[:] for point in DEFAULT_RESTRICTED_ZONE]
#This means different cameras/sources can have their own saved zones.
        state.reset_tracking_state()
        source_kind, source_key, source_status = kind, key, "Connecting"
        camera_error = None
        session_id = stream_generation
        worker = threading.Thread(target=processing_loop, args=(session_id, source, kind), daemon=True)
        worker.start()


def save_face_capture(face_image):
    """Save compact JPEG evidence only when an alert has a detected face."""
    filename = f"face_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
    target = CAPTURES / filename
    cv2.imwrite(str(target), face_image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return f"/static/captures/{filename}"


def save_body_capture(body_image):
    """Save a person crop when the detector produced a usable full-body box."""
    filename = f"body_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
    target = CAPTURES / filename
    cv2.imwrite(str(target), body_image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return f"/static/captures/{filename}"


@app.on_event("startup")
def startup():
    initialise()


@app.on_event("shutdown")
def shutdown():
    server_shutdown.set()
    state.running = False
    release_active_capture()


@app.get("/")
def dashboard():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status") #This gives the frontend the current Sentinel status.
def status():
    # A stopped source must never leave a stale detection in the dashboard.
    people = state.person_count if state.running else 0
    return {"running": state.running, "camera": state.source_name, "source_kind": source_kind, "source_status": source_status, "persons": people, "alerts": total_events(), "crowd_threshold": state.crowd_threshold, "alert_cooldown": state.alert_cooldown, "restricted_zone": state.restricted_zone, "zone_saved": len(state.restricted_zone) >= 3, "error": camera_error}


@app.get("/api/events")
def events():
    return recent_events()


@app.post("/api/settings")
def save_settings(settings: Settings):
    if len(settings.restricted_zone) < 3 or any(len(point) != 2 or not all(0 <= value <= 1 for value in point) for point in settings.restricted_zone):
        raise HTTPException(400, "restricted_zone must contain normalized x,y points")
    state.crowd_threshold = settings.crowd_threshold
    state.restricted_zone = settings.restricted_zone
    state.alert_cooldown = settings.alert_cooldown
    save_zone(source_key, state.restricted_zone)
    return {"ok": True}


@app.delete("/api/zone")
def delete_zone():
    state.restricted_zone = []
    clear_zone(source_key)
    return {"ok": True}


@app.post("/api/camera/start")
def start_camera():
    if state.running and not isinstance(state.source, str):
        return {"ok": True, "message": "Camera is already running."}
    start_source(0, "Camera 01", "webcam", "webcam:0")
    return {"ok": True}


@app.post("/api/camera/stop")
def stop_camera():
    global latest_jpeg, stream_generation, camera_error, source_status
    with source_lock:
        stream_generation += 1
        state.running = False
        state.person_count = 0
        camera_error = None
        source_status = "Offline"
        release_active_capture()
        with stream_lock:
            latest_jpeg = None
    return {"ok": True}


@app.post("/api/events/clear")
@app.delete("/api/events")
def delete_events():
    """Clear alert history and the face evidence captured for those alerts."""
    image_paths = clear_events()
    for image_path in image_paths:
        # Use only the app-generated filename as a guard against deleting
        # anything outside the evidence directory.
        target = CAPTURES / Path(image_path).name
        if target.is_file():
            target.unlink()
    return {"ok": True}


@app.post("/api/video")
async def upload_video(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    allowed_extensions = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
    if suffix not in allowed_extensions:
        raise HTTPException(400, "Choose a video file: MP4, AVI, MOV, MKV, WebM, or M4V.")
    # Do not reuse the active filename: Windows cannot always replace a file
    # while OpenCV still has it open, and doing so can corrupt playback.
    destination = UPLOADS / f"{uuid.uuid4().hex}{suffix}"
    try:
        with destination.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
        probe, _ = open_capture(str(destination))
        if probe is None:
            destination.unlink(missing_ok=True)
            raise HTTPException(415, "This video could not be decoded. Please use an H.264 MP4 or AVI file.")
        probe.release()
    except HTTPException:
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, f"Could not save the video: {error}") from error
    start_source(str(destination), file.filename or "Uploaded CCTV Video", "video", f"video:{destination.name}")
    return {"ok": True, "name": state.source_name}


@app.post("/api/rtsp/test")
def test_rtsp(camera: RTSPCamera):
    source = rtsp_url(camera)
    capture, _ = open_capture(source)
    if capture is None:
        raise HTTPException(502, "Connection failed. Check the RTSP URL, credentials, and camera network access.")
    capture.release()
    return {"ok": True, "message": "Connected"}


@app.post("/api/rtsp/connect")
def connect_rtsp(camera: RTSPCamera):
    source = rtsp_url(camera)
    # Source key deliberately excludes password; no password is saved in SQLite.
    import hashlib
    key = "rtsp:" + hashlib.sha256((camera.name + "|" + camera.url.split("@")[-1]).encode()).hexdigest()[:20]
#The purpose is to identify the camera without storing the password in SQLite.
    start_source(source, camera.name.strip(), "rtsp", key)
    return {"ok": True, "name": camera.name.strip()}


@app.get("/api/stream")
def stream():
    def frames():
        while not server_shutdown.is_set():
            with stream_lock:
                image = latest_jpeg
            if image:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + image + b"\r\n"
            time.sleep(.06)
    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/frame")
def frame():
    """Return the newest processed frame as a normal image response."""
    with stream_lock:
        image = latest_jpeg
    if image is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})
