(() => {
    let browserStream = null;
    let browserVideo = null;
    let browserCanvas = null;
    let browserSending = false;
    let browserStopRequested = false;
    let sendTimer = null;
    let sending = false;

    const $id = id => document.getElementById(id);

    function setFeedStream(useProcessedStream = true) {
        const stream = $id("stream");
        if (!stream) return;
        stream.src = useProcessedStream
            ? "/api/stream?browser=" + Date.now()
            : "/api/frame?session=" + Date.now();
    }

    function stopBrowserMedia() {
        browserSending = false;
        browserStopRequested = true;
        sending = false;
        if (sendTimer) {
            clearTimeout(sendTimer);
            sendTimer = null;
        }
        if (browserStream) {
            browserStream.getTracks().forEach(track => track.stop());
            browserStream = null;
        }
        if (browserVideo) {
            browserVideo.pause();
            browserVideo.srcObject = null;
        }
    }

    function makeCaptureElements() {
        if (!browserVideo) {
            browserVideo = document.createElement("video");
            browserVideo.autoplay = true;
            browserVideo.playsInline = true;
            browserVideo.muted = true;
            browserVideo.style.display = "none";
            document.body.appendChild(browserVideo);
        }
        if (!browserCanvas) {
            browserCanvas = document.createElement("canvas");
            browserCanvas.style.display = "none";
            document.body.appendChild(browserCanvas);
        }
    }

    function captureBlob() {
        return new Promise(resolve => {
            if (!browserVideo?.videoWidth || !browserVideo?.videoHeight) {
                resolve(null);
                return;
            }

            // 640px input keeps network + JPEG overhead low while preserving
            // enough detail for person/foot-point restricted-zone detection.
            const maxWidth = 640;
            const scale = Math.min(1, maxWidth / browserVideo.videoWidth);
            browserCanvas.width = Math.max(1, Math.round(browserVideo.videoWidth * scale));
            browserCanvas.height = Math.max(1, Math.round(browserVideo.videoHeight * scale));

            const ctx = browserCanvas.getContext("2d", {
                alpha: false,
                desynchronized: true
            });
            ctx.drawImage(browserVideo, 0, 0, browserCanvas.width, browserCanvas.height);
            browserCanvas.toBlob(resolve, "image/jpeg", 0.65);
        });
    }

    async function sendOneFrame() {
        if (!browserSending || browserStopRequested || sending) return;

        const blob = await captureBlob();
        if (!blob || !browserSending || browserStopRequested) return;

        sending = true;
        try {
            const data = new FormData();
            data.append("file", blob, "browser-frame.jpg");

            const response = await fetch("/api/browser/frame", {
                method: "POST",
                cache: "no-store",
                body: data,
                keepalive: false
            });

            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                throw new Error(body.detail || `Frame upload failed (${response.status})`);
            }
        } catch (error) {
            if (browserSending) {
                say(error.message || "Browser frame upload failed.", true);
                if (error.message.includes("not active")) {
                    await window.stopCamera();
                    return;
                }
            }
        } finally {
            sending = false;
        }
    }

    async function captureLoop() {
        // Controlled cadence prevents bandwidth/CPU saturation. The backend
        // accepts frames quickly and keeps only the newest frame for AI work.
        while (browserSending && !browserStopRequested) {
            await sendOneFrame();
            if (!browserSending || browserStopRequested) break;
            await new Promise(resolve => {
                sendTimer = setTimeout(resolve, 90);
            });
        }
    }

    window.startCamera = async function startBrowserCamera() {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            say("Browser webcam requires HTTPS (or localhost) and camera permission.", true);
            return;
        }

        try {
            stopBrowserMedia();
            browserStopRequested = false;
            say("Requesting browser camera permission...");

            browserStream = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    facingMode: "user",
                    width: { ideal: 640, max: 1280 },
                    height: { ideal: 480, max: 720 },
                    frameRate: { ideal: 15, max: 20 }
                }
            });

            makeCaptureElements();
            browserVideo.srcObject = browserStream;
            await browserVideo.play();

            const startResult = await endpoint("/api/browser/start", { method: "POST" });
            if (!startResult?.ok) {
                throw new Error("Server could not start browser camera mode.");
            }

            browserSending = true;
            browserStopRequested = false;
            feedState(true);

            // Use the long-lived multipart endpoint for the processed display.
            // This avoids the old 80ms /api/frame polling loop and always shows
            // the newest annotated AI frame produced by the backend.
            if (typeof stopPreview === "function") stopPreview();
            setFeedStream(true);

            say("● BROWSER CAMERA  ● LIVE AI  ● RESTRICTED-ZONE MONITORING");
            captureLoop();
        } catch (error) {
            stopBrowserMedia();
            if (typeof stopPreview === "function") stopPreview();
            feedState(false);

            if (error.name === "NotAllowedError" || error.name === "SecurityError") {
                say("Camera permission was denied. Allow camera access for this site and try again.", true);
            } else if (error.name === "NotFoundError") {
                say("No camera was found on this device.", true);
            } else {
                say(error.message || "Could not start browser camera.", true);
            }
        }
    };

    window.stopCamera = async function stopBrowserCamera() {
        stopBrowserMedia();
        try {
            await endpoint("/api/camera/stop", { method: "POST" });
        } catch (error) {
            say(error.message, true);
        }
        if (typeof stopPreview === "function") stopPreview();
        feedState(false);
        say("Camera stopped.");
    };

    const originalSource = window.source;
    window.source = function browserAwareSource(key) {
        if (key !== "webcam") stopBrowserMedia();
        return originalSource(key);
    };

    window.addEventListener("beforeunload", stopBrowserMedia);
})();
