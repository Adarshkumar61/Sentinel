const endpoint = (p, o = {}) =>
    fetch(p, { cache: "no-store", ...o }).then(async r => {
        const b = await r.json().catch(() => ({}));
        if (!r.ok) throw Error(b.detail || "Request failed");
        return b;
    });

const $ = id => document.getElementById(id);

const zone = [];

let savedZone = [];
let draft = false;
let drag = false;
let start;
let thresholdTimer;
let current = "webcam";
let frameTimer;

const feed = $("feed");
const stream = $("stream");
const overlay = $("zoneOverlay");
const message = $("sourceMessage");


// ============================================================
// GENERAL UI
// ============================================================

function say(text, error = false) {
    message.textContent = text || "";
    message.classList.toggle("error", error);
}

function source(key) {
    current = key;

    document
        .querySelectorAll(".source-tab")
        .forEach(x =>
            x.classList.toggle("active", x.dataset.source === key)
        );

    ["webcam", "upload", "rtsp"].forEach(x => {
        $(x + "Panel").hidden = x !== key;
    });
}

function feedState(on) {
    feed.classList.toggle("idle", !on);
    paint();
}

// ============================================================
// AUTHORIZED PERSONS
// ============================================================

function authorizedSay(text, error = false) {
    const target = $("authorizedMessage");
    target.textContent = text || "";
    target.classList.toggle("error", error);
}

function renderAuthorized(persons) {
    const list = $("authorizedList");
    list.replaceChildren();
    if (!persons.length) {
        list.innerHTML = '<p class="muted">No authorized persons registered.</p>';
        return;
    }
    persons.forEach(person => {
        const row = document.createElement("div");
        row.className = "authorized-person" + (person.active ? "" : " inactive");
        const info = document.createElement("div");
        info.innerHTML = `<div class="authorized-person-name"><span class="authorized-check">${person.active ? "✓" : "○"}</span></div><p class="muted">${person.image_count} face ${person.image_count === 1 ? "embedding" : "embeddings"} · ${person.active ? "Active" : "Disabled"}</p>`;
        info.querySelector(".authorized-person-name").append(document.createTextNode(person.name));
        const actions = document.createElement("div");
        actions.className = "authorized-actions";
        [["Edit", () => editAuthorizedPerson(person)], ["Add Photos", () => addAuthorizedPhotos(person)], [person.active ? "Disable" : "Enable", () => toggleAuthorizedPerson(person)], ["Delete", () => deleteAuthorizedPerson(person)]].forEach(([label, handler]) => {
            const button = document.createElement("button");
            button.className = "ghost authorized-remove";
            button.textContent = label;
            button.onclick = handler;
            actions.append(button);
        });
        row.append(info, actions);
        list.append(row);
    });
}

async function registerAuthorizedPerson() {
    const name = $("authorizedName").value.trim();
    const files = [...$("authorizedPhoto").files];
    if (!name || !files.length) return authorizedSay("Enter a name and choose one to three face photos.", true);
    if (files.length > 3) return authorizedSay("Choose no more than three face photos.", true);
    const data = new FormData();
    data.append("name", name);
    files.forEach(file => data.append("files", file));
    try {
        $("registerAuthorized").disabled = true;
        await endpoint("/api/authorized-faces", { method: "POST", body: data });
        $("authorizedName").value = "";
        $("authorizedPhoto").value = "";
        authorizedSay(`${name} is now authorized.`);
        await refreshAuthorized();
    } catch (error) {
        authorizedSay(error.message, true);
    } finally {
        $("registerAuthorized").disabled = false;
    }
}

async function refreshAuthorized() {
    try { renderAuthorized(await endpoint("/api/authorized-faces")); } catch (_) { /* dashboard stays available */ }
}

async function editAuthorizedPerson(person) {
    const name = prompt("Authorized person name", person.name);
    if (name === null || !name.trim()) return;
    try {
        await endpoint(`/api/authorized-faces/${person.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
        await refreshAuthorized();
    } catch (error) { authorizedSay(error.message, true); }
}

function addAuthorizedPhotos(person) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/jpeg,image/png,image/webp";
    input.multiple = true;
    input.onchange = async () => {
        const files = [...input.files];
        if (!files.length) return;
        if (files.length > 3) return authorizedSay("Choose no more than three face photos.", true);
        const data = new FormData();
        files.forEach(file => data.append("files", file));
        try {
            await endpoint(`/api/authorized-faces/${person.id}/images`, { method: "POST", body: data });
            authorizedSay(`Face photos added for ${person.name}.`);
            await refreshAuthorized();
        } catch (error) { authorizedSay(error.message, true); }
    };
    input.click();
}

async function toggleAuthorizedPerson(person) {
    try {
        await endpoint(`/api/authorized-faces/${person.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: !person.active }) });
        await refreshAuthorized();
    } catch (error) { authorizedSay(error.message, true); }
}

async function deleteAuthorizedPerson(person) {
    if (!confirm(`Delete ${person.name}? This only removes their authorization data.`)) return;
    try {
        await endpoint(`/api/authorized-faces/${person.id}`, { method: "DELETE" });
        authorizedSay(`${person.name} was removed.`);
        await refreshAuthorized();
    } catch (error) { authorizedSay(error.message, true); }
}


// ============================================================
// CAMERA / VIDEO
// ============================================================

document
    .querySelectorAll(".source-tab")
    .forEach(x => x.onclick = () => source(x.dataset.source));

$("videoFile").onchange = e => {
    $("selectedVideo").textContent =
        e.target.files[0]?.name || "";
};

function refreshFrame() {
    if (!feed.classList.contains("idle")) {
        stream.src = "/api/frame?session=" + Date.now();
    }
}

function preview() {
    clearInterval(frameTimer);
    refreshFrame();
    frameTimer = setInterval(refreshFrame, 80);
}

function stopPreview() {
    clearInterval(frameTimer);
    frameTimer = null;
}

async function startCamera() {
    try {
        say("Preparing YOLO detection engine...");
        let result;

        // The server deliberately keeps the physical webcam closed until
        // YOLO is warm. This avoids showing raw camera frames first and then
        // jumping back to delayed annotated frames.
        for (let attempt = 0; attempt < 120; attempt += 1) {
            result = await endpoint("/api/camera/start", { method: "POST" });
            if (result?.ok) break;
            if (!result?.warming) {
                throw new Error(result?.message || "Could not prepare YOLO.");
            }
            await new Promise(resolve => setTimeout(resolve, 250));
        }

        if (!result?.ok) {
            throw new Error("YOLO is taking too long to start. Please try again.");
        }

        feedState(true);
        preview();

    } catch (e) {
        feedState(false);
        stopPreview();
        say(e.message, true);
    }
}

async function stopCamera() {
    try {
        await endpoint("/api/camera/stop", {
            method: "POST"
        });

        stopPreview();
        feedState(false);
        say("Camera stopped.");

    } catch (e) {
        say(e.message, true);
    }
}

async function startUpload() {
    const file = $("videoFile").files[0];

    if (!file) {
        return say(
            "Choose a video before starting analysis.",
            true
        );
    }

    try {
        say(`Uploading ${file.name}...`);

        const data = new FormData();
        data.append("file", file);

        await endpoint("/api/video", {
            method: "POST",
            body: data
        });

        feedState(true);
        preview();

    } catch (e) {
        say(e.message, true);
    }
}

function rtsp() {
    return {
        url: $("rtspUrl").value.trim(),
        name: $("rtspName").value.trim() || "IP Camera",
        username: $("rtspUser").value.trim(),
        password: $("rtspPassword").value
    };
}

async function testRtsp() {
    try {
        say("Connecting...");

        await endpoint("/api/rtsp/test", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(rtsp())
        });

        say("✓ Connection successful");

    } catch (e) {
        say("✕ Connection failed: " + e.message, true);
    }
}

async function connectRtsp() {
    try {
        say("Connecting camera...");

        await endpoint("/api/rtsp/connect", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(rtsp())
        });

        feedState(true);
        preview();

        say(
            "● CONNECTED  ● STREAMING  ● AI ANALYSIS ACTIVE"
        );

    } catch (e) {
        say(e.message, true);
    }
}


// ============================================================
// RESTRICTED ZONE
// ============================================================

function layoutOverlay() {
    const a = stream.getBoundingClientRect();
    const b = feed.getBoundingClientRect();

    Object.assign(overlay.style, {
        left: a.left - b.left + "px",
        top: a.top - b.top + "px",
        width: a.width + "px",
        height: a.height + "px"
    });
}

function paint() {
    layoutOverlay();

    if (
        feed.classList.contains("idle") ||
        zone.length !== 4
    ) {
        overlay.innerHTML = "";
        overlay.style.pointerEvents = "none";
        return;
    }

    const x1 =
        Math.min(zone[0][0], zone[2][0]) * 1000;

    const y1 =
        Math.min(zone[0][1], zone[2][1]) * 1000;

    const x2 =
        Math.max(zone[0][0], zone[2][0]) * 1000;

    const y2 =
        Math.max(zone[0][1], zone[2][1]) * 1000;

    overlay.innerHTML = `
        <rect
            x="${x1}"
            y="${y1}"
            width="${x2 - x1}"
            height="${y2 - y1}"
            fill="rgba(255,40,70,.20)"
            stroke="#ff526b"
            stroke-width="7"
        />

        <text
            x="${x1 + 12}"
            y="${y1 + 28}"
            fill="#fff"
            font-size="24"
            font-weight="bold"
        >
            RESTRICTED ZONE
        </text>
    `;

    overlay.style.pointerEvents =
        draft ? "auto" : "none";
}

function pt(e) {
    const r = overlay.getBoundingClientRect();

    return [
        Math.max(
            0,
            Math.min(
                1,
                (e.clientX - r.left) / r.width
            )
        ),

        Math.max(
            0,
            Math.min(
                1,
                (e.clientY - r.top) / r.height
            )
        )
    ];
}

function actions() {
    const hasZone = zone.length === 4;

    $("drawZone").hidden = draft;
    $("draftActions").hidden = !draft;
    $("savedActions").hidden = draft || !hasZone;
}

function beginZone() {
    savedZone = zone.map(x => [...x]);

    zone.splice(0);

    draft = true;

    actions();
    paint();

    say(
        "Drag on the video to draw a restricted rectangle."
    );
}

function editZone() {
    if (zone.length !== 4) {
        return beginZone();
    }

    savedZone = zone.map(x => [...x]);

    draft = true;

    actions();
    paint();

    say(
        "Drag on the video to replace the rectangle."
    );
}

function cancelZone() {
    zone.splice(
        0,
        zone.length,
        ...savedZone.map(x => [...x])
    );

    draft = false;

    actions();
    paint();

    say(
        zone.length
            ? "Saved zone restored."
            : "Zone drawing cancelled."
    );
}

async function saveZone() {
    if (zone.length !== 4) {
        return say(
            "Draw a rectangle first.",
            true
        );
    }

    try {
        await saveSettings();

        savedZone = zone.map(x => [...x]);

        draft = false;

        actions();
        paint();

        say("✓ Restricted Zone Saved");

    } catch (e) {
        say(e.message, true);
    }
}

async function clearZone() {
    if (
        !zone.length ||
        !confirm("Remove the current restricted zone?")
    ) {
        return;
    }

    try {
        await endpoint("/api/zone", {
            method: "DELETE"
        });

        zone.splice(0);
        savedZone = [];
        draft = false;

        actions();
        paint();

        say("Restricted zone removed.");

    } catch (e) {
        say(e.message, true);
    }
}

overlay.onpointerdown = e => {
    if (!draft) return;

    drag = true;

    start = pt(e);

    overlay.setPointerCapture(e.pointerId);

    zone.splice(
        0,
        zone.length,
        ...[
            start,
            [...start],
            [...start],
            [...start]
        ]
    );

    paint();
};

overlay.onpointermove = e => {
    if (!drag) return;

    const end = pt(e);

    const [x, y] = start;
    const [a, b] = end;

    zone.splice(
        0,
        4,
        [x, y],
        [a, y],
        [a, b],
        [x, b]
    );

    paint();
};

overlay.onpointerup = () => {
    drag = false;
};

window.onresize = paint;

stream.onload = paint;


// ============================================================
// SETTINGS
// ============================================================

async function saveSettings() {
    return endpoint("/api/settings", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            crowd_threshold:
                +$("threshold").value,

            restricted_zone: zone
        })
    });
}

$("threshold").oninput = e => {
    $("thresholdValue").value = e.target.value;

    clearTimeout(thresholdTimer);

    thresholdTimer = setTimeout(
        saveSettings,
        350
    );
};


// ============================================================
// EVIDENCE PHOTO
// ============================================================

function openPhoto(path) {
    $("evidencePhoto").src = path;
    $("photoModal").classList.add("open");
}

function closePhoto(e) {
    if (
        !e ||
        e.target === $("photoModal")
    ) {
        $("photoModal").classList.remove("open");
        $("evidencePhoto").src = "";
    }
}


// ============================================================
// BLOCKCHAIN HELPERS
// ============================================================

const mst = {
    config: null,
    signer: null,
    contract: null
};

function shortValue(value) {
    return value && value.length > 16
        ? `${value.slice(0, 10)}...${value.slice(-6)}`
        : (value || "Unavailable");
}

async function copyValue(value) {
    try {
        await navigator.clipboard.writeText(value);
        walletMessage("Copied to clipboard.");

    } catch (_) {
        walletMessage(
            "Clipboard access was unavailable.",
            true
        );
    }
}

function textElement(tag, text, className) {
    const element = document.createElement(tag);

    element.textContent = text;

    if (className) {
        element.className = className;
    }

    return element;
}

function safeCapturePath(path) {
    return (
        typeof path === "string" &&
        /^\/static\/captures\/(face|body)_[a-zA-Z0-9_.-]+\.jpg$/
            .test(path)
    )
        ? path
        : null;
}

function copyButton(label, value) {
    const button =
        textElement(
            "button",
            label,
            "copy-value"
        );

    button.type = "button";
    button.title = "Copy full value";

    button.onclick = () =>
        copyValue(value);

    return button;
}

function addChainDetail(
    panel,
    label,
    value,
    copyable = false
) {
    const item =
        document.createElement("span");

    item.append(
        `${label}: `,
        copyable
            ? copyButton(shortValue(value), value)
            : document.createTextNode(
                value || "Unavailable"
            )
    );

    panel.append(item);
}


// ============================================================
// BRIDGEKEY
// ============================================================

function walletProvider() {
    return (
        window.bridgekey ||
        window.ethereum ||
        null
    );
}

function walletMessage(
    text,
    error = false
) {
    const el = $("walletStatus");

    el.textContent = text;

    el.classList.toggle(
        "error",
        error
    );
}

function setBridgeButtonConnected(address) {
    const button = $("connectWallet");

    if (!button) return;

    button.textContent =
        "✓ BridgeKey Connected";

    button.style.color = "#55d98a";
    button.style.borderColor = "#55d98a";

    walletMessage(
        `BridgeKey connected: ${shortValue(address)}`
    );
}

function setBridgeButtonDefault() {
    const button = $("connectWallet");

    if (!button) return;

    button.textContent =
        "Connect BridgeKey";

    button.style.color = "";
    button.style.borderColor = "";
}

function setBridgeButtonFailed() {
    const button = $("connectWallet");

    if (!button) return;

    button.textContent =
        "✕ Connection Failed";

    button.style.color = "#ff526b";
    button.style.borderColor = "#ff526b";

    setTimeout(() => {
        setBridgeButtonDefault();
    }, 2000);
}

async function loadMstConfig() {
    try {
        mst.config =
            await endpoint(
                "/api/blockchain/config"
            );

        $("blockchainInfo").textContent =
            `Contract ${mst.config.contractAddress} · ` +
            `Chain ${mst.config.chainId} · ` +
            `Event registration is automated by Sentinel backend.`;

    } catch (error) {
        $("blockchainInfo").textContent =
            "MST configuration unavailable.";

        walletMessage(
            error.message,
            true
        );
    }
}

async function connectBridgeKey() {
    try {
        if (!mst.config) {
            await loadMstConfig();
        }

        const injected =
            walletProvider();

        if (!injected) {
            setBridgeButtonFailed();

            walletMessage(
                "BridgeKey wallet not found. Install/unlock BridgeKey, then refresh.",
                true
            );

            return;
        }

        // Request wallet access.
        await injected.request({
            method: "eth_requestAccounts"
        });

        const provider =
            new ethers.BrowserProvider(
                injected
            );

        let network =
            await provider.getNetwork();

        // Switch to MST Testnet if required.
        if (
            Number(network.chainId) !==
            Number(mst.config.chainId)
        ) {
            const chainId =
                ethers.toBeHex(
                    BigInt(
                        mst.config.chainId
                    )
                );

            try {
                await injected.request({
                    method:
                        "wallet_switchEthereumChain",

                    params: [
                        {
                            chainId
                        }
                    ]
                });

            } catch (error) {

                if (error.code !== 4902) {
                    throw error;
                }

                await injected.request({
                    method:
                        "wallet_addEthereumChain",

                    params: [
                        {
                            chainId,

                            chainName:
                                mst.config.chainName,

                            rpcUrls: [
                                mst.config.rpcUrl
                            ],

                            blockExplorerUrls: [
                                mst.config.explorerUrl
                            ]
                        }
                    ]
                });
            }

            // Refresh provider network after switching.
            network =
                await provider.getNetwork();
        }

        mst.signer =
            await provider.getSigner();

        mst.contract =
            new ethers.Contract(
                mst.config.contractAddress,
                mst.config.abi,
                mst.signer
            );

        const address =
            await mst.signer.getAddress();

        // IMPORTANT:
        // BridgeKey is only connected for the UI.
        // Backend signs Sentinel evidence registrations.
        setBridgeButtonConnected(address);

        walletMessage(
            `✓ BridgeKey connected: ${shortValue(address)}`
        );

    } catch (error) {

        mst.signer = null;
        mst.contract = null;

        setBridgeButtonFailed();

        walletMessage(
            `BridgeKey connection failed: ${
                error.shortMessage ||
                error.message
            }`,
            true
        );
    }
}


// ============================================================
// BLOCKCHAIN READ-ONLY VERIFICATION
// ============================================================

async function verifyEvidence(eventId) {
    try {
        await endpoint(
            `/api/events/${encodeURIComponent(eventId)}/verify`,
            {
                method: "POST"
            }
        );

        walletMessage(
            "Read-only MST verification succeeded."
        );

        await refresh();

    } catch (error) {
        walletMessage(
            `Verification failed: ${error.message}`,
            true
        );
    }
}


// ============================================================
// BLOCKCHAIN EVENT UI
// ============================================================

function renderBlockchainPanel(
    panel,
    event
) {
    const status =
        event.verification_status;

    const blockchainStatus =
        event.blockchain_status;


    // --------------------------------------------------------
    // VERIFIED
    // --------------------------------------------------------

    if (
        status === "VERIFIED" ||
        blockchainStatus === "CONFIRMED"
    ) {
        panel.append(
            textElement(
                "b",
                "✓ BLOCKCHAIN VERIFIED",
                "chain-status verified"
            )
        );

        addChainDetail(
            panel,
            "Evidence ID",
            event.event_id,
            true
        );

        addChainDetail(
            panel,
            "Local SHA-256",
            event.evidence_hash,
            true
);
if (
    event.evidence_hash &&
    event.on_chain_evidence_hash
) {
    const localHash =
        event.evidence_hash
            .replace(/^0x/, "")
            .toLowerCase();

    const chainHash =
        event.on_chain_evidence_hash
            .replace(/^0x/, "")
            .toLowerCase();

    addChainDetail(
        panel,
        "Hash Match",
        localHash === chainHash
            ? "✓ MATCHED"
            : "✕ MISMATCH",
        false
    );
}
addChainDetail(
    panel,
    "On-Chain SHA-256",
    event.on_chain_evidence_hash,
    true
);

        addChainDetail(
            panel,
            "Registered",
            event.blockchain_registered_at
                ? new Date(
                    event.blockchain_registered_at
                ).toLocaleString()
                : "Unavailable"
        );

        addChainDetail(
            panel,
            "Wallet",
            event.registered_by,
            true
        );

        if (event.blockchain_tx) {
            const link =
                textElement(
                    "a",
                    `View transaction ${shortValue(
                        event.blockchain_tx
                    )}`,
                    "tx-link"
                );

            link.href =
                `${mst.config?.explorerUrl || ""}/tx/${event.blockchain_tx}`;

            link.target = "_blank";
            link.rel = "noopener";

            panel.append(link);
        }

        return;
    }


    // --------------------------------------------------------
    // REGISTERING
    // --------------------------------------------------------

    if (
        blockchainStatus === "REGISTERING"
    ) {
        panel.append(
            textElement(
                "b",
                "🔄 REGISTERING ON MST TESTNET",
                "chain-status pending"
            )
        );

        addChainDetail(
            panel,
            "Evidence ID",
            event.event_id,
            true
        );

        addChainDetail(
            panel,
            "Hash",
            event.evidence_hash,
            true
        );

        return;
    }


    // --------------------------------------------------------
    // FAILED
    // --------------------------------------------------------

    if (
        blockchainStatus === "FAILED"
    ) {
        panel.append(
            textElement(
                "b",
                "❌ MST REGISTRATION FAILED",
                "chain-status failed"
            )
        );

        addChainDetail(
            panel,
            "Evidence ID",
            event.event_id,
            true
        );

        if (event.evidence_hash) {
            addChainDetail(
                panel,
                "Hash",
                event.evidence_hash,
                true
            );
        }

        if (event.blockchain_error) {
            addChainDetail(
                panel,
                "Error",
                event.blockchain_error
            );
        }

        return;
    }


    // --------------------------------------------------------
    // PENDING
    // --------------------------------------------------------

    if (
        blockchainStatus === "PENDING" ||
        !blockchainStatus
    ) {
        panel.append(
            textElement(
                "b",
                "⏳ PENDING — MST REGISTRATION QUEUED",
                "chain-status pending"
            )
        );

        addChainDetail(
            panel,
            "Evidence ID",
            event.event_id,
            true
        );

        if (event.evidence_hash) {
            addChainDetail(
                panel,
                "Hash",
                event.evidence_hash,
                true
            );
        }

        return;
    }


    // --------------------------------------------------------
    // LEGACY / MANUAL TRANSACTION RECOVERY
    // --------------------------------------------------------

    if (
        event.blockchain_tx &&
        status !== "VERIFIED"
    ) {
        panel.append(
            textElement(
                "b",
                "PENDING / VERIFY",
                "chain-status pending"
            )
        );

        const verify =
            textElement(
                "button",
                "Verify on MST",
                "ghost register-evidence"
            );

        verify.type = "button";

        verify.onclick =
            () => verifyEvidence(
                event.event_id
            );

        panel.append(verify);
    }
}


// ============================================================
// MAIN EVENT RENDER
// ============================================================

function render(events) {
    window.sentinelEvents = events;

    const list = $("eventList");

    list.replaceChildren();

    if (!events.length) {
        list.append(
            textElement(
                "p",
                "No alerts recorded yet.",
                "muted"
            )
        );

        return;
    }

    events.forEach(event => {

        const row =
            document.createElement("div");

        row.className =
            `event${
                event.image_path ||
                event.body_image_path
                    ? " has-photo"
                    : ""
            }`;


        // ----------------------------------------------------
        // Evidence photos
        // ----------------------------------------------------

        const captures = [
            safeCapturePath(
                event.image_path
            ),

            safeCapturePath(
                event.body_image_path
            )

        ].filter(Boolean);


        if (captures.length) {

            const holder =
                document.createElement("span");

            captures.forEach(
                (path, index) => {

                    const button =
                        document.createElement(
                            "button"
                        );

                    button.type = "button";

                    button.className =
                        `face-thumb${
                            index
                                ? " body-thumb"
                                : ""
                        }`;

                    button.title =
                        "View captured evidence";

                    button.onclick =
                        () => openPhoto(path);

                    const image =
                        document.createElement(
                            "img"
                        );

                    image.src = path;
                    image.alt =
                        "Captured evidence";

                    button.append(image);

                    holder.append(button);
                }
            );

            row.append(holder);

        } else {

            row.append(
                textElement(
                    "span",
                    "NO\nEVIDENCE",
                    "no-face"
                )
            );
        }


        // ----------------------------------------------------
        // Time
        // ----------------------------------------------------

        row.append(
            textElement(
                "span",
                new Date(
                    event.created_at
                ).toLocaleString(),
                "time"
            )
        );


        // ----------------------------------------------------
        // Event details
        // ----------------------------------------------------

        const details =
            document.createElement(
                "span"
            );

        details.append(
            textElement(
                "b",
                event.event_type ||
                    "Security event"
            )
        );

        details.append(
            document.createElement("br")
        );

        details.append(
            textElement(
                "small",
                `Camera: ${
                    event.camera || "Unknown"
                } · ${
                    event.details || ""
                }`,
                "muted"
            )
        );

        row.append(details);


        // ----------------------------------------------------
        // Severity
        // ----------------------------------------------------

        row.append(
            textElement(
                "span",
                (
                    event.severity ||
                    "warning"
                ).toUpperCase(),

                `severity ${
                    event.severity ===
                    "critical"
                        ? "critical"
                        : "warning"
                }`
            )
        );


        // ----------------------------------------------------
        // Blockchain panel
        // ----------------------------------------------------

        if (event.event_id) {

            const panel =
                document.createElement(
                    "div"
                );

            panel.className =
                "blockchain-event";

            renderBlockchainPanel(
                panel,
                event
            );

            if (panel.childNodes.length) {
                row.append(panel);
            }
        }

        list.append(row);
    });
}


// ============================================================
// DELETE ALERTS
// ============================================================

async function deleteAlerts() {
    if (
        !confirm(
            "Delete all recorded alerts and captured evidence?"
        )
    ) {
        return;
    }

    try {
        await endpoint(
            "/api/events/clear",
            {
                method: "POST"
            }
        );

        await refresh();

    } catch (error) {
        say(error.message, true);
    }
}


// ============================================================
// REFRESH DASHBOARD
// ============================================================

async function refresh() {
    try {

        const [
            status,
            events
        ] = await Promise.all([
            endpoint("/api/status"),
            endpoint("/api/events")
        ]);


        $("persons").textContent =
            status.persons;

        $("alerts").textContent =
            status.alerts;

        $("analysis").textContent =
            status.error
                ? "Camera Error"
                : status.running
                    ? "Active"
                    : "Standby";

        $("sourceKind").textContent =
            status.camera;

        $("cameraName").textContent =
            status.camera;

        $("systemStatus").textContent =
            status.error
                ? "● CAMERA DISCONNECTED"
                : status.running
                    ? "● CONNECTED"
                    : "● OFFLINE";

        $("systemStatus").className =
            "status " +
            (
                status.running
                    ? "on"
                    : ""
            );

        $("reconnectButton").hidden =
            !(
                status.source_kind ===
                "rtsp" &&
                status.error
            );


        if (
            document.activeElement !==
            $("threshold")
        ) {
            $("threshold").value =
                status.crowd_threshold;

            $("thresholdValue").value =
                status.crowd_threshold;
        }


        feedState(
            status.running
        );


        if (
            status.running &&
            !frameTimer
        ) {
            preview();
        }


        if (!status.running) {
            stopPreview();
        }


        if (
            !draft &&
            JSON.stringify(zone) !==
            JSON.stringify(
                status.restricted_zone
            )
        ) {

            zone.splice(
                0,
                zone.length,
                ...status.restricted_zone
            );

            savedZone =
                zone.map(x => [...x]);

            actions();
            paint();
        }


        // IMPORTANT:
        // Every second this gets fresh DB data.
        // Therefore:
        //
        // PENDING
        //     ↓
        // REGISTERING
        //     ↓
        // VERIFIED
        //
        // appears live on the dashboard.

        render(events);
        refreshAuthorized();

    } catch (_) {
        // Keep dashboard alive if one refresh fails.
    }
}


// ============================================================
// INITIALIZATION
// ============================================================

stream.onerror = () => {
    if (
        !feed.classList.contains("idle")
    ) {
        setTimeout(
            refreshFrame,
            250
        );
    }
};

source("webcam");

actions();
paint();

refresh();
refreshAuthorized();

setInterval(
    refresh,
    1000
);


// ============================================================
// BRIDGEKEY BUTTON
// ============================================================

$("connectWallet").onclick =
    connectBridgeKey;

loadMstConfig();
