"""Low-latency YOLOv8 detection and tracking for the surveillance stream."""
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class VisionState:
    source: int | str = 0
    source_name: str = "Camera 01"
    running: bool = False
    # A visible, usable default for a newly opened camera. Zones remain
    # normalized so both this default and user-drawn rectangles scale with
    # every incoming frame resolution.
    restricted_zone: list = field(default_factory=lambda: [[0.62, 0.12], [0.94, 0.12], [0.94, 0.82], [0.62, 0.82]])
    crowd_threshold: int = 4
    alert_cooldown: float = 4.0
    person_count: int = 0
    alert_cooldowns: dict = field(default_factory=dict)
    model: object | None = field(default=None, init=False, repr=False)
    class_names: dict = field(default_factory=dict, init=False)
    model_error: str | None = field(default=None, init=False)
    model_loading: bool = field(default=False, init=False)
    model_lock: object = field(default_factory=threading.Lock, init=False, repr=False)
    face_detector: object = field(default=None, init=False, repr=False)
    face_detectors: list = field(default_factory=list, init=False, repr=False)
    people_in_zone: set = field(default_factory=set, init=False, repr=False)
    crowd_active: bool = field(default=False, init=False)

    # Browser webcams arrive through HTTP. Do not make the HTTP request wait
    # for YOLO CPU inference; keep only the newest frame and let one worker
    # process it in the background. This prevents Render latency from making
    # the browser camera appear frozen while still using the same AI pipeline.
    browser_condition: object = field(default_factory=threading.Condition, init=False, repr=False)
    browser_latest_frame: object = field(default=None, init=False, repr=False)
    browser_latest_annotated: object = field(default=None, init=False, repr=False)
    browser_pending_events: list = field(default_factory=list, init=False, repr=False)
    browser_worker: object = field(default=None, init=False, repr=False)
    browser_session: int = field(default=0, init=False, repr=False)

    def reset_tracking_state(self):
        """Called when a source changes so IDs cannot leak between cameras."""
        self.people_in_zone.clear()
        self.crowd_active = False
        self.alert_cooldowns.clear()
        # Invalidate any old browser worker/frame when switching sources.
        with self.browser_condition:
            self.browser_session += 1
            self.browser_latest_frame = None
            self.browser_latest_annotated = None
            self.browser_pending_events.clear()
            self.browser_condition.notify_all()

    def clear_zone_state(self):
        """A zone replacement must not retain entry state from the old zone."""
        self.people_in_zone.clear()

    # COCO IDs: person, backpack, handbag, suitcase, car, motorcycle, bus, truck.
    target_classes: tuple = (0, 1, 2, 3, 5, 7, 24, 26, 28)

    def __post_init__(self):
        # Different Haar cascades are complementary. The default cascade is
        # retained for compatibility; alt2 is more tolerant of webcam faces.
        cascade_root = cv2.data.haarcascades
        self.face_detectors = [
            cv2.CascadeClassifier(cascade_root + filename)
            for filename in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
        ]
        self.face_detectors = [detector for detector in self.face_detectors if not detector.empty()]
        self.face_detector = self.face_detectors[0] if self.face_detectors else cv2.CascadeClassifier()

    def load_model(self):
        """Load once without blocking preview frames while it warms up."""
        with self.model_lock:
            if self.model or self.model_error or self.model_loading:
                return
            self.model_loading = True
        try:
            from ultralytics import YOLO

            self.model = YOLO("yolov8n.pt")
            self.class_names = self.model.names
        except Exception as error:  # Dashboard remains usable with a clear status.
            self.model_error = str(error)
        finally:
            with self.model_lock:
                self.model_loading = False

    def should_alert(self, key: str, seconds: float = 12) -> bool:
        now = time.monotonic()
        if now - self.alert_cooldowns.get(key, 0) < seconds:
            return False
        self.alert_cooldowns[key] = now
        return True

    def capture_face(self, frame, person_box):
        """Detect and return the largest visible face only at alert time."""
        x1, y1, x2, y2 = person_box
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
        person = frame[y1:y2, x1:x2]
        if person.size == 0 or not self.face_detectors:
            return None
        # Faces are expected in the upper part of a detected person. Histogram
        # equalisation handles the dark/bright webcam lighting common indoors.
        upper_person = person[:max(1, int(person.shape[0] * .72)), :]
        gray = cv2.cvtColor(upper_person, cv2.COLOR_BGR2GRAY)
        equalized = cv2.equalizeHist(gray)
        faces = []
        for detector in self.face_detectors:
            for candidate in (gray, equalized):
                detected = detector.detectMultiScale(candidate, scaleFactor=1.06, minNeighbors=3, minSize=(24, 24))
                faces.extend(detected)
        if not faces:
            return None
        fx, fy, fw, fh = max(faces, key=lambda face: face[2] * face[3])
        padding = int(max(fw, fh) * .22)
        left, top = max(0, fx - padding), max(0, fy - padding)
        right, bottom = min(upper_person.shape[1], fx + fw + padding), min(upper_person.shape[0], fy + fh + padding)
        return upper_person[top:bottom, left:right].copy()

    @staticmethod
    def capture_body(frame, person_box):
        """Return the detected person's full bounding-box crop as evidence."""
        x1, y1, x2, y2 = person_box
        height, width = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        body = frame[y1:y2, x1:x2]
        return body.copy() if body.size else None

    def _process_sync(self, frame):
        """Run one complete YOLO frame synchronously in the AI worker."""
        self.load_model()
        raw_frame = frame
        height, width = frame.shape[:2]
        zone = np.array([(int(x * width), int(y * height)) for x, y in self.restricted_zone], np.int32) if len(self.restricted_zone) == 4 else None
        events = []
        detections = []

        if self.model:
            # 320 keeps CPU webcam/video streams responsive while retaining
            # enough detail for person-focused surveillance.
            results = self.model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                classes=list(self.target_classes),
                conf=0.32,
                iou=0.45,
                imgsz=320,
                device=0 if self._cuda_available() else "cpu",
                verbose=False,
            )
            result = results[0]
            boxes = result.boxes
            if boxes is not None and len(boxes):
                coordinates = boxes.xyxy.cpu().numpy().astype(int)
                classes = boxes.cls.cpu().numpy().astype(int)
                ids = boxes.id.int().cpu().tolist() if boxes.id is not None else [None] * len(coordinates)
                confidences = boxes.conf.cpu().numpy()
                detections = zip(coordinates, classes, ids, confidences)

        people = 0
        active_zone_ids = set()
        frame = frame.copy()
        if zone is not None:
            overlay = frame.copy()
            cv2.fillPoly(overlay, [zone], (0, 0, 180))
            frame = cv2.addWeighted(overlay, 0.20, frame, 0.80, 0)
            cv2.polylines(frame, [zone], True, (0, 40, 255), 2)
            cv2.putText(frame, "RESTRICTED ZONE", tuple(zone[0] + [4, 22]), cv2.FONT_HERSHEY_SIMPLEX, .5, (30, 30, 255), 2)

        for coordinates, class_id, track_id, confidence in detections:
            # OpenCV's Python bindings do not reliably accept NumPy scalar
            # values for a point tuple on every build. Convert detector output
            # to native Python ints before passing to drawing/geometry APIs.
            x1, y1, x2, y2 = (int(value) for value in coordinates)
            label = self.class_names.get(class_id, str(class_id)).title()
            is_person = class_id == 0
            foot_point = ((x1 + x2) // 2, y2)
            in_zone = is_person and zone is not None and cv2.pointPolygonTest(zone, foot_point, False) >= 0
            color = (0, 40, 255) if in_zone else ((35, 220, 90) if is_person else (255, 185, 40))
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            suffix = f" #{track_id}" if track_id is not None else ""
            cv2.putText(frame, f"{label}{suffix} {confidence:.0%}", (x1, max(22, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, .48, color, 2)
            if is_person:
                people += 1
                zone_key = str(track_id) if track_id is not None else f"box-{x1}-{y1}"
                if in_zone:
                    active_zone_ids.add(zone_key)
                # Keep evidence fresh while someone remains in the restricted
                # area. The per-track cooldown prevents frame-by-frame spam,
                # while allowing a new capture every configured interval.
                if in_zone and self.should_alert(f"zone-{zone_key}", self.alert_cooldown):
                    face = self.capture_face(raw_frame, (x1, y1, x2, y2))
                    body = self.capture_body(raw_frame, (x1, y1, x2, y2))
                    face_note = " Face captured." if face is not None else " Face not visible."
                    body_note = " Full-body evidence captured." if body is not None else ""
                    action = "entered" if zone_key not in self.people_in_zone else "remains in"
                    events.append(("Restricted Area Entry", f"Person #{track_id} {action} the restricted zone.{face_note}{body_note}", "critical", face, body))

        self.people_in_zone = active_zone_ids
        self.person_count = people
        if people > self.crowd_threshold and not self.crowd_active and self.should_alert("crowd", self.alert_cooldown):
            # Event consumers always unpack five fields: type, details,
            # severity, face image and body image. Keep crowd alerts in that
            # same shape so publishing the annotated frame cannot crash.
            events.append(("Crowd Detected", f"{people} people detected; threshold is {self.crowd_threshold}.", "warning", None, None))
            self.crowd_active = True
        elif people <= self.crowd_threshold:
            self.crowd_active = False
        cv2.putText(frame, f"PEOPLE: {people}", (16, 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 3)
        cv2.putText(frame, f"PEOPLE: {people}", (16, 34), cv2.FONT_HERSHEY_SIMPLEX, .8, (20, 230, 100), 1)
        return frame, events

    def _browser_worker_loop(self, session):
        """Run YOLO independently so /api/browser/frame stays fast."""
        while self.running and self.source_name == "Browser Camera" and self.browser_session == session:
            with self.browser_condition:
                while (
                    self.browser_latest_frame is None
                    and self.running
                    and self.source_name == "Browser Camera"
                    and self.browser_session == session
                ):
                    self.browser_condition.wait(timeout=0.5)
                if not self.running or self.source_name != "Browser Camera" or self.browser_session != session:
                    break
                frame = self.browser_latest_frame
                self.browser_latest_frame = None

            try:
                annotated, events = self._process_sync(frame)
                with self.browser_condition:
                    self.browser_latest_annotated = annotated
                    if events:
                        self.browser_pending_events.extend(events)
                    self.browser_condition.notify_all()
            except Exception as error:
                # Keep the local/browser transport alive even if model inference
                # fails. The main API can still return the newest camera frame.
                print(f"⚠️ Browser AI inference error: {error}")

    def _process_browser(self, frame):
        """Accept a browser frame and return the newest available result."""
        with self.browser_condition:
            if self.browser_worker is None or not self.browser_worker.is_alive():
                session = self.browser_session
                self.browser_worker = threading.Thread(
                    target=self._browser_worker_loop,
                    args=(session,),
                    daemon=True,
                    name="sentinel-browser-vision",
                )
                self.browser_worker.start()
            # Latest-frame semantics: never build an inference backlog.
            self.browser_latest_frame = frame
            self.browser_condition.notify()
            annotated = self.browser_latest_annotated
            events = list(self.browser_pending_events)
            self.browser_pending_events.clear()

        if annotated is None:
            # Until the first YOLO result is ready, return the real camera frame.
            # The browser UI already has a local preview, so this is only a
            # transport fallback and never blocks camera visibility.
            annotated = frame.copy()
        return annotated, events

    def process(self, frame):
        """Process one frame, using a non-blocking worker for browser webcams."""
        if self.source_name == "Browser Camera":
            return self._process_browser(frame)
        return self._process_sync(frame)

    @staticmethod
    def _cuda_available():
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False
