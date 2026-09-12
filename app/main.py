import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .telegram_alert import send_telegram_alert, send_telegram_blockchain_update
from .database import (
    add_event,
    clear_events,
    clear_zone,
    confirm_blockchain_event,
    get_event,
    initialise,
    load_zone,
    mark_blockchain_submitted,
    recent_events,
    save_zone,
    total_events,
    claim_blockchain_registration,
    mark_blockchain_failed,
)
from .blockchain import BlockchainClient, BlockchainVerificationError
from .blockchain_config import public_config
from .hashing import sha256_event, sha256_file
from .vision import VisionState
from queue import Queue, Empty

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

# Alert side-effects run outside the AI/video processing loop.
# This prevents Telegram/network/disk/SQLite delays from freezing the live frame.
event_queue = Queue(maxsize=100)
event_worker_thread = None
browser_frame_lock = threading.Lock()
# Used only when a source has no saved zone yet. Coordinates are normalized
# and therefore keep the same physical area at every webcam/video resolution.
DEFAULT_RESTRICTED_ZONE = [[0.62, 0.12], [0.94, 0.12], [0.94, 0.82], [0.62, 0.82]]


class Settings(BaseModel):
    crowd_threshold: int = Field(ge=1, le=100)
    restricted_zone: list[list[float]]
    alert_cooldown: float = Field(default=2, ge=1, le=3600)


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
        for _ in range(20):
            ok, frame = capture.read()
            if ok and frame is not None and frame.size:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return capture, None
            time.sleep(.02)
        capture.release()
        return None, None

    backends = (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
    for index in range(4):
        for backend in backends:
            capture = cv2.VideoCapture(index, backend)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(8):
                ok, frame = capture.read()
                if ok and frame is not None and frame.size and (frame.mean() > 2 or frame.std() > 1):
                    return capture, index
                time.sleep(.04)
            capture.release()
    return None, None


def event_worker():
    """Handle evidence, database writes and Telegram without blocking video AI."""
    while not server_shutdown.is_set():
        try:
            event = event_queue.get(timeout=0.5)
        except Empty:
            continue

        try:
            event_type = event["event_type"]
            details = event["details"]
            severity = event["severity"]
            camera_name = event["camera_name"]
            face_image = event["face_image"]
            body_image = event["body_image"]

            image_path = save_face_capture(face_image) if face_image is not None else None
            body_image_path = save_body_capture(body_image) if body_image is not None else None

            event_id = f"SENT-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            detected_at = datetime.now().astimezone().isoformat(timespec="seconds")
            face_file = CAPTURES / Path(image_path).name if image_path else None
            body_file = CAPTURES / Path(body_image_path).name if body_image_path else None
            evidence_file = face_file or body_file
            evidence_hash = (
                sha256_file(evidence_file)
                if evidence_file and evidence_file.is_file()
                else sha256_event(event_id, event_type, camera_name, details)
            )
            add_event(event_type, camera_name, details, severity, image_path, body_image_path, event_id, evidence_hash, detected_at)

            send_telegram_alert(
                event_type=event_type,
                details=details,
                severity=severity,
                source_name=camera_name,
                face_path=str(face_file) if face_file and face_file.is_file() else None,
                body_path=str(body_file) if body_file and body_file.is_file() else None,
                evidence_path=str(evidence_file) if evidence_file and evidence_file.is_file() else None,
                event_id=event_id,
                evidence_hash=evidence_hash,
                detected_at=detected_at,
            )

            threading.Thread(
                target=process_single_blockchain_registration,
                args=(event_id,),
                daemon=True,
                name=f"mst-registration-{event_id}",
            ).start()
        except Exception as error:
            print(f"⚠️ Event worker error: {error}")
        finally:
            event_queue.task_done()


def process_single_blockchain_registration(event_id: str):
    """Automatically register one Sentinel event on MST Testnet."""
    try:
        if not claim_blockchain_registration(event_id):
            print(f"ℹ️ Blockchain registration already claimed: {event_id}")
            return

        event = get_event(event_id)
        if not event:
            mark_blockchain_failed(event_id, "Event was not found after registration claim.")
            print(f"⚠️ Event not found for blockchain registration: {event_id}")
            return

        if not event.get("evidence_hash"):
            mark_blockchain_failed(event_id, "Evidence hash is missing.")
            return

        print(f"🔄 Registering {event_id} on MST Testnet...")
        result = BlockchainClient().register_evidence(event_id=event_id, evidence_hash=event["evidence_hash"])
        verified = BlockchainClient().verify_registration(
            event_id=event_id,
            evidence_hash=event["evidence_hash"],
            transaction_hash=result["transaction_hash"],
        )
        confirm_blockchain_event(event_id, **verified)
        updated_event = get_event(event_id)
        print(f"✅ {event_id} VERIFIED ON MST TESTNET: {verified['transaction_hash']}")
        threading.Thread(
            target=send_telegram_blockchain_update,
            args=(updated_event,),
            daemon=True,
            name=f"telegram-blockchain-{event_id}",
        ).start()

    except BlockchainVerificationError as error:
        mark_blockchain_failed(event_id, str(error))
        print(f"❌ MST registration failed for {event_id}: {error}")
    except Exception as error:
        mark_blockchain_failed(event_id, str(error))
        print(f"❌ Unexpected MST registration error for {event_id}: {error}")


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

    frame_queue = deque(maxlen=1)
    queue_lock = threading.Lock()
    capture_done = threading.Event()

    def capture_frames():
        global camera_error, source_status
        while is_active():
            ok, frame = capture.read()
            if not ok:
                if kind == "video" and is_active():
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                if session_id == stream_generation:
                    camera_error = "Camera disconnected. Use Reconnect to try again." if kind == "rtsp" else "No frames received from camera. Try another camera or restart it."
                    source_status = "Disconnected"
                break
            with queue_lock:
                frame_queue.append(frame)
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
            try:
                event_queue.put_nowait({
                    "event_type": event_type,
                    "details": details,
                    "severity": severity,
                    "camera_name": state.source_name,
                    "face_image": face_image,
                    "body_image": body_image,
                })
            except Exception:
                print("⚠️ Event queue full. Alert side-effects skipped.")

        ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            with stream_lock:
                latest_jpeg = encoded.tobytes()

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
    with source_lock:
        stream_generation += 1
        state.running = False
        release_active_capture()
        if worker and worker.is_alive():
            worker.join(timeout=2)
        with stream_lock:
            latest_jpeg = None
        state.source, state.source_name, state.running = source, name, True
        saved_zone = load_zone(key)
        state.restricted_zone = saved_zone or [point[:] for point in DEFAULT_RESTRICTED_ZONE]
        state.reset_tracking_state()
        source_kind, source_key, source_status = kind, key, "Connecting"
        camera_error = None
        session_id = stream_generation
        worker = threading.Thread(target=processing_loop, args=(session_id, source, kind), daemon=True)
        worker.start()


def save_face_capture(face_image):
    filename = f"face_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
    target = CAPTURES / filename
    cv2.imwrite(str(target), face_image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return f"/static/captures/{filename}"


def save_body_capture(body_image):
    filename = f"body_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
    target = CAPTURES / filename
    cv2.imwrite(str(target), body_image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return f"/static/captures/{filename}"


@app.on_event("startup")
def startup():
    global event_worker_thread
    initialise()
    event_worker_thread = threading.Thread(target=event_worker, daemon=True, name="sentinel-event-worker")
    event_worker_thread.start()
    print("Sentinel AI started.")
    print("Background event worker started.")


@app.on_event("shutdown")
def shutdown():
    server_shutdown.set()
    state.running = False
    release_active_capture()


@app.get("/")
def dashboard():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def status():
    people = state.person_count if state.running else 0
    return {"running": state.running, "camera": state.source_name, "source_kind": source_kind, "source_status": source_status, "persons": people, "alerts": total_events(), "crowd_threshold": state.crowd_threshold, "alert_cooldown": state.alert_cooldown, "restricted_zone": state.restricted_zone, "zone_saved": len(state.restricted_zone) >= 3, "error": camera_error}


@app.get("/api/events")
def events():
    return recent_events()


@app.get("/api/blockchain/config")
def blockchain_config():
    """Return public MST configuration; evidence registration is backend-automatic."""
    return public_config()


class BlockchainReceipt(BaseModel):
    transaction_hash: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")


@app.post("/api/events/{event_id}/blockchain-submitted")
def save_blockchain_submission(event_id: str, receipt: BlockchainReceipt):
    event = get_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if event.get("verification_status") == "VERIFIED":
        raise HTTPException(409, "This event is already blockchain verified.")
    mark_blockchain_submitted(event_id, receipt.transaction_hash)
    return {"ok": True, "status": "PENDING"}


@app.post("/api/events/{event_id}/blockchain-receipt")
def save_blockchain_receipt(event_id: str, receipt: BlockchainReceipt):
    event = get_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not event.get("evidence_hash"):
        raise HTTPException(409, "This legacy event has no canonical evidence hash.")
    try:
        verified = BlockchainClient().verify_registration(event_id, event["evidence_hash"], receipt.transaction_hash)
    except BlockchainVerificationError as error:
        raise HTTPException(409, str(error)) from error
    confirm_blockchain_event(event_id, **verified)
    updated_event = get_event(event_id)
    threading.Thread(target=send_telegram_blockchain_update, args=(updated_event,), daemon=True).start()
    return {"ok": True, "verification": verified}


@app.post("/api/events/{event_id}/verify")
def verify_blockchain_event(event_id: str):
    """Read-only recovery/verification endpoint; never creates a transaction."""
    event = get_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not event.get("blockchain_tx") or not event.get("evidence_hash"):
        raise HTTPException(409, "This event has not been registered on MST Testnet.")
    try:
        verified = BlockchainClient().verify_registration(event_id, event["evidence_hash"], event["blockchain_tx"])
    except BlockchainVerificationError as error:
        raise HTTPException(409, str(error)) from error
    confirm_blockchain_event(event_id, **verified)
    return {"ok": True, "verification": verified}


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


@app.post("/api/browser/start")
def start_browser_camera():
    """Start a browser-owned webcam session; the Render server never opens a physical camera."""
    global latest_jpeg, stream_generation, camera_error, source_kind, source_key, source_status
    with source_lock:
        stream_generation += 1
        state.running = True
        state.source = None
        state.source_name = "Browser Camera"
        state.reset_tracking_state()
        saved_zone = load_zone("browser:0")
        state.restricted_zone = saved_zone or [point[:] for point in DEFAULT_RESTRICTED_ZONE]
        source_kind = "browser"
        source_key = "browser:0"
        source_status = "Streaming / AI analysis active"
        camera_error = None
        release_active_capture()
        with stream_lock:
            latest_jpeg = None
    return {"ok": True, "message": "Browser webcam ready."}


@app.post("/api/browser/frame")
async def process_browser_frame(file: UploadFile = File(...)):
    """Process one browser webcam frame through the same Sentinel AI/event/MST pipeline."""
    global latest_jpeg, camera_error, source_status
    if source_kind != "browser" or not state.running:
        raise HTTPException(409, "Browser webcam is not active.")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Empty camera frame.")

    frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise HTTPException(400, "Invalid JPEG camera frame.")

    with browser_frame_lock:
        try:
            annotated, events = state.process(frame)
            for event_type, details, severity, face_image, body_image in events:
                try:
                    event_queue.put_nowait({
                        "event_type": event_type,
                        "details": details,
                        "severity": severity,
                        "camera_name": "Browser Camera",
                        "face_image": face_image,
                        "body_image": body_image,
                    })
                except Exception:
                    print("⚠️ Event queue full. Alert side-effects skipped.")

            ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                raise HTTPException(500, "Could not encode processed camera frame.")
            with stream_lock:
                latest_jpeg = encoded.tobytes()
            source_status = "Streaming / AI analysis active"
            camera_error = None
        except HTTPException:
            raise
        except Exception as error:
            camera_error = f"Browser camera processing failed: {error}"
            source_status = "Disconnected"
            raise HTTPException(500, str(error)) from error

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
    image_paths = clear_events()
    for image_path in image_paths:
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
    import hashlib
    key = "rtsp:" + hashlib.sha256((camera.name + "|" + camera.url.split("@")[-1]).encode()).hexdigest()[:20]
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
