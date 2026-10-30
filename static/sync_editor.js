/* Lyrics Editor — text-first editing with safe word-timing reconciliation. */
"use strict";

const state = {
  cues: [],
  songs: [],
  song: null,
  loadedStem: null,
  source: "none",              // none | active | draft | import
  draftRevision: null,
  baseActiveRevision: null,
  activeRevision: null,
  revisionSong: null,
  duration: 0,
  wordsMeta: null,
  dirty: false,
  selectedIndex: -1,
  selectedWordIndex: -1,
  mode: "lyrics",              // lyrics | timing | words
  history: [],
  redo: [],
  audioReady: false,
  audioToken: 0,
  loadToken: 0,
  validationErrors: [],
  follow: true,
  ripple: false,
  showExactTimes: false,
  localAudioUrl: null,
  activeCueIndex: -1,
  activeWordIndex: -1,
  previewEls: [],
  rowEls: [],
  wordChipEls: [],
  saving: false,
  loopActive: false,
  loopStart: 0,
  loopEnd: 0,
  zoomPx: 0,
  textEdit: null,              // {index, originalText, originalWords, originalReview, snapshot, pushed}
  lastReconcileNotice: null,   // {index, message}
  lastCaret: null,             // {index, offset}
  lastFocusedCue: null,
};

let wavesurfer = null;
const AUTOSAVE_MS = 30000;
let autosaveTimer = null;
let renderTimer = null;
let reconcileResolve = null;

/* ---------- dom ---------- */
const $ = id => document.getElementById(id);
const songContext = $("songContext");
const statusChip = $("statusChip");
const songSelect = $("songSelect");
const loadSongBtn = $("loadSongBtn");
const emptyState = $("emptyState");
const workspace = $("workspace");
const emptyStatus = $("emptyStatus");
const saveBtn = $("saveBtn");
const publishBtn = $("publishBtn");
const saveStatus = $("saveStatus");
const validationEl = $("validation");
const cuesEl = $("cues");
const cueCount = $("cueCount");
const lyricsPanel = $("lyricsPanel");
const wordPanel = $("wordPanel");
const timingPanel = $("timingPanel");
const wordFocusText = $("wordFocusText");
const wordChips = $("wordChips");
const wordInspector = $("wordInspector");
const wordLineLabel = $("wordLineLabel");
const prevLineBtn = $("prevLineBtn");
const nextLineBtn = $("nextLineBtn");
const backToLyricsBtn = $("backToLyricsBtn");
const createWordTimingBtn = $("createWordTimingBtn");
const stopWordTimingBtn = $("stopWordTimingBtn");
const subtitlePreview = $("subtitlePreview");
const cueOverlay = $("cueOverlay");
const playPauseBtn = $("playPauseBtn");
const playPauseBtnBottom = $("playPauseBtnBottom");
const timeEl = $("timeDisplay");
const timeElBottom = $("timeDisplayBottom");
const audioStatus = $("audioStatus");
const srtOutput = $("srtOutput");
const undoBtn = $("undoBtn");
const redoBtn = $("redoBtn");
const zoomOutBtn = $("zoomOutBtn");
const zoomFitBtn = $("zoomFitBtn");
const zoomInBtn = $("zoomInBtn");
const loopCueBtn = $("loopCueBtn");
const rateSelect = $("rateSelect");
const exactTimesToggle = $("exactTimesToggle");
const exactTimesPanel = $("exactTimesPanel");
const exactStartInput = $("exactStartInput");
const exactEndInput = $("exactEndInput");
const offsetInput = $("offsetInput");
const draftNote = $("draftNote");
const modeHint = $("modeHint");
const reconcileDialog = $("reconcileDialog");
const reconcileMessage = $("reconcileMessage");
const reconcileApplyBtn = $("reconcileApplyBtn");
const reconcileCancelBtn = $("reconcileCancelBtn");

/* ---------- utilities ---------- */
function pad2(value) { return String(value).padStart(2, "0"); }
function pad3(value) { return String(value).padStart(3, "0"); }
function fileStem(filename) { return String(filename).replace(/\.[^.]+$/, ""); }
function lyricsStem(filename) {
  return String(filename).replace(/\.words\.json$/i, "").replace(/\.[^.]+$/, "");
}
function roundTime(value) { return Math.round(Number(value) * 1000) / 1000; }
function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
function normalizeText(text) { return String(text ?? "").replace(/\s+/g, " ").trim(); }

function formatTime(seconds) {
  const totalMs = Math.max(0, Math.round(Number(seconds) * 1000));
  const h = Math.floor(totalMs / 3600000);
  const m = Math.floor((totalMs % 3600000) / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${pad2(h)}:${pad2(m)}:${pad2(s)},${pad3(ms)}`;
}
function formatClock(seconds) { return formatTime(seconds).replace(",", "."); }
function formatPlayerTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${pad2(total % 60)}`;
}
function formatDelta(seconds) {
  const sign = seconds >= 0 ? "+" : "";
  return `${sign}${Number(seconds).toFixed(1)}s`;
}
function parseTime(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  let text = value.trim().replace(",", ".");
  if (!text) return null;
  let sign = 1;
  if (text[0] === "-" || text[0] === "+") {
    sign = text[0] === "-" ? -1 : 1;
    text = text.slice(1);
  }
  if (!/^\d+(?::\d+){0,2}(?:\.\d+)?$/.test(text)) return null;
  const parts = text.split(":");
  if (parts.length > 3) return null;
  let seconds;
  if (parts.length === 3) {
    const h = Number(parts[0]);
    const m = Number(parts[1]);
    const s = Number(parts[2]);
    if (m >= 60 || s >= 60) return null;
    seconds = h * 3600 + m * 60 + s;
  } else if (parts.length === 2) {
    const m = Number(parts[0]);
    const s = Number(parts[1]);
    if (s >= 60) return null;
    seconds = m * 60 + s;
  } else {
    seconds = Number(parts[0]);
  }
  return Number.isFinite(seconds) ? sign * seconds : null;
}

function cloneCue(cue) {
  return {
    start: cue.start,
    end: cue.end,
    text: cue.text,
    instrumental: !!cue.instrumental,
    words: Array.isArray(cue.words)
      ? cue.words.map(word => ({ text: word.text, start: word.start, end: word.end }))
      : [],
    review: cue.review || null,
    needsReconcile: false,
  };
}

function makeCue({ start, end, text, instrumental, words }) {
  const cleanText = String(text ?? "");
  const isInstr = !!instrumental
    || normalizeText(cleanText).toLowerCase() === "[instrumental]";
  return {
    start: roundTime(Math.max(0, Number(start) || 0)),
    end: roundTime(Math.max(0, Number(end) || 0)),
    text: isInstr ? "[Instrumental]" : cleanText,
    instrumental: isInstr,
    words: isInstr || !Array.isArray(words) ? [] : words.map(word => ({
      text: String(word?.text ?? ""),
      start: roundTime(Number(word?.start) || 0),
      end: roundTime(Number(word?.end) || 0),
    })),
    review: null,
    needsReconcile: false,
  };
}

function parseSRT(text) {
  const parsed = [];
  const normalized = String(text).replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  for (const block of normalized.split(/\n{2,}/)) {
    const lines = block.split("\n").map(line => line.replace(/\s+$/, ""))
      .filter(line => line.trim() !== "");
    if (!lines.length) continue;
    const timeIndex = lines.findIndex(line => line.includes("-->"));
    if (timeIndex === -1) continue;
    const [startRaw, endRaw] = lines[timeIndex].split("-->").map(part => part.trim());
    const start = parseTime(startRaw);
    const end = parseTime(endRaw);
    if (start === null || end === null || end <= start) continue;
    const lineText = lines.slice(timeIndex + 1).join(" ").trim();
    const instrumental = normalizeText(lineText).toLowerCase() === "[instrumental]";
    parsed.push(makeCue({
      start,
      end,
      text: instrumental ? "[Instrumental]" : lineText,
      instrumental,
      words: [],
    }));
  }
  parsed.sort((a, b) => a.start - b.start || a.end - b.end);
  return parsed;
}

function parseWordsData(data) {
  if (!data || !Array.isArray(data.lines) || !data.lines.length) {
    return { error: "No lyric lines were found in that file." };
  }
  const parsed = [];
  const warnings = [];
  for (let index = 0; index < data.lines.length; index += 1) {
    const line = data.lines[index];
    if (!line || typeof line !== "object") {
      warnings.push(`Line ${index + 1} could not be read.`);
      continue;
    }
    const start = Number(line.start);
    const end = Number(line.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      warnings.push(`Line ${index + 1} has an invalid time range.`);
      continue;
    }
    const text = String(line.text ?? "");
    if (!normalizeText(text)) {
      warnings.push(`Line ${index + 1} is empty.`);
      continue;
    }
    const instrumental = !!line.instrumental
      || normalizeText(text).toLowerCase() === "[instrumental]";
    let words = [];
    if (Array.isArray(line.words)) {
      for (const word of line.words) {
        const wordStart = Number(word?.start);
        const wordEnd = Number(word?.end);
        if (!Number.isFinite(wordStart) || !Number.isFinite(wordEnd)
            || wordEnd <= wordStart) {
          warnings.push(`Line ${index + 1} has an invalid word time.`);
          words = null;
          break;
        }
        words.push({ text: String(word?.text ?? ""), start: wordStart, end: wordEnd });
      }
    }
    if (words === null) continue;
    parsed.push(makeCue({
      start,
      end,
      text: instrumental ? "[Instrumental]" : text,
      instrumental,
      words: instrumental ? [] : words,
    }));
  }
  if (!parsed.length) {
    return { error: warnings.slice(0, 3).join(" ") || "No lyric lines could be read." };
  }
  return {
    cues: parsed,
    meta: {
      version: data.version ?? 1,
      song: data.song ?? "",
      duration: Number.isFinite(Number(data.duration)) ? Number(data.duration) : 0,
      generated_by: data.generated_by ?? "",
    },
    warnings,
  };
}

function hasWordCues() {
  return state.cues.some(cue => cue.words.length > 0);
}

/* ---------- validation ---------- */
function validateCues() {
  const errors = [];
  const warnings = [];
  let previousEnd = -Infinity;
  state.cues.forEach((cue, index) => {
    const label = `Line ${index + 1}`;
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end)) {
      errors.push(`${label} has an invalid time.`);
      return;
    }
    if (cue.start < -0.001) errors.push(`${label} starts before the song begins.`);
    if (cue.end <= cue.start + 0.001) errors.push(`${label} must end after it starts.`);
    if (cue.start < previousEnd - 0.001) {
      errors.push(`${label} starts before the previous line ends.`);
    }
    previousEnd = Math.max(previousEnd, cue.end);

    if (cue.instrumental) {
      if (normalizeText(cue.text).toLowerCase() !== "[instrumental]") {
        errors.push(`${label} is marked as an instrumental break but has lyric text.`);
      }
      if (cue.words.length) errors.push(`${label} is an instrumental break but has word timing.`);
    } else {
      if (!normalizeText(cue.text)) {
        errors.push(`${label} is empty. Add words or remove the line.`);
      }
      if (cue.words.length) {
        if (cue.needsReconcile) {
          warnings.push(`${label} has text waiting to be reconciled.`);
        } else if (normalizeText(cue.text)) {
          const joined = normalizeText(cue.words.map(word => word.text).join(" "));
          if (joined !== normalizeText(cue.text)) {
            errors.push(`${label}'s text does not match its word timing.`);
          }
        }
        let previousWordEnd = null;
        cue.words.forEach((word, wordIndex) => {
          const wordLabel = `${label}, word ${wordIndex + 1}`;
          if (!Number.isFinite(word.start) || !Number.isFinite(word.end)) {
            errors.push(`${wordLabel} has an invalid time.`);
            return;
          }
          if (word.end <= word.start + 0.0005) {
            errors.push(`${wordLabel} must end after it starts.`);
          }
          if (word.start < cue.start - 0.001) errors.push(`${wordLabel} starts before the line.`);
          if (word.end > cue.end + 0.001) errors.push(`${wordLabel} ends after the line.`);
          if (previousWordEnd !== null && word.start < previousWordEnd - 0.001) {
            errors.push(`${wordLabel} overlaps the previous word.`);
          }
          if (!normalizeText(word.text)) errors.push(`${wordLabel} is empty.`);
          previousWordEnd = Math.max(previousWordEnd ?? -Infinity, word.end);
        });
      }
    }
    if (state.duration > 0 && cue.start > state.duration + 0.5) {
      warnings.push(`${label} starts after the audio ends.`);
    }
    if (state.duration > 0 && cue.end > state.duration + 0.5) {
      warnings.push(`${label} ends after the audio ends.`);
    }
  });
  return { errors, warnings };
}

function rowHasError(index) {
  const prefix = `Line ${index + 1}`;
  return state.validationErrors.some(message => message.startsWith(prefix));
}

/* ---------- status and header ---------- */
function setStatus(message, type = "muted") {
  const className = type === "muted" ? "" : type;
  saveStatus.textContent = message || "";
  saveStatus.className = `save-status ${className}`.trim();
  if (emptyStatus) {
    emptyStatus.textContent = message || "";
    emptyStatus.className = `save-status ${className}`.trim();
  }
}

function statusInfo() {
  if (!state.song) return { text: "No song open", className: "" };
  if (state.saving) return { text: "Saving…", className: "is-draft" };
  if (state.dirty) {
    if (state.source === "draft") return { text: "Draft · unsaved changes", className: "is-warn" };
    if (state.source === "active") return { text: "Published · newer edits", className: "is-warn" };
    if (state.source === "import") return { text: "Imported · not published", className: "is-warn" };
    return { text: "Unsaved changes", className: "is-warn" };
  }
  if (state.source === "draft") return { text: "Draft · saved", className: "is-draft" };
  if (state.source === "active") return { text: "Published · live", className: "is-live" };
  if (state.source === "import") return { text: "Imported · not published", className: "is-warn" };
  return { text: "No lyrics yet", className: "" };
}

function updateHeader() {
  const info = statusInfo();
  statusChip.textContent = info.text;
  statusChip.className = `status-chip ${info.className}`.trim();
  if (state.song) {
    const prefix = state.song.id ? `${state.song.id} — ` : "";
    const artist = state.song.artist ? ` — ${state.song.artist}` : "";
    songContext.textContent = `${prefix}${state.song.title}${artist}`;
  } else {
    songContext.textContent = "Choose a song to begin";
  }

  if (!state.song) {
    draftNote.textContent = "Choose a song to start editing.";
  } else if (state.source === "draft") {
    draftNote.textContent = "Draft is local. The player is still using the published lyrics.";
  } else if (state.source === "active" && state.dirty) {
    draftNote.textContent = "These edits are not published yet.";
  } else if (state.source === "active") {
    draftNote.textContent = "The player is using this version.";
  } else if (state.source === "import") {
    draftNote.textContent = "Imported lyrics are not live until you publish.";
  } else {
    draftNote.textContent = "Draft is not live. The player keeps the published lyrics.";
  }

  updateButtons();
}

function updateButtons() {
  const unavailable = state.saving || !state.song || !state.cues.length
    || state.validationErrors.length > 0;
  saveBtn.disabled = unavailable;
  publishBtn.disabled = unavailable;
  saveBtn.textContent = state.saving ? "Saving…" : "Save draft";
  publishBtn.textContent = state.saving ? "Working…" : "Publish";
  saveBtn.title = state.validationErrors.length
    ? "Fix the highlighted lines before saving."
    : "Save a local draft. The player keeps the published lyrics.";
  publishBtn.title = state.validationErrors.length
    ? "Fix the highlighted lines before publishing."
    : "Publish these lyrics. The player switches immediately.";
  undoBtn.disabled = !state.history.length;
  redoBtn.disabled = !state.redo.length;
  exactTimesToggle.checked = state.showExactTimes;

  const canExport = state.cues.length > 0;
  $("downloadSrtBtn").disabled = !canExport;
  $("downloadWordsBtn").disabled = !canExport || !hasWordCues();
  $("addInstrumentalBtn").disabled = !state.song;
}

function updateValidationView() {
  const { errors, warnings } = validateCues();
  state.validationErrors = errors;
  if (!errors.length) {
    validationEl.hidden = true;
    validationEl.replaceChildren();
  } else {
    validationEl.hidden = false;
    validationEl.replaceChildren();
    errors.slice(0, 5).forEach(message => {
      const line = document.createElement("div");
      line.textContent = message;
      validationEl.appendChild(line);
    });
    if (errors.length > 5) {
      const more = document.createElement("div");
      more.textContent = `…and ${errors.length - 5} more.`;
      validationEl.appendChild(more);
    }
  }
  state.rowEls.forEach((row, index) => {
    row.row.classList.toggle("has-error", rowHasError(index));
  });
  if (errors.length && state.mode !== "lyrics") {
    setStatus(errors[0], "error");
  }
  updateButtons();
  return { errors, warnings };
}

/* ---------- history / autosave ---------- */
function historySnapshot() {
  return JSON.stringify({
    cues: state.cues.map(cue => ({ ...cue, needsReconcile: false })),
    wordsMeta: state.wordsMeta,
  });
}
function pushHistorySnapshot(snapshot) {
  state.history.push(snapshot);
  if (state.history.length > 50) state.history.shift();
  state.redo = [];
  updateButtons();
}
function pushHistory() {
  pushHistorySnapshot(historySnapshot());
}
function restoreHistorySnapshot(snapshotText) {
  const snapshot = JSON.parse(snapshotText);
  state.cues = (snapshot.cues || []).map(cloneCue);
  state.wordsMeta = snapshot.wordsMeta || null;
  state.selectedWordIndex = -1;
  state.selectedIndex = Math.min(state.selectedIndex, state.cues.length - 1);
  state.textEdit = null;
  state.lastReconcileNotice = null;
  state.dirty = true;
}
function undo() {
  if (!state.history.length) return;
  state.redo.push(historySnapshot());
  restoreHistorySnapshot(state.history.pop());
  renderAll();
  setStatus("Undid the last change.", "muted");
  scheduleAutosave();
}
function redo() {
  if (!state.redo.length) return;
  state.history.push(historySnapshot());
  restoreHistorySnapshot(state.redo.pop());
  renderAll();
  setStatus("Redid the last change.", "muted");
  scheduleAutosave();
}

function cancelAutosave() {
  clearTimeout(autosaveTimer);
  autosaveTimer = null;
}
function scheduleAutosave() {
  cancelAutosave();
  if (!state.dirty || state.saving || !state.song || !state.revisionSong
      || !state.cues.length) return;
  autosaveTimer = setTimeout(() => {
    autosaveTimer = null;
    if (state.dirty && !state.saving) saveDraft({ autosave: true });
  }, AUTOSAVE_MS);
}
function markDirty() {
  state.dirty = true;
  updateHeader();
  scheduleAutosave();
}
function isEditingText() {
  return document.activeElement instanceof Element
    && document.activeElement.matches(".lyric-text, .word-text-input");
}

async function confirmDiscard() {
  if (!state.dirty) return true;
  return karaokeConfirm({
    title: "Discard unsaved changes?",
    message: "Your edits since the last save will be lost.",
    confirmLabel: "Discard changes",
    tone: "danger",
    focus: "cancel",
  });
}

/* ---------- text editing and reconciliation ---------- */
function ensureTextEdit(index) {
  const cue = state.cues[index];
  if (!cue) return null;
  if (state.textEdit && state.textEdit.index === index) return state.textEdit;
  const originalText = cue.words.length
    ? normalizeText(cue.words.map(word => word.text).join(" "))
    : cue.text;
  state.textEdit = {
    index,
    originalText,
    originalWords: cue.words.map(word => ({ ...word })),
    originalReview: cue.review || null,
    originalDirty: state.dirty,
    snapshot: historySnapshot(),
    pushed: false,
  };
  return state.textEdit;
}

function onTextInput(index, value, element = null) {
  const cue = state.cues[index];
  if (!cue) return;
  ensureTextEdit(index);
  cue.text = value;
  cue.needsReconcile = true;
  markDirty();
  renderPreviewOnlyText();
  autoGrowTextarea(element);
}

function pushTextHistory(edit) {
  if (!edit || edit.pushed) return;
  pushHistorySnapshot(edit.snapshot);
  edit.pushed = true;
}

function restoreTextEdit(edit) {
  const cue = state.cues[edit.index];
  if (!cue) return;
  cue.text = edit.originalText;
  cue.words = edit.originalWords.map(word => ({ ...word }));
  cue.review = edit.originalReview;
  cue.needsReconcile = false;
  state.dirty = edit.originalDirty;
  state.textEdit = null;
  updateHeader();
}

async function askReconcileChoice(message) {
  if (!reconcileDialog || typeof reconcileDialog.showModal !== "function") return null;
  if (reconcileDialog.open) return null;
  reconcileMessage.textContent = message
    || "Choose how to handle this line. Other lines are not affected.";
  reconcileDialog.querySelector('input[name="reconcileChoice"][value="spread"]').checked = true;
  return new Promise(resolve => {
    reconcileResolve = resolve;
    try {
      reconcileDialog.showModal();
    } catch {
      reconcileResolve = null;
      resolve(null);
    }
  });
}
function settleReconcile(choice) {
  if (reconcileDialog && reconcileDialog.open) reconcileDialog.close();
  const resolve = reconcileResolve;
  reconcileResolve = null;
  if (resolve) resolve(choice);
}

async function commitCueText(index, { interactive = true } = {}) {
  const cue = state.cues[index];
  if (!cue || !cue.needsReconcile) return true;
  const edit = (state.textEdit && state.textEdit.index === index)
    ? state.textEdit
    : {
      index,
      originalText: cue.text,
      originalWords: cue.words.map(word => ({ ...word })),
      originalReview: cue.review || null,
      originalDirty: state.dirty,
      snapshot: historySnapshot(),
      pushed: false,
    };
  const normalized = normalizeText(cue.text);

  if (normalized === normalizeText(edit.originalText)) {
    cue.text = edit.originalText;
    cue.words = edit.originalWords.map(word => ({ ...word }));
    cue.review = edit.originalReview;
    cue.needsReconcile = false;
    state.dirty = edit.originalDirty;
    state.textEdit = null;
    updateHeader();
    scheduleRenderAll();
    return true;
  }

  if (!cue.words.length && !edit.originalWords.length) {
    pushTextHistory(edit);
    cue.text = normalized;
    cue.needsReconcile = false;
    state.textEdit = null;
    markDirty();
    scheduleRenderAll();
    return true;
  }

  const result = LyricReconcile.reconcileLine({
    ...cue,
    text: edit.originalText,
    words: edit.originalWords,
    review: edit.originalReview,
  }, normalized);

  if (result.status === "empty") {
    pushTextHistory(edit);
    cue.text = "";
    cue.words = edit.originalWords.map(word => ({ ...word }));
    cue.review = edit.originalReview;
    cue.needsReconcile = false;
    state.textEdit = null;
    markDirty();
    scheduleRenderAll();
    setStatus("This line is empty. Add words or remove the line.", "warn");
    return true;
  }

  if (result.status === "ok") {
    pushTextHistory(edit);
    cue.text = result.text;
    cue.words = result.words.map(word => ({ ...word }));
    cue.review = result.review || null;
    cue.needsReconcile = false;
    state.textEdit = null;
    state.lastReconcileNotice = cue.review
      ? { index, message: result.message }
      : null;
    markDirty();
    scheduleRenderAll();
    setStatus(result.message, cue.review ? "warn" : "ok");
    return true;
  }

  if (!interactive) {
    setStatus("Finish this text change before saving. The new words do not fit yet.", "warn");
    return false;
  }

  const choice = await askReconcileChoice(result.message);
  if (choice === "spread") {
    const spread = LyricReconcile.createWordTimings({ ...cue, text: normalized });
    if (spread.status !== "ok") {
      setStatus(spread.message || "The new words could not be fitted into this line.", "error");
      return false;
    }
    pushTextHistory(edit);
    cue.text = normalized;
    cue.words = spread.words.map(word => ({ ...word }));
    cue.review = "words-redistributed";
    cue.needsReconcile = false;
    state.textEdit = null;
    state.lastReconcileNotice = {
      index,
      message: "The timing was spread across this line. Check the result.",
    };
    markDirty();
    scheduleRenderAll();
    setStatus("Timing spread across this line. Check the timing.", "warn");
    return true;
  }
  if (choice === "line") {
    pushTextHistory(edit);
    cue.text = normalized;
    cue.words = [];
    cue.review = null;
    cue.needsReconcile = false;
    state.textEdit = null;
    state.lastReconcileNotice = {
      index,
      message: "Word-by-word highlighting was turned off for this line only.",
    };
    markDirty();
    scheduleRenderAll();
    setStatus("This line now uses whole-line highlighting.", "warn");
    return true;
  }

  restoreTextEdit(edit);
  scheduleRenderAll();
  setStatus("Text change canceled.", "muted");
  return false;
}

async function commitAllText({ interactive = true } = {}) {
  const pending = state.cues
    .map((cue, index) => cue.needsReconcile ? index : -1)
    .filter(index => index >= 0);
  for (const index of pending) {
    const ok = await commitCueText(index, { interactive });
    if (!ok) return false;
  }
  return true;
}

function scheduleRenderAll() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(() => {
    renderTimer = null;
    renderAll();
  }, 0);
}

function autoGrowTextarea(element) {
  if (!(element instanceof HTMLTextAreaElement)) return;
  element.style.height = "auto";
  element.style.height = `${Math.min(element.scrollHeight, 260)}px`;
}

/* ---------- selection / mode ---------- */
function selectCue(index, { render = false, clearNotice = true } = {}) {
  state.selectedIndex = (index >= 0 && index < state.cues.length) ? index : -1;
  state.selectedWordIndex = -1;
  if (clearNotice) state.lastReconcileNotice = null;
  if (state.loopActive) {
    const cue = state.cues[state.selectedIndex];
    if (cue) {
      state.loopStart = cue.start;
      state.loopEnd = cue.end;
    } else {
      state.loopActive = false;
      updateLoopButton();
    }
  }
  if (render) renderAll();
  else updateSelectionClasses();
}

function updateSelectionClasses() {
  state.rowEls.forEach((row, index) => {
    row.row.classList.toggle("selected", index === state.selectedIndex);
    row.row.classList.toggle("active", index === state.activeCueIndex);
  });
  state.previewEls.forEach((entry, index) => {
    entry.line.classList.toggle("selected", index === state.selectedIndex);
    entry.line.classList.toggle("active", index === state.activeCueIndex);
  });
  Array.from(cueOverlay.children).forEach((marker, index) => {
    marker.classList.toggle("selected", index === state.selectedIndex);
    marker.classList.toggle("active", index === state.activeCueIndex);
  });
  updateExactInputs();
}

function ensureLyricSelection() {
  if (state.selectedIndex >= 0 && state.selectedIndex < state.cues.length) return;
  const first = state.cues.findIndex(cue => !cue.instrumental);
  state.selectedIndex = first >= 0 ? first : (state.cues.length ? 0 : -1);
  state.selectedWordIndex = -1;
}

async function setMode(mode) {
  if (!["lyrics", "timing", "words"].includes(mode)) return;
  if (!(await commitAllText({ interactive: true }))) return;
  state.mode = mode;
  if (mode === "words") ensureLyricSelection();
  renderAll();
  if (mode !== "lyrics") refreshWaveform();
  if (mode === "words") focusWordPanel();
}

function focusWordPanel() {
  const textarea = wordFocusText.querySelector("textarea");
  if (textarea) {
    textarea.focus({ preventScroll: true });
  }
}

/* ---------- rendering ---------- */
function renderAll() {
  if (!state.song) {
    emptyState.classList.remove("d-none");
    workspace.classList.add("d-none");
  } else {
    emptyState.classList.add("d-none");
    workspace.classList.remove("d-none");
  }
  renderMode();
  renderLyrics();
  renderWordPanel();
  renderPreview();
  renderMarkers();
  updateValidationView();
  updateCounts();
  updateHeader();
  updateHighlight(currentTime());
  updateExactInputs();
  updateLoopButton();
}

function renderMode() {
  const wordsMode = state.mode === "words";
  const timingMode = state.mode === "timing" || wordsMode;
  lyricsPanel.classList.toggle("d-none", wordsMode);
  wordPanel.classList.toggle("d-none", !wordsMode);
  timingPanel.classList.toggle("d-none", !timingMode);
  workspace.classList.toggle("timing-active", timingMode);
  document.querySelectorAll(".mode-tab").forEach(button => {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  let hint = wordsMode
    ? "Advanced: adjust individual word timing for one line. Other lines are not affected."
    : (state.mode === "timing"
      ? "Hear a line, then move it earlier or later. Exact times are under More."
      : "Correct the words. Timing is kept safe automatically.");
  if (state.mode === "timing" && state.ripple) {
    hint += " Later lines move with the selected line.";
  }
  modeHint.textContent = hint;
  exactTimesPanel.classList.toggle("d-none", !state.showExactTimes || !timingMode);
}

function makeButton(label, handler, className = "line-action") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", evt => {
    evt.stopPropagation();
    handler();
  });
  return button;
}

function makeLineDetails(cue, index) {
  const details = document.createElement("details");
  details.className = "more-menu";
  const summary = document.createElement("summary");
  summary.textContent = "Line actions";
  details.appendChild(summary);
  const panel = document.createElement("div");
  panel.className = "more-panel line-options-panel";

  if (cue.review) {
    const looksRight = document.createElement("button");
    looksRight.type = "button";
    looksRight.className = "menu-button";
    looksRight.textContent = "Timing looks right";
    looksRight.addEventListener("click", evt => {
      evt.stopPropagation();
      markTimingReviewed(index);
      details.removeAttribute("open");
    });
    panel.appendChild(looksRight);
  }

  if (state.mode === "timing" || state.mode === "words") {
    const exact = document.createElement("button");
    exact.type = "button";
    exact.className = "menu-button";
    exact.textContent = "Show exact times";
    exact.addEventListener("click", evt => {
      evt.stopPropagation();
      state.showExactTimes = true;
      details.removeAttribute("open");
      renderAll();
    });
    panel.appendChild(exact);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "menu-button danger";
  remove.textContent = cue.instrumental ? "Remove instrumental break…" : "Remove line…";
  remove.addEventListener("click", evt => {
    evt.stopPropagation();
    details.removeAttribute("open");
    removeCue(index);
  });
  panel.appendChild(remove);
  details.appendChild(panel);
  return details;
}

function renderLineActions(cue, index) {
  const actions = document.createElement("div");
  actions.className = "line-actions";

  if (state.mode === "timing") {
    actions.appendChild(makeButton("Play line", () => playCue(index), "line-action primary"));
    actions.appendChild(makeButton("Start here", () => setStartAtPlayhead(index)));
    actions.appendChild(makeButton("End here", () => setEndAtPlayhead(index)));
    actions.appendChild(makeButton("Earlier 0.1s", () => nudgeCue(index, -0.1)));
    actions.appendChild(makeButton("Later 0.1s", () => nudgeCue(index, 0.1)));
    const details = document.createElement("details");
    details.className = "more-menu";
    const summary = document.createElement("summary");
    summary.textContent = "Line actions";
    details.appendChild(summary);
    const panel = document.createElement("div");
    panel.className = "more-panel line-options-panel";
    [
      ["Earlier 0.5s", () => nudgeCue(index, -0.5)],
      ["Later 0.5s", () => nudgeCue(index, 0.5)],
      ["Loop this line", () => loopCue(index)],
    ].forEach(([label, handler]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "menu-button";
      button.textContent = label;
      button.addEventListener("click", evt => {
        evt.stopPropagation();
        details.removeAttribute("open");
        handler();
      });
      panel.appendChild(button);
    });
    if (cue.review) {
      const reviewed = document.createElement("button");
      reviewed.type = "button";
      reviewed.className = "menu-button";
      reviewed.textContent = "Timing looks right";
      reviewed.addEventListener("click", evt => {
        evt.stopPropagation();
        details.removeAttribute("open");
        markTimingReviewed(index);
      });
      panel.appendChild(reviewed);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "menu-button danger";
    remove.textContent = cue.instrumental ? "Remove instrumental break…" : "Remove line…";
    remove.addEventListener("click", evt => {
      evt.stopPropagation();
      details.removeAttribute("open");
      removeCue(index);
    });
    panel.appendChild(remove);
    details.appendChild(panel);
    actions.appendChild(details);
    return actions;
  }

  actions.appendChild(makeButton("Add line below", () => addCue(false, index), "line-action primary"));
  actions.appendChild(makeButton("Split line", () => splitSelected(index)));
  if (index < state.cues.length - 1) {
    actions.appendChild(makeButton("Merge with next", () => mergeSelected(index)));
  }
  actions.appendChild(makeButton("Fix timing", () => setMode("timing")));
  actions.appendChild(makeLineDetails(cue, index));
  return actions;
}

function renderNotice(index) {
  const notice = state.lastReconcileNotice;
  if (!notice || notice.index !== index) return null;
  const box = document.createElement("div");
  box.className = "reconcile-notice";
  const text = document.createElement("span");
  text.textContent = notice.message;
  box.appendChild(text);
  box.appendChild(makeButton("Review timing", () => {
    state.selectedIndex = index;
    setMode("timing");
  }));
  box.appendChild(makeButton("Undo", undo));
  return box;
}

function renderLyrics() {
  cuesEl.replaceChildren();
  state.rowEls = [];
  state.cues.forEach((cue, index) => {
    const row = document.createElement("div");
    row.className = "lyric-row";
    if (index === state.selectedIndex) row.classList.add("selected");
    if (index === state.activeCueIndex) row.classList.add("active");
    if (rowHasError(index)) row.classList.add("has-error");
    row.dataset.index = String(index);

    const number = document.createElement("div");
    number.className = "lyric-number";
    number.textContent = String(index + 1).padStart(2, "0");

    const main = document.createElement("div");
    main.className = "lyric-main";

    let textInput = null;
    if (cue.instrumental) {
      const label = document.createElement("div");
      label.className = "instrumental-row";
      const badge = document.createElement("span");
      badge.className = "instrumental-badge";
      badge.textContent = "Instrumental break";
      label.appendChild(badge);
      const note = document.createElement("span");
      note.textContent = "No lyrics during this section.";
      label.appendChild(note);
      main.appendChild(label);
    } else {
      const line = document.createElement("div");
      line.className = "lyric-text-line";
      textInput = document.createElement("textarea");
      textInput.className = "lyric-text";
      textInput.rows = 1;
      textInput.value = cue.text;
      textInput.setAttribute("aria-label", `Lyric line ${index + 1}`);
      textInput.addEventListener("focus", () => {
        state.lastFocusedCue = index;
        selectCue(index, { render: false, clearNotice: false });
        ensureTextEdit(index);
      });
      textInput.addEventListener("input", evt => onTextInput(index, evt.target.value, textInput));
      textInput.addEventListener("blur", () => {
        commitCueText(index, { interactive: true });
      });
      textInput.addEventListener("keyup", () => {
        state.lastCaret = { index, offset: textInput.selectionStart };
      });
      textInput.addEventListener("click", () => {
        state.lastCaret = { index, offset: textInput.selectionStart };
      });
      line.appendChild(textInput);
      if (cue.review) line.appendChild(makeReviewChip(cue, index));
      main.appendChild(line);
    }

    if (index === state.selectedIndex) {
      main.appendChild(renderLineActions(cue, index));
      const notice = renderNotice(index);
      if (notice) main.appendChild(notice);
    }

    row.append(number, main);
    row.addEventListener("focusin", () => {
      if (state.selectedIndex !== index) selectCue(index, { render: false });
    });
    row.addEventListener("click", evt => {
      if (evt.target.closest("button, input, textarea, summary, a")) return;
      selectCue(index, { render: true });
      const currentInput = state.rowEls[index]?.textInput;
      if (currentInput) currentInput.focus();
    });

    cuesEl.appendChild(row);
    state.rowEls.push({ row, textInput, index });
    autoGrowTextarea(textInput);
  });
}

function makeReviewChip(cue, index) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "review-chip";
  chip.textContent = "Check timing";
  chip.title = "Review this line's timing";
  chip.addEventListener("click", evt => {
    evt.stopPropagation();
    state.selectedIndex = index;
    setMode("timing");
  });
  return chip;
}

function renderWordPanel() {
  if (state.mode !== "words") return;
  ensureLyricSelection();
  const cue = state.cues[state.selectedIndex];
  if (!cue) {
    wordLineLabel.textContent = "No line selected";
    wordFocusText.replaceChildren();
    wordChips.replaceChildren();
    wordInspector.textContent = "Select a lyric line first.";
    prevLineBtn.disabled = true;
    nextLineBtn.disabled = true;
    return;
  }

  wordLineLabel.textContent = `Line ${state.selectedIndex + 1} of ${state.cues.length}`
    + (cue.instrumental ? " · Instrumental break" : "");
  prevLineBtn.disabled = state.selectedIndex <= 0;
  nextLineBtn.disabled = state.selectedIndex >= state.cues.length - 1;

  wordFocusText.replaceChildren();
  if (!cue.instrumental) {
    const textarea = document.createElement("textarea");
    textarea.className = "word-text-input";
    textarea.rows = 2;
    textarea.value = cue.text;
    textarea.setAttribute("aria-label", "Lyric text for this line");
    textarea.addEventListener("focus", () => {
      state.lastFocusedCue = state.selectedIndex;
      ensureTextEdit(state.selectedIndex);
    });
    textarea.addEventListener("input", evt => {
      onTextInput(state.selectedIndex, evt.target.value, textarea);
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
    });
    textarea.addEventListener("blur", () => {
      commitCueText(state.selectedIndex, { interactive: true });
    });
    wordFocusText.appendChild(textarea);
  }

  wordChips.replaceChildren();
  state.wordChipEls = [];
  if (cue.instrumental) {
    const note = document.createElement("p");
    note.className = "muted-note";
    note.textContent = "This row is an instrumental break, so there are no words to tune.";
    wordChips.appendChild(note);
  } else if (!cue.words.length) {
    const box = document.createElement("div");
    box.className = "word-empty";
    const note = document.createElement("p");
    note.className = "muted-note";
    note.textContent = "This line uses whole-line highlighting.";
    const add = document.createElement("button");
    add.type = "button";
    add.className = "btn btn-sm btn-outline-light";
    add.textContent = "Add word-by-word timing";
    add.addEventListener("click", createWordTiming);
    box.append(note, add);
    wordChips.appendChild(box);
  } else {
    if (state.selectedWordIndex >= cue.words.length) state.selectedWordIndex = -1;
    cue.words.forEach((word, wordIndex) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "word-chip";
      if (wordIndex === state.selectedWordIndex) chip.classList.add("selected");
      chip.textContent = word.text || "?";
      chip.addEventListener("click", () => selectWord(state.selectedIndex, wordIndex));
      wordChips.appendChild(chip);
      state.wordChipEls.push(chip);
    });
  }

  createWordTimingBtn.disabled = cue.instrumental || cue.words.length > 0;
  stopWordTimingBtn.disabled = !cue.words.length;
  renderWordInspector();
}

function renderWordInspector() {
  wordInspector.replaceChildren();
  const cue = state.cues[state.selectedIndex];
  if (!cue) {
    wordInspector.textContent = "Select a lyric line first.";
    return;
  }
  if (cue.instrumental || !cue.words.length) {
    const note = document.createElement("span");
    note.className = "muted-note";
    note.textContent = cue.instrumental
      ? "Instrumental breaks have no word timing."
      : "Select “Add word-by-word timing” if you need to tune individual words.";
    wordInspector.appendChild(note);
    return;
  }
  const word = cue.words[state.selectedWordIndex];
  if (!word) {
    const note = document.createElement("span");
    note.className = "muted-note";
    note.textContent = "Select a word to adjust its timing.";
    wordInspector.appendChild(note);
    return;
  }

  const title = document.createElement("div");
  title.className = "word-inspector-title";
  const name = document.createElement("strong");
  name.textContent = `Word “${word.text}”`;
  const times = document.createElement("span");
  times.className = "word-inspector-times";
  times.textContent = `${formatClock(word.start)} → ${formatClock(word.end)}`;
  title.append(name, times);

  const actions = document.createElement("div");
  actions.className = "word-inspector-actions";
  [
    ["−0.1s", () => nudgeSelectedWord(-0.1)],
    ["−0.05s", () => nudgeSelectedWord(-0.05)],
    ["+0.05s", () => nudgeSelectedWord(0.05)],
    ["+0.1s", () => nudgeSelectedWord(0.1)],
    ["Start here", () => setSelectedWordBoundary("start")],
    ["End here", () => setSelectedWordBoundary("end")],
    ["Loop word", () => loopSelectedWord()],
  ].forEach(([label, handler]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm btn-outline-secondary";
    button.textContent = label;
    button.addEventListener("click", handler);
    actions.appendChild(button);
  });

  const validation = document.createElement("p");
  validation.className = "muted-note";
  const valid = LyricReconcile.validateWords(cue.words, cue, cue.text);
  validation.textContent = valid
    ? "Timing is valid."
    : "This word timing is not valid yet. Adjust the start or end.";
  if (!valid) validation.style.color = "var(--error)";

  wordInspector.append(title, actions, validation);
}

function renderPreview() {
  subtitlePreview.replaceChildren();
  state.previewEls = [];
  if (!state.cues.length) {
    const placeholder = document.createElement("div");
    placeholder.className = "muted-note";
    placeholder.textContent = "No lyrics loaded.";
    subtitlePreview.appendChild(placeholder);
    return;
  }
  state.cues.forEach((cue, index) => {
    const line = document.createElement("div");
    line.className = "preview-line";
    if (cue.instrumental) line.classList.add("instrumental");
    if (index === state.selectedIndex) line.classList.add("selected");
    if (index === state.activeCueIndex) line.classList.add("active");
    line.addEventListener("click", () => {
      selectCue(index, { render: true });
      if (state.audioReady) seekTo(cue.start, false);
    });
    if (cue.instrumental || !cue.words.length) {
      line.textContent = cue.instrumental ? "[Instrumental]" : cue.text;
    } else {
      cue.words.forEach(word => {
        const span = document.createElement("span");
        span.className = "preview-word";
        span.textContent = word.text;
        span.addEventListener("click", evt => {
          evt.stopPropagation();
          selectCue(index, { render: true });
          if (state.audioReady) seekTo(word.start, false);
        });
        line.appendChild(span);
        line.appendChild(document.createTextNode(" "));
      });
    }
    subtitlePreview.appendChild(line);
    state.previewEls.push({ line, cue, wordEls: Array.from(line.querySelectorAll(".preview-word")) });
  });
  updateHighlight(currentTime());
}

function renderPreviewOnlyText() {
  state.cues.forEach((cue, index) => {
    const entry = state.previewEls[index];
    if (!entry || cue.words.length) return;
    entry.line.textContent = cue.instrumental ? "[Instrumental]" : cue.text;
  });
}

function renderMarkers() {
  cueOverlay.replaceChildren();
  if (!state.duration || !state.cues.length) return;
  state.cues.forEach((cue, index) => {
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = "cue-marker";
    if (cue.instrumental) marker.classList.add("instrumental");
    marker.title = `Line ${index + 1}`;
    const left = Math.max(0, Math.min(100, (cue.start / state.duration) * 100));
    const right = Math.max(0, Math.min(100, (cue.end / state.duration) * 100));
    marker.style.left = `${left}%`;
    marker.style.width = `${Math.max(right - left, 0.25)}%`;
    marker.addEventListener("click", evt => {
      evt.stopPropagation();
      selectCue(index, { render: true });
      if (state.audioReady) seekTo(cue.start, false);
    });
    cueOverlay.appendChild(marker);
  });
  updateSelectionClasses();
}

function updateHighlight(now) {
  let active = -1;
  for (let index = 0; index < state.cues.length; index += 1) {
    const cue = state.cues[index];
    if (now >= cue.start && now < cue.end) {
      active = index;
      break;
    }
  }
  if (active !== state.activeCueIndex) {
    state.activeCueIndex = active;
    state.activeWordIndex = -1;
    state.rowEls.forEach((row, index) => row.row.classList.toggle("active", index === active));
    state.previewEls.forEach((entry, index) => {
      entry.line.classList.toggle("active", index === active);
      entry.wordEls.forEach(element => element.classList.remove("active", "sung"));
    });
    Array.from(cueOverlay.children).forEach((marker, index) => {
      marker.classList.toggle("active", index === active);
    });
    if (active >= 0 && state.follow && state.mode !== "lyrics" && !isEditingText()) {
      const row = state.rowEls[active]?.row;
      if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  state.wordChipEls.forEach(chip => chip.classList.remove("active", "sung"));
  if (active < 0 || state.mode !== "words" || active !== state.selectedIndex) return;
  const cue = state.cues[active];
  if (!cue || !cue.words.length) return;
  const entry = state.previewEls[active];
  let wordIndex = -1;
  for (let index = 0; index < cue.words.length; index += 1) {
    if (now >= cue.words[index].start) wordIndex = index;
    if (now < cue.words[index].end) break;
  }
  state.activeWordIndex = wordIndex;
  state.wordChipEls.forEach((chip, index) => {
    chip.classList.toggle("active", index === wordIndex);
    chip.classList.toggle("sung", index < wordIndex);
  });
  if (entry) {
    entry.wordEls.forEach((element, index) => {
      element.classList.toggle("active", index === wordIndex);
      element.classList.toggle("sung", index < wordIndex);
    });
  }
}

function updateCounts() {
  const lyricCount = state.cues.filter(cue => !cue.instrumental).length;
  const instrumentalCount = state.cues.length - lyricCount;
  const parts = [`${lyricCount} ${lyricCount === 1 ? "line" : "lines"}`];
  if (instrumentalCount) {
    parts.push(`${instrumentalCount} instrumental ${instrumentalCount === 1 ? "break" : "breaks"}`);
  }
  cueCount.textContent = parts.join(" · ");
}

function updateExactInputs() {
  if (!exactTimesPanel) return;
  exactTimesPanel.classList.toggle(
    "d-none", !state.showExactTimes || state.mode === "lyrics");
  const cue = state.cues[state.selectedIndex];
  if (!cue) return;
  if (document.activeElement !== exactStartInput) exactStartInput.value = formatTime(cue.start);
  if (document.activeElement !== exactEndInput) exactEndInput.value = formatTime(cue.end);
}

/* ---------- waveform and transport ---------- */
function destroyWaveSurfer() {
  cancelAutosave();
  if (wavesurfer) {
    try { wavesurfer.destroy(); } catch { /* ignore */ }
    wavesurfer = null;
  }
  state.audioReady = false;
  state.loopActive = false;
  state.loopStart = 0;
  state.loopEnd = 0;
  updateLoopButton();
  if (state.localAudioUrl) {
    URL.revokeObjectURL(state.localAudioUrl);
    state.localAudioUrl = null;
  }
  state.duration = 0;
  cueOverlay.replaceChildren();
  updateTimes(0);
  syncTransportButtons();
}

function refreshWaveform() {
  if (!wavesurfer || !state.audioReady) return;
  try { wavesurfer.zoom(state.zoomPx || 0); } catch { /* ignore */ }
}

function loadAudio(url, label) {
  if (typeof WaveSurfer === "undefined") {
    setStatus("Audio could not load. Check the local WaveSurfer asset.", "error");
    return;
  }
  const token = ++state.audioToken;
  destroyWaveSurfer();
  audioStatus.textContent = `Loading ${label}…`;
  audioStatus.className = "audio-status";
  wavesurfer = WaveSurfer.create({
    container: "#waveform",
    height: 148,
    waveColor: "#3f3f3f",
    progressColor: "#ff0033",
    cursorColor: "#ffffff",
    barWidth: 2,
    barGap: 1,
    normalize: true,
  });
  wavesurfer.on("ready", () => {
    if (token !== state.audioToken) return;
    state.audioReady = true;
    state.duration = wavesurfer.getDuration();
    if (rateSelect) {
      try { wavesurfer.setPlaybackRate(Number(rateSelect.value) || 1); } catch { /* ignore */ }
    }
    audioStatus.textContent = `Loaded ${label}`;
    audioStatus.className = "audio-status";
    updateTimes(0);
    renderMarkers();
    updateHighlight(currentTime());
    updateValidationView();
    updateCounts();
    updateLoopButton();
    refreshWaveform();
    syncTransportButtons();
  });
  wavesurfer.on("timeupdate", time => {
    if (token !== state.audioToken) return;
    if (state.loopActive && state.loopEnd > state.loopStart
        && time >= state.loopEnd - 0.03) {
      try { wavesurfer.setTime(state.loopStart); } catch { /* ignore */ }
      const promise = wavesurfer.play();
      if (promise && typeof promise.catch === "function") promise.catch(() => {});
      return;
    }
    updateTimes(time);
    updateHighlight(time);
  });
  wavesurfer.on("seeking", time => {
    if (token !== state.audioToken) return;
    updateTimes(time);
    updateHighlight(time);
  });
  wavesurfer.on("play", () => syncTransportButtons("Pause"));
  wavesurfer.on("pause", () => syncTransportButtons("Play"));
  wavesurfer.on("finish", () => {
    syncTransportButtons("Play");
    if (state.loopActive && state.loopEnd > state.loopStart) {
      try { wavesurfer.setTime(state.loopStart); } catch { /* ignore */ }
      const promise = wavesurfer.play();
      if (promise && typeof promise.catch === "function") promise.catch(() => {});
    }
  });
  wavesurfer.load(url).catch(err => {
    if (token !== state.audioToken) return;
    state.audioReady = false;
    const message = (err && err.message) ? err.message : String(err);
    audioStatus.textContent = "Audio could not load.";
    setStatus(`Audio could not load: ${message}`, "error");
    syncTransportButtons();
  });
}

function currentTime() {
  if (!wavesurfer || !state.audioReady) return 0;
  try { return wavesurfer.getCurrentTime(); } catch { return 0; }
}
function requireAudio(message = "Audio is still loading.") {
  if (!wavesurfer || !state.audioReady) {
    setStatus(message, "warn");
    return false;
  }
  return true;
}
function updateTimes(time) {
  const text = `${formatPlayerTime(time)} / ${formatPlayerTime(state.duration || 0)}`;
  timeEl.textContent = text;
  timeElBottom.textContent = text;
}
function syncTransportButtons(label) {
  const text = label || (wavesurfer && state.audioReady && wavesurfer.isPlaying()
    ? "Pause" : "Play");
  playPauseBtn.textContent = text;
  playPauseBtnBottom.textContent = text;
}
function togglePlayback() {
  if (!wavesurfer || !state.audioReady) {
    setStatus("Audio is still loading.", "warn");
    return;
  }
  wavesurfer.playPause();
}
function stopPlayback() {
  if (!wavesurfer || !state.audioReady) return;
  try { wavesurfer.stop(); } catch { /* ignore */ }
  syncTransportButtons("Play");
}
function seekTo(seconds, play = false) {
  if (!requireAudio()) return;
  const position = Math.max(0, Math.min(state.duration || seconds, seconds));
  try { wavesurfer.setTime(position); } catch { /* ignore */ }
  updateTimes(position);
  updateHighlight(position);
  if (play) {
    const promise = wavesurfer.play();
    if (promise && typeof promise.catch === "function") promise.catch(() => {});
  }
}
function playCue(index) {
  const cue = state.cues[index];
  if (!cue) return;
  seekTo(Math.max(0, cue.start - 0.6), true);
  setStatus(`Playing line ${index + 1}.`, "muted");
}
function updateLoopButton() {
  loopCueBtn.textContent = state.loopActive ? "Stop loop" : "Loop line";
  loopCueBtn.classList.toggle("is-active", state.loopActive);
}
function loopCue(index = state.selectedIndex) {
  if (state.loopActive) {
    state.loopActive = false;
    state.loopStart = 0;
    state.loopEnd = 0;
    updateLoopButton();
    setStatus("Loop stopped.", "muted");
    return;
  }
  const cue = state.cues[index];
  if (!cue) {
    setStatus("Select a line to loop.", "warn");
    return;
  }
  if (!requireAudio()) return;
  if (cue.end <= cue.start + 0.05) {
    setStatus("That line is too short to loop.", "warn");
    return;
  }
  state.loopStart = cue.start;
  state.loopEnd = cue.end;
  state.loopActive = true;
  updateLoopButton();
  seekTo(cue.start, true);
  setStatus(`Looping line ${index + 1}.`, "muted");
}
function loopSelectedWord() {
  const entry = selectedWordEntry();
  if (!entry) return;
  if (!requireAudio()) return;
  state.loopStart = entry.word.start;
  state.loopEnd = entry.word.end;
  state.loopActive = true;
  updateLoopButton();
  seekTo(entry.word.start, true);
  setStatus(`Looping word “${entry.word.text}”.`, "muted");
}
function changeZoom(direction) {
  if (!requireAudio()) return;
  if (direction === 0) {
    state.zoomPx = 0;
  } else {
    const current = state.zoomPx || 40;
    state.zoomPx = Math.round(clamp(
      direction > 0 ? current * 1.4 : current / 1.4, 10, 500));
  }
  refreshWaveform();
}
function applyPlaybackRate() {
  if (!rateSelect || !wavesurfer || !state.audioReady) return;
  try { wavesurfer.setPlaybackRate(Number(rateSelect.value) || 1); } catch { /* ignore */ }
}

/* ---------- word timing operations ---------- */
function selectedWordEntry() {
  const cue = state.cues[state.selectedIndex];
  if (!cue || cue.instrumental || !cue.words.length) return null;
  const index = state.selectedWordIndex;
  if (index < 0 || index >= cue.words.length) return null;
  return { cue, index, word: cue.words[index] };
}
function selectWord(cueIndex, wordIndex) {
  const cue = state.cues[cueIndex];
  if (!cue || !cue.words[wordIndex]) return;
  state.selectedIndex = cueIndex;
  state.selectedWordIndex = wordIndex;
  renderWordPanel();
  if (state.audioReady) seekTo(cue.words[wordIndex].start, false);
}
function nudgeSelectedWord(delta) {
  const entry = selectedWordEntry();
  if (!entry) return;
  const { cue, index, word } = entry;
  const previousEnd = index > 0 ? cue.words[index - 1].end : cue.start;
  const nextStart = index < cue.words.length - 1 ? cue.words[index + 1].start : cue.end;
  const applied = clamp(delta, previousEnd - word.start, nextStart - word.end);
  if (Math.abs(applied) < 0.0005) {
    setStatus("There is no room to nudge that word in this direction.", "warn");
    return;
  }
  pushHistory();
  word.start = roundTime(word.start + applied);
  word.end = roundTime(word.end + applied);
  cue.review = null;
  markDirty();
  renderAll();
  setStatus(`Word “${word.text}” nudged ${formatDelta(applied)}.`, "muted");
}
function setSelectedWordBoundary(kind) {
  if (!requireAudio("Load audio before setting a word boundary.")) return;
  const entry = selectedWordEntry();
  if (!entry) return;
  const { cue, index, word } = entry;
  const target = roundTime(currentTime());
  const previousEnd = index > 0 ? cue.words[index - 1].end : cue.start;
  const nextStart = index < cue.words.length - 1 ? cue.words[index + 1].start : cue.end;
  if (kind === "start") {
    if (target < previousEnd - 0.001 || target >= word.end - 0.12) {
      setStatus("That word start would overlap. Move the playhead somewhere inside the word.", "error");
      return;
    }
    pushHistory();
    word.start = target;
  } else {
    if (target <= word.start + 0.12 || target > nextStart + 0.001) {
      setStatus("That word end would overlap. Move the playhead somewhere inside the word.", "error");
      return;
    }
    pushHistory();
    word.end = target;
  }
  cue.review = null;
  markDirty();
  renderAll();
  setStatus(`Word “${word.text}” ${kind} set.`, "muted");
}
function createWordTiming() {
  const cue = state.cues[state.selectedIndex];
  if (!cue || cue.instrumental) {
    setStatus("Select a lyric line first.", "warn");
    return;
  }
  if (cue.words.length) {
    setStatus("This line already has word-by-word timing.", "muted");
    return;
  }
  const result = LyricReconcile.createWordTimings(cue);
  if (result.status !== "ok") {
    setStatus(result.message, "warn");
    return;
  }
  pushHistory();
  cue.words = result.words.map(word => ({ ...word }));
  cue.review = result.review;
  state.selectedWordIndex = -1;
  state.lastReconcileNotice = { index: state.selectedIndex, message: result.message };
  markDirty();
  renderAll();
  setStatus(result.message, "warn");
}
async function stopWordTiming() {
  const cue = state.cues[state.selectedIndex];
  if (!cue || !cue.words.length) return;
  const confirmed = await karaokeConfirm({
    title: "Stop word-by-word highlighting on this line?",
    message: "Only this line changes. Every other line keeps its word timing.",
    confirmLabel: "Use line highlighting",
    tone: "danger",
    focus: "cancel",
  });
  if (!confirmed) return;
  pushHistory();
  cue.words = [];
  cue.review = null;
  state.selectedWordIndex = -1;
  markDirty();
  renderAll();
  setStatus("This line now uses whole-line highlighting.", "warn");
}

/* ---------- structural operations ---------- */
async function addCue(instrumental = false, afterIndex = null) {
  if (!state.song) {
    setStatus("Choose a song first.", "warn");
    return;
  }
  if (!(await commitAllText({ interactive: true }))) return;

  let insertAt = afterIndex === null ? state.cues.length : afterIndex + 1;
  insertAt = clamp(insertAt, 0, state.cues.length);
  const previous = state.cues[insertAt - 1] || null;
  const next = state.cues[insertAt] || null;
  let start = previous ? previous.end : 0;
  let end = start + 2;
  let placed = false;

  const gapStart = previous ? previous.end : 0;
  const gapEnd = next ? next.start : gapStart + 2;
  if (gapEnd - gapStart >= 0.3) {
    const playhead = state.audioReady ? currentTime() : null;
    if (playhead !== null && playhead > gapStart + 0.1 && playhead < gapEnd - 0.2) {
      start = roundTime(playhead);
    } else {
      start = roundTime(gapStart);
    }
    end = roundTime(Math.min(start + 2, gapEnd - 0.001));
    if (end <= start + 0.12) end = roundTime(gapEnd - 0.001);
    placed = true;
  }

  if (!placed) {
    const last = state.cues[state.cues.length - 1];
    start = last ? roundTime(last.end + 0.05) : 0;
    end = roundTime(start + 2);
    insertAt = state.cues.length;
  }

  const cue = makeCue({
    start,
    end,
    text: instrumental ? "[Instrumental]" : "",
    instrumental,
    words: [],
  });
  cue.review = placed ? null : "new-line-needs-timing";
  pushHistory();
  state.cues.splice(insertAt, 0, cue);
  state.selectedIndex = insertAt;
  state.selectedWordIndex = -1;
  state.lastReconcileNotice = placed
    ? null
    : { index: insertAt, message: "There was no free time after that line, so it was added at the end. Fix its timing when ready." };
  markDirty();
  state.mode = "lyrics";
  renderAll();
  if (!instrumental) {
    setTimeout(() => {
      const row = state.rowEls[insertAt];
      if (row && row.textInput) row.textInput.focus();
    }, 0);
  }
  setStatus(instrumental
    ? "Instrumental break added."
    : (placed ? "Line added. Type the words." : "Line added at the end. Check its timing."),
  placed ? "ok" : "warn");
}

async function removeCue(index) {
  const cue = state.cues[index];
  if (!cue) return;
  const confirmed = await karaokeConfirm({
    title: cue.instrumental ? "Remove this instrumental break?" : `Remove line ${index + 1}?`,
    message: "You can undo this change.",
    confirmLabel: cue.instrumental ? "Remove break" : "Remove line",
    tone: "danger",
    focus: "cancel",
  });
  if (!confirmed) return;
  pushHistory();
  state.cues.splice(index, 1);
  state.selectedIndex = Math.min(index, state.cues.length - 1);
  state.selectedWordIndex = -1;
  state.lastReconcileNotice = null;
  markDirty();
  renderAll();
  setStatus(cue.instrumental ? "Instrumental break removed." : `Line ${index + 1} removed.`, "muted");
}

async function splitSelected(index = state.selectedIndex) {
  const cue = state.cues[index];
  if (!cue) return;
  if (!(await commitAllText({ interactive: true }))) return;
  const row = state.rowEls[index];
  const textarea = row ? row.textInput : null;
  let caret = Math.floor(cue.text.length / 2);
  if (textarea) {
    const focused = document.activeElement === textarea
      || state.lastFocusedCue === index
      || (state.lastCaret && state.lastCaret.index === index);
    if (focused && textarea.selectionStart > 0) caret = textarea.selectionStart;
  }
  const playhead = state.audioReady ? currentTime() : null;
  const result = LyricReconcile.splitLine(cue, caret, playhead);
  if (result.status !== "ok") {
    setStatus(result.message, "warn");
    return;
  }
  pushHistory();
  state.cues.splice(index, 1, result.first, result.second);
  state.selectedIndex = index;
  state.selectedWordIndex = -1;
  markDirty();
  renderAll();
  setTimeout(() => {
    const newRow = state.rowEls[index];
    if (newRow && newRow.textInput) {
      newRow.textInput.focus();
      newRow.textInput.setSelectionRange(newRow.textInput.value.length, newRow.textInput.value.length);
    }
  }, 0);
  setStatus(result.message, result.estimated ? "warn" : "ok");
}

async function mergeSelected(index = state.selectedIndex) {
  const first = state.cues[index];
  const second = state.cues[index + 1];
  if (!first || !second) {
    setStatus("There is no following line to merge.", "warn");
    return;
  }
  if (!(await commitAllText({ interactive: true }))) return;
  let result = LyricReconcile.mergeLines(first, second);
  if (result.status === "mixed") {
    const confirmed = await karaokeConfirm({
      title: "Create word timing for the merged line?",
      message: "One line has word-by-word timing and the other uses whole-line highlighting. Creating timing for the other half keeps word highlighting. Choose Cancel to leave both lines unchanged.",
      confirmLabel: "Create timing and merge",
      focus: "confirm",
    });
    if (!confirmed) return;
    result = LyricReconcile.mergeLines(first, second, "create");
  }
  if (result.status !== "ok") {
    setStatus(result.message, result.status === "mixed" ? "warn" : "error");
    return;
  }
  pushHistory();
  state.cues.splice(index, 2, result.cue);
  state.selectedIndex = index;
  state.selectedWordIndex = -1;
  state.lastReconcileNotice = result.cue.review
    ? { index, message: result.message }
    : null;
  markDirty();
  renderAll();
  setStatus(result.message, result.cue.review ? "warn" : "ok");
}

async function clearCues() {
  if (!state.cues.length) return;
  const confirmed = await karaokeConfirm({
    title: "Clear all lyrics?",
    message: "Every lyric line will be removed from this draft. You can undo this change.",
    confirmLabel: "Clear lyrics",
    tone: "danger",
    focus: "cancel",
  });
  if (!confirmed) return;
  pushHistory();
  state.cues = [];
  state.selectedIndex = -1;
  state.selectedWordIndex = -1;
  state.lastReconcileNotice = null;
  markDirty();
  renderAll();
  setStatus("All lyric lines cleared.", "muted");
}

/* ---------- timing operations ---------- */
function shiftCue(cue, delta) {
  if (!cue) return;
  cue.start = roundTime(cue.start + delta);
  cue.end = roundTime(cue.end + delta);
  cue.words.forEach(word => {
    word.start = roundTime(word.start + delta);
    word.end = roundTime(word.end + delta);
  });
}
function fitWordsToCue(cue) {
  if (!cue.words.length) return;
  const lastWordEnd = cue.words[cue.words.length - 1].end;
  if (lastWordEnd <= cue.end + 0.001) return;
  const span = lastWordEnd - cue.start;
  const target = cue.end - cue.start;
  if (span <= 0 || target <= 0) return;
  const factor = target / span;
  cue.words.forEach(word => {
    word.start = roundTime(cue.start + (word.start - cue.start) * factor);
    word.end = roundTime(cue.start + (word.end - cue.start) * factor);
  });
}
function shiftFollowing(sourceIndex, delta) {
  for (let index = sourceIndex + 1; index < state.cues.length; index += 1) {
    shiftCue(state.cues[index], delta);
  }
}
async function moveCueBy(index, delta) {
  const cue = state.cues[index];
  if (!cue) return;
  if (delta < 0) {
    if (cue.start + delta < -0.001) {
      setStatus("This line would start before the song begins.", "warn");
      return;
    }
    const previous = state.cues[index - 1];
    if (previous && cue.start + delta < previous.end - 0.001) {
      setStatus("This line would overlap the previous line.", "warn");
      return;
    }
    pushHistory();
    shiftCue(cue, delta);
    cue.review = null;
    markDirty();
    renderAll();
    setStatus(`Line ${index + 1} moved ${formatDelta(delta)}.`, "muted");
    return;
  }

  const next = state.cues[index + 1];
  if (next && cue.end + delta > next.start - 0.001 && !state.ripple) {
    const confirmed = await karaokeConfirm({
      title: "There isn’t room before the next line.",
      message: "Move the following lines too? Their spacing will be preserved.",
      confirmLabel: "Move following lines",
      focus: "cancel",
    });
    if (!confirmed) return;
    pushHistory();
    shiftCue(cue, delta);
    shiftFollowing(index, delta);
    cue.review = null;
    markDirty();
    renderAll();
    setStatus(`Line ${index + 1} moved ${formatDelta(delta)}; following lines moved too.`, "muted");
    return;
  }
  pushHistory();
  shiftCue(cue, delta);
  if (state.ripple) shiftFollowing(index, delta);
  cue.review = null;
  markDirty();
  renderAll();
  setStatus(`Line ${index + 1} moved ${formatDelta(delta)}.`, "muted");
}
function nudgeCue(index, delta) {
  return moveCueBy(index, delta);
}
function setCueStart(index, newStart) {
  const cue = state.cues[index];
  if (!cue) return;
  newStart = roundTime(newStart);
  if (newStart < -0.001) {
    setStatus("A line cannot start before the song begins.", "warn");
    return;
  }
  if (newStart >= cue.end - 0.12) {
    setStatus("The start must stay before the end of the line.", "warn");
    return;
  }
  const previous = state.cues[index - 1];
  if (previous && newStart < previous.end - 0.001) {
    setStatus("This would overlap the previous line.", "warn");
    return;
  }
  pushHistory();
  const delta = roundTime(newStart - cue.start);
  shiftCue(cue, delta);
  if (state.ripple) shiftFollowing(index, delta);
  cue.review = null;
  markDirty();
  renderAll();
  setStatus(`Line ${index + 1} moved ${formatDelta(delta)}.`, "muted");
}
async function setCueEnd(index, newEnd) {
  const cue = state.cues[index];
  if (!cue) return;
  newEnd = roundTime(newEnd);
  if (newEnd <= cue.start + 0.12) {
    setStatus("The end must stay after the start of the line.", "warn");
    return;
  }
  const next = state.cues[index + 1];
  if (next && newEnd > next.start - 0.001 && !state.ripple) {
    const confirmed = await karaokeConfirm({
      title: "There isn’t room before the next line.",
      message: "Move the following lines too? Their spacing will be preserved.",
      confirmLabel: "Move following lines",
      focus: "cancel",
    });
    if (!confirmed) return;
    pushHistory();
    const delta = roundTime(newEnd - cue.end);
    cue.end = newEnd;
    fitWordsToCue(cue);
    shiftFollowing(index, delta);
    cue.review = null;
    markDirty();
    renderAll();
    setStatus(`Line ${index + 1} extended; following lines moved too.`, "muted");
    return;
  }
  pushHistory();
  const delta = roundTime(newEnd - cue.end);
  cue.end = newEnd;
  fitWordsToCue(cue);
  if (state.ripple) shiftFollowing(index, delta);
  cue.review = null;
  markDirty();
  renderAll();
  setStatus(`Line ${index + 1} end adjusted ${formatDelta(delta)}.`, "muted");
}
function setStartAtPlayhead(index) {
  if (!requireAudio()) return;
  setCueStart(index, currentTime());
}
function setEndAtPlayhead(index) {
  if (!requireAudio()) return;
  setCueEnd(index, currentTime());
}
function applyShift(delta) {
  if (!state.cues.length) {
    setStatus("There are no lyric lines to shift.", "warn");
    return;
  }
  let applied = roundTime(delta);
  const starts = [];
  state.cues.forEach(cue => {
    starts.push(cue.start);
    cue.words.forEach(word => starts.push(word.start));
  });
  const minStart = Math.min(...starts);
  if (minStart + applied < -0.001) {
    const maxEarlier = roundTime(-minStart);
    setStatus(`The first line would start before the song begins. The largest shift earlier is ${formatDelta(-maxEarlier)}.`, "warn");
    return;
  }
  if (Math.abs(applied) < 0.0005) {
    setStatus("Nothing to shift.", "muted");
    return;
  }
  pushHistory();
  state.cues.forEach(cue => shiftCue(cue, applied));
  markDirty();
  renderAll();
  setStatus(`Every line moved ${formatDelta(applied)}.`, "ok");
}
function applyExactTimes() {
  const cue = state.cues[state.selectedIndex];
  if (!cue) {
    setStatus("Select a line first.", "warn");
    return;
  }
  const start = parseTime(exactStartInput.value);
  const end = parseTime(exactEndInput.value);
  if (start === null || end === null) {
    setStatus("Enter times like 0:12.4 or 0:12.400.", "error");
    return;
  }
  if (end <= start + 0.12) {
    setStatus("The end must be after the start.", "error");
    return;
  }
  const previous = state.cues[state.selectedIndex - 1];
  if (previous && start < previous.end - 0.001) {
    setStatus("This would overlap the previous line.", "warn");
    return;
  }
  const next = state.cues[state.selectedIndex + 1];
  if (next && end > next.start - 0.001) {
    setStatus("This would overlap the next line.", "warn");
    return;
  }
  pushHistory();
  cue.start = roundTime(start);
  cue.end = roundTime(end);
  fitWordsToCue(cue);
  cue.review = null;
  markDirty();
  renderAll();
  setStatus("Exact times applied.", "ok");
}
function markTimingReviewed(index) {
  const cue = state.cues[index];
  if (!cue || !cue.review) return;
  pushHistory();
  cue.review = null;
  state.lastReconcileNotice = null;
  markDirty();
  renderAll();
  setStatus("Timing marked as reviewed.", "ok");
}

/* ---------- song loading ---------- */
async function loadSongOptions(selectedFilename) {
  const current = selectedFilename || songSelect.value;
  try {
    const response = await fetch("/api/songs", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.songs = (data && Array.isArray(data.songs)) ? data.songs : [];
    songSelect.replaceChildren();
    if (!state.songs.length) {
      const option = document.createElement("option");
      option.disabled = true;
      option.selected = true;
      option.value = "";
      option.textContent = "No songs found in the songs folder";
      songSelect.appendChild(option);
      loadSongBtn.disabled = true;
      return;
    }
    const placeholder = document.createElement("option");
    placeholder.disabled = true;
    placeholder.value = "";
    placeholder.textContent = "Choose a song…";
    songSelect.appendChild(placeholder);
    state.songs.forEach(song => {
      const option = document.createElement("option");
      option.value = song.filename;
      option.textContent = `${song.id ? song.id + " — " : ""}${song.title}${song.artist ? " (" + song.artist + ")" : ""}`;
      songSelect.appendChild(option);
    });
    if (current && state.songs.some(song => song.filename === current)) {
      songSelect.value = current;
    } else if (state.song && state.songs.some(song => song.filename === state.song.filename)) {
      songSelect.value = state.song.filename;
    } else {
      songSelect.value = "";
    }
    loadSongBtn.disabled = false;
  } catch (err) {
    songSelect.replaceChildren();
    const option = document.createElement("option");
    option.disabled = true;
    option.selected = true;
    option.value = "";
    option.textContent = "Failed to load the song list";
    songSelect.appendChild(option);
    loadSongBtn.disabled = true;
    setStatus(`Could not load songs: ${err.message || err}`, "error");
  }
}

async function fetchEditorDraft(filename) {
  const response = await fetch(
    `/api/editor/draft?song_id=${encodeURIComponent(filename)}`,
    { cache: "no-store" }
  );
  const data = await response.json().catch(() => ({}));
  return { response, data };
}

function applyReviewMetadata(review) {
  const map = new Map();
  if (Array.isArray(review)) {
    review.forEach(item => {
      if (item && Number.isInteger(item.line) && item.line > 0
          && typeof item.reason === "string" && item.reason.trim()) {
        map.set(item.line, item.reason.trim());
      }
    });
  }
  state.cues.forEach((cue, index) => {
    cue.review = map.get(index + 1) || null;
    cue.needsReconcile = false;
  });
}

async function loadSong(song) {
  if (!song) return;
  if (state.dirty && !(await confirmDiscard())) return;
  const token = ++state.loadToken;
  cancelAutosave();
  destroyWaveSurfer();
  state.song = song;
  state.loadedStem = null;
  state.source = "none";
  state.draftRevision = null;
  state.baseActiveRevision = null;
  state.activeRevision = null;
  state.revisionSong = null;
  state.cues = [];
  state.wordsMeta = null;
  state.selectedIndex = -1;
  state.selectedWordIndex = -1;
  state.mode = "lyrics";
  state.history = [];
  state.redo = [];
  state.dirty = false;
  state.duration = 0;
  state.activeCueIndex = -1;
  state.activeWordIndex = -1;
  state.textEdit = null;
  state.lastReconcileNotice = null;
  state.lastCaret = null;
  state.lastFocusedCue = null;
  state.validationErrors = [];
  emptyState.classList.add("d-none");
  workspace.classList.remove("d-none");
  renderAll();
  setStatus(`Loading ${song.title}…`, "muted");
  window.history.replaceState({}, "", `/sync_editor.html?song=${encodeURIComponent(song.filename)}`);

  let bundle;
  try {
    bundle = await fetchEditorDraft(song.filename);
  } catch (err) {
    if (token !== state.loadToken) return;
    setStatus(`Could not load lyrics: ${err.message || err}`, "error");
    updateHeader();
    return;
  }
  if (token !== state.loadToken) return;
  if (!bundle.response.ok) {
    setStatus(bundle.data.error || "Could not load lyrics for this song.", "error");
    updateHeader();
    return;
  }

  const srtText = typeof bundle.data.srt === "string" ? bundle.data.srt : null;
  const wordsJson = (bundle.data.words && typeof bundle.data.words === "object")
    ? bundle.data.words : null;
  const wordsError = typeof bundle.data.words_error === "string"
    ? bundle.data.words_error : null;
  state.source = ["draft", "active"].includes(bundle.data.source)
    ? bundle.data.source : "none";
  state.draftRevision = typeof bundle.data.draft_revision === "string"
    ? bundle.data.draft_revision : null;
  state.activeRevision = typeof bundle.data.active_revision === "string"
    ? bundle.data.active_revision : null;
  state.baseActiveRevision = typeof bundle.data.base_active_revision === "string"
    ? bundle.data.base_active_revision : state.activeRevision;
  state.revisionSong = song.filename;

  const srtCues = srtText ? parseSRT(srtText) : [];
  const parsedWords = wordsJson ? parseWordsData(wordsJson) : null;
  if (parsedWords && parsedWords.cues && parsedWords.cues.length) {
    state.cues = parsedWords.cues.map(cloneCue);
    state.wordsMeta = parsedWords.meta || null;
    state.loadedStem = fileStem(song.filename);
    if (parsedWords.warnings && parsedWords.warnings.length) {
      setStatus(
        `Loaded lyrics with word-by-word timing. ${parsedWords.warnings[0]}`,
        "warn",
      );
    } else {
      setStatus(state.source === "draft"
        ? "Draft loaded. It is not live until you publish."
        : "Published lyrics loaded.", "muted");
    }
  } else if (srtCues.length) {
    state.cues = srtCues.map(cloneCue);
    state.wordsMeta = null;
    state.loadedStem = fileStem(song.filename);
    setStatus(wordsError
      ? `${wordsError} The lyric lines were loaded without word timing.`
      : (state.source === "draft"
        ? "Draft loaded. It is not live until you publish."
        : "Published lyrics loaded."), wordsError ? "warn" : "muted");
  } else if (wordsError) {
    setStatus(wordsError, "error");
  } else {
    setStatus("No lyrics yet. Add the first line.", "warn");
  }

  applyReviewMetadata(bundle.data.review);
  if (state.baseActiveRevision && state.activeRevision
      && state.baseActiveRevision !== state.activeRevision) {
    setStatus("This draft is based on older published lyrics. Publishing is blocked until you reload the song.", "warn");
  }
  state.selectedIndex = state.cues.findIndex(cue => !cue.instrumental);
  if (state.selectedIndex < 0 && state.cues.length) state.selectedIndex = 0;
  renderAll();
  loadAudio(
    `/songs/${encodeURIComponent(song.filename)}`,
    state.song.title || song.filename,
  );
  updateHeader();
}

function showSongPicker() {
  cancelAutosave();
  destroyWaveSurfer();
  state.song = null;
  state.cues = [];
  state.wordsMeta = null;
  state.source = "none";
  state.draftRevision = null;
  state.baseActiveRevision = null;
  state.activeRevision = null;
  state.revisionSong = null;
  state.selectedIndex = -1;
  state.selectedWordIndex = -1;
  state.history = [];
  state.redo = [];
  state.dirty = false;
  state.textEdit = null;
  state.lastReconcileNotice = null;
  state.lastCaret = null;
  state.lastFocusedCue = null;
  state.validationErrors = [];
  emptyState.classList.remove("d-none");
  workspace.classList.add("d-none");
  songContext.textContent = "Choose a song to begin";
  window.history.replaceState({}, "", "/sync_editor.html");
  updateHeader();
  updateButtons();
  renderAll();
}

async function changeSong() {
  if (!(await confirmDiscard())) return;
  showSongPicker();
  setStatus("Choose a song to begin.", "muted");
}

function matchSongByStem(stem) {
  return state.songs.find(song => fileStem(song.filename) === stem) || null;
}

async function importSrtFile(file) {
  if (!(await confirmDiscard())) return;
  try {
    const text = await file.text();
    const parsed = parseSRT(text);
    if (!parsed.length) {
      setStatus("No lyric lines were found in that file.", "error");
      return;
    }
    const song = matchSongByStem(lyricsStem(file.name)) || state.song;
    if (!song) {
      setStatus("Open a song first, or use a filename that matches the song.", "warn");
      return;
    }
    state.song = song;
    state.cues = parsed.map(cloneCue);
    state.wordsMeta = null;
    state.loadedStem = lyricsStem(file.name);
    state.source = "import";
    state.draftRevision = null;
    state.baseActiveRevision = null;
    state.activeRevision = null;
    state.revisionSong = null;
    state.selectedIndex = 0;
    state.selectedWordIndex = -1;
    state.history = [];
    state.redo = [];
    state.dirty = true;
    state.textEdit = null;
    state.lastReconcileNotice = null;
    renderAll();
    scheduleAutosave();
    setStatus(`Imported ${parsed.length} lyric lines. Save a draft to keep them locally.`, "ok");
  } catch (err) {
    setStatus(`Could not read that file: ${err.message || err}`, "error");
  }
}

async function importWordsFile(file) {
  if (!(await confirmDiscard())) return;
  try {
    const text = await file.text();
    let parsedJson;
    try {
      parsedJson = JSON.parse(text);
    } catch (err) {
      setStatus("That file is not valid JSON.", "error");
      return;
    }
    const result = parseWordsData(parsedJson);
    if (!result.cues || !result.cues.length) {
      setStatus(result.error || "No lyric lines were found in that file.", "error");
      return;
    }
    const song = matchSongByStem(lyricsStem(file.name)) || state.song;
    if (!song) {
      setStatus("Open a song first, or use a filename that matches the song.", "warn");
      return;
    }
    state.song = song;
    state.cues = result.cues.map(cloneCue);
    state.wordsMeta = result.meta || null;
    state.loadedStem = lyricsStem(file.name);
    state.source = "import";
    state.draftRevision = null;
    state.baseActiveRevision = null;
    state.activeRevision = null;
    state.revisionSong = null;
    state.selectedIndex = 0;
    state.selectedWordIndex = -1;
    state.history = [];
    state.redo = [];
    state.dirty = true;
    state.textEdit = null;
    state.lastReconcileNotice = null;
    renderAll();
    scheduleAutosave();
    setStatus(result.warnings && result.warnings.length
      ? `Imported ${result.cues.length} lyric lines with warnings. ${result.warnings[0]}`
      : `Imported ${result.cues.length} lyric lines with word timing.`, "ok");
  } catch (err) {
    setStatus(`Could not read that file: ${err.message || err}`, "error");
  }
}

/* ---------- draft save / publish ---------- */
async function ensureDraftRevisions({ autosave = false } = {}) {
  if (!state.song) return false;
  if (state.revisionSong === state.song.filename && state.baseActiveRevision) return true;
  let info;
  try {
    info = await fetchEditorDraft(state.song.filename);
  } catch (err) {
    if (!autosave) setStatus(`Could not check the saved draft: ${err.message || err}`, "error");
    return false;
  }
  if (!info.response.ok) {
    if (!autosave) {
      setStatus(info.data.error || "Could not check the saved draft.", "error");
    }
    return false;
  }
  const existingRevision = typeof info.data.draft_revision === "string"
    ? info.data.draft_revision : null;
  const loadedRevision = state.revisionSong === state.song.filename
    && state.draftRevision === existingRevision;
  if (existingRevision && !loadedRevision) {
    if (autosave) {
      setStatus("Autosave paused: a saved draft exists. Reload it to recover.", "warn");
      return false;
    }
    const confirmed = await karaokeConfirm({
      title: "Replace the saved draft?",
      message: "A saved draft already exists for this song. Continuing will replace it with the editor contents.",
      confirmLabel: "Replace draft",
      tone: "danger",
      focus: "cancel",
    });
    if (!confirmed) return false;
  }
  state.draftRevision = existingRevision;
  state.baseActiveRevision = typeof info.data.base_active_revision === "string"
    ? info.data.base_active_revision : info.data.active_revision;
  state.activeRevision = typeof info.data.active_revision === "string"
    ? info.data.active_revision : state.baseActiveRevision;
  state.revisionSong = state.song.filename;
  return !!state.baseActiveRevision;
}

function reviewPayload() {
  return state.cues
    .map((cue, index) => cue.review
      ? { line: index + 1, reason: String(cue.review).slice(0, 120) }
      : null)
    .filter(Boolean);
}

function buildSRT() {
  return state.cues.map((cue, index) => {
    const text = cue.instrumental ? "[Instrumental]" : normalizeText(cue.text);
    return `${index + 1}\n${formatTime(cue.start)} --> ${formatTime(cue.end)}\n${text}\n`;
  }).join("\n");
}

function buildWordsJSON() {
  const meta = state.wordsMeta || {};
  const data = {
    version: meta.version ?? 1,
    song: state.song ? fileStem(state.song.filename) : (meta.song || ""),
    generated_by: meta.generated_by || "Lyrics Editor",
    lines: state.cues.map(cue => ({
      start: roundTime(cue.start),
      end: roundTime(cue.end),
      text: cue.instrumental ? "[Instrumental]" : normalizeText(cue.text),
      instrumental: cue.instrumental,
      words: cue.instrumental ? [] : cue.words.map(word => ({
        text: word.text,
        start: roundTime(word.start),
        end: roundTime(word.end),
      })),
    })),
  };
  if (Number.isFinite(Number(meta.duration)) && Number(meta.duration) > 0) {
    data.duration = Number(meta.duration);
  } else if (state.duration > 0) {
    data.duration = roundTime(state.duration);
  }
  return data;
}

async function saveDraft({ autosave = false } = {}) {
  if (autosave && isEditingText()) {
    scheduleAutosave();
    return false;
  }
  if (!state.song) {
    if (!autosave) setStatus("Choose a song first.", "error");
    return false;
  }
  if (!state.cues.length) {
    if (!autosave) setStatus("Add at least one lyric line first.", "error");
    return false;
  }
  if (!(await commitAllText({ interactive: !autosave }))) return false;
  const { errors } = validateCues();
  if (errors.length) {
    if (!autosave) {
      setStatus(`Fix ${errors.length} highlighted ${errors.length === 1 ? "issue" : "issues"} before saving.`, "error");
      updateValidationView();
    }
    return false;
  }
  if (state.saving) {
    if (!autosave) setStatus("A save or publish is already in progress.", "warn");
    return false;
  }
  if (autosave && !state.dirty) return false;
  cancelAutosave();
  if (!(await ensureDraftRevisions({ autosave }))) return false;

  const snapshot = historySnapshot();
  const body = {
    song_id: state.song.filename,
    base_draft_revision: state.draftRevision,
    base_active_revision: state.baseActiveRevision,
    review: reviewPayload(),
  };
  if (hasWordCues()) body.words = buildWordsJSON();
  else body.srt = buildSRT();

  cancelAutosave();
  state.saving = true;
  updateButtons();
  setStatus(autosave ? "Autosaving draft…" : "Saving draft…", "muted");
  try {
    const response = await fetch("/api/editor/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 409) {
      state.draftRevision = null;
      state.revisionSong = null;
      throw new Error("The saved draft changed. Reload the song before saving.");
    }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

    if (historySnapshot() === snapshot) state.dirty = false;
    state.draftRevision = typeof data.draft_revision === "string"
      ? data.draft_revision : null;
    state.activeRevision = typeof data.active_revision === "string"
      ? data.active_revision : state.activeRevision;
    state.source = "draft";
    state.revisionSong = state.song.filename;
    if (state.dirty) {
      setStatus(autosave ? "Draft autosaved; newer edits are still unsaved." : "Draft saved; newer edits are still unsaved.", "warn");
    } else if (state.activeRevision && state.baseActiveRevision
        && state.activeRevision !== state.baseActiveRevision) {
      setStatus("Draft saved, but the published lyrics changed. Publishing is blocked until you reload.", "warn");
    } else {
      setStatus(autosave ? "Draft autosaved. It is not live." : "Draft saved. It is not live.", "ok");
    }
    updateHeader();
    if (state.dirty) scheduleAutosave();
    return true;
  } catch (err) {
    setStatus(`Draft save failed: ${err.message || err}`, "error");
    return false;
  } finally {
    state.saving = false;
    updateHeader();
  }
}

async function approveLyrics() {
  if (!state.song || !state.cues.length) {
    setStatus("Choose a song with lyric lines before publishing.", "error");
    return;
  }
  if (!(await commitAllText({ interactive: true }))) return;
  const { errors } = validateCues();
  if (errors.length) {
    setStatus(`Fix ${errors.length} highlighted ${errors.length === 1 ? "issue" : "issues"} before publishing.`, "error");
    updateValidationView();
    return;
  }
  const reviewLines = state.cues
    .map((cue, index) => cue.review ? index + 1 : null)
    .filter(line => line !== null);
  const title = state.song.title || state.song.filename;
  const messageParts = [
    `${title}${state.song.artist ? " — " + state.song.artist : ""}.`,
    `${state.cues.length} ${state.cues.length === 1 ? "lyric line" : "lyric lines"}.`,
  ];
  if (reviewLines.length) {
    messageParts.push(
      `${reviewLines.length} ${reviewLines.length === 1 ? "line has" : "lines have"} a timing check: ${reviewLines.join(", ")}.`,
    );
  }
  messageParts.push("The player will switch to this version immediately.");
  const confirmed = await karaokeConfirm({
    title: "Publish these lyrics?",
    message: messageParts.join(" "),
    confirmLabel: "Publish lyrics",
    focus: "confirm",
  });
  if (!confirmed) return;

  const saved = await saveDraft();
  if (!saved) return;
  if (state.dirty) {
    setStatus("The draft changed while it was saving. Save again before publishing.", "warn");
    return;
  }
  if (!state.draftRevision || !state.baseActiveRevision) {
    setStatus("Could not confirm the saved draft. Try again.", "error");
    return;
  }

  const approvalSnapshot = historySnapshot();
  state.saving = true;
  updateButtons();
  setStatus("Publishing…", "muted");
  try {
    const response = await fetch("/api/editor/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        song_id: state.song.filename,
        draft_revision: state.draftRevision,
        base_active_revision: state.baseActiveRevision,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 404) {
      state.draftRevision = null;
      state.revisionSong = null;
      throw new Error("There is no saved draft to publish. Save a draft and try again.");
    }
    if (response.status === 409) {
      if (typeof data.active_revision === "string") {
        state.activeRevision = data.active_revision;
      }
      if (Object.prototype.hasOwnProperty.call(data, "draft_revision")) {
        state.draftRevision = null;
        state.revisionSong = null;
      }
      throw new Error("The published lyrics changed while you were editing. Your draft is safe. Reload the song to continue.");
    }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

    state.source = "active";
    state.draftRevision = null;
    state.baseActiveRevision = typeof data.revision === "string" ? data.revision : null;
    state.activeRevision = state.baseActiveRevision;
    state.revisionSong = state.song.filename;
    state.loadedStem = fileStem(state.song.filename);
    state.dirty = historySnapshot() !== approvalSnapshot;
    cancelAutosave();
    state.song.words = !!data.words;
    state.song.lyrics = true;
    if (!state.dirty) {
      state.cues.forEach(cue => { cue.review = null; });
    }
    setStatus(state.dirty
      ? "Published. Newer edits are still unpublished."
      : "Published. The player is using this version.", state.dirty ? "warn" : "ok");
    updateHeader();
    await loadSongOptions(state.song.filename);
    updateHeader();
    if (state.dirty) scheduleAutosave();
  } catch (err) {
    setStatus(`Publish failed: ${err.message || err}`, "error");
  } finally {
    state.saving = false;
    updateHeader();
  }
}

/* ---------- downloads ---------- */
function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type: type || "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
function stemForDownload() {
  if (state.song) return fileStem(state.song.filename);
  if (state.loadedStem) return state.loadedStem;
  return "lyrics";
}

/* ---------- keyboard ---------- */
document.addEventListener("keydown", evt => {
  if ((evt.ctrlKey || evt.metaKey) && (evt.key === "s" || evt.key === "S")) {
    evt.preventDefault();
    saveDraft();
    return;
  }
  const isInteractive = evt.target instanceof Element
    && evt.target.closest("button, a, input, textarea, select, [role='slider']");
  if (isInteractive) return;
  if ((evt.ctrlKey || evt.metaKey) && (evt.key === "z" || evt.key === "Z")) {
    evt.preventDefault();
    if (evt.shiftKey) redo(); else undo();
    return;
  }
  if ((evt.ctrlKey || evt.metaKey) && (evt.key === "y" || evt.key === "Y")) {
    evt.preventDefault();
    redo();
    return;
  }
  if (evt.code === "Space") {
    evt.preventDefault();
    togglePlayback();
    return;
  }
  if (evt.key === "ArrowLeft") {
    evt.preventDefault();
    seekTo(currentTime() - 1, false);
    return;
  }
  if (evt.key === "ArrowRight") {
    evt.preventDefault();
    seekTo(currentTime() + 1, false);
    return;
  }
  if (evt.key === "[" && state.selectedIndex >= 0) {
    evt.preventDefault();
    nudgeCue(state.selectedIndex, -0.1);
    return;
  }
  if (evt.key === "]" && state.selectedIndex >= 0) {
    evt.preventDefault();
    nudgeCue(state.selectedIndex, 0.1);
    return;
  }
  if ((evt.key === "s" || evt.key === "S") && state.selectedIndex >= 0) {
    evt.preventDefault();
    setStartAtPlayhead(state.selectedIndex);
    return;
  }
  if ((evt.key === "e" || evt.key === "E") && state.selectedIndex >= 0) {
    evt.preventDefault();
    setEndAtPlayhead(state.selectedIndex);
  }
});

/* ---------- event wiring ---------- */
document.querySelectorAll(".mode-tab").forEach(button => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});
backToLyricsBtn.addEventListener("click", () => setMode("lyrics"));

loadSongBtn.addEventListener("click", () => {
  const song = state.songs.find(item => item.filename === songSelect.value);
  if (!song) {
    setStatus("Choose a song first.", "warn");
    return;
  }
  loadSong(song);
});
songSelect.addEventListener("change", () => {
  loadSongBtn.disabled = !songSelect.value;
});
$("reloadSongsBtn").addEventListener("click", () => loadSongOptions(songSelect.value));
$("changeSongBtn").addEventListener("click", changeSong);
$("loadAudioOnlyBtn").addEventListener("click", () => {
  const song = state.song || state.songs.find(item => item.filename === songSelect.value);
  if (!song) {
    setStatus("Choose a song first.", "warn");
    return;
  }
  if (!state.song) {
    state.song = song;
    emptyState.classList.add("d-none");
    workspace.classList.remove("d-none");
    renderAll();
  }
  loadAudio(`/songs/${encodeURIComponent(song.filename)}`, song.filename);
  setStatus("Audio loaded. Lyrics are unchanged.", "muted");
});

$("addCueBtn").addEventListener("click", () => addCue(false, state.selectedIndex));
$("addCueFooterBtn").addEventListener("click", () => addCue(false, state.cues.length - 1));
$("addInstrumentalBtn").addEventListener("click", () => addCue(true, state.selectedIndex));

undoBtn.addEventListener("click", undo);
redoBtn.addEventListener("click", redo);
saveBtn.addEventListener("click", () => saveDraft());
publishBtn.addEventListener("click", approveLyrics);
$("clearCuesBtn").addEventListener("click", clearCues);

$("importSrtBtn").addEventListener("click", () => $("srtFile").click());
$("importWordsBtn").addEventListener("click", () => $("wordsFile").click());
$("importAudioBtn").addEventListener("click", () => $("audioFile").click());
$("srtFile").addEventListener("change", evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = "";
  if (file) importSrtFile(file);
});
$("wordsFile").addEventListener("change", evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = "";
  if (file) importWordsFile(file);
});
$("audioFile").addEventListener("change", evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = "";
  if (!file) return;
  const url = URL.createObjectURL(file);
  state.localAudioUrl = url;
  loadAudio(url, file.name);
  setStatus(`Loaded local audio: ${file.name}.`, "muted");
});

playPauseBtn.addEventListener("click", togglePlayback);
playPauseBtnBottom.addEventListener("click", togglePlayback);
$("stopBtn").addEventListener("click", stopPlayback);
zoomOutBtn.addEventListener("click", () => changeZoom(-1));
zoomFitBtn.addEventListener("click", () => changeZoom(0));
zoomInBtn.addEventListener("click", () => changeZoom(1));
loopCueBtn.addEventListener("click", () => loopCue(state.selectedIndex));
rateSelect.addEventListener("change", applyPlaybackRate);
$("applyOffsetBtn").addEventListener("click", () => {
  const value = parseFloat(offsetInput.value);
  if (!Number.isFinite(value)) {
    setStatus("Enter a number of seconds to shift.", "error");
    return;
  }
  applyShift(value);
});
document.querySelectorAll("button[data-shift]").forEach(button => {
  button.addEventListener("click", () => applyShift(parseFloat(button.dataset.shift)));
});
$("rippleToggle").addEventListener("change", evt => {
  state.ripple = !!evt.target.checked;
  renderMode();
});
$("followToggle").addEventListener("change", evt => {
  state.follow = !!evt.target.checked;
});
exactTimesToggle.addEventListener("change", evt => {
  state.showExactTimes = !!evt.target.checked;
  const more = $("moreMenu");
  if (more) more.removeAttribute("open");
  if (state.showExactTimes && state.mode === "lyrics") {
    setMode("timing");
  } else {
    renderAll();
  }
});
$("applyExactTimesBtn").addEventListener("click", applyExactTimes);

prevLineBtn.addEventListener("click", () => {
  state.selectedIndex = Math.max(0, state.selectedIndex - 1);
  state.selectedWordIndex = -1;
  renderAll();
});
nextLineBtn.addEventListener("click", () => {
  state.selectedIndex = Math.min(state.cues.length - 1, state.selectedIndex + 1);
  state.selectedWordIndex = -1;
  renderAll();
});
createWordTimingBtn.addEventListener("click", createWordTiming);
stopWordTimingBtn.addEventListener("click", stopWordTiming);

$("previewSrtBtn").addEventListener("click", () => {
  srtOutput.textContent = state.cues.length ? buildSRT() : "No lyrics yet.";
  srtOutput.classList.toggle("d-none");
});
$("downloadSrtBtn").addEventListener("click", () => {
  downloadFile(`${stemForDownload()}.srt`, buildSRT(), "text/plain");
});
$("downloadWordsBtn").addEventListener("click", () => {
  downloadFile(`${stemForDownload()}.words.json`, JSON.stringify(buildWordsJSON(), null, 2) + "\n", "application/json");
});

reconcileApplyBtn.addEventListener("click", () => {
  const checked = reconcileDialog.querySelector('input[name="reconcileChoice"]:checked');
  settleReconcile(checked ? checked.value : "spread");
});
reconcileCancelBtn.addEventListener("click", () => settleReconcile(null));
reconcileDialog.addEventListener("cancel", evt => {
  evt.preventDefault();
  settleReconcile(null);
});
reconcileDialog.addEventListener("click", evt => {
  if (evt.target === reconcileDialog) settleReconcile(null);
});

document.addEventListener("click", evt => {
  document.querySelectorAll("details.more-menu[open]").forEach(details => {
    if (!details.contains(evt.target)) details.removeAttribute("open");
  });
});
document.querySelectorAll(".menu-button").forEach(button => {
  button.addEventListener("click", () => {
    if (button.id === "previewSrtBtn") return;
    const details = button.closest("details");
    if (details) details.removeAttribute("open");
  });
});
window.addEventListener("beforeunload", evt => {
  if (!state.dirty) return;
  evt.preventDefault();
  evt.returnValue = "";
});

/* ---------- bootstrap ---------- */
(async function bootstrap() {
  await loadSongOptions();
  const params = new URLSearchParams(window.location.search);
  const target = params.get("song");
  if (target) {
    const song = state.songs.find(item => item.filename === target)
      || state.songs.find(item => fileStem(item.filename) === fileStem(target));
    if (song) {
      songSelect.value = song.filename;
      await loadSong(song);
    } else {
      showSongPicker();
      setStatus("That song was not found in the library.", "error");
    }
  } else {
    showSongPicker();
    setStatus("Choose a song to begin.", "muted");
  }
  renderAll();
})();
