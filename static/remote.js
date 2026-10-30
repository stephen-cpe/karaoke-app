// --- KARAOKE APP PHONE REMOTE --------------------------------------------
// This page is intentionally audio-free. It observes the shared session and
// sends commands to the server; only the host player page owns an Audio object.
// State arrives over SSE, with polling as a fallback (and as a slow heartbeat).

(() => {
  "use strict";

  const CLIENT_ID = (() => {
    try {
      const key = "karaoke.remote.clientId";
      const existing = sessionStorage.getItem(key);
      if (existing) return existing;
      const created = (crypto.randomUUID && crypto.randomUUID()) ||
        `remote-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      sessionStorage.setItem(key, created);
      return created;
    } catch {
      return `remote-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
  })();

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SSE_RETRY_MAX_MS = 15000;
  const POLL_INTERVAL_MS = 2500;
  const HEARTBEAT_INTERVAL_MS = 4000;

  // --- DOM ---------------------------------------------------------------
  const ui = {
    connectionPill: document.getElementById("connection-pill"),
    connectionText: document.getElementById("connection-text"),
    installButton: document.getElementById("install-button"),
    offlineBanner: document.getElementById("offline-banner"),
    offlineBannerText: document.getElementById("offline-banner-text"),
    nowArt: document.getElementById("now-art"),
    nowStatus: document.getElementById("now-status"),
    nowTitle: document.getElementById("now-title"),
    nowArtist: document.getElementById("now-artist"),
    keyBadge: document.getElementById("key-badge"),
    keyValue: document.getElementById("key-value"),
    lyricsLine: document.getElementById("lyrics-line"),
    lyricsNext: document.getElementById("lyrics-next"),
    currentTime: document.getElementById("current-time"),
    durationTime: document.getElementById("duration-time"),
    seekSlider: document.getElementById("seek-slider"),
    restartButton: document.getElementById("restart-button"),
    playPauseButton: document.getElementById("play-pause-button"),
    skipButton: document.getElementById("skip-button"),
    keyDown: document.getElementById("key-down"),
    keyUp: document.getElementById("key-up"),
    keyReadout: document.getElementById("key-readout"),
    keyScale: document.getElementById("key-scale"),
    volumeSlider: document.getElementById("volume-slider"),
    volumeValue: document.getElementById("volume-value"),
    tabQueue: document.getElementById("tab-queue"),
    tabSongs: document.getElementById("tab-songs"),
    queuePanel: document.getElementById("queue-panel"),
    songsPanel: document.getElementById("songs-panel"),
    queueTabCount: document.getElementById("queue-tab-count"),
    clearQueue: document.getElementById("clear-queue"),
    stopClear: document.getElementById("stop-clear"),
    queueList: document.getElementById("queue-list"),
    searchInput: document.getElementById("search-input"),
    clearSearch: document.getElementById("clear-search"),
    songSummary: document.getElementById("song-summary"),
    songList: document.getElementById("song-list"),
    toast: document.getElementById("toast")
  };

  // --- SHARED STATE ------------------------------------------------------
  let shared = null;                 // Latest server snapshot
  let receivedAt = 0;                // performance.now() when it arrived
  let reachable = false;             // Can this phone reach Flask?
  let hadNetworkError = false;
    let live = false;                  // SSE connection is open
  let eventSource = null;
  let reconnectTimer = null;
  let reconnectAttempts = 0;
  let pollTimer = null;
  let pollInFlight = false;
  let heartbeatTimer = null;

  // Per-song lyric data. Word timing sidecars are preferred; .srt is fallback.
  const lyricsCache = new Map();
  let lyricsLines = [];
  let hasWordTimings = false;
  let lyricsState = "idle";          // idle | loading | ready | missing
  let lyricsToken = 0;
  let activeLineIndex = -1;
  let activeWordIndex = -1;
  let gapRendered = false;
  let lastNextLine = "";
  let currentKey = null;
  let lastNowSignature = "";
  let lastQueueSignature = null;
  let lastProgressSecond = -1;
  let lastProgressDuration = "";
  let lastAnnouncedError = null;

  // Controls / interaction state
  let transportBusy = false;
  let keyBusy = false;
  let queueBusy = false;
  let seekDragging = false;
  let seekTimer = null;
  let lastSeekValue = null;
  let lastSeekAt = 0;
  let volumeTimer = null;
  let volumeHoldUntil = 0;
  let lastVolumeValue = null;
  let lastVolumeAt = 0;
  const addPending = new Set();

  // Library / tabs / PWA
  let songs = [];
  let libraryState = "loading";      // loading | ready | error
  let searchQuery = "";
  let activeTab = "queue";
  let deferredInstallPrompt = null;
  let toastTimer = null;

  // Playback loop handle (requestAnimationFrame while playing, timeout while
  // paused). The remote never plays media; this only advances the lyric clock.
  let playbackHandle = null;
  let playbackIsRaf = false;

  // --- HELPERS -----------------------------------------------------------
  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  function makeIcon(name, className = "") {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon" + (className ? " " + className : ""));
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#icon-" + name);
    svg.appendChild(use);
    return svg;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function pluralize(count, singular, plural = singular + "s") {
    return count === 1 ? singular : plural;
  }

  function formatPlaybackTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) {
      return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function signedSemitones(value) {
    return value > 0 ? `+${value}` : String(value);
  }

  function fileStem(filename) {
    return String(filename || "").replace(/\.[^.]+$/, "");
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return {};
    }
  }

  function isNetworkError(err) {
    return err instanceof TypeError ||
      (err && (err.name === "NetworkError" || err.name === "AbortError"));
  }

  function userMessage(err, fallback) {
    if (isNetworkError(err)) {
      return "Can't reach the karaoke server. Check the Wi-Fi connection.";
    }
    if (err && err.message) return err.message;
    return fallback || "Something went wrong.";
  }

  function artGradient(song) {
    const seed = String(song.id || song.title || "karaoke");
    let hash = 0;
    for (let i = 0; i < seed.length; i++) {
      hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
    }
    const hueA = hash % 360;
    const hueB = (hueA + 48) % 360;
    return `linear-gradient(135deg, hsl(${hueA} 34% 28%), hsl(${hueB} 40% 13%))`;
  }

  function showToast(message, kind = "") {
    if (!ui.toast) return;
    const text = String(message || "").trim();
    if (!text) return;
    ui.toast.textContent = text.length > 220 ? text.slice(0, 217) + "…" : text;
    ui.toast.className = "toast" + (kind ? " is-" + kind : "");
    ui.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      ui.toast.hidden = true;
    }, 3400);
  }

  function markReachable() {
    reachable = true;
    hadNetworkError = false;
    updateConnectionUI();
    renderTransport();
    renderKey();
    renderVolume();
    renderQueueCounts();
    renderProgress();
    schedulePlaybackLoop();
  }

  function markUnreachable() {
    reachable = false;
    hadNetworkError = true;
    live = false;
    closeEvents();
    stopPlaybackLoop();
    updateConnectionUI();
    renderTransport();
    renderKey();
    renderVolume();
    renderQueueCounts();
    renderProgress();
    startPolling();
    scheduleReconnect(1500);
  }

  // --- STATE INTAKE ------------------------------------------------------

  function applyState(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;

    const revision = Number(snapshot.revision);
    if (shared && snapshot.server_id && shared.server_id === snapshot.server_id &&
        Number.isFinite(revision) && revision < (Number(shared.revision) || 0)) {
      return; // An older poll/SSE response arrived after a newer one.
    }

    const previousStatus = shared ? shared.status : null;
    shared = {
      server_id: snapshot.server_id || null,
      revision: Number.isFinite(revision) ? revision : 0,
      host_connected: !!snapshot.host_connected,
      status: typeof snapshot.status === "string" ? snapshot.status : "idle",
      position: Number.isFinite(Number(snapshot.position))
        ? Math.max(0, Number(snapshot.position)) : 0,
      duration: Number.isFinite(Number(snapshot.duration))
        ? Math.max(0, Number(snapshot.duration)) : 0,
      current: snapshot.current && typeof snapshot.current === "object"
        ? snapshot.current : null,
      queue: Array.isArray(snapshot.queue)
        ? snapshot.queue.filter(song => song && song.filename) : [],
      transpose: Number.isFinite(Number(snapshot.transpose))
        ? clamp(Math.trunc(Number(snapshot.transpose)), -4, 4) : 0,
      volume: Number.isFinite(Number(snapshot.volume))
        ? clamp(Number(snapshot.volume), 0, 1) : 1,
      error: snapshot.error ? String(snapshot.error) : null
    };
    receivedAt = performance.now();
    reachable = true;
    hadNetworkError = false;

    const song = shared.current;
    const nextKey = song ? song.filename : null;
    if (nextKey !== currentKey) {
      currentKey = nextKey;
      activeLineIndex = -1;
      activeWordIndex = -1;
      gapRendered = false;
      lastNextLine = "";
      loadLyricsFor(song);
    }

    if (!shared.error) lastAnnouncedError = null;
    if (shared.error && shared.error !== lastAnnouncedError) {
      lastAnnouncedError = shared.error;
      showToast(shared.error, "error");
    }

    if (shared.status !== previousStatus) {
      stopPlaybackLoop();
      schedulePlaybackLoop();
    }

    updateConnectionUI();
    renderNowPlaying();
    renderTransport();
    renderKey();
    renderVolume();
    renderQueueCounts();
    renderProgress();

    const queueSignature = queueFingerprint();
    if (queueSignature !== lastQueueSignature) renderQueue();

    schedulePlaybackLoop();
    updateLyrics(projectedPosition());
  }

  function projectedPosition() {
    if (!shared || !shared.current) return 0;
    let position = Number(shared.position) || 0;
    if (shared.status === "playing" && canControl()) {
      position += Math.max(0, performance.now() - receivedAt) / 1000;
    }
    if (shared.duration > 0) position = Math.min(position, shared.duration);
    return Math.max(0, position);
  }

  function hostIsOnline() {
    // The server owns host presence. A regular heartbeat below makes the
    // server notice a player that stopped reporting, even when SSE is quiet.
    return !!(shared && shared.host_connected);
  }

  function canControl() {
    return reachable && hostIsOnline();
  }

  // --- CONNECTION --------------------------------------------------------

  function updateConnectionUI() {
    if (!ui.connectionPill) return;

    const online = hostIsOnline();
    let text = "Connecting…";
    let stateClass = "is-waiting";
    let banner = "";

    if (!reachable) {
      if (hadNetworkError) {
        text = "Server offline";
        stateClass = "is-offline";
        banner = navigator.onLine === false
          ? "This phone is offline. Reconnect to Wi-Fi to control the player."
          : "Can't reach the karaoke server. Retrying…";
      }
    } else if (!shared) {
      text = "Connecting…";
    } else if (!online) {
      text = "Player offline";
      stateClass = "is-waiting";
      banner = "The player is offline. Controls will resume when the TV page reconnects.";
    } else {
      text = "Player online";
      stateClass = "is-online";
    }

    ui.connectionPill.classList.remove("is-online", "is-waiting", "is-offline");
    ui.connectionPill.classList.add(stateClass);
    if (ui.connectionText) ui.connectionText.textContent = text;

    if (ui.offlineBanner) {
      ui.offlineBanner.hidden = !banner;
      if (ui.offlineBannerText && banner) ui.offlineBannerText.textContent = banner;
    }
  }

  function closeEvents() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    live = false;
  }

  async function refreshState() {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      const response = await fetch("/api/state", {
        headers: { "Accept": "application/json" },
        cache: "no-store"
      });
      const data = await readJson(response);
      markReachable();
      if (response.ok && data.state) {
        applyState(data.state);
      } else if (!response.ok) {
        console.warn("State request failed:", data.error || response.status);
      }
    } catch (err) {
      if (isNetworkError(err) && !live) {
        reachable = false;
        hadNetworkError = true;
        updateConnectionUI();
        renderTransport();
        renderKey();
        renderVolume();
        renderProgress();
        renderQueueCounts();
      }
    } finally {
      pollInFlight = false;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshState, POLL_INTERVAL_MS);
    refreshState();
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function startHeartbeat() {
    if (heartbeatTimer) return;
    heartbeatTimer = setInterval(() => {
      if (document.hidden) return;
      refreshState();
    }, HEARTBEAT_INTERVAL_MS);
  }

  function scheduleReconnect(delay = null) {
    if (eventSource || reconnectTimer) return;
    const wait = delay === null
      ? Math.min(SSE_RETRY_MAX_MS, 1500 * Math.pow(1.7, reconnectAttempts))
      : delay;
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (!document.hidden) openEvents();
    }, wait);
  }

  function openEvents() {
    if (eventSource) return;
    if (!("EventSource" in window)) {
      startPolling();
      return;
    }

    let source;
    try {
      source = new EventSource(
        `/api/events?role=remote&client_id=${encodeURIComponent(CLIENT_ID)}`);
    } catch {
      startPolling();
      scheduleReconnect(2000);
      return;
    }
    eventSource = source;

    source.onopen = () => {
      if (source !== eventSource) return;
      live = true;
      reconnectAttempts = 0;
      markReachable();
      stopPolling();
      startHeartbeat();
      refreshState();
    };

    source.addEventListener("state", event => {
      if (source !== eventSource) return;
      live = true;
      try {
        applyState(JSON.parse(event.data));
      } catch (err) {
        console.warn("Ignoring malformed state event:", err);
      }
    });

    source.onerror = () => {
      if (source !== eventSource) return;
      live = false;
      source.close();
      eventSource = null;
      updateConnectionUI();
      startPolling();
      scheduleReconnect();
    };
  }

  // --- RENDERING: NOW PLAYING -------------------------------------------

  function renderNowPlaying() {
    const song = shared ? shared.current : null;
    const hasSong = !!song;
    const status = shared ? shared.status : "idle";

    document.body.classList.toggle("has-song", hasSong);
    document.body.classList.toggle("is-playing", hasSong && status === "playing");

    const signature = song
      ? [song.filename, song.title, song.artist, song.id].join("\u0001")
      : "";
    if (signature !== lastNowSignature) {
      lastNowSignature = signature;
      if (ui.nowArt) {
        ui.nowArt.style.background = song ? artGradient(song) : "";
      }
    }

    if (ui.nowTitle) {
      ui.nowTitle.textContent = song ? song.title || "Unknown song" : "Choose a song";
      ui.nowTitle.title = song ? song.title || "" : "";
    }
    if (ui.nowArtist) {
      ui.nowArtist.textContent = song
        ? song.artist || "Unknown artist"
        : "Add something from the library below.";
      ui.nowArtist.title = song ? song.artist || "" : "";
    }

    if (ui.nowStatus) {
      let label = "Nothing playing";
      let statusClass = "is-muted";
      if (song) {
        if (status === "playing") label = "Now playing";
        else if (status === "loading") label = "Loading";
        else if (status === "paused") label = "Paused";
        else if (status === "error") {
          label = "Playback problem";
          statusClass = "is-error";
        } else {
          label = "Ready";
        }
      }
      ui.nowStatus.textContent = label;
      ui.nowStatus.className = "now-status" + (statusClass ? " " + statusClass : "");
    }

    const transpose = shared ? shared.transpose : 0;
    if (ui.keyValue) ui.keyValue.textContent = signedSemitones(transpose);
    if (ui.keyBadge) {
      ui.keyBadge.title = `Current song key: ${signedSemitones(transpose)} semitones`;
    }
  }

  function renderTransport() {
    const song = shared ? shared.current : null;
    const queue = shared ? shared.queue : [];
    const online = canControl();
    const loading = shared ? shared.status === "loading" : false;
    const playing = shared ? shared.status === "playing" : false;
    const canStart = !!song || queue.length > 0;

    if (ui.playPauseButton) {
      ui.playPauseButton.disabled = !online || transportBusy || loading || !canStart;
      ui.playPauseButton.classList.toggle("is-playing", playing);
      ui.playPauseButton.title = playing ? "Pause" : "Play";
      ui.playPauseButton.setAttribute("aria-label", playing ? "Pause" : "Play");
    }
    if (ui.restartButton) {
      ui.restartButton.disabled = !online || transportBusy || !song || loading;
    }
    if (ui.skipButton) {
      ui.skipButton.disabled = !online || transportBusy || !canStart;
    }
  }

  function renderKey() {
    const transpose = shared ? shared.transpose : 0;
    const online = canControl();
    const hasSong = !!(shared && shared.current);

    if (ui.keyReadout) ui.keyReadout.textContent = signedSemitones(transpose);

    if (ui.keyScale) {
      const dots = ui.keyScale.querySelectorAll("i");
      dots.forEach((dot, index) => {
        dot.classList.toggle("is-active", index === transpose + 4);
        dot.classList.toggle("is-center", index === 4);
      });
    }

    if (ui.keyDown) {
      ui.keyDown.disabled = !online || !hasSong || keyBusy || transpose <= -4;
    }
    if (ui.keyUp) {
      ui.keyUp.disabled = !online || !hasSong || keyBusy || transpose >= 4;
    }
  }

  function renderVolume() {
    if (!ui.volumeSlider) return;
    const online = canControl();
    ui.volumeSlider.disabled = !online;

    // Do not fight the user's finger while they are adjusting the slider.
    if (performance.now() < volumeHoldUntil) return;

    const volume = shared ? shared.volume : 1;
    const percent = Math.round(clamp(volume, 0, 1) * 100);
    ui.volumeSlider.value = String(percent);
    ui.volumeSlider.style.setProperty("--volume-percent", percent + "%");
    if (ui.volumeValue) ui.volumeValue.textContent = percent + "%";
    ui.volumeSlider.setAttribute("aria-valuetext", percent + "% volume");
  }

  function renderQueueCounts() {
    const queue = shared ? shared.queue : [];
    const hasSong = !!(shared && shared.current);
    if (ui.queueTabCount) ui.queueTabCount.textContent = String(queue.length);
    if (ui.clearQueue) {
      ui.clearQueue.disabled = !reachable || queueBusy || queue.length === 0;
    }
    if (ui.stopClear) {
      ui.stopClear.disabled = !reachable || queueBusy || (!hasSong && queue.length === 0);
    }
  }

  // --- RENDERING: PROGRESS ----------------------------------------------

  function updateProgressDisplay(now) {
    if (!ui.seekSlider || !shared) return;

    const duration = shared.duration;
    const ratio = duration > 0 ? clamp(now / duration, 0, 1) : 0;
    const sliderValue = String(Math.round(ratio * 1000));
    if (ui.seekSlider.value !== sliderValue) ui.seekSlider.value = sliderValue;
    ui.seekSlider.style.setProperty("--seek-percent", (ratio * 100) + "%");

    const whole = Math.floor(now);
    if (whole !== lastProgressSecond) {
      lastProgressSecond = whole;
      if (ui.currentTime) ui.currentTime.textContent = formatPlaybackTime(now);
      ui.seekSlider.setAttribute(
        "aria-valuetext",
        `${formatPlaybackTime(now)} of ${formatPlaybackTime(duration)}`);
    }
    if (ui.durationTime && duration !== lastProgressDuration) {
      lastProgressDuration = duration;
      ui.durationTime.textContent = formatPlaybackTime(duration);
    }
  }

  function resetProgressUI() {
    lastProgressSecond = -1;
    lastProgressDuration = "";
    if (ui.currentTime) ui.currentTime.textContent = "0:00";
    if (ui.durationTime) ui.durationTime.textContent = "0:00";
    if (ui.seekSlider) {
      ui.seekSlider.value = "0";
      ui.seekSlider.style.setProperty("--seek-percent", "0%");
      ui.seekSlider.disabled = true;
    }
  }

  function renderProgress() {
    if (!ui.seekSlider) return;
    if (!shared || !shared.current) {
      resetProgressUI();
      return;
    }

    const canSeek = canControl() && shared.duration > 0;
    ui.seekSlider.disabled = !canSeek;
    if (seekDragging) {
      updateSeekPreview();
      return;
    }
    updateProgressDisplay(projectedPosition());
  }

  function updateSeekPreview() {
    if (!ui.seekSlider || !shared) return;
    const duration = shared.duration;
    const ratio = Number(ui.seekSlider.value) / 1000;
    const seconds = ratio * duration;
    ui.seekSlider.style.setProperty("--seek-percent", (ratio * 100) + "%");
    if (ui.currentTime) ui.currentTime.textContent = formatPlaybackTime(seconds);
  }

  // --- PLAYBACK CLOCK / LYRICS HIGHLIGHTING ------------------------------

  function stopPlaybackLoop() {
    if (playbackHandle === null) return;
    if (playbackIsRaf) cancelAnimationFrame(playbackHandle);
    else clearTimeout(playbackHandle);
    playbackHandle = null;
  }

  function schedulePlaybackLoop() {
    if (playbackHandle !== null) return;
    if (document.hidden || !shared || !shared.current || !canControl()) return;

    if (shared.status === "playing") {
      playbackIsRaf = true;
      playbackHandle = requestAnimationFrame(playbackTick);
    } else {
      playbackIsRaf = false;
      playbackHandle = setTimeout(playbackTick, 400);
    }
  }

  function playbackTick() {
    playbackHandle = null;
    if (document.hidden || !shared || !shared.current) return;

    const now = projectedPosition();
    updateProgressDisplay(now);
    updateLyrics(now);
    schedulePlaybackLoop();
  }

  // --- LYRICS LOADING ----------------------------------------------------

  function loadLyricsFor(song) {
    const token = ++lyricsToken;
    lyricsLines = [];
    hasWordTimings = false;
    lyricsState = "idle";
    activeLineIndex = -1;
    activeWordIndex = -1;
    gapRendered = false;
    lastNextLine = "";

    if (!song || !song.filename) {
      setLyricsPlaceholder("Lyrics will appear here.");
      return;
    }

    const stem = fileStem(song.filename);
    const cached = lyricsCache.get(stem);
    if (cached) {
      applyLyricsData(cached);
      return;
    }

    if (song.lyrics === false && song.words === false) {
      const missing = { lines: [], hasWords: false, state: "missing" };
      lyricsCache.set(stem, missing);
      applyLyricsData(missing);
      return;
    }

    lyricsState = "loading";
    setLyricsPlaceholder("Loading lyrics…");

    const encoded = encodeURIComponent(stem);
    const srtPromise = fetch(`/lyrics/${encoded}.srt`, { cache: "no-cache" })
      .then(response => response.ok ? response.text() : null)
      .catch(() => null);
    const wordsPromise = fetch(`/lyrics/${encoded}.words.json`, { cache: "no-cache" })
      .then(response => response.ok ? response.json() : null)
      .catch(() => null);

    Promise.all([srtPromise, wordsPromise]).then(([srt, words]) => {
      if (token !== lyricsToken) return;

      const wordLines = normalizeWordTimings(words);
      let lines = [];
      let hasWords = false;

      if (wordLines.length) {
        lines = wordLines;
        hasWords = wordLines.some(line => line.words.length > 0);
      } else if (srt) {
        lines = parseSrt(srt);
      }

      const data = {
        lines,
        hasWords,
        state: lines.length ? "ready" : "missing"
      };
      lyricsCache.set(stem, data);
      applyLyricsData(data);
    });
  }

  function applyLyricsData(data) {
    lyricsLines = data.lines || [];
    hasWordTimings = !!data.hasWords;
    lyricsState = data.state || (lyricsLines.length ? "ready" : "missing");
    activeLineIndex = -1;
    activeWordIndex = -1;
    gapRendered = false;
    lastNextLine = "";

    if (lyricsState === "ready" && lyricsLines.length) {
      updateLyrics(projectedPosition());
      schedulePlaybackLoop();
    } else {
      setLyricsPlaceholder("No synced lyrics for this song.");
    }
  }

  function setLyricsPlaceholder(text) {
    if (!ui.lyricsLine) return;
    if (ui.lyricsLine.className !== "lyrics-line is-placeholder") {
      ui.lyricsLine.className = "lyrics-line is-placeholder";
    }
    if (ui.lyricsLine.textContent !== text) ui.lyricsLine.textContent = text;
    if (ui.lyricsNext) ui.lyricsNext.textContent = "";
    lastNextLine = "";
  }

  function setLyricsGap() {
    if (!ui.lyricsLine) return;
    ui.lyricsLine.className = "lyrics-line";
    ui.lyricsLine.textContent = "";
  }

  function isInstrumental(text) {
    return /^\s*\[\s*instrumental\s*\]\s*$/i.test(String(text || ""));
  }

  function parseTimestamp(value) {
    const match = /(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})/.exec(String(value || ""));
    if (!match) return null;
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    const seconds = Number(match[3]);
    const millis = Number((match[4] + "00").slice(0, 3));
    return hours * 3600 + minutes * 60 + seconds + millis / 1000;
  }

  function parseSrt(text) {
    const normalized = String(text || "")
      .replace(/^\uFEFF/, "")
      .replace(/\r\n?/g, "\n");
    const lines = [];

    normalized.split(/\n{2,}/).forEach(block => {
      const rows = block.split("\n");
      const timingIndex = rows.findIndex(row => row.includes("-->"));
      if (timingIndex < 0) return;

      const parts = rows[timingIndex].split("-->");
      if (parts.length < 2) return;

      const start = parseTimestamp(parts[0]);
      const end = parseTimestamp(parts[1]);
      const lyricText = rows.slice(timingIndex + 1)
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();

      if (start === null || end === null || end <= start || !lyricText) return;
      lines.push({
        start,
        end,
        text: lyricText,
        instrumental: isInstrumental(lyricText),
        words: []
      });
    });

    lines.sort((a, b) => a.start - b.start || a.end - b.end);
    return lines;
  }

  function normalizeWordTimings(data) {
    if (!data || !Array.isArray(data.lines) || !data.lines.length) return [];

    const lines = [];
    data.lines.forEach(raw => {
      if (!raw || typeof raw !== "object") return;
      const start = Number(raw.start);
      const end = Number(raw.end);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;

      const instrumental = !!raw.instrumental || isInstrumental(raw.text);
      const words = Array.isArray(raw.words) && !instrumental
        ? raw.words.map(word => ({
            text: String(word && word.text || "").trim(),
            start: Number(word && word.start),
            end: Number(word && word.end)
          })).filter(word =>
            word.text &&
            Number.isFinite(word.start) &&
            Number.isFinite(word.end) &&
            word.end >= word.start
          ).sort((a, b) => a.start - b.start)
        : [];

      const text = String(raw.text || "").trim() ||
        words.map(word => word.text).join(" ");
      if (!text && !words.length && !instrumental) return;

      lines.push({ start, end, text, instrumental, words });
    });

    lines.sort((a, b) => a.start - b.start || a.end - b.end);
    return lines;
  }

  // --- LYRICS RENDERING --------------------------------------------------

  function findActiveLineIndex(now) {
    let low = 0;
    let high = lyricsLines.length - 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      const line = lyricsLines[mid];
      if (now < line.start) high = mid - 1;
      else if (now >= line.end) low = mid + 1;
      else return mid;
    }
    return -1;
  }

  function findNextLineIndex(now) {
    let low = 0;
    let high = lyricsLines.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (lyricsLines[mid].start > now) high = mid;
      else low = mid + 1;
    }
    return low < lyricsLines.length ? low : -1;
  }

  function renderActiveLine(line) {
    if (!ui.lyricsLine) return;
    ui.lyricsLine.className = "lyrics-line";

    if (line.instrumental) {
      ui.lyricsLine.classList.add("is-instrumental");
      ui.lyricsLine.textContent = "Instrumental";
      return;
    }

    if (!hasWordTimings || !line.words.length) {
      ui.lyricsLine.textContent = line.text;
      return;
    }

    ui.lyricsLine.replaceChildren();
    line.words.forEach((word, index) => {
      if (index > 0) ui.lyricsLine.appendChild(document.createTextNode(" "));
      const span = makeElement("span", "word", word.text);
      ui.lyricsLine.appendChild(span);
    });
  }

  function updateNextPreview(now, activeIndex) {
    if (!ui.lyricsNext) return;
    const nextIndex = activeIndex >= 0 ? activeIndex + 1 : findNextLineIndex(now);
    const next = nextIndex >= 0 && nextIndex < lyricsLines.length
      ? lyricsLines[nextIndex] : null;
    const text = next
      ? (next.instrumental ? "Instrumental" : next.text || "")
      : "";
    if (text === lastNextLine) return;
    lastNextLine = text;
    ui.lyricsNext.textContent = text;
  }

  function updateWordHighlight(now) {
    if (!hasWordTimings || activeLineIndex < 0 || activeLineIndex >= lyricsLines.length) {
      return;
    }
    const line = lyricsLines[activeLineIndex];
    if (line.instrumental || !line.words.length) return;

    let wordIndex = -1;
    for (let i = 0; i < line.words.length; i++) {
      const word = line.words[i];
      if (now >= word.start) wordIndex = i;
      if (now < word.end) break;
    }
    if (wordIndex === activeWordIndex) return;

    const spans = ui.lyricsLine.querySelectorAll(".word");
    spans.forEach((span, index) => {
      span.classList.toggle("sung", index < wordIndex);
      span.classList.toggle("active", index === wordIndex);
    });
    activeWordIndex = wordIndex;
  }

  function updateLyrics(now) {
    if (!shared || !shared.current) {
      lyricsState = "idle";
      lyricsLines = [];
      hasWordTimings = false;
      activeLineIndex = -1;
      activeWordIndex = -1;
      setLyricsPlaceholder("Lyrics will appear here.");
      return;
    }

    if (lyricsState === "loading") {
      setLyricsPlaceholder("Loading lyrics…");
      return;
    }

    if (lyricsState !== "ready" || !lyricsLines.length) {
      setLyricsPlaceholder("No synced lyrics for this song.");
      return;
    }

    const lineIndex = findActiveLineIndex(now);
    if (lineIndex === -1) {
      if (activeLineIndex !== -1 || !gapRendered) {
        activeLineIndex = -1;
        activeWordIndex = -1;
        gapRendered = true;
        setLyricsGap();
      }
      updateNextPreview(now, -1);
      return;
    }

    gapRendered = false;
    if (lineIndex !== activeLineIndex) {
      activeLineIndex = lineIndex;
      activeWordIndex = -1;
      renderActiveLine(lyricsLines[lineIndex]);
      updateNextPreview(now, lineIndex);
    }
    updateWordHighlight(now);
  }

  // --- QUEUE -------------------------------------------------------------

  function queueFingerprint() {
    const queue = shared ? shared.queue : [];
    const current = shared && shared.current ? shared.current.filename : "";
    return current + "\u0003" + queue.map(song =>
      [song.filename, song.title, song.artist].join("\u0001")).join("\u0002");
  }

  function makeQueueButton(iconName, label, onClick, extraClass = "") {
    const button = makeElement("button", "queue-action" + (extraClass ? " " + extraClass : ""));
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.appendChild(makeIcon(iconName));
    button.addEventListener("click", onClick);
    return button;
  }

  function makeEmptyState(iconName, title, copy) {
    const empty = makeElement("div", "empty-state");
    const icon = makeElement("span", "empty-icon");
    icon.appendChild(makeIcon(iconName));
    empty.append(
      icon,
      makeElement("p", "empty-title", title),
      makeElement("p", "empty-copy", copy)
    );
    return empty;
  }

  function renderQueue() {
    if (!ui.queueList) return;
    lastQueueSignature = queueFingerprint();
    ui.queueList.replaceChildren();

    const queue = shared ? shared.queue : [];
    if (!queue.length) {
      const copy = shared && shared.current
        ? "The current song is the last one. Add more from the Songs tab."
        : "No songs queued. Add something from the Songs tab.";
      ui.queueList.appendChild(makeEmptyState("queue", "The queue is empty", copy));
      return;
    }

    queue.forEach((song, index) => {
      const row = makeElement("div", "queue-row");
      row.append(makeElement("span", "queue-index", String(index + 1).padStart(2, "0")));

      const copy = makeElement("div", "queue-copy");
      const title = makeElement("p", "queue-title", song.title || "Unknown song");
      title.title = song.title || "";
      const artist = makeElement("p", "queue-artist", song.artist || "Unknown artist");
      artist.title = song.artist || "";
      copy.append(title, artist);

      const actions = makeElement("div", "queue-actions");
      const up = makeQueueButton("up", `Move ${song.title || "song"} up`,
        () => moveQueueItem(index, -1));
      up.disabled = queueBusy || !reachable || index === 0;

      const down = makeQueueButton("down", `Move ${song.title || "song"} down`,
        () => moveQueueItem(index, 1));
      down.disabled = queueBusy || !reachable || index === queue.length - 1;

      const remove = makeQueueButton("close", `Remove ${song.title || "song"} from the queue`,
        () => removeQueueItem(index), "is-remove");
      remove.disabled = queueBusy || !reachable;

      actions.append(up, down, remove);
      row.append(copy, actions);
      ui.queueList.appendChild(row);
    });
  }

  async function runQueueAction(action, payload, fallbackMessage) {
    if (queueBusy) return;
    queueBusy = true;
    renderQueue();
    renderQueueCounts();
    try {
      await sendControl(action, payload);
    } catch (err) {
      showToast(userMessage(err, fallbackMessage), "error");
    } finally {
      queueBusy = false;
      renderQueue();
      renderQueueCounts();
    }
  }

  function removeQueueItem(index) {
    runQueueAction("remove", { index }, "Could not remove that song.");
  }

  function moveQueueItem(index, delta) {
    const queue = shared ? shared.queue : [];
    const target = index + delta;
    if (target < 0 || target >= queue.length) return;
    runQueueAction("reorder", { from: index, to: target }, "Could not reorder the queue.");
  }

  async function clearQueue() {
    if (queueBusy) return;
    const count = shared ? shared.queue.length : 0;
    if (!count) return;
    const confirmed = await karaokeConfirm({
      title: "Clear the queue?",
      message: `Remove ${count} queued ${pluralize(count, "song")}. The current song keeps playing.`,
      confirmLabel: "Clear queue",
      tone: "danger",
      focus: "cancel"
    });
    if (!confirmed) return;
    runQueueAction("clear", {}, "Could not clear the queue.");
  }

  async function stopAndClear() {
    if (queueBusy) return;
    const confirmed = await karaokeConfirm({
      title: "Stop and clear?",
      message: "Stop the current song and remove every queued song. Playback stays stopped until you choose something else.",
      confirmLabel: "Stop & clear",
      tone: "danger",
      focus: "cancel"
    });
    if (!confirmed) return;
    runQueueAction("stop", {}, "Could not stop the player.");
  }

  // --- LIBRARY -----------------------------------------------------------

  function filteredSongs() {
    const query = searchQuery.toLowerCase();
    if (!query) return songs;
    return songs.filter(song =>
      String(song.id || "").toLowerCase().includes(query) ||
      String(song.title || "").toLowerCase().includes(query) ||
      String(song.artist || "").toLowerCase().includes(query) ||
      String(song.filename || "").toLowerCase().includes(query)
    );
  }

  function updateSongSummary(count = 0) {
    if (!ui.songSummary) return;
    if (libraryState === "loading") {
      ui.songSummary.textContent = "Loading your library…";
      return;
    }
    if (libraryState === "error") {
      ui.songSummary.textContent = "Library unavailable.";
      return;
    }
    if (searchQuery) {
      ui.songSummary.textContent =
        `${count} ${pluralize(count, "result")} for “${searchQuery}”`;
      return;
    }
    const withLyrics = songs.filter(song => song.lyrics || song.words).length;
    ui.songSummary.textContent =
      `${songs.length} ${pluralize(songs.length, "song")} · ` +
      `${withLyrics} with synced lyrics`;
  }

  function renderSongs() {
    if (!ui.songList) return;
    ui.songList.replaceChildren();

    if (ui.clearSearch) ui.clearSearch.hidden = !searchQuery;

    if (libraryState === "loading") {
      updateSongSummary();
      const loading = makeElement("div", "loading-state", "Loading your library…");
      ui.songList.appendChild(loading);
      return;
    }

    if (libraryState === "error") {
      updateSongSummary();
      const error = makeEmptyState("music", "Couldn’t load your library",
        "Make sure the server is running, then try again.");
      const retry = makeElement("button", "retry-button", "Try again");
      retry.type = "button";
      retry.addEventListener("click", loadLibrary);
      error.appendChild(retry);
      ui.songList.appendChild(error);
      return;
    }

    if (!songs.length) {
      updateSongSummary();
      ui.songList.appendChild(makeEmptyState("music", "No songs yet",
        "Add audio files to the songs folder, then refresh this remote."));
      return;
    }

    const list = filteredSongs();
    updateSongSummary(list.length);

    if (!list.length) {
      ui.songList.appendChild(makeEmptyState("search",
        `No results for “${searchQuery}”`,
        "Try a different title, artist, or song ID."));
      return;
    }

    list.forEach((song, index) => {
      const row = makeElement("div", "song-row");
      const id = makeElement("span", "song-id", song.id || String(index + 1).padStart(2, "0"));
      id.title = song.id ? `Song ID ${song.id}` : "";

      const copy = makeElement("div", "song-copy");
      const titleLine = makeElement("div", "song-title-line");
      const title = makeElement("span", "song-title", song.title || "Unknown song");
      title.title = song.title || "";
      titleLine.appendChild(title);

      if (song.lyrics || song.words) {
        const pill = makeElement("span", "lyrics-pill", "Lyrics");
        pill.title = song.words ? "Word-synced lyrics available" : "Synced lyrics available";
        titleLine.appendChild(pill);
      }

      const artist = makeElement("p", "song-artist", song.artist || "Unknown artist");
      artist.title = song.artist || "";
      copy.append(titleLine, artist);

      const add = makeElement("button", "add-song");
      add.type = "button";
      add.title = `Add ${song.title || "song"} to the queue`;
      add.setAttribute("aria-label", `Add ${song.title || "song"} to the queue`);
      add.appendChild(makeIcon("plus"));
      add.disabled = addPending.has(song.filename);
      add.addEventListener("click", () => addSong(song, add));

      row.append(id, copy, add);
      ui.songList.appendChild(row);
    });
  }

  async function loadLibrary() {
    libraryState = "loading";
    renderSongs();
    try {
      const response = await fetch("/api/songs", {
        headers: { "Accept": "application/json" },
        cache: "no-store"
      });
      const data = await readJson(response);
      if (!response.ok) {
        throw new Error(data.error || `Could not load songs (${response.status}).`);
      }
      markReachable();
      songs = Array.isArray(data.songs) ? data.songs.slice() : [];
      songs.sort((a, b) =>
        String(a.title || "").localeCompare(String(b.title || ""), undefined, {
          sensitivity: "base"
        }));
      libraryState = "ready";
    } catch (err) {
      libraryState = "error";
      if (isNetworkError(err)) markUnreachable();
    }
    renderSongs();
  }

  function flashAddedButton(button) {
    if (!button) return;
    button.classList.add("is-added");
    const use = button.querySelector("use");
    if (use) use.setAttribute("href", "#icon-check");
    clearTimeout(button._addedTimer);
    button._addedTimer = setTimeout(() => {
      button.classList.remove("is-added");
      const icon = button.querySelector("use");
      if (icon) icon.setAttribute("href", "#icon-plus");
      button.disabled = addPending.has(button.dataset.filename);
    }, 1400);
  }

  async function addSong(song, button) {
    if (!song || !song.filename || addPending.has(song.filename)) return;
    addPending.add(song.filename);
    if (button) {
      button.dataset.filename = song.filename;
      button.disabled = true;
      const use = button.querySelector("use");
      if (use) use.setAttribute("href", "#icon-check");
    }

    try {
      const data = await sendControl("add", { filename: song.filename });
      if (data.started) {
        showToast(`Playing “${song.title || "song"}” now`, "success");
      } else {
        showToast(`Added “${song.title || "song"}” to the queue`, "success");
      }
      if (button) {
        button.disabled = false;
        flashAddedButton(button);
      }
    } catch (err) {
      showToast(userMessage(err, "Could not add that song."), "error");
      if (button) {
        button.disabled = false;
        const use = button.querySelector("use");
        if (use) use.setAttribute("href", "#icon-plus");
      }
    } finally {
      addPending.delete(song.filename);
    }
  }

  // --- CONTROL TRANSPORT -------------------------------------------------

  async function sendControl(action, payload = {}) {
    let response;
    try {
      response = await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        cache: "no-store",
        body: JSON.stringify({ action, ...payload })
      });
    } catch (err) {
      if (isNetworkError(err)) {
        markUnreachable();
        throw new Error("Can't reach the karaoke server. Check the Wi-Fi connection.");
      }
      throw err;
    }

    const data = await readJson(response);
    markReachable();
    if (!response.ok) {
      throw new Error(data.error || `Request failed (${response.status}).`);
    }
    if (data.state) applyState(data.state);
    return data;
  }

  async function runTransport(action, payload = {}, fallback = "Playback command failed.") {
    if (transportBusy) return;
    transportBusy = true;
    renderTransport();
    try {
      await sendControl(action, payload);
    } catch (err) {
      showToast(userMessage(err, fallback), "error");
    } finally {
      transportBusy = false;
      renderTransport();
    }
  }

  function togglePlayPause() {
    if (!shared || transportBusy) return;
    if (shared.status === "loading") return;
    const action = shared.status === "playing" ? "pause" : "play";
    runTransport(action, {}, action === "pause"
      ? "Could not pause the player."
      : "Could not start playback.");
  }

  function restartSong() {
    runTransport("restart", {}, "Could not restart the song.");
  }

  function skipSong() {
    runTransport("skip", {}, "Could not skip the song.");
  }

  async function changeKey(delta) {
    if (keyBusy || !shared || !shared.current || !hostIsOnline()) return;
    const target = clamp(shared.transpose + delta, -4, 4);
    if (target === shared.transpose) return;

    keyBusy = true;
    renderKey();
    try {
      await sendControl("transpose", { semitones: target });
    } catch (err) {
      showToast(userMessage(err, "Could not change the key."), "error");
    } finally {
      keyBusy = false;
      renderKey();
    }
  }

  function beginSeek() {
    if (!shared || !shared.current || !hostIsOnline() || shared.duration <= 0) return;
    seekDragging = true;
    clearTimeout(seekTimer);
  }

  function previewSeek() {
    seekDragging = true;
    updateSeekPreview();
    clearTimeout(seekTimer);
    seekTimer = setTimeout(() => commitSeek(false), 240);
  }

  function commitSeek(release) {
    clearTimeout(seekTimer);
    if (!shared || !shared.current || !hostIsOnline() || shared.duration <= 0) {
      if (release) {
        seekDragging = false;
        renderProgress();
      }
      return;
    }

    const value = ui.seekSlider ? ui.seekSlider.value : "0";
    const now = Date.now();
    if (value === lastSeekValue && now - lastSeekAt < 600) {
      if (release) {
        seekDragging = false;
        renderProgress();
      }
      return;
    }
    lastSeekValue = value;
    lastSeekAt = now;

    const position = (Number(value) / 1000) * shared.duration;
    sendControl("seek", { position: Math.round(position * 1000) / 1000 })
      .catch(err => showToast(userMessage(err, "Could not seek."), "error"))
      .finally(() => {
        if (release) {
          seekDragging = false;
          renderProgress();
        }
      });
  }

  function releaseSeek() {
    if (!seekDragging) return;
    seekDragging = false;
    commitSeek(true);
  }

  function updateVolumePreview() {
    if (!ui.volumeSlider) return;
    volumeHoldUntil = performance.now() + 900;
    const percent = Number(ui.volumeSlider.value);
    ui.volumeSlider.style.setProperty("--volume-percent", percent + "%");
    if (ui.volumeValue) ui.volumeValue.textContent = percent + "%";
    ui.volumeSlider.setAttribute("aria-valuetext", percent + "% volume");
  }

  function sendVolume() {
    if (!ui.volumeSlider || !shared || !hostIsOnline()) return;
    clearTimeout(volumeTimer);
    const percent = clamp(Number(ui.volumeSlider.value), 0, 100);
    const volume = Math.round((percent / 100) * 1000) / 1000;
    const now = Date.now();
    if (lastVolumeValue !== null && Math.abs(lastVolumeValue - volume) < 0.005 &&
        now - lastVolumeAt < 700) {
      return;
    }
    lastVolumeValue = volume;
    lastVolumeAt = now;
    volumeHoldUntil = performance.now() + 900;
    sendControl("volume", { volume })
      .catch(err => showToast(userMessage(err, "Could not change the volume."), "error"));
  }

  // --- TABS --------------------------------------------------------------

  function setTab(name, focus = false) {
    if (name !== "queue" && name !== "songs") name = "queue";
    activeTab = name;

    const tabs = [ui.tabQueue, ui.tabSongs].filter(Boolean);
    tabs.forEach(tab => {
      const selected = tab.dataset.tab === name;
      tab.classList.toggle("is-active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("tabindex", selected ? "0" : "-1");
      if (selected && focus) tab.focus();
    });

    if (ui.queuePanel) ui.queuePanel.hidden = name !== "queue";
    if (ui.songsPanel) ui.songsPanel.hidden = name !== "songs";

    try {
      sessionStorage.setItem("karaoke.remote.tab", name);
    } catch { /* private mode */ }
  }

  function setupTabs() {
    let saved = null;
    try {
      saved = sessionStorage.getItem("karaoke.remote.tab");
    } catch { /* private mode */ }
    if (saved === "songs" || saved === "queue") activeTab = saved;

    const tabs = [ui.tabQueue, ui.tabSongs].filter(Boolean);
    tabs.forEach(tab => {
      tab.addEventListener("click", () => setTab(tab.dataset.tab));
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const index = tabs.indexOf(tab);
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        setTab(tabs[next].dataset.tab, true);
      });
    });

    setTab(activeTab);
  }

  // --- PWA ---------------------------------------------------------------

  function isStandalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      window.navigator.standalone === true;
  }

  function setupPWA() {
    if (isStandalone() && ui.installButton) ui.installButton.hidden = true;

    window.addEventListener("beforeinstallprompt", event => {
      event.preventDefault();
      deferredInstallPrompt = event;
      if (ui.installButton && !isStandalone()) ui.installButton.hidden = false;
    });

    if (ui.installButton) {
      ui.installButton.addEventListener("click", async () => {
        if (!deferredInstallPrompt) return;
        deferredInstallPrompt.prompt();
        try {
          await deferredInstallPrompt.userChoice;
        } catch { /* the browser dismissed the prompt */ }
        deferredInstallPrompt = null;
        ui.installButton.hidden = true;
      });
    }

    window.addEventListener("appinstalled", () => {
      deferredInstallPrompt = null;
      if (ui.installButton) ui.installButton.hidden = true;
    });

    // Service workers need HTTPS or localhost. On a plain-HTTP LAN address the
    // remote still works; only offline caching/installability is unavailable.
    if ("serviceWorker" in navigator && window.isSecureContext) {
      const register = () => {
        navigator.serviceWorker.register("/sw.js", { scope: "/" })
          .catch(err => console.warn("Service worker registration failed:", err));
      };
      if (document.readyState === "complete") register();
      else window.addEventListener("load", register, { once: true });
    }
  }

  // --- EVENT WIRING ------------------------------------------------------

  function setupHandlers() {
    if (ui.playPauseButton) ui.playPauseButton.addEventListener("click", togglePlayPause);
    if (ui.restartButton) ui.restartButton.addEventListener("click", restartSong);
    if (ui.skipButton) ui.skipButton.addEventListener("click", skipSong);
    if (ui.keyDown) ui.keyDown.addEventListener("click", () => changeKey(-1));
    if (ui.keyUp) ui.keyUp.addEventListener("click", () => changeKey(1));
    if (ui.clearQueue) ui.clearQueue.addEventListener("click", clearQueue);
    if (ui.stopClear) ui.stopClear.addEventListener("click", stopAndClear);

    if (ui.searchInput) {
      ui.searchInput.addEventListener("input", () => {
        searchQuery = ui.searchInput.value.trim();
        if (ui.clearSearch) ui.clearSearch.hidden = !searchQuery;
        renderSongs();
      });
    }

    if (ui.clearSearch) {
      ui.clearSearch.addEventListener("click", () => {
        if (!ui.searchInput) return;
        ui.searchInput.value = "";
        searchQuery = "";
        ui.clearSearch.hidden = true;
        renderSongs();
        ui.searchInput.focus();
      });
    }

    if (ui.seekSlider) {
      ui.seekSlider.addEventListener("pointerdown", beginSeek);
      ui.seekSlider.addEventListener("keydown", beginSeek);
      ui.seekSlider.addEventListener("input", previewSeek);
      ui.seekSlider.addEventListener("change", () => {
        seekDragging = false;
        commitSeek(true);
      });
    }
    window.addEventListener("pointerup", releaseSeek);
    window.addEventListener("pointercancel", releaseSeek);

    if (ui.volumeSlider) {
      ui.volumeSlider.addEventListener("pointerdown", () => {
        volumeHoldUntil = performance.now() + 900;
      });
      ui.volumeSlider.addEventListener("input", () => {
        updateVolumePreview();
        clearTimeout(volumeTimer);
        volumeTimer = setTimeout(() => sendVolume(), 160);
      });
      ui.volumeSlider.addEventListener("change", () => {
        updateVolumePreview();
        sendVolume();
      });
    }

    window.addEventListener("offline", () => {
      reachable = false;
      hadNetworkError = true;
      live = false;
      closeEvents();
      updateConnectionUI();
    });

    window.addEventListener("online", () => {
      refreshState();
      scheduleReconnect(300);
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        stopPlaybackLoop();
        return;
      }
      updateConnectionUI();
      refreshState();
      if (!eventSource) {
        reconnectAttempts = 0;
        openEvents();
      }
      schedulePlaybackLoop();
    });
  }

  // --- STARTUP -----------------------------------------------------------

  function init() {
    setupTabs();
    setupHandlers();
    setupPWA();

    updateConnectionUI();
    renderNowPlaying();
    renderTransport();
    renderKey();
    renderVolume();
    renderQueueCounts();
    renderProgress();
    renderQueue();
    lastQueueSignature = null; // Force the first real snapshot to render the queue.
    renderSongs();

    startHeartbeat();
    loadLibrary();
    refreshState();
    openEvents();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
