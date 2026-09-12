(() => {
    let browserStream = null;
    let browserVideo = null;
    let browserCanvas = null;
    let browserSending = false;
    let browserRequestInFlight = false;
    let browserStopRequested = false;

    function stopBrowserMedia() {
        browserSending = false;
        browserStopRequested = true;

        if (browserStream) {
            browserStream.getTracks().forEach(track => track.stop());
            browserStream = null;
        }

        if (browserVideo) {
            browserVideo.pause();
            browserVideo.srcObject = null;
        }

        browserRequestInFlight = false;
    }

    function makeBrowserCaptureElements() {
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
            if (!browserVideo || !browserCanvas ||
                !browserVideo.videoWidth || !browserVideo.videoHeight) {
                resolve(null);
                return;
            }

            const maxWidth = 640;
            const scale = Math.min(1, maxWidth / browserVideo.videoWidth);
            browserCanvas.width = Math.max(1, Math.round(browserVideo.videoWidth * scale));
            browserCanvas.height = Math.max(1, Math.round(browserVideo.videoHeight * scale));

            const ctx = browserCanvas.getContext("2d", { alpha: false });
            ctx.drawImage(browserVideo, 0, 0, browserCanvas.width, browserCanvas.height);
            browserCanvas.toBlob(resolve, "image/jpeg", 0.72);
        });
    }

    async function sendBrowserFrame() {
        if (!browserSending || browserStopRequested || browserRequestInFlight) return;

        const blob = await captureBlob();
        if (!blob || !browserSending || browserStopRequested) return;

        browserRequestInFlight = true;
        try {
            const data = new FormData();
            data.append("file", blob, "browser-frame.jpg");

            const response = await fetch("/api/browser/frame", {
                method: "POST",
                cache: "no-store",
                body: data
            });

            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                throw new Error(body.detail || `Frame processing failed (${response.status})`);
            }
        } catch (error) {
            if (browserSending) {
                say(error.message, true);
                if (error.message.includes("not active") || error.message.includes("409")) {
                    await stopCamera();
                }
            }
        } finally {
            browserRequestInFlight = false;
        }
    }

    async function browserCaptureLoop() {
        while (browserSending && !browserStopRequested) {
            await sendBrowserFrame();
            if (!browserSending || browserStopRequested) break;
            await new Promise(resolve => setTimeout(resolve, 80));
        }
    }

    window.startCamera = async function startCamera() {
        if (!navigator.mediaDevices?.getUserMedia) {
            say("Browser camera access is unavailable. Use HTTPS or a supported browser.", true);
            return;
        }

        try {
            stopBrowserMedia();
            browserStopRequested = false;
            say("Requesting browser camera permission...");

            browserStream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: "user",
                    width: { ideal: 640 },
                    height: { ideal: 480 }
                },
                audio: false
            });

            makeBrowserCaptureElements();
            browserVideo.srcObject = browserStream;
            await browserVideo.play();

            await endpoint("/api/browser/start", { method: "POST" });

            browserSending = true;
            browserStopRequested = false;
            feedState(true);
            preview();
            say("● BROWSER CAMERA  ● STREAMING  ● AI ANALYSIS ACTIVE");
            browserCaptureLoop();
        } catch (error) {
            stopBrowserMedia();
            stopPreview();
            feedState(false);

            if (error.name === "NotAllowedError") {
                say("Camera permission was denied. Allow camera access and try again.", true);
            } else if (error.name === "NotFoundError") {
                say("No camera was found on this device.", true);
            } else {
                say(error.message || "Could not start browser camera.", true);
            }
        }
    };

    window.stopCamera = async function stopCamera() {
        stopBrowserMedia();

        try {
            await endpoint("/api/camera/stop", { method: "POST" });
        } catch (error) {
            say(error.message, true);
        }

        stopPreview();
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
