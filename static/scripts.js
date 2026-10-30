// --- SHARED KARAOKE SESSION -----------------------------------------------
// This page is the one and only audio player. The server owns the queue and
// all controls; phones send commands to it and this page executes them.
// The local audio element is the single source of truth for playback time.

const CLIENT_ID = (() => {
  try {
    const existing = sessionStorage.getItem("karaoke.host.clientId");
    if (existing) return existing;
    const created = (crypto.randomUUID && crypto.randomUUID()) ||
      `host-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    sessionStorage.setItem("karaoke.host.clientId", created);
    return created;
  } catch {
    return `host-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
})();

const shared = {
  server_id: null,
  revision: 0,
  status: "idle",
  position: 0,
  current: null,
  queue: [],
  transpose: 0,
  volume: 1,
};

let songs = [];
let currentSong = null;        // The song actually loaded into the audio element.
let queue = [];                // Mirrors the shared server queue.
let transposeLevel = 0;
let hostVolume = 1;
let audioUnlocked = false;

let registered = false;
let live = false;
let revoked = false;
let connecting = false;
let serverId = null;
let lastCommandId = 0;
let seenCommandIds = new Set();
let commandChain = Promise.resolve();
let events = null;
let reconnectTimer = null;
let reportTimer = null;
let commandPollTimer = null;
let reportInFlight = false;
let reportQueued = false;
let reportFailures = 0;
let loadToken = 0;
let loading = false;
let errorActive = false;
let autoplayBlocked = false;
let lastPlaybackError = null;
let autoSkipTimer = null;
let transposeBusy = false;
let transposeBusyToken = 0;
let hostNoticeTimer = null;
let queueRenderKey = "";
let transposeRenderKey = "";
let lyricsRequestToken = 0;
let lyricsClearTimer = null;

// Command progress is per browser tab (sessionStorage) and survives a page
// reload. Without this a refreshed player can replay commands it already ran
// and, for example, pause again after the singer resumed.
const COMMAND_STATE_KEY = "karaoke.host.commandState";

function readStoredCommandState() {
  try {
    const raw = sessionStorage.getItem(COMMAND_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const storedId = Number(parsed.lastCommandId);
    return {
      serverId: typeof parsed.serverId === "string" ? parsed.serverId : null,
      lastCommandId: Number.isFinite(storedId) ? Math.max(0, storedId) : 0
    };
  } catch {
    return null;
  }
}

const storedCommandState = readStoredCommandState();
if (storedCommandState) {
  serverId = storedCommandState.serverId;
  lastCommandId = storedCommandState.lastCommandId;
}

function rememberCommandState() {
  try {
    sessionStorage.setItem(COMMAND_STATE_KEY, JSON.stringify({
      serverId: serverId || null,
      lastCommandId: Math.max(0, Number(lastCommandId) || 0)
    }));
  } catch { /* private mode / storage disabled */ }
}

// Main audio element, managed directly so seeking and transposition stay simple.
const audioPlayer = new Audio();
audioPlayer.preload = "auto";
audioPlayer.volume = hostVolume;

let lyricsTimeline = [];
let wordTimeline = null; // Optional word-level JSON sidecar for the current song
let activeLyricLineIndex = -1;
let activeLyricWordIndex = -1;
let lineLevelLyricIndex = -1;
let lyricsAnimationFrame = null;

const audioUrlCache = new Map();

// --- DOM REFERENCES ---
const nowPlayingEl = document.getElementById("now-playing");
const nowPlayingArtistEl = document.getElementById("now-playing-artist");
const nowPlayingArtEl = document.getElementById("now-playing-art");
const lyricsEl = document.getElementById("lyrics-line");
const queueDisplayEl = document.getElementById("queue-display");
const queueCountEl = document.getElementById("queue-count");
const queueSummaryEl = document.getElementById("queue-summary");
const queueClearButton = document.getElementById("queue-clear");
const songListContent = document.getElementById("song-list-content");
const searchInput = document.getElementById("search-input");
const songSummaryEl = document.getElementById("song-summary");
const transposeIndicatorBar = document.getElementById("transpose-indicator-bar");
const transposeValueEl = document.getElementById("transpose-value");
const playPauseButton = document.getElementById("play-pause-button");
const restartButton = document.getElementById("restart-button");
const skipButton = document.getElementById("skip-button");
const keyDownButton = document.getElementById("key-down");
const keyUpButton = document.getElementById("key-up");
const progressTrack = document.getElementById("progress-track");
const progressFill = document.getElementById("progress-fill");
const currentTimeEl = document.getElementById("current-time");
const durationTimeEl = document.getElementById("duration-time");
const hostStatusDot = document.getElementById("host-status-dot");
const hostStatusText = document.getElementById("host-status-text");
const hostJoinUrl = document.getElementById("host-join-url");
const copyJoinUrlButton = document.getElementById("copy-join-url");
const hostOverlay = document.getElementById("host-overlay");
const hostOverlayTitle = document.getElementById("host-overlay-title");
const hostOverlayText = document.getElementById("host-overlay-text");
const hostTakeoverButton = document.getElementById("host-takeover-button");
const hostReloadButton = document.getElementById("host-reload-button");
const audioUnlock = document.getElementById("audio-unlock");
const audioUnlockButton = document.getElementById("audio-unlock-button");

// --- SMALL DOM HELPERS ---
const SVG_NS = "http://www.w3.org/2000/svg";

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

function pluralize(count, singular, plural = singular + "s") {
  return count === 1 ? singular : plural;
}

function formatPlaybackTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function signedSemitones(value) {
  return value > 0 ? `+${value}` : String(value);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function fileStem(filename) {
  // "00001_Title_Artist.mp3" -> "00001_Title_Artist" (used for lyrics lookup)
  return filename.replace(/\.[^.]+$/, "");
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

// --- ERROR REPORTING ---
// Anything that goes wrong in the browser is forwarded to the server log
// (karaoke.log / the terminal), so problems can be diagnosed from one place.
const MEDIA_ERROR_NAMES = {
  1: "MEDIA_ERR_ABORTED",
  2: "MEDIA_ERR_NETWORK",
  3: "MEDIA_ERR_DECODE",
  4: "MEDIA_ERR_SRC_NOT_SUPPORTED"
};

function reportToServer(level, message, details) {
  try {
    fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      keepalive: true,
      body: JSON.stringify({ level, message, details })
    }).catch(() => {}); // never let error reporting itself break the app
  } catch (err) {
    console.error("Could not send error to server:", err);
  }
}

function mediaErrorMessage(audio) {
  const mediaError = audio && audio.error;
  if (!mediaError) return "The audio element could not load the file.";
  return `${MEDIA_ERROR_NAMES[mediaError.code] || `media error ${mediaError.code}`}` +
    (mediaError.message ? `: ${mediaError.message}` : "");
}

// --- HOST CONNECTION ------------------------------------------------------

function setHostStatus(kind, text) {
  if (!hostStatusDot || !hostStatusText) return;
  hostStatusDot.classList.remove("is-online", "is-warning", "is-error");
  if (kind) hostStatusDot.classList.add(`is-${kind}`);
  hostStatusText.textContent = text;
}

function updateHostStatus() {
  if (revoked) {
    setHostStatus("error", "Another screen is the player");
  } else if (!registered) {
    setHostStatus("warning", "Connecting to the server…");
  } else if (errorActive) {
    setHostStatus("warning", "Player online · playback problem");
  } else if (!live) {
    setHostStatus("warning", "Player online · reconnecting live updates");
  } else {
    setHostStatus("online", "Player online · phones can control");
  }
}

function showHostNotice(message) {
  setHostStatus("error", message || "Command failed");
  clearTimeout(hostNoticeTimer);
  hostNoticeTimer = setTimeout(updateHostStatus, 4000);
}

function showAudioUnlock(show = true) {
  audioUnlocked = !show;
  if (audioUnlock) audioUnlock.hidden = !show;
}

function armAudioFromGesture() {
  audioUnlocked = true;
  if (audioUnlock) audioUnlock.hidden = true;
  if (currentSong && audioPlayer.paused &&
      (shared.status === "playing" || autoplayBlocked)) {
    safePlay().then(played => {
      if (played) {
        autoplayBlocked = false;
        reportNow();
      }
    });
  }
}

async function unlockAudio() {
  armAudioFromGesture();
  try {
    if (currentSong && audioPlayer.src) {
      if (audioPlayer.paused && shared.status === "playing") {
        await safePlay();
      }
      return;
    }
    // A tiny silent WAV gives Safari/Chrome a concrete media gesture to
    // remember, so the first remote-requested song is allowed to play.
    const silence = silentWavDataUri(0.12);
    audioPlayer.src = silence;
    await audioPlayer.play();
    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.load();
  } catch {
    // The click still counts as a user gesture in most browsers; the next
    // play attempt will show the unlock prompt again if needed.
    showAudioUnlock(true);
  }
}

function silentWavDataUri(seconds) {
  const sampleRate = 8000;
  const sampleCount = Math.max(1, Math.floor(sampleRate * seconds));
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);
  const writeAscii = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, "data");
  view.setUint32(40, sampleCount * 2, true);
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 8192) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  return "data:audio/wav;base64," + btoa(binary);
}

function closeEvents() {
  if (events) {
    events.close();
    events = null;
  }
  live = false;
}

function scheduleReconnect(delay = 2500) {
  if (revoked || reconnectTimer) return;
  registered = false;
  live = false;
  updateHostStatus();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectHost();
  }, delay);
}

async function connectHost(takeover = false) {
  if (revoked && !takeover) return;
  if (connecting) return;
  connecting = true;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  closeEvents();
  stopReporting();
  stopCommandPolling();

  try {
    const response = await fetch("/api/host/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: CLIENT_ID })
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.error || `Server returned ${response.status}`);

    revoked = false;
    if (hostOverlay) hostOverlay.hidden = true;
    registered = true;
    reportFailures = 0;
    applySharedState(data.state || {});
    await syncPlaybackWithSnapshot(data.state || {});
    if (Array.isArray(data.commands)) data.commands.forEach(enqueueCommand);

    startReporting();
    startCommandPolling();
    openEvents();
    updateHostStatus();
  } catch (err) {
    console.warn("Host registration failed:", err);
    registered = false;
    updateHostStatus();
    scheduleReconnect(3000);
  } finally {
    connecting = false;
  }
}

function openEvents() {
  closeEvents();
  const source = new EventSource(
    `/api/events?role=host&client_id=${encodeURIComponent(CLIENT_ID)}`);
  events = source;

  source.onopen = () => {
    if (source !== events) return;
    live = true;
    updateHostStatus();
    reportNow();
  };

  source.addEventListener("state", event => {
    if (source !== events) return;
    try {
      applySharedState(JSON.parse(event.data));
      updateHostStatus();
    } catch (err) {
      console.warn("Ignoring malformed state event:", err);
    }
  });

  source.addEventListener("command", event => {
    if (source !== events) return;
    try {
      enqueueCommand(JSON.parse(event.data));
    } catch (err) {
      console.warn("Ignoring malformed command event:", err);
    }
  });

  source.addEventListener("host_revoked", event => {
    if (source !== events) return;
    let reason = "Another browser is controlling the karaoke player.";
    try {
      const data = JSON.parse(event.data);
      if (data && data.reason) reason = data.reason;
    } catch { /* keep default */ }
    onHostRevoked(reason);
  });

  source.onerror = () => {
    if (source !== events) return;
    live = false;
    updateHostStatus();
    if (revoked) return;
    closeEvents();
    scheduleReconnect(2200);
  };
}

function onHostRevoked(reason) {
  revoked = true;
  registered = false;
  live = false;
  closeEvents();
  stopReporting();
  stopCommandPolling();
  clearTimeout(autoSkipTimer);
  stopAudio();
  currentSong = null;
  shared.current = null;
  updateNowPlayingUI();
  if (hostOverlay) {
    hostOverlayTitle.textContent = "Another screen is the player";
    hostOverlayText.textContent = reason;
    hostOverlay.hidden = false;
  }
  updateHostStatus();
}

async function syncPlaybackWithSnapshot(snapshot) {
  const song = snapshot.current;
  if (!song) {
    if (currentSong) {
      stopAudio();
      currentSong = null;
      clearLyricTimers();
      updateNowPlayingUI();
    }
    return;
  }

  if (currentSong && currentSong.filename === song.filename && audioPlayer.src) {
    if (Number(currentSong.transpose) !== Number(snapshot.transpose)) {
      await loadSong(song, {
        transpose: snapshot.transpose,
        position: snapshot.position,
        autoplay: snapshot.status === "playing"
      });
      return;
    }
    if (snapshot.status === "playing" && audioPlayer.paused) {
      safePlay().then(played => { if (played) reportNow(); });
    } else if (snapshot.status !== "playing" && !audioPlayer.paused) {
      audioPlayer.pause();
    }
    const target = Number(snapshot.position);
    if (Number.isFinite(target) && audioPlayer.readyState >= 1 &&
        Math.abs(audioPlayer.currentTime - target) > 2.5) {
      try { audioPlayer.currentTime = clamp(target, 0, audioPlayer.duration || target); } catch { /* ignore */ }
    }
    if (!lyricsTimeline.length && !wordTimeline) loadLyrics(fileStem(song.filename));
    return;
  }

  const autoplay = snapshot.status === "playing" || snapshot.status === "loading";
  try {
    await loadSong(song, {
      position: Number(snapshot.position) || 0,
      autoplay,
      transpose: Number(snapshot.transpose) || 0
    });
  } catch {
    // loadSong already recorded the failure and scheduled a skip.
  }
}

function startReporting() {
  if (reportTimer) clearInterval(reportTimer);
  reportTimer = setInterval(reportNow, 1000);
  reportNow();
}

function stopReporting() {
  if (reportTimer) clearInterval(reportTimer);
  reportTimer = null;
  reportInFlight = false;
  reportQueued = false;
}

function reportNow() {
  if (!registered || revoked) return;
  if (reportInFlight) {
    reportQueued = true;
    return;
  }

  const duration = Number.isFinite(audioPlayer.duration) ? audioPlayer.duration : 0;
  const position = currentSong && Number.isFinite(audioPlayer.currentTime)
    ? audioPlayer.currentTime : 0;
  let status = "idle";
  if (loading) {
    status = "loading";
  } else if (errorActive) {
    status = "error";
  } else if (currentSong) {
    status = audioPlayer.paused ? "paused" : "playing";
  } else if (shared.current) {
    status = shared.status === "loading" ? "loading" : "paused";
  }

  const commandServerId = serverId;
  reportInFlight = true;
  fetch("/api/host/report", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: CLIENT_ID,
      status,
      position,
      duration,
      transpose: transposeLevel,
      volume: hostVolume,
      last_command_id: lastCommandId,
      filename: currentSong ? currentSong.filename : null,
      error: errorActive ? lastPlaybackError : null
    })
  })
    .then(async response => {
      const data = await readJson(response);
      if (response.status === 409) {
        onHostRevoked(data.error || "This browser is no longer the active player.");
        return;
      }
      if (!response.ok) throw new Error(data.error || `Report failed (${response.status})`);
      reportFailures = 0;
      if (serverId === commandServerId) applySharedState(data.state || {});
      if (Array.isArray(data.commands)) data.commands.forEach(enqueueCommand);
      updateHostStatus();
    })
    .catch(err => {
      reportFailures += 1;
      console.warn("Host report failed:", err);
      if (reportFailures >= 2) {
        scheduleReconnect(2500);
      }
    })
    .finally(() => {
      reportInFlight = false;
      if (reportQueued) {
        reportQueued = false;
        reportNow();
      }
    });
}

function startCommandPolling() {
  stopCommandPolling();
  commandPollTimer = setInterval(pollCommands, 2000);
}

function stopCommandPolling() {
  if (commandPollTimer) clearInterval(commandPollTimer);
  commandPollTimer = null;
}

async function pollCommands() {
  if (!registered || revoked) return;
  try {
    const response = await fetch(
      `/api/host/commands?client_id=${encodeURIComponent(CLIENT_ID)}`);
    const data = await readJson(response);
    if (response.status === 409) {
      scheduleReconnect(1200);
      return;
    }
    if (!response.ok) return;
    if (Array.isArray(data.commands)) data.commands.forEach(enqueueCommand);
  } catch {
    // The report heartbeat also detects a dead connection.
  }
}

// --- SHARED STATE ---------------------------------------------------------

function applySharedState(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;

  const incomingServerId = snapshot.server_id || null;
  const incomingRevision = Number(snapshot.revision);
  if (shared.server_id && incomingServerId && shared.server_id === incomingServerId &&
      Number.isFinite(incomingRevision) && incomingRevision < shared.revision) {
    // An older report response arrived after a newer SSE state. Applying it
    // could cancel a song that a phone just queued.
    return;
  }

  if (serverId && incomingServerId && incomingServerId !== serverId) {
    // The Flask process restarted, so command ids started over.
    lastCommandId = 0;
    seenCommandIds = new Set();
    rememberCommandState();
  }
  if (incomingServerId && incomingServerId !== serverId) {
    serverId = incomingServerId;
    rememberCommandState();
  }

  shared.server_id = incomingServerId || shared.server_id;
  shared.revision = Number.isFinite(incomingRevision) ? incomingRevision : shared.revision;
  shared.status = snapshot.status || "idle";
  shared.position = Number(snapshot.position) || 0;
  shared.current = snapshot.current || null;
  shared.queue = Array.isArray(snapshot.queue) ? snapshot.queue : [];
  shared.transpose = Number.isFinite(Number(snapshot.transpose))
    ? Number(snapshot.transpose) : shared.transpose;
  shared.volume = Number.isFinite(Number(snapshot.volume))
    ? Number(snapshot.volume) : shared.volume;

  queue = shared.queue;
  transposeLevel = clamp(shared.transpose, -4, 4);
  hostVolume = clamp(shared.volume, 0, 1);
  if (Math.abs(audioPlayer.volume - hostVolume) > 0.001) {
    audioPlayer.volume = hostVolume;
  }

  if (!shared.current && !shared.queue.length && shared.status === "idle" && currentSong) {
    stopAudio();
    currentSong = null;
    clearLyricTimers();
  }

  updateNowPlayingUI();
  updateQueueDisplay();
  updateConnectionDependentControls();
}

function updateConnectionDependentControls() {
  const online = registered && !revoked;
  const hasCurrent = !!shared.current;
  const hasQueue = shared.queue.length > 0;
  if (playPauseButton) playPauseButton.disabled = !online || (!hasCurrent && !hasQueue);
  if (restartButton) restartButton.disabled = !online || !hasCurrent;
  if (skipButton) skipButton.disabled = !online || (!hasCurrent && !hasQueue);
  if (progressTrack) {
    progressTrack.setAttribute("aria-disabled",
      online && hasCurrent ? "false" : "true");
  }
  if (queueClearButton) queueClearButton.disabled = !registered || revoked;
}

// --- COMMAND QUEUE --------------------------------------------------------

function enqueueCommand(command) {
  if (!command || typeof command !== "object") return;
  const id = Number(command.id);
  if (!Number.isFinite(id) || id <= 0) return;
  if (revoked) return;
  if (id <= lastCommandId || seenCommandIds.has(id)) return;

  seenCommandIds.add(id);
  if (seenCommandIds.size > 500) {
    for (const value of seenCommandIds) {
      if (value <= lastCommandId) seenCommandIds.delete(value);
    }
  }

  const commandServerId = serverId;
  commandChain = commandChain
    .then(async () => {
      if (revoked) return;
      await executeCommand(command);
    })
    .catch(err => {
      console.error("Command failed:", command, err);
      handlePlaybackFailure(err, "A playback command failed.");
    })
    .finally(() => {
      if (serverId === commandServerId) {
        lastCommandId = Math.max(lastCommandId, id);
        rememberCommandState();
        reportNow();
      }
    });
}

async function executeCommand(command) {
  switch (command.action) {
    case "load": {
      if (!command.song) throw new Error("Load command has no song.");
      try {
        await loadSong(command.song, {
          position: Number(command.position) || 0,
          autoplay: command.autoplay !== false,
          transpose: Number(command.transpose) || 0
        });
      } catch {
        // loadSong has already recorded the error and scheduled a skip.
      }
      break;
    }

    case "play": {
      errorActive = false;
      lastPlaybackError = null;
      if (!currentSong && shared.current) {
        await loadSong(shared.current, {
          position: Number(shared.position) || 0,
          autoplay: true,
          transpose: transposeLevel
        });
      } else {
        await safePlay();
      }
      break;
    }

    case "pause": {
      loading = false;
      autoplayBlocked = false;
      audioPlayer.pause();
      break;
    }

    case "restart": {
      if (!currentSong) throw new Error("Nothing is loaded to restart.");
      errorActive = false;
      lastPlaybackError = null;
      try { audioPlayer.currentTime = 0; } catch { /* ignore */ }
      updatePlaybackProgress();
      loadLyrics(fileStem(currentSong.filename));
      await safePlay();
      break;
    }

    case "seek": {
      if (!currentSong) break;
      const duration = Number.isFinite(audioPlayer.duration) ? audioPlayer.duration : 0;
      const target = clamp(Number(command.position) || 0, 0,
        duration > 0 ? duration : Number(command.position) || 0);
      try { audioPlayer.currentTime = target; } catch { /* ignore */ }
      updatePlaybackProgress();
      break;
    }

    case "transpose": {
      if (!currentSong) break;
      transposeBusy = true;
      updateTransposeUI();
      const wasPlaying = !audioPlayer.paused;
      const position = Number.isFinite(audioPlayer.currentTime) ? audioPlayer.currentTime : 0;
      try {
        await loadSong(currentSong, {
          position,
          autoplay: wasPlaying,
          transpose: clamp(Number(command.semitones) || 0, -4, 4)
        });
      } finally {
        transposeBusyToken += 1;
        transposeBusy = false;
        updateTransposeUI();
      }
      break;
    }

    case "volume": {
      hostVolume = clamp(Number(command.volume) || 0, 0, 1);
      audioPlayer.volume = hostVolume;
      break;
    }

    case "stop": {
      stopAudio();
      currentSong = null;
      shared.current = null;
      shared.queue = [];
      queue = [];
      loading = false;
      errorActive = false;
      autoplayBlocked = false;
      lastPlaybackError = null;
      clearLyricTimers();
      updateNowPlayingUI();
      updateQueueDisplay();
      updatePlayPauseUI();
      break;
    }

    default:
      reportToServer("warning", `Unknown player command: ${command.action}`);
      break;
  }
}

// --- AUDIO ----------------------------------------------------------------

async function resolveAudioUrl(filename, semitones) {
  const key = `${filename}|${semitones}`;
  if (audioUrlCache.has(key)) return audioUrlCache.get(key);

  const response = await fetch(
    `/transpose?song_id=${encodeURIComponent(filename)}&semitones=${semitones}`);
  const data = await readJson(response);
  if (!response.ok || data.error) {
    const error = new Error(data.error || `Could not prepare the audio (${response.status}).`);
    error.details = data.details || null;
    throw error;
  }
  audioUrlCache.set(key, data.url);
  return data.url;
}

function waitForMetadata(audio, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    if (audio.readyState >= 1 && Number.isFinite(audio.duration)) {
      resolve();
      return;
    }

    let settled = false;
    const cleanup = () => {
      audio.removeEventListener("loadedmetadata", onLoaded);
      audio.removeEventListener("error", onError);
      clearTimeout(timer);
    };
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback(value);
    };
    const onLoaded = () => finish(resolve);
    const onError = () => finish(reject, new Error(mediaErrorMessage(audio)));
    const timer = setTimeout(
      () => finish(reject, new Error("Timed out while loading the audio file.")),
      timeoutMs);

    audio.addEventListener("loadedmetadata", onLoaded);
    audio.addEventListener("error", onError);
  });
}

function safePlay() {
  const promise = audioPlayer.play();
  if (!promise) return Promise.resolve(true);
  return promise.then(() => {
    errorActive = false;
    lastPlaybackError = null;
    autoplayBlocked = false;
    updatePlayPauseUI();
    updateHostStatus();
    return true;
  }).catch(err => {
    if (err && err.name === "NotAllowedError") {
      autoplayBlocked = true;
      showAudioUnlock(true);
      reportToServer("warning", "Browser blocked playback until the host screen is clicked", {
        song: currentSong ? currentSong.filename : null
      });
      return false;
    }
    if (err && err.name === "AbortError") return false; // a newer load took over
    throw err;
  });
}

async function loadSong(song, options = {}) {
  if (!song || !song.filename) throw new Error("No song was supplied.");

  const token = ++loadToken;
  const transpose = clamp(
    Number.isFinite(Number(options.transpose)) ? Number(options.transpose) : transposeLevel,
    -4, 4);
  const position = Math.max(0, Number(options.position) || 0);
  const autoplay = options.autoplay !== false;

  loading = true;
  errorActive = false;
  autoplayBlocked = false;
  lastPlaybackError = null;
  clearTimeout(autoSkipTimer);

  // Stop the previous song before the (possibly slow) transposition request.
  // This also guarantees that its "ended" event cannot be mistaken for the
  // new song if the old track finishes during a key change.
  try {
    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.load();
  } catch (err) {
    console.warn("Could not clear the previous audio:", err);
  }

  currentSong = { ...song, transpose };
  transposeLevel = transpose;
  clearLyricTimers();
  resetProgressUI();
  updateNowPlayingUI();
  updateQueueDisplay();
  updateTransposeUI();
  updatePlayPauseUI();
  reportNow();

  try {
    const url = await resolveAudioUrl(song.filename, transpose);
    if (token !== loadToken) return;

    audioPlayer.src = url;
    audioPlayer.volume = hostVolume;
    audioPlayer.load();
    await waitForMetadata(audioPlayer);
    if (token !== loadToken) return;

    const duration = Number.isFinite(audioPlayer.duration) ? audioPlayer.duration : 0;
    if (position > 0 && duration > 0) {
      try {
        audioPlayer.currentTime = Math.min(position, Math.max(0, duration - 0.15));
      } catch { /* ignore */ }
    }

    loadLyrics(fileStem(song.filename));

    if (autoplay) {
      const played = await safePlay();
      if (!played) {
        loading = false;
        updatePlayPauseUI();
        reportNow();
        return;
      }
    } else {
      audioPlayer.pause();
    }

    loading = false;
    updatePlaybackProgress();
    updatePlayPauseUI();
    updateNowPlayingUI();
    reportNow();
  } catch (err) {
    if (token !== loadToken) return;
    loading = false;
    handlePlaybackFailure(err, "Could not play this song.");
    throw err;
  }
}

function stopAudio() {
  loadToken += 1;
  loading = true;
  clearTimeout(autoSkipTimer);
  try {
    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.load();
  } catch (err) {
    console.warn("Could not clear the audio element:", err);
  } finally {
    loading = false;
  }
  resetProgressUI();
}

function handlePlaybackFailure(err, message) {
  loading = false;
  if (err && (err.name === "NotAllowedError" || err.name === "AbortError")) return;
  errorActive = true;
  lastPlaybackError = `${message} ${err && err.message ? err.message : String(err)}`.trim();
  updateConnectionDependentControls();
  updatePlayPauseUI();
  updateHostStatus();
  reportNow();
  scheduleAutoSkip();
  reportToServer("error", message, {
    song: currentSong ? currentSong.filename : null,
    error: String(err),
    serverDetails: err && err.details ? err.details : null
  });
}

function scheduleAutoSkip() {
  clearTimeout(autoSkipTimer);
  autoSkipTimer = setTimeout(() => {
    if (!errorActive || !currentSong || !registered || revoked) return;
    sendControl("skip").catch(() => {});
  }, 5000);
}

// --- LIBRARY --------------------------------------------------------------

function filterSongs(query) {
  return songs.filter(song =>
    (song.id || "").toLowerCase().includes(query) ||
    (song.title || "").toLowerCase().includes(query) ||
    (song.artist || "").toLowerCase().includes(query)
  );
}

function updateLibrarySummary(filteredCount = songs.length, query = "") {
  if (!songSummaryEl) return;
  if (!songs.length) {
    songSummaryEl.textContent = "No songs found in the songs folder.";
    return;
  }
  if (query) {
    songSummaryEl.textContent = `${filteredCount} ${pluralize(filteredCount, "result")} for “${query}”`;
    return;
  }
  const lyricsCount = songs.filter(song => song.lyrics).length;
  songSummaryEl.textContent = `${songs.length} ${pluralize(songs.length, "song")} · ${lyricsCount} with synced lyrics`;
}

function renderLibraryError() {
  songListContent.replaceChildren();
  const empty = makeElement("div", "library-empty");
  const icon = makeElement("span", "empty-icon");
  icon.appendChild(makeIcon("music"));
  empty.append(
    icon,
    makeElement("p", "empty-title", "Couldn’t load your library"),
    makeElement("p", "empty-copy", "Make sure the server is running, then refresh this page.")
  );
  songListContent.appendChild(empty);
  if (songSummaryEl) songSummaryEl.textContent = "Library unavailable.";
}

function displaySongs(list, query = "") {
  songListContent.replaceChildren();
  updateLibrarySummary(list.length, query);

  if (!list.length) {
    const empty = makeElement("div", "library-empty");
    const icon = makeElement("span", "empty-icon");
    icon.appendChild(makeIcon(query ? "search" : "music"));
    empty.append(
      icon,
      makeElement("p", "empty-title", query ? `No results for “${query}”` : "No songs yet"),
      makeElement("p", "empty-copy", query
        ? "Try a different song, artist, or ID."
        : "Add audio files to the songs folder and refresh this page.")
    );

    if (query) {
      const copy = empty.querySelector(".empty-copy");
      const clear = makeElement("button", "", "Clear search");
      clear.type = "button";
      clear.addEventListener("click", () => {
        searchInput.value = "";
        displaySongs(songs);
        searchInput.focus();
      });
      copy.append(" ", clear);
    }

    songListContent.appendChild(empty);
    return;
  }

  list.forEach((song, index) => {
    const row = makeElement("div", "song-row");

    const id = makeElement("span", "song-id", song.id || String(index + 1).padStart(2, "0"));
    id.title = song.id ? `Song ID ${song.id}` : "";

    const main = makeElement("div", "song-main");
    const titleLine = makeElement("div", "song-title-line");
    const title = makeElement("span", "song-title", song.title);
    title.title = song.title;
    titleLine.appendChild(title);

    if (song.lyrics) {
      const badge = makeElement("span", "lyrics-badge");
      badge.title = "Synced lyrics available";
      badge.append(makeIcon("lyrics"), makeElement("span", "", "Lyrics"));
      titleLine.appendChild(badge);
    }

    const artist = makeElement("div", "song-artist", song.artist || "Unknown artist");
    artist.title = song.artist || "Unknown artist";
    main.append(titleLine, artist);

    const actions = makeElement("div", "song-actions");
    if (document.body.dataset.editorAvailable === "1") {
      const editLink = makeElement("a", "edit-lyrics-button");
      editLink.href = `/sync_editor.html?song=${encodeURIComponent(song.filename)}`;
      editLink.title = `Edit lyrics for ${song.title}`;
      editLink.setAttribute("aria-label", `Edit lyrics for ${song.title}`);
      editLink.append(makeIcon("tune"), makeElement("span", "", "Edit lyrics"));
      actions.appendChild(editLink);
    }

    const addButton = makeElement("button", "add-button");
    addButton.type = "button";
    addButton.title = `Add ${song.title} to the queue`;
    addButton.setAttribute("aria-label", `Add ${song.title} to the queue`);
    addButton.append(makeIcon("plus"), makeElement("span", "", "Add"));
    addButton.addEventListener("click", () => {
      addToQueue(song)
        .then(() => flashAddedButton(addButton))
        .catch(() => {});
    });
    actions.appendChild(addButton);

    row.append(id, main, actions);
    songListContent.appendChild(row);
  });
}

function flashAddedButton(button) {
  if (!button) return;
  button.classList.add("is-added");
  button.querySelector("use")?.setAttribute("href", "#icon-check");
  const label = button.querySelector("span");
  if (label) label.textContent = "Added";

  clearTimeout(button._addedTimer);
  button._addedTimer = setTimeout(() => {
    button.classList.remove("is-added");
    button.querySelector("use")?.setAttribute("href", "#icon-plus");
    if (label) label.textContent = "Add";
  }, 1300);
}

// --- UI UPDATE FUNCTIONS --------------------------------------------------

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

function updateNowPlayingUI() {
  const song = shared.current;
  const hasSong = !!song;
  document.body.classList.toggle("has-song", hasSong);

  if (song) {
    nowPlayingEl.textContent = song.title;
    nowPlayingEl.title = song.title;
    nowPlayingArtistEl.textContent = song.artist || "Unknown artist";
    nowPlayingArtistEl.title = song.artist || "Unknown artist";
    nowPlayingArtEl.style.background = artGradient(song);
  } else {
    nowPlayingEl.textContent = "Nothing playing";
    nowPlayingEl.title = "";
    nowPlayingArtistEl.textContent = "Add a song to start the queue";
    nowPlayingArtistEl.title = "";
    nowPlayingArtEl.style.background = "";
  }

  updateConnectionDependentControls();
  updatePlayPauseUI();
  updateTransposeUI();
  if (!hasSong) resetProgressUI();
}

function updatePlayPauseUI() {
  const playing = !!shared.current && !audioPlayer.paused && !!currentSong;
  if (playPauseButton) {
    playPauseButton.classList.toggle("is-playing", playing);
    playPauseButton.title = playing ? "Pause" : "Play";
    playPauseButton.setAttribute("aria-label", playing ? "Pause" : "Play");
  }
}

function updateQueueDisplay() {
  maybeSyncQueueCount();
  if (!queueDisplayEl) return;

  const song = shared.current;
  const renderKey = JSON.stringify({
    current: song ? song.filename : null,
    queue: queue.map(queuedSong => queuedSong.filename)
  });
  if (renderKey === queueRenderKey && queueDisplayEl.childElementCount) return;
  queueRenderKey = renderKey;
  queueDisplayEl.replaceChildren();

  if (!song && !queue.length) {
    renderQueueEmpty();
    return;
  }

  if (song) {
    const now = makeElement("div", "queue-now");
    const art = makeElement("div", "queue-now-art");
    art.style.background = artGradient(song);
    const equalizer = makeElement("span", "equalizer");
    equalizer.append(makeElement("i"), makeElement("i"), makeElement("i"));
    art.appendChild(equalizer);

    const copy = makeElement("div", "queue-now-copy");
    const label = makeElement("p", "queue-now-label");
    label.append(makeElement("span", "", "Now playing"));
    const title = makeElement("p", "queue-now-title", song.title);
    title.title = song.title;
    const artist = makeElement("p", "queue-now-artist", song.artist || "Unknown artist");
    artist.title = song.artist || "Unknown artist";
    copy.append(label, title, artist);
    now.append(art, copy);
    queueDisplayEl.appendChild(now);
  }

  if (queue.length) {
    queueDisplayEl.appendChild(makeElement("p", "queue-section-label", "Next in queue"));
    queue.forEach((queuedSong, index) => {
      const item = makeElement("div", "queue-item");
      item.append(
        makeElement("span", "queue-item-index", String(index + 1).padStart(2, "0"))
      );
      const copy = makeElement("div", "queue-item-copy");
      const title = makeElement("p", "queue-item-title", queuedSong.title);
      title.title = queuedSong.title;
      const artist = makeElement("p", "queue-item-artist", queuedSong.artist || "Unknown artist");
      artist.title = queuedSong.artist || "Unknown artist";
      copy.append(title, artist);

      const remove = makeElement("button", "queue-remove");
      remove.type = "button";
      remove.title = `Remove ${queuedSong.title}`;
      remove.setAttribute("aria-label", `Remove ${queuedSong.title} from the queue`);
      remove.appendChild(makeIcon("close"));
      remove.addEventListener("click", () => {
        remove.disabled = true;
        sendControl("remove", { index }).catch(() => {
          remove.disabled = false;
        });
      });

      item.append(copy, remove);
      queueDisplayEl.appendChild(item);
    });
  } else if (song) {
    queueDisplayEl.appendChild(makeElement("p", "queue-note", "No more songs queued. Add another from the library."));
  }
}

function maybeSyncQueueCount() {
  if (!queueCountEl || !queueSummaryEl) return;
  queueCountEl.textContent = String(queue.length);
  queueCountEl.classList.toggle("is-zero", queue.length === 0);
  queueCountEl.setAttribute("aria-label", `${queue.length} ${pluralize(queue.length, "song")} in queue`);
  queueSummaryEl.textContent = queue.length
    ? `${queue.length} ${pluralize(queue.length, "song")}`
    : "Empty";
  if (queueClearButton) {
    queueClearButton.hidden = queue.length === 0;
    queueClearButton.disabled = !registered || revoked;
  }
}

function renderQueueEmpty() {
  const empty = makeElement("div", "queue-empty");
  const icon = makeElement("span", "empty-icon");
  icon.appendChild(makeIcon("queue"));
  empty.append(
    icon,
    makeElement("p", "empty-title", "Your queue is empty"),
    makeElement("p", "empty-copy", "Add songs from the library and they’ll line up here.")
  );
  queueDisplayEl.appendChild(empty);
}

function updateLyrics(text) {
  const value = String(text || "");
  const instrumental = /^\s*\[\s*instrumental\s*\]\s*$/i.test(value);
  lyricsEl.classList.toggle("instrumental", instrumental);
  lyricsEl.textContent = instrumental ? "Instrumental" : value;
}

function updateTransposeUI() {
  if (!transposeIndicatorBar) return;

  const online = registered && !revoked;
  const renderKey = `${transposeLevel}|${online}|${!!shared.current}|${transposeBusy}`;
  if (renderKey === transposeRenderKey) return;
  transposeRenderKey = renderKey;
  transposeIndicatorBar.replaceChildren();

  for (let i = -4; i <= 4; i++) {
    const dot = makeElement("span", "key-dot");
    if (i === transposeLevel) dot.classList.add("is-active");
    if (i === 0) dot.classList.add("is-center");
    dot.title = `Key ${signedSemitones(i)}`;
    transposeIndicatorBar.appendChild(dot);
  }

  if (transposeValueEl) {
    transposeValueEl.textContent = signedSemitones(transposeLevel);
    transposeValueEl.title = `Transpose ${signedSemitones(transposeLevel)} semitones`;
  }

  if (keyDownButton) {
    keyDownButton.disabled = !online || !shared.current || transposeBusy || transposeLevel <= -4;
  }
  if (keyUpButton) {
    keyUpButton.disabled = !online || !shared.current || transposeBusy || transposeLevel >= 4;
  }
}

// --- PLAYBACK PROGRESS ----------------------------------------------------

function updatePlaybackProgress() {
  const duration = Number.isFinite(audioPlayer.duration) ? audioPlayer.duration : 0;
  const current = Number.isFinite(audioPlayer.currentTime) ? audioPlayer.currentTime : 0;
  const percent = duration > 0 ? clamp((current / duration) * 100, 0, 100) : 0;

  if (progressFill) progressFill.style.width = `${percent}%`;
  if (currentTimeEl) currentTimeEl.textContent = formatPlaybackTime(currentSong ? current : 0);
  if (durationTimeEl) durationTimeEl.textContent = formatPlaybackTime(currentSong ? duration : 0);
  if (progressTrack) progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
}

function resetProgressUI() {
  if (progressFill) progressFill.style.width = "0%";
  if (currentTimeEl) currentTimeEl.textContent = "0:00";
  if (durationTimeEl) durationTimeEl.textContent = "0:00";
  if (progressTrack) progressTrack.setAttribute("aria-valuenow", "0");
}

function seekTo(target) {
  if (!currentSong) return;
  const duration = Number.isFinite(audioPlayer.duration) ? audioPlayer.duration : 0;
  if (duration <= 0) return;
  try { audioPlayer.currentTime = clamp(target, 0, duration); } catch { return; }
  updatePlaybackProgress();
  reportNow();
}

function seekFromPointer(event) {
  if (!currentSong) return;
  const duration = audioPlayer.duration;
  if (!Number.isFinite(duration) || duration <= 0) return;
  const rect = progressTrack.getBoundingClientRect();
  if (!rect.width) return;
  const fraction = clamp((event.clientX - rect.left) / rect.width, 0, 1);
  seekTo(fraction * duration);
}

if (progressTrack) {
  progressTrack.addEventListener("pointerdown", event => {
    if (!currentSong || !registered) return;
    progressTrack.setPointerCapture?.(event.pointerId);
    seekFromPointer(event);
  });
  progressTrack.addEventListener("pointermove", event => {
    if (!currentSong || !registered) return;
    if (progressTrack.hasPointerCapture?.(event.pointerId)) seekFromPointer(event);
  });
  progressTrack.addEventListener("pointerup", event => {
    progressTrack.releasePointerCapture?.(event.pointerId);
  });
  progressTrack.addEventListener("keydown", event => {
    if (!currentSong || !registered) return;
    const duration = audioPlayer.duration;
    if (!Number.isFinite(duration) || duration <= 0) return;
    let next = null;
    if (event.key === "ArrowLeft") next = audioPlayer.currentTime - 5;
    else if (event.key === "ArrowRight") next = audioPlayer.currentTime + 5;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = duration;
    if (next === null) return;
    event.preventDefault();
    seekTo(next);
  });
}

// --- LYRICS HANDLING ------------------------------------------------------

function stopLyricsAnimation() {
  if (lyricsAnimationFrame !== null) {
    cancelAnimationFrame(lyricsAnimationFrame);
    lyricsAnimationFrame = null;
  }
}

function startLyricsAnimation() {
  // Only animate while audio is actually playing; a paused host should not
  // burn a requestAnimationFrame loop (important on low-power laptops).
  if (lyricsAnimationFrame !== null || audioPlayer.paused) return;
  const tick = () => {
    if (wordTimeline && currentSong) {
      updateWordHighlight(audioPlayer.currentTime);
    }
    lyricsAnimationFrame = requestAnimationFrame(tick);
  };
  lyricsAnimationFrame = requestAnimationFrame(tick);
}

function parseAndSyncLyrics(srt) {
  clearLyricTimers();

  const normalized = String(srt || "").replace(/^\uFEFF/, "");
  const lines = normalized.split(/\r?\n\r?\n/).map(block => {
    const parts = block.trim().split(/\r?\n/);
    const timingIndex = parts.findIndex(line => line.includes("-->"));
    if (timingIndex < 0) return null;
    const [startStr, endStr] = parts[timingIndex].split("-->");
    if (!endStr) return null;
    const start = parseTime(startStr);
    const end = parseTime(endStr.trim().split(/\s+/)[0]);
    const text = parts.slice(timingIndex + 1).join(" ").trim();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      return null;
    }
    return { start, end, text };
  }).filter(Boolean);

  lyricsTimeline = lines.sort((a, b) => a.start - b.start || a.end - b.end);
  lineLevelLyricIndex = -1;
}

// Word-level sidecar produced by tools/generate_lyrics.py. The ordinary
// .srt file remains the fallback and works with any external SRT player.
function parseWordTimings(data) {
  clearLyricTimers();
  if (!data || !Array.isArray(data.lines) || !data.lines.length) return false;

  wordTimeline = data;
  lyricsTimeline = data.lines.map(line => ({
    start: Number(line.start),
    end: Number(line.end),
    text: String(line.text || ""),
    instrumental: !!line.instrumental,
    words: Array.isArray(line.words)
      ? line.words.map(word => ({
          text: String(word.text || ""),
          start: Number(word.start),
          end: Number(word.end)
        }))
      : []
  })).filter(line => Number.isFinite(line.start) && Number.isFinite(line.end) && line.end > line.start);

  if (!lyricsTimeline.length) {
    wordTimeline = null;
    return false;
  }

  activeLyricLineIndex = -1;
  activeLyricWordIndex = -1;
  startLyricsAnimation();
  updateWordHighlight(audioPlayer.currentTime);
  return true;
}

function renderWordLine(line) {
  const instrumental = !!line.instrumental || /^\s*\[\s*instrumental\s*\]\s*$/i.test(line.text || "");
  lyricsEl.classList.toggle("instrumental", instrumental);
  lyricsEl.innerHTML = "";

  if (instrumental) {
    lyricsEl.textContent = "Instrumental";
    return;
  }

  if (!line.words.length) {
    lyricsEl.textContent = line.text;
    return;
  }

  line.words.forEach((word, index) => {
    if (index > 0) lyricsEl.appendChild(document.createTextNode(" "));
    const span = document.createElement("span");
    span.className = "word";
    span.textContent = word.text;
    lyricsEl.appendChild(span);
  });
}

function lyricLineAt(now) {
  let low = 0;
  let high = lyricsTimeline.length - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const line = lyricsTimeline[mid];
    if (now < line.start) {
      high = mid - 1;
    } else if (now >= line.end) {
      low = mid + 1;
    } else {
      return mid;
    }
  }
  return -1;
}

function updateWordHighlight(now) {
  if (!wordTimeline || !lyricsTimeline.length) return;

  const lineIndex = lyricLineAt(now);
  if (lineIndex === -1) {
    if (activeLyricLineIndex !== -1) {
      activeLyricLineIndex = -1;
      activeLyricWordIndex = -1;
      lyricsEl.classList.remove("instrumental");
      lyricsEl.textContent = "";
    }
    return;
  }

  const line = lyricsTimeline[lineIndex];
  if (lineIndex !== activeLyricLineIndex) {
    activeLyricLineIndex = lineIndex;
    activeLyricWordIndex = -1;
    renderWordLine(line);
  }

  if (line.instrumental || !line.words.length) return;

  let wordIndex = -1;
  for (let i = 0; i < line.words.length; i++) {
    const word = line.words[i];
    if (now >= word.start) wordIndex = i;
    if (now < word.end) break;
  }
  if (wordIndex === activeLyricWordIndex) return;

  const spans = lyricsEl.querySelectorAll(".word");
  spans.forEach((span, index) => {
    span.classList.toggle("active", index === wordIndex);
    span.classList.toggle("sung", index < wordIndex);
  });
  activeLyricWordIndex = wordIndex;
}

audioPlayer.addEventListener("timeupdate", () => {
  updatePlaybackProgress();
  if (wordTimeline || !lyricsTimeline.length || audioPlayer.paused) return;

  const now = audioPlayer.currentTime;
  let activeIndex = -1;
  for (let i = 0; i < lyricsTimeline.length; i++) {
    if (now >= lyricsTimeline[i].start && now < lyricsTimeline[i].end) {
      activeIndex = i;
      break;
    }
    if (lyricsTimeline[i].start > now) break;
  }

  if (activeIndex !== lineLevelLyricIndex) {
    lineLevelLyricIndex = activeIndex;
    updateLyrics(activeIndex >= 0 ? lyricsTimeline[activeIndex].text : "");
  }
});

function clearLyricTimers() {
  lyricsRequestToken += 1;
  clearTimeout(lyricsClearTimer);
  lyricsClearTimer = null;
  stopLyricsAnimation();
  lyricsTimeline = [];
  wordTimeline = null;
  lineLevelLyricIndex = -1;
  activeLyricLineIndex = -1;
  activeLyricWordIndex = -1;
  lyricsEl.classList.remove("instrumental");
  lyricsEl.textContent = "";
}

function parseTime(t) {
  const text = String(t || "").trim().replace(",", ".");
  const parts = text.split(":");
  if (parts.length < 1 || parts.length > 3) return NaN;
  const values = parts.map(part => Number(part));
  if (values.some(value => !Number.isFinite(value))) return NaN;
  let seconds;
  if (values.length === 3) seconds = values[0] * 3600 + values[1] * 60 + values[2];
  else if (values.length === 2) seconds = values[0] * 60 + values[1];
  else seconds = values[0];
  return Number.isFinite(seconds) ? seconds : NaN;
}

function loadLyrics(filename) {
  // Filename should not have extension here. Absolute paths keep the lookup
  // working on /player and /player/ as well as on /.
  const token = ++lyricsRequestToken;
  clearTimeout(lyricsClearTimer);
  lyricsClearTimer = null;
  const base = `/lyrics/${encodeURIComponent(filename)}`;
  const srtPath = `${base}.srt`;
  const wordsPath = `${base}.words.json`;

  const srtPromise = fetch(srtPath, { cache: "no-store" })
    .then(res => res.ok ? res.text() : null)
    .catch(() => null);
  const wordsPromise = fetch(wordsPath, { cache: "no-store" })
    .then(res => res.ok ? res.json() : null)
    .catch(() => null);

  Promise.all([srtPromise, wordsPromise])
    .then(([srt, words]) => {
      if (token !== lyricsRequestToken) return;
      if (words && parseWordTimings(words)) return;
      if (srt) {
        parseAndSyncLyrics(srt);
        return;
      }
      throw new Error("Lyrics not found");
    })
    .catch(() => {
      if (token !== lyricsRequestToken) return;
      clearLyricTimers();
      updateLyrics("Lyrics not found for this song.");
      lyricsClearTimer = setTimeout(() => updateLyrics(""), 5000);
    });
}

// --- TRANSPORT / QUEUE COMMANDS ------------------------------------------

async function sendControl(action, payload = {}) {
  const response = await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload })
  });
  const data = await readJson(response);
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    if (response.status === 409 && /active player|offline/i.test(error.message)) {
      scheduleReconnect(1200);
    }
    throw error;
  }
  if (data.state) applySharedState(data.state);
  return data;
}

async function addToQueue(song) {
  await sendControl("add", { filename: song.filename });
}

async function togglePlayPause() {
  if (!registered || revoked || loading) return;

  if (!shared.current) {
    // The server starts the first queued song when asked to play.
    if (!shared.queue.length) return;
    try { await sendControl("play"); } catch (err) { showHostNotice(err.message); }
    return;
  }

  if (currentSong && currentSong.filename === shared.current.filename) {
    if (!audioPlayer.src) {
      try { await sendControl("play"); } catch (err) { showHostNotice(err.message); }
      return;
    }
    if (audioPlayer.paused) {
      const played = await safePlay();
      if (!played) showAudioUnlock(true);
    } else {
      audioPlayer.pause();
    }
    updatePlayPauseUI();
    reportNow();
    return;
  }

  // The server has a current song but this browser has not loaded it yet.
  // Let the server's transport state drive the load.
  try {
    await sendControl(audioPlayer.paused ? "play" : "pause");
  } catch (err) {
    showHostNotice(err.message);
  }
}

async function toggleRepeat() {
  if (!currentSong || !registered || loading || !audioPlayer.src) return;
  errorActive = false;
  lastPlaybackError = null;
  try { audioPlayer.currentTime = 0; } catch { /* ignore */ }
  updatePlaybackProgress();
  loadLyrics(fileStem(currentSong.filename));
  const played = await safePlay();
  if (!played) showAudioUnlock(true);
  reportNow();
}

async function cancelSong() {
  if (!registered) return;
  try {
    await sendControl("skip");
  } catch (err) {
    showHostNotice(err.message);
  }
}

async function changeTranspose(direction) {
  if (!registered || !shared.current || transposeBusy) return;

  const target = clamp(transposeLevel + direction, -4, 4);
  if (target === transposeLevel) return;

  transposeBusy = true;
  const busyToken = ++transposeBusyToken;
  transposeLevel = target;
  updateTransposeUI();

  try {
    await sendControl("transpose", { semitones: target });
    // executeCommand() clears the busy flag when the new audio is ready.
    setTimeout(() => {
      if (transposeBusy && transposeBusyToken === busyToken) {
        transposeBusy = false;
        updateTransposeUI();
      }
    }, 60000);
  } catch (err) {
    if (transposeBusyToken === busyToken) {
      transposeBusy = false;
      transposeLevel = clamp(shared.transpose, -4, 4);
      updateTransposeUI();
    }
    showHostNotice(err.message);
  }
}

// --- HOST UI EVENTS -------------------------------------------------------

searchInput.addEventListener("input", () => {
  const query = searchInput.value.trim();
  displaySongs(query ? filterSongs(query.toLowerCase()) : songs, query);
});

restartButton?.addEventListener("click", toggleRepeat);
playPauseButton?.addEventListener("click", togglePlayPause);
skipButton?.addEventListener("click", cancelSong);
keyDownButton?.addEventListener("click", () => changeTranspose(-1));
keyUpButton?.addEventListener("click", () => changeTranspose(1));

document.addEventListener("keydown", evt => {
  const target = evt.target;
  const isTyping = target instanceof HTMLElement &&
    (target.matches("input, textarea, select") || target.isContentEditable);
  const isInteractive = target instanceof Element &&
    target.closest("button, a, input, textarea, select, [role='slider']");

  if (evt.key === "/" && !isTyping) {
    evt.preventDefault();
    searchInput.focus();
    searchInput.select();
    return;
  }

  if (evt.key === "Escape" && document.activeElement === searchInput) {
    if (searchInput.value) {
      searchInput.value = "";
      displaySongs(songs);
    } else {
      searchInput.blur();
    }
    return;
  }

  if (evt.code === "Space" && !isTyping && !isInteractive && shared.current) {
    evt.preventDefault();
    togglePlayPause();
  }
});

audioPlayer.addEventListener("ended", () => {
  stopLyricsAnimation();
  if (!currentSong || loading) return;
  const endedFilename = currentSong.filename;
  fetch("/api/host/ended", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: CLIENT_ID,
      last_command_id: lastCommandId,
      filename: endedFilename
    })
  }).catch(err => {
    console.warn("Could not report the end of the song:", err);
  });
});

audioPlayer.addEventListener("play", () => {
  autoplayBlocked = false;
  startLyricsAnimation();
  updatePlayPauseUI();
  reportNow();
});
audioPlayer.addEventListener("pause", () => {
  stopLyricsAnimation();
  if (wordTimeline) updateWordHighlight(audioPlayer.currentTime);
  updatePlayPauseUI();
  reportNow();
});
audioPlayer.addEventListener("seeked", () => {
  if (wordTimeline) updateWordHighlight(audioPlayer.currentTime);
});
audioPlayer.addEventListener("loadedmetadata", updatePlaybackProgress);
audioPlayer.addEventListener("durationchange", updatePlaybackProgress);
audioPlayer.addEventListener("emptied", updatePlaybackProgress);

audioPlayer.addEventListener("error", () => {
  if (!audioPlayer.currentSrc || !currentSong || loading) return;
  const message = "Audio playback failed. " + mediaErrorMessage(audioPlayer);
  handlePlaybackFailure(new Error(mediaErrorMessage(audioPlayer)), message);
});

if (queueClearButton) {
  queueClearButton.addEventListener("click", async () => {
    if (!queue.length || queueClearButton.disabled) return;
    const count = queue.length;
    const confirmed = await karaokeConfirm({
      title: "Clear the queue?",
      message: `Remove ${count} queued ${pluralize(count, "song")} from Up next. The current song will keep playing.`,
      confirmLabel: "Clear queue",
      tone: "danger",
      focus: "cancel"
    });
    if (!confirmed) return;

    queueClearButton.disabled = true;
    try {
      await sendControl("clear");
    } catch (err) {
      showHostNotice(err.message);
    } finally {
      updateConnectionDependentControls();
    }
  });
}

if (copyJoinUrlButton) {
  copyJoinUrlButton.addEventListener("click", async () => {
    const value = hostJoinUrl ? hostJoinUrl.textContent.trim() : "";
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const helper = document.createElement("textarea");
      helper.value = value;
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
    copyJoinUrlButton.textContent = "Copied";
    setTimeout(() => { copyJoinUrlButton.textContent = "Copy"; }, 1400);
  });
}

if (audioUnlockButton) {
  audioUnlockButton.addEventListener("click", event => {
    event.stopPropagation();
    unlockAudio();
  });
}

document.addEventListener("pointerdown", () => {
  if (!audioUnlocked) armAudioFromGesture();
}, { capture: true });

document.addEventListener("keydown", () => {
  if (!audioUnlocked) armAudioFromGesture();
}, { capture: true });

if (hostTakeoverButton) {
  hostTakeoverButton.addEventListener("click", () => {
    revoked = false;
    if (hostOverlay) hostOverlay.hidden = true;
    connectHost(true);
  });
}

if (hostReloadButton) {
  hostReloadButton.addEventListener("click", () => window.location.reload());
}

// --- STARTUP --------------------------------------------------------------

fetch("/api/info", { cache: "no-store" })
  .then(res => res.ok ? res.json() : null)
  .then(info => {
    if (!info || !hostJoinUrl) return;
    const joinUrl = info.join_url || `${location.protocol}//${location.hostname}:${info.port || location.port}`;
    hostJoinUrl.textContent = joinUrl;
    hostJoinUrl.title = joinUrl;
  })
  .catch(() => {
    if (hostJoinUrl) {
      const fallback = `${location.protocol}//${location.hostname}:${location.port}`;
      hostJoinUrl.textContent = fallback;
      hostJoinUrl.title = fallback;
    }
  });

fetch("/api/songs", { cache: "no-store" })
  .then(res => res.json())
  .then(data => {
    songs.push(...data.songs.sort((a, b) => a.title.localeCompare(b.title)));
    displaySongs(songs);
  })
  .catch(err => {
    console.error("Failed to load song list:", err);
    reportToServer("error", "Failed to load song list from /api/songs", { error: String(err) });
    renderLibraryError();
  });

updateNowPlayingUI();
updateQueueDisplay();
updateTransposeUI();
updatePlaybackProgress();
updateHostStatus();
connectHost();
