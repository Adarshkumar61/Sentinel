(() => {
    let browserStream = null;
    let browserVideo = null;
    let browserCanvas = null;
    let browserSending = false;
    let browserStopRequested = false;
    let sendTimer = null;
    let sending = false;
    let processedStreamReady = false;

    const $id = id => document.getElementById(id);

    function setFeedStream() {
        const stream = $id("stream");
        if (!stream) return;
        processedStreamReady = false;
        stream.style.opacity = "0";
        stream.style.position = "relative";
        stream.style.zIndex = "2";
        stream.src = "/api/stream?browser=" + Date.now();
    }

    function ensureLocalPreview() {
        const feed = $id("feed");
        if (!feed) return;

        if (!browserVideo) {
            browserVideo = document.createElement("video");
            browserVideo.id = "browserLocalPreview";
            browserVideo.autoplay = true;
            browserVideo.playsInline = true;
            browserVideo.muted = true;
            browserVideo.setAttribute("aria-label", "Browser webcam preview");
            Object.assign(browserVideo.style, {
                position: "absolute",
                inset: "0",
                width: "100%",
                height: "100%",
                objectFit: "cover",
                display: "none",
                zIndex: "1",
                background: "#040a12"
            });
            feed.insertBefore(browserVideo, $id("stream"));
        }

        browserVideo.style.display = "block";
    }

    function stopBrowserMedia() {
        browserSending = false;
        browserStopRequested = true;
        sending = false;
        processedStreamReady = false;

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
            browserVideo.style.display = "none";
        }

        const stream = $id("stream");
        if (stream) {
            stream.style.opacity = "1";
            stream.removeAttribute("src");
        }
    }

    function makeCaptureElements() {
        ensureLocalPreview();

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
                console.error("Sentinel browser frame:", error);
                say(error.message || "Browser frame upload failed.", true);
                // Keep the local webcam preview alive. A temporary backend
                // failure must not make it look like the camera itself died.
            }
        } finally {
            sending = false;
        }
    }

    async function captureLoop() {
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

            // Show the real local webcam immediately. This is independent of
            // Render/YOLO latency, so the user can never mistake a backend
            // processing delay for a dead camera.
            feedState(true);
            ensureLocalPreview();

            const startResult = await endpoint("/api/browser/start", { method: "POST" });
            if (!startResult?.ok) {
                throw new Error("Server could not start browser camera mode.");
            }

            browserSending = true;
            browserStopRequested = false;

            if (typeof stopPreview === "function") stopPreview();
            setFeedStream();

            say("● CAMERA LIVE  ● AI ANALYSIS STARTING  ● RESTRICTED-ZONE MONITORING");
            captureLoop();
        } catch (error) {
            console.error("Sentinel webcam start:", error);
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
            console.error("Sentinel webcam stop:", error);
        }
        if (typeof stopPreview === "function") stopPreview();
        feedState(false);
        say("Camera stopped.");
    };

    // When the backend's annotated MJPEG stream produces its first frame,
    // place it over the local preview. Until then the real webcam remains
    // visible, giving immediate feedback while YOLO warms up.
    const processedImage = $id("stream");
    if (processedImage) {
        processedImage.addEventListener("load", () => {
            if (browserSending && !processedStreamReady) {
                processedStreamReady = true;
                processedImage.style.opacity = "1";
                say("● CAMERA LIVE  ● AI ANALYSIS ACTIVE  ● RESTRICTED-ZONE MONITORING");
            }
        });
    }

    const originalSource = window.source;
    window.source = function browserAwareSource(key) {
        if (key !== "webcam") stopBrowserMedia();
        return originalSource(key);
    };

    window.addEventListener("beforeunload", stopBrowserMedia);
})();
