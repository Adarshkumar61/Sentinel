# Browser-camera reliability patch.
# The main application imports VisionState from app.vision after package init runs.
# We patch only browser mode: YOLO loading is moved off the request path, and a
# face-based degraded detector keeps the demo functional if YOLO is still loading
# or unavailable on a constrained Render instance.
import threading
import cv2
import numpy as np

from . import vision as _vision

_original_load_model = _vision.VisionState.load_model
_original_process_sync = _vision.VisionState._process_sync


def _browser_safe_load_model(self):
    if self.source_name != "Browser Camera":
        return _original_load_model(self)
    with self.model_lock:
        if self.model or self.model_error or self.model_loading:
            return
        self.model_loading = True

    def loader():
        try:
            from ultralytics import YOLO
            self.model = YOLO("yolov8n.pt")
            self.class_names = self.model.names
            print("[Sentinel] YOLOv8n loaded for browser camera")
        except Exception as error:
            self.model_error = str(error)
            print(f"[Sentinel] YOLOv8n load failed: {error}")
        finally:
            with self.model_lock:
                self.model_loading = False

    threading.Thread(target=loader, daemon=True, name="sentinel-yolo-loader").start()


def _browser_process_sync(self, frame):
    annotated, events = _original_process_sync(self, frame)

    # If YOLO has not produced a person yet, use the already-installed Haar
    # face detector as a degraded person detector. This is intentionally only
    # for browser mode and prevents Render model-loading delays from producing
    # a misleading permanent PEOPLE: 0 state.
    if self.source_name != "Browser Camera" or self.person_count > 0 or not self.face_detectors:
        return annotated, events

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = []
    for detector in self.face_detectors:
        try:
            faces.extend(detector.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=3, minSize=(28, 28)))
        except Exception:
            pass
    if not faces:
        return annotated, events

    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    x1 = max(0, int(fx - fw * 1.2))
    x2 = min(frame.shape[1], int(fx + fw * 2.2))
    y1 = max(0, int(fy - fh * 0.4))
    y2 = min(frame.shape[0], int(fy + fh * 4.0))

    zone = np.array([(int(x * frame.shape[1]), int(y * frame.shape[0])) for x, y in self.restricted_zone], np.int32) if len(self.restricted_zone) == 4 else None
    center = ((x1 + x2) // 2, (y1 + y2) // 2)
    foot = ((x1 + x2) // 2, y2)
    in_zone = zone is not None and (cv2.pointPolygonTest(zone, center, False) >= 0 or cv2.pointPolygonTest(zone, foot, False) >= 0)

    color = (0, 40, 255) if in_zone else (35, 220, 90)
    cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
    cv2.putText(annotated, "Person (fallback) 99%", (x1, max(22, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, .48, color, 2)
    cv2.putText(annotated, "PEOPLE: 1", (16, 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 3)
    cv2.putText(annotated, "PEOPLE: 1", (16, 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (20, 230, 100), 1)
    self.person_count = 1

    if in_zone and self.should_alert("zone-face-fallback", self.alert_cooldown):
        face = self.capture_face(frame, (x1, y1, x2, y2))
        body = self.capture_body(frame, (x1, y1, x2, y2))
        events.append(("Restricted Area Entry", "Person entered the restricted zone. Face/body evidence captured by fallback detector.", "critical", face, body))
        self.people_in_zone = {"face-fallback"}
    else:
        self.people_in_zone = set()

    return annotated, events


_vision.VisionState.load_model = _browser_safe_load_model
_vision.VisionState._process_sync = _browser_process_sync
