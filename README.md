# SentinelAI

AI-Powered Intelligent Video Surveillance

An MVP CCTV analytics system built with FastAPI, OpenCV and SQLite.

## Included in version 1

- Webcam, uploaded video, or an explicitly configured RTSP/IP-camera input
- Person detection (OpenCV HOG) with lightweight centroid tracking
- Configurable restricted zone
- Per-source persisted rectangle zones, drawable and editable directly on the live view
- Crowd threshold alerts
- Alert/event storage in SQLite
- Decoupled camera display and AI inference loops (the newest frame is always
  displayed, while YOLO/tracking runs at the machine's available speed)
- Live display/AI FPS indicators in the browser dashboard

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## RTSP camera

Select **IP Camera / RTSP**, enter a camera name and `rtsp://` URL, optionally
enter credentials, then use **Test Connection** before **Connect Camera**.
Credentials are used only to open the stream and are not stored in SQLite.
