/* ---------- state ---------- */
const state = {
  cues: [],
  songs: [],
  song: null,          // selected save destination
  loadedStem: null,    // stem the cues came from (for mismatch warnings)
  duration: 0,
  wordsLoaded: false,
  wordsMeta: null,
  dirty: false,
  selectedIndex: -1,
  history: [],
  audioReady: false,
  audioToken: 0,
  loadToken: 0,
  validationErrors: [],
  follow: true,
  ripple: false,
  localAudioUrl: null,
  activeCueIndex: -1,
  activeWordIndex: -1,
  previewEls: [],
  rowEls: [],
  saving: false,
};

let wavesurfer = null;

/* ---------- dom helpers ---------- */
const $ = id => document.getElementById(id);
const loadBadge = $('loadBadge');
const cueCount = $('cueCount');
const wordCount = $('wordCount');
const songSelect = $('songSelect');
const saveBtn = $('saveBtn');
const saveStatus = $('saveStatus');
const validationEl = $('validation');
const cuesEl = $('cues');
const subtitlePreview = $('subtitlePreview');
const cueOverlay = $('cueOverlay');
const playPauseBtn = $('playPauseBtn');
const timeEl = $('timeDisplay');
const audioStatus = $('audioStatus');
const srtOutput = $('srtOutput');
const toggleWordModeBtn = $('toggleWordModeBtn');
const undoBtn = $('undoBtn');

/* ---------- utilities ---------- */
function pad2(value) { return String(value).padStart(2, '0'); }
function pad3(value) { return String(value).padStart(3, '0'); }
function fileStem(filename) { return String(filename).replace(/\.[^.]+$/, ''); }
function lyricsStem(filename) { return String(filename).replace(/\.words\.json$/i, '').replace(/\.[^.]+$/, ''); }
function roundTime(value) { return Math.round(Number(value) * 1000) / 1000; }
function normalizeText(text) { return String(text ?? '').replace(/\s+/g, ' ').trim(); }
function cloneCue(cue) { return { start: cue.start, end: cue.end, text: cue.text, instrumental: cue.instrumental, words: cue.words.map(w => ({ text: w.text, start: w.start, end: w.end })) }; }

function parseTime(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string') return null;
  let text = value.trim().replace(',', '.');
  if (!text) return null;
  let sign = 1;
  if (text[0] === '-' || text[0] === '+') { sign = text[0] === '-' ? -1 : 1; text = text.slice(1); }
  if (!/^\d+(?::\d+){0,2}(?:\.\d+)?$/.test(text)) return null;
  const parts = text.split(':');
  if (parts.length > 3) return null;
  let seconds;
  if (parts.length === 3) {
    const h = Number(parts[0]), m = Number(parts[1]), s = Number(parts[2]);
    if (m >= 60 || s >= 60) return null;
    seconds = h * 3600 + m * 60 + s;
  } else if (parts.length === 2) {
    const m = Number(parts[0]), s = Number(parts[1]);
    if (s >= 60) return null;
    seconds = m * 60 + s;
  } else {
    seconds = Number(parts[0]);
  }
  return Number.isFinite(seconds) ? sign * seconds : null;
}

function formatTime(seconds) {
  const totalMs = Math.max(0, Math.round(Number(seconds) * 1000));
  const h = Math.floor(totalMs / 3600000);
  const m = Math.floor((totalMs % 3600000) / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${pad2(h)}:${pad2(m)}:${pad2(s)},${pad3(ms)}`;
}
function formatClock(seconds) {
  return formatTime(seconds).replace(',', '.');
}
function formatDuration(seconds) {
  return `${Number(seconds).toFixed(3)}s`;
}
function formatDelta(seconds) {
  const sign = seconds >= 0 ? '+' : '';
  return `${sign}${Number(seconds).toFixed(3)}s`;
}

function makeCue({ start, end, text, instrumental, words }) {
  const cleanText = String(text ?? '');
  const isInstr = !!instrumental || normalizeText(cleanText).toLowerCase() === '[instrumental]';
  return {
    start: roundTime(Math.max(0, Number(start) || 0)),
    end: roundTime(Math.max(0, Number(end) || 0)),
    text: isInstr ? '[Instrumental]' : cleanText,
    instrumental: isInstr,
    words: isInstr || !Array.isArray(words) ? [] : words.map(w => ({
      text: String(w?.text ?? ''),
      start: roundTime(Number(w?.start) || 0),
      end: roundTime(Number(w?.end) || 0)
    }))
  };
}

function parseSRT(text) {
  const cues = [];
  const normalized = String(text).replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  for (const block of normalized.split(/\n{2,}/)) {
    const lines = block.split('\n').map(line => line.replace(/\s+$/, '')).filter(line => line.trim() !== '');
    if (!lines.length) continue;
    const timeIndex = lines.findIndex(line => line.includes('-->'));
    if (timeIndex === -1) continue;
    const [startRaw, endRaw] = lines[timeIndex].split('-->').map(part => part.trim());
    const start = parseTime(startRaw);
    const end = parseTime(endRaw);
    if (start === null || end === null || end <= start) continue;
    const cueText = lines.slice(timeIndex + 1).join(' ').trim();
    const instrumental = normalizeText(cueText).toLowerCase() === '[instrumental]';
    cues.push(makeCue({ start, end, text: instrumental ? '[Instrumental]' : cueText, instrumental, words: [] }));
  }
  cues.sort((a, b) => a.start - b.start || a.end - b.end);
  return cues;
}

function parseWordsData(data) {
  if (!data || !Array.isArray(data.lines) || !data.lines.length) {
    return { error: 'No "lines" array found in the word timing file.' };
  }
  const cues = [];
  const errors = [];
  for (let i = 0; i < data.lines.length; i++) {
    const line = data.lines[i];
    if (!line || typeof line !== 'object') { errors.push(`line ${i + 1}: not an object`); continue; }
    const start = Number(line.start);
    const end = Number(line.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) { errors.push(`line ${i + 1}: invalid time range`); continue; }
    const text = String(line.text ?? '');
    if (!normalizeText(text)) { errors.push(`line ${i + 1}: empty text`); continue; }
    const instrumental = !!line.instrumental || normalizeText(text).toLowerCase() === '[instrumental]';
    let words = [];
    if (Array.isArray(line.words)) {
      for (const word of line.words) {
        const ws = Number(word?.start);
        const we = Number(word?.end);
        if (!Number.isFinite(ws) || !Number.isFinite(we) || we <= ws) { errors.push(`line ${i + 1}: invalid word time`); words = null; break; }
        words.push({ text: String(word?.text ?? ''), start: ws, end: we });
      }
    }
    if (words === null) continue;
    cues.push(makeCue({ start, end, text: instrumental ? '[Instrumental]' : text, instrumental, words: instrumental ? [] : words }));
  }
  if (errors.length && !cues.length) {
    return { error: errors.slice(0, 4).join('; ') };
  }
  if (errors.length) {
    return { cues, meta: null, warnings: errors.slice(0, 4) };
  }
  return {
    cues,
    meta: {
      version: data.version ?? 1,
      song: data.song ?? '',
      duration: Number.isFinite(Number(data.duration)) ? Number(data.duration) : 0,
      generated_by: data.generated_by ?? ''
    },
    warnings: []
  };
}

function cuesMatch(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (Math.abs(a[i].start - b[i].start) > 0.002) return false;
    if (Math.abs(a[i].end - b[i].end) > 0.002) return false;
    if (normalizeText(a[i].text) !== normalizeText(b[i].text)) return false;
  }
  return true;
}

function validateCues() {
  const errors = [];
  const warnings = [];
  let previousEnd = -Infinity;
  for (let i = 0; i < state.cues.length; i++) {
    const cue = state.cues[i];
    const label = `Cue ${i + 1}`;
    if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end)) { errors.push(`${label}: invalid time`); continue; }
    if (cue.start < -0.001) errors.push(`${label}: start is negative`);
    if (cue.end <= cue.start + 0.001) errors.push(`${label}: end must be after start`);
    if (cue.start < previousEnd - 0.001) errors.push(`${label}: overlaps cue ${i} (${formatClock(cue.start)} < ${formatClock(previousEnd)})`);
    previousEnd = Math.max(previousEnd, cue.end);
    if (cue.instrumental) {
      if (normalizeText(cue.text).toLowerCase() !== '[instrumental]') errors.push(`${label}: instrumental text must be [Instrumental]`);
      if (cue.words.length) errors.push(`${label}: instrumental cue has word timings`);
    } else {
      if (!normalizeText(cue.text)) errors.push(`${label}: text is empty`);
      if (state.wordsLoaded && !cue.words.length) warnings.push(`${label}: no word timings; will display line-level only`);
      if (cue.words.length) {
        const joined = normalizeText(cue.words.map(w => w.text).join(' '));
        if (joined !== normalizeText(cue.text)) errors.push(`${label}: text does not match its word timings`);
        let previousWordEnd = null;
        for (let j = 0; j < cue.words.length; j++) {
          const w = cue.words[j];
          const wlabel = `${label}, word ${j + 1}`;
          if (!Number.isFinite(w.start) || !Number.isFinite(w.end)) { errors.push(`${wlabel}: invalid time`); continue; }
          if (w.end <= w.start + 0.0005) errors.push(`${wlabel}: end must be after start`);
          if (w.start < cue.start - 0.001) errors.push(`${wlabel}: starts before the cue`);
          if (w.end > cue.end + 0.001) errors.push(`${wlabel}: ends after the cue`);
          if (previousWordEnd !== null && w.start < previousWordEnd - 0.001) errors.push(`${wlabel}: overlaps a previous word`);
          if (!normalizeText(w.text)) errors.push(`${wlabel}: empty word text`);
          previousWordEnd = Math.max(previousWordEnd || -Infinity, w.end);
        }
      }
    }
    if (state.duration > 0 && cue.start > state.duration + .5) warnings.push(`${label}: starts after the audio ends`);
    if (state.duration > 0 && cue.end > state.duration + .5) warnings.push(`${label}: ends after the audio ends`);
  }
  return { errors, warnings };
}



/* ---------- history ---------- */
function pushHistory() {
  state.history.push(JSON.stringify({ cues: state.cues, wordsLoaded: state.wordsLoaded, wordsMeta: state.wordsMeta }));
  if (state.history.length > 50) state.history.shift();
  updateUndoButton();
}
function restoreHistorySnapshot(snapshotText) {
  const snapshot = JSON.parse(snapshotText);
  state.cues = snapshot.cues.map(cloneCue);
  state.wordsLoaded = !!snapshot.wordsLoaded;
  state.wordsMeta = snapshot.wordsMeta || null;
}
function undo() {
  if (!state.history.length) return;
  restoreHistorySnapshot(state.history.pop());
  state.dirty = true;
  state.selectedIndex = Math.min(state.selectedIndex, state.cues.length - 1);
  renderAll();
  setStatus('Undid the last change.', 'muted');
}

/* ---------- marking ---------- */
function markDirty() { state.dirty = true; updateHeader(); }

async function confirmDiscard() {
  if (!state.dirty) return true;
  return karaokeConfirm({
    title: 'Discard unsaved changes?',
    message: 'Your edits since the last save will be lost.',
    confirmLabel: 'Discard changes',
    tone: 'danger',
    focus: 'cancel'
  });
}

function deepClone(obj) { return JSON.parse(JSON.stringify(obj)); }

/* ---------- shifting ---------- */
function shiftCue(cue, delta) {
  if (!cue) return;
  cue.start = roundTime(cue.start + delta);
  cue.end = roundTime(cue.end + delta);
  for (const word of cue.words) {
    word.start = roundTime(word.start + delta);
    word.end = roundTime(word.end + delta);
  }
}
function fitWordsToCue(cue) {
  if (!cue.words.length) return;
  const lastWordEnd = cue.words[cue.words.length - 1].end;
  if (lastWordEnd <= cue.end + 0.001) return;
  const span = lastWordEnd - cue.start;
  const target = cue.end - cue.start;
  if (span <= 0 || target <= 0) return;
  const factor = target / span;
  for (const word of cue.words) {
    word.start = roundTime(cue.start + (word.start - cue.start) * factor);
    word.end = roundTime(cue.start + (word.end - cue.start) * factor);
  }
}
function setCueStart(index, newStart) {
  const cue = state.cues[index];
  if (!cue) return;
  newStart = roundTime(Math.max(0, newStart));
  if (Math.abs(newStart - cue.start) < 0.0005) { syncRowInputs(); return; }
  pushHistory();
  const delta = roundTime(newStart - cue.start);
  shiftCue(cue, delta);
  if (state.ripple) {
    for (let i = index + 1; i < state.cues.length; i++) shiftCue(state.cues[i], delta);
  }
  markDirty();
  refreshTiming();
  setStatus(`Cue ${index + 1} moved ${formatDelta(delta)}.`, 'muted');
}
function setCueEnd(index, newEnd) {
  const cue = state.cues[index];
  if (!cue) return;
  newEnd = roundTime(newEnd);
  if (newEnd <= cue.start + 0.001) { setStatus(`Cue ${index + 1}: end must be after start.`, 'error'); syncRowInputs(); return; }
  if (Math.abs(newEnd - cue.end) < 0.0005) { syncRowInputs(); return; }
  pushHistory();
  const delta = roundTime(newEnd - cue.end);
  cue.end = newEnd;
  fitWordsToCue(cue);
  if (state.ripple) {
    for (let i = index + 1; i < state.cues.length; i++) shiftCue(state.cues[i], delta);
  }
  markDirty();
  refreshTiming();
  setStatus(`Cue ${index + 1} end adjusted ${formatDelta(delta)}.`, 'muted');
}
function nudgeCue(index, delta) {
  const cue = state.cues[index];
  if (!cue) return;
  let applied = delta;
  if (cue.start + applied < 0) applied = -cue.start;
  if (Math.abs(applied) < 0.0005) return;
  pushHistory();
  shiftCue(cue, applied);
  if (state.ripple) {
    for (let i = index + 1; i < state.cues.length; i++) shiftCue(state.cues[i], applied);
  }
  markDirty();
  refreshTiming();
  setStatus(`Cue ${index + 1} nudged ${formatDelta(applied)}.`, 'muted');
}

/* ---------- global shift ---------- */
function applyShift(delta) {
  if (!state.cues.length) { setStatus('Load some cues first.', 'error'); return; }
  let applied = roundTime(delta);
  const starts = state.cues.reduce((acc, cue) => acc.concat([cue.start].concat(cue.words.map(w => w.start))), []);
  const minStart = Math.min(...starts);
  if (minStart + applied < 0) applied = roundTime(-minStart);
  if (Math.abs(applied) < 0.0005) { setStatus('Nothing to shift.', 'muted'); return; }
  pushHistory();
  for (const cue of state.cues) shiftCue(cue, applied);
  markDirty();
  refreshTiming();
  setStatus(`Shifted every cue by ${formatDelta(applied)}.`, 'ok');
}

/* ---------- rows ---------- */
function selectCue(index) {
  state.selectedIndex = (index >= 0 && index < state.cues.length) ? index : -1;
  state.rowEls.forEach((row, i) => row.row.classList.toggle('selected', i === state.selectedIndex));
  state.previewEls.forEach((entry, i) => entry.line.classList.toggle('selected', i === state.selectedIndex));
  renderMarkersActive();
}

function renderWordChips(container, cue, cueIndex) {
  container.innerHTML = '';
  if (cue.instrumental) {
    const badge = document.createElement('span');
    badge.className = 'instrumental-badge';
    badge.textContent = 'Instrumental';
    container.appendChild(badge);
    return;
  }
  if (!cue.words.length) {
    const span = document.createElement('span');
    span.className = 'muted-note';
    span.textContent = state.wordsLoaded ? 'Line-level only' : 'No word timings loaded';
    container.appendChild(span);
    return;
  }
  let valid = true;
  for (const word of cue.words) {
    if (!(word.text && Number.isFinite(word.start) && Number.isFinite(word.end) && word.end > word.start)) valid = false;
  }
  cue.words.forEach((word, wordIndex) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'word-chip' + (valid ? '' : ' bad');
    chip.textContent = word.text || '?';
    chip.title = `${word.text} · ${formatClock(word.start)} → ${formatClock(word.end)} (click to seek)`;
    chip.addEventListener('click', evt => {
      evt.stopPropagation();
      selectCue(cueIndex);
      seekTo(word.start, false);
    });
    container.appendChild(chip);
    if (wordIndex < cue.words.length - 1) container.appendChild(document.createTextNode(' '));
  });
  if (!valid) {
    const span = document.createElement('span');
    span.className = 'muted-note';
    span.textContent = ' invalid timing';
    container.appendChild(span);
  }
}

function renderCueList() {
  cuesEl.innerHTML = '';
  state.rowEls = [];
  state.cues.forEach((cue, index) => {
    const row = document.createElement('div');
    row.className = 'cue-row' + (cue.instrumental ? ' instrumental' : '');
    if (index === state.selectedIndex) row.classList.add('selected');
    if (index === state.activeCueIndex) row.classList.add('active');

    const number = document.createElement('div');
    number.className = 'cue-number';
    number.textContent = index + 1;

    const times = document.createElement('div');
    times.className = 'cue-times';

    const startInput = document.createElement('input');
    startInput.type = 'text';
    startInput.className = 'cue-start';
    startInput.value = formatTime(cue.start);

    const arrow = document.createElement('span');
    arrow.className = 'time-arrow';
    arrow.textContent = '→';

    const endInput = document.createElement('input');
    endInput.type = 'text';
    endInput.className = 'cue-end';
    endInput.value = formatTime(cue.end);

    times.append(startInput, arrow, endInput);

    const main = document.createElement('div');
    main.className = 'cue-main';

    const textRow = document.createElement('div');
    textRow.className = 'cue-text-row';

    const textInput = document.createElement('input');
    textInput.type = 'text';
    textInput.className = 'cue-text';
    textInput.value = cue.instrumental ? '[Instrumental]' : cue.text;
    textInput.disabled = cue.instrumental || (state.wordsLoaded && cue.words.length > 0);
    if (textInput.disabled) {
      textInput.title = cue.instrumental
        ? 'Instrumental cue text cannot be edited'
        : 'Text is locked while this cue has word timings. Click "Unlock text" to edit and switch the cue to line-level mode.';
    }

    const durationEl = document.createElement('span');
    durationEl.className = 'cue-duration';
    durationEl.textContent = formatDuration(cue.end - cue.start);

    textRow.append(textInput, durationEl);

    const wordsEl = document.createElement('div');
    wordsEl.className = 'cue-words';
    renderWordChips(wordsEl, cue, index);

    main.append(textRow, wordsEl);

    const actions = document.createElement('div');
    actions.className = 'cue-actions';

    function makeButton(label, title, handler, extraClass) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-outline-secondary' + (extraClass ? ' ' + extraClass : '');
      btn.textContent = label;
      btn.title = title;
      btn.addEventListener('click', evt => { evt.stopPropagation(); handler(); });
      return btn;
    }

    actions.append(
      makeButton('Play', 'Play from this cue', () => { seekTo(cue.start, true); }, ''),
      makeButton('−0.1', 'Nudge this cue earlier by 0.1s', () => { nudgeCue(index, -0.1); }, ''),
      makeButton('+0.1', 'Nudge this cue later by 0.1s', () => { nudgeCue(index, 0.1); }, ''),
      makeButton('Start', 'Set this cue start to the current playhead position', () => { if (!state.audioReady || !wavesurfer) { setStatus('Load audio first to set a cue to the playhead.', 'warn'); return; } setCueStart(index, wavesurfer.getCurrentTime()); }, ''),
      makeButton('End', 'Set this cue end to the current playhead position', () => { if (!state.audioReady || !wavesurfer) { setStatus('Load audio first to set a cue to the playhead.', 'warn'); return; } setCueEnd(index, wavesurfer.getCurrentTime()); }, ''),
      makeButton('Lead-in', 'Play from 1s before this cue to hear the hand-off', () => { seekTo(Math.max(0, cue.start - 1), true); }, ''),
    );
    if (state.wordsLoaded && cue.words.length) {
      actions.append(makeButton('Unlock', 'Remove word timings from this cue so its text can be edited. The cue becomes line-level only.', async () => {
        const confirmed = await karaokeConfirm({
          title: 'Remove word timings?',
          message: 'This cue will display line-level only after you save.',
          confirmLabel: 'Remove timings',
          tone: 'danger',
          focus: 'cancel'
        });
        if (!confirmed) return;
        pushHistory();
        state.cues[index].words = [];
        markDirty();
        renderAll();
        setStatus(`Cue ${index + 1}: word timings removed; text unlocked.`, 'warn');
      }, ''));
    }
    actions.append(makeButton('Delete', 'Delete this cue', () => deleteCue(index), 'danger'));

    row.append(number, times, main, actions);

    row.addEventListener('focusin', () => selectCue(index));
    row.addEventListener('click', evt => {
      if (evt.target.closest('button, input')) return;
      selectCue(index);
    });

    startInput.addEventListener('change', () => {
      const value = parseTime(startInput.value);
      if (value === null) { setStatus(`Cue ${index + 1}: invalid start time.`, 'error'); startInput.value = formatTime(state.cues[index].start); return; }
      setCueStart(index, value);
    });
    startInput.addEventListener('blur', () => { if (state.cues[index]) startInput.value = formatTime(state.cues[index].start); });
    endInput.addEventListener('change', () => {
      const value = parseTime(endInput.value);
      if (value === null) { setStatus(`Cue ${index + 1}: invalid end time.`, 'error'); endInput.value = formatTime(state.cues[index].end); return; }
      setCueEnd(index, value);
    });
    endInput.addEventListener('blur', () => { if (state.cues[index]) endInput.value = formatTime(state.cues[index].end); });
    textInput.addEventListener('input', () => {
      if (state.cues[index]) { state.cues[index].text = textInput.value; markDirty(); renderPreviewOnlyText(); }
    });
    textInput.addEventListener('change', () => {
      if (state.cues[index]) {
        state.cues[index].text = textInput.value.trim();
        markDirty();
        renderPreview();
        refreshTiming();
      }
    });

    cuesEl.appendChild(row);
    state.rowEls.push({ row, startInput, endInput, textInput, durationEl, wordsEl });
  });
}

async function deleteCue(index) {
  if (index < 0 || index >= state.cues.length) return;
  const confirmed = await karaokeConfirm({
    title: `Delete cue ${index + 1}?`,
    message: 'You can undo this change.',
    confirmLabel: 'Delete cue',
    tone: 'danger',
    focus: 'cancel'
  });
  if (!confirmed) return;
  pushHistory();
  state.cues.splice(index, 1);
  state.selectedIndex = -1;
  markDirty();
  renderAll();
  setStatus(`Deleted cue ${index + 1}.`, 'muted');
}

function addCue(instrumental) {
  if (state.wordsLoaded && !instrumental) {
    setStatus('Drop the word timings first (button above) to add or edit lyric cues.', 'warn');
    return;
  }
  const start = state.audioReady && wavesurfer ? wavesurfer.getCurrentTime() : (state.cues.length ? state.cues[state.cues.length - 1].end + 0.5 : 0);
  let text = instrumental ? '[Instrumental]' : '';
  let hintEnd = start + 2;
  const next = state.cues.find(c => c.start > start);
  if (next && hintEnd > next.start) hintEnd = Math.max(start + 0.2, next.start - 0.001);
  const cue = makeCue({ start, end: hintEnd, text, instrumental, words: [] });
  let insertAt = state.cues.length;
  for (let i = 0; i < state.cues.length; i++) {
    if (state.cues[i].start > cue.start) { insertAt = i; break; }
  }
  pushHistory();
  state.cues.splice(insertAt, 0, cue);
  state.selectedIndex = insertAt;
  markDirty();
  renderAll();
  const row = state.rowEls[insertAt];
  if (row && !instrumental) row.textInput.focus();
  setStatus(`Added cue ${insertAt + 1}.`, 'ok');
}

async function clearCues() {
  if (!state.cues.length) return;
  const confirmed = await karaokeConfirm({
    title: 'Clear all cues?',
    message: 'Every cue will be removed. You can undo this change.',
    confirmLabel: 'Clear cues',
    tone: 'danger',
    focus: 'cancel'
  });
  if (!confirmed) return;
  pushHistory();
  state.cues = [];
  state.selectedIndex = -1;
  markDirty();
  renderAll();
  setStatus('Cleared all cues.', 'muted');
}

async function dropWordTimings() {
  if (!state.wordsLoaded) return;
  const confirmed = await karaokeConfirm({
    title: 'Drop all word timings?',
    message: 'The SRT stays, but the player will fall back to line-level display after you save.',
    confirmLabel: 'Drop timings',
    tone: 'danger',
    focus: 'cancel'
  });
  if (!confirmed) return;
  pushHistory();
  for (const cue of state.cues) cue.words = [];
  state.wordsLoaded = false;
  state.wordsMeta = null;
  markDirty();
  renderAll();
  setStatus('Word timings dropped. Text is now editable.', 'warn');
}

/* ---------- preview ---------- */
function renderPreview() {
  subtitlePreview.innerHTML = '';
  state.previewEls = [];
  if (!state.cues.length) {
    const placeholder = document.createElement('div');
    placeholder.className = 'muted-note';
    placeholder.textContent = 'No cues loaded.';
    subtitlePreview.appendChild(placeholder);
    return;
  }
  state.cues.forEach((cue, index) => {
    const line = document.createElement('div');
    line.className = 'preview-line' + (cue.instrumental ? ' instrumental' : '');
    if (index === state.selectedIndex) line.classList.add('selected');
    line.dataset.index = index;
    line.title = `${formatClock(cue.start)} → ${formatClock(cue.end)}`;
    if (cue.instrumental || !cue.words.length) {
      line.textContent = cue.instrumental ? '[Instrumental]' : cue.text;
    } else {
      cue.words.forEach((word, wordIndex) => {
        const span = document.createElement('span');
        span.className = 'preview-word';
        span.textContent = word.text;
        span.addEventListener('click', evt => {
          evt.stopPropagation();
          selectCue(index);
          seekTo(word.start, false);
        });
        line.appendChild(span);
        if (wordIndex < cue.words.length - 1) line.appendChild(document.createTextNode(' '));
      });
    }
    line.addEventListener('click', () => { selectCue(index); seekTo(cue.start, false); });
    subtitlePreview.appendChild(line);
    state.previewEls.push({ line, cue, wordEls: Array.from(line.querySelectorAll('.preview-word')) });
  });
  updateHighlight(currentTime());
}
function renderPreviewOnlyText() {
  // Fast text-only update while typing in cue text inputs.
  state.cues.forEach((cue, index) => {
    const entry = state.previewEls[index];
    if (!entry || cue.words.length) return;
    entry.line.textContent = cue.instrumental ? '[Instrumental]' : cue.text;
  });
}

function updateHighlight(now) {
  let active = -1;
  for (let i = 0; i < state.cues.length; i++) {
    const cue = state.cues[i];
    if (now >= cue.start && now < cue.end) { active = i; break; }
  }
  if (active !== state.activeCueIndex) {
    state.activeCueIndex = active;
    state.activeWordIndex = -1;
    state.previewEls.forEach((entry, i) => {
      entry.line.classList.toggle('active', i === active);
      entry.wordEls.forEach(el => el.classList.remove('active', 'sung'));
    });
    state.rowEls.forEach((row, i) => row.row.classList.toggle('active', i === active));
    renderMarkersActive();
    if (active >= 0 && state.follow) {
      const element = state.previewEls[active]?.line;
      if (element) element.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }
  if (active === -1) return;
  const entry = state.previewEls[active];
  if (!entry || !entry.wordEls.length || !state.cues[active].words.length) return;
  let wordIndex = -1;
  const words = state.cues[active].words;
  for (let i = 0; i < words.length; i++) {
    if (now >= words[i].start) wordIndex = i;
    if (now < words[i].end) break;
  }
  if (wordIndex === state.activeWordIndex) return;
  state.activeWordIndex = wordIndex;
  entry.wordEls.forEach((el, i) => {
    el.classList.toggle('active', i === wordIndex);
    el.classList.toggle('sung', i < wordIndex);
  });
}

/* ---------- markers ---------- */
function renderMarkers() {
  cueOverlay.innerHTML = '';
  if (!state.duration || !state.cues.length) return;
  state.cues.forEach((cue, index) => {
    const marker = document.createElement('button');
    marker.type = 'button';
    marker.className = 'cue-marker' + (cue.instrumental ? ' instrumental' : '');
    marker.title = `#${index + 1} ${formatClock(cue.start)} → ${formatClock(cue.end)}`;
    const left = Math.max(0, Math.min(100, (cue.start / state.duration) * 100));
    const right = Math.max(0, Math.min(100, (cue.end / state.duration) * 100));
    marker.style.left = `${left}%`;
    marker.style.width = `${Math.max(right - left, 0.25)}%`;
    marker.addEventListener('click', evt => {
      evt.stopPropagation();
      selectCue(index);
      seekTo(cue.start, false);
    });
    cueOverlay.appendChild(marker);
  });
  renderMarkersActive();
}
function renderMarkersActive() {
  Array.from(cueOverlay.children).forEach((marker, index) => {
    marker.classList.toggle('active', index === state.activeCueIndex);
    marker.classList.toggle('selected', index === state.selectedIndex);
  });
}

/* ---------- rendering refresh ---------- */
function syncRowInputs() {
  state.rowEls.forEach((row, index) => {
    const cue = state.cues[index];
    if (!cue) return;
    if (document.activeElement !== row.startInput) row.startInput.value = formatTime(cue.start);
    if (document.activeElement !== row.endInput) row.endInput.value = formatTime(cue.end);
  });
}
function updateRowContents() {
  state.rowEls.forEach((row, index) => {
    const cue = state.cues[index];
    if (!cue) return;
    row.durationEl.textContent = formatDuration(cue.end - cue.start);
    row.row.classList.toggle('instrumental', cue.instrumental);
    row.row.classList.toggle('selected', index === state.selectedIndex);
    row.row.classList.toggle('active', index === state.activeCueIndex);
    row.textInput.disabled = cue.instrumental || (state.wordsLoaded && cue.words.length > 0);
    if (cue.instrumental) row.textInput.value = '[Instrumental]';
    renderWordChips(row.wordsEl, cue, index);
  });
}
function refreshTiming() {
  syncRowInputs();
  updateRowContents();
  renderMarkers();
  updateHighlight(currentTime());
  updateValidationView();
  updateCounts();
  updateSaveButton();
}
function updateValidationView() {
  const { errors, warnings } = validateCues();
  state.validationErrors = errors;
  validationEl.innerHTML = '';
  function addLine(text, type) {
    const div = document.createElement('div');
    div.textContent = text;
    div.className = type;
    validationEl.appendChild(div);
  }
  if (!state.cues.length) {
    addLine('No cues loaded yet. Add a cue, import an .srt, or load a song.', 'muted');
  } else if (!errors.length) {
    addLine(state.wordsLoaded ? 'Structure OK. Word timings will be saved.' : 'Structure OK. SRT-only mode.', 'ok');
  } else {
    errors.slice(0, 5).forEach(err => addLine(err, 'error'));
    if (errors.length > 5) addLine(`… ${errors.length - 5} more errors`, 'error');
  }
  warnings.slice(0, 3).forEach(warn => addLine(warn, 'warn'));
  if (warnings.length > 3) addLine(`… ${warnings.length - 3} more warnings`, 'warn');
  updateSaveButton();
}
function updateCounts() {
  const lyricCount = state.cues.filter(c => !c.instrumental).length;
  const instrumentalCount = state.cues.length - lyricCount;
  const words = state.cues.reduce((total, cue) => total + cue.words.length, 0);
  cueCount.textContent = `${state.cues.length} cues (${lyricCount} lyric, ${instrumentalCount} instr.)`;
  wordCount.textContent = `${words} words`;
  const dlSrt = document.getElementById('downloadSrtBtn');
  const dlWords = document.getElementById('downloadWordsBtn');
  if (dlSrt) dlSrt.disabled = !state.cues.length;
  if (dlWords) dlWords.disabled = !state.cues.length || !state.wordsLoaded;
}
function renderAll() {
  renderCueList();
  renderPreview();
  renderMarkers();
  updateValidationView();
  updateCounts();
  updateHeader();
  updateSaveButton();
  updateUndoButton();
  updateHighlight(currentTime());
}

/* ---------- waveform ---------- */
function destroyWaveSurfer() {
  if (wavesurfer) {
    try { wavesurfer.destroy(); } catch {}
    wavesurfer = null;
  }
  state.audioReady = false;
  if (state.localAudioUrl) {
    URL.revokeObjectURL(state.localAudioUrl);
    state.localAudioUrl = null;
  }
  state.duration = 0;
  cueOverlay.innerHTML = '';
  timeEl.textContent = '00:00:00,000 / 00:00:00,000';
}
function loadAudio(url, label) {
  if (typeof WaveSurfer === 'undefined') {
    setStatus('WaveSurfer failed to load. Check static/vendor/wavesurfer.min.js.', 'error');
    return;
  }
  const token = ++state.audioToken;
  destroyWaveSurfer();
  audioStatus.textContent = `Loading ${label}…`;
  audioStatus.className = 'audio-status';
  wavesurfer = WaveSurfer.create({
    container: '#waveform',
    height: 150,
    waveColor: '#3f3f3f',
    progressColor: '#ff0033',
    cursorColor: '#ffffff',
    barWidth: 2,
    barGap: 1,
    normalize: true,
  });
  wavesurfer.on('ready', () => {
    if (token !== state.audioToken) return;
    state.audioReady = true;
    state.duration = wavesurfer.getDuration();
    audioStatus.textContent = `Loaded ${label} (${formatClock(state.duration)})`;
    audioStatus.className = 'audio-status';
    timeEl.textContent = `00:00:00,000 / ${formatClock(state.duration)}`;
    renderMarkers();
    updateHighlight(currentTime());
    updateValidationView();
    updateCounts();
  });
  wavesurfer.on('timeupdate', time => {
    if (token !== state.audioToken) return;
    timeEl.textContent = `${formatClock(time)} / ${formatClock(state.duration || 0)}`;
    updateHighlight(time);
  });
  wavesurfer.on('seeking', time => {
    if (token !== state.audioToken) return;
    timeEl.textContent = `${formatClock(time)} / ${formatClock(state.duration || 0)}`;
    updateHighlight(time);
  });
  wavesurfer.on('play', () => { playPauseBtn.textContent = 'Pause'; });
  wavesurfer.on('pause', () => { playPauseBtn.textContent = 'Play'; });
  wavesurfer.on('finish', () => { playPauseBtn.textContent = 'Play'; });
  wavesurfer.load(url).catch(err => {
    if (token !== state.audioToken) return;
    state.audioReady = false;
    const message = (err && err.message) ? err.message : String(err);
    audioStatus.textContent = `Could not load audio: ${message}`;
    audioStatus.className = 'audio-status';
    setStatus(`Could not load audio: ${message}`, 'error');
  });
}
function currentTime() {
  if (!wavesurfer || !state.audioReady) return 0;
  try { return wavesurfer.getCurrentTime(); } catch { return 0; }
}
function requireAudio(message = 'Load audio first.') {
  if (!wavesurfer || !state.audioReady) { setStatus(message, 'warn'); return false; }
  return true;
}
function seekTo(seconds, play = false) {
  if (!requireAudio()) return;
  const position = Math.max(0, Math.min(state.duration || seconds, seconds));
  try { wavesurfer.setTime(position); } catch {}
  timeEl.textContent = `${formatClock(position)} / ${formatClock(state.duration || 0)}`;
  updateHighlight(position);
  if (play) {
    const promise = wavesurfer.play();
    if (promise && typeof promise.catch === 'function') promise.catch(() => {});
  }
}

/* ---------- status ---------- */
function setStatus(message, type = 'muted') {
  saveStatus.textContent = message;
  saveStatus.className = type === 'muted' ? 'text-secondary small' : (type === 'ok' ? 'text-success small' : (type === 'warn' ? 'text-warning small' : 'text-danger small'));
}
function updateHeader() {
  let badge = 'No song loaded';
  if (state.song) {
    badge = `${state.song.id ? state.song.id + ' — ' : ''}${state.song.title}`;
    if (state.song.artist) badge += ` (${state.song.artist})`;
    if (state.cues.length) badge += state.wordsLoaded ? ' · words' : ' · SRT only';
  } else if (state.cues.length) {
    badge = `Imported cues${state.wordsLoaded ? ' (words)' : ' (SRT)'}`;
  }
  if (state.dirty) badge += ' · unsaved';
  loadBadge.textContent = badge;
  loadBadge.className = state.dirty ? 'badge bg-warning text-dark' : 'badge badge-soft';
  toggleWordModeBtn.classList.toggle('d-none', !state.wordsLoaded);
  updateSaveButton();
}
function updateSaveButton() {
  saveBtn.disabled = state.saving || !state.song || !state.cues.length || state.validationErrors.length > 0;
  if (state.validationErrors.length > 0) {
    saveBtn.title = 'Fix the validation errors before saving.';
  } else if (!state.song) {
    saveBtn.title = 'Pick a song first.';
  } else if (!state.cues.length) {
    saveBtn.title = 'Add at least one cue.';
  } else {
    saveBtn.title = '';
  }
}
function updateUndoButton() { undoBtn.disabled = !state.history.length; }

/* ---------- song loading ---------- */
async function loadSongOptions(selectedFilename) {
  const current = selectedFilename || songSelect.value;
  try {
    const response = await fetch('/api/songs', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.songs = (data && Array.isArray(data.songs)) ? data.songs : [];
    songSelect.innerHTML = '';
    if (!state.songs.length) {
      const option = document.createElement('option');
      option.disabled = true;
      option.selected = true;
      option.value = '';
      option.textContent = 'No songs found in the songs/ folder';
      songSelect.appendChild(option);
      updateHeader();
      updateSaveButton();
      return;
    }
    for (const song of state.songs) {
      const option = document.createElement('option');
      option.value = song.filename;
      const flags = [song.lyrics ? 'SRT' : null, song.words ? 'words' : null].filter(Boolean).join('+');
      option.textContent = `${song.id ? song.id + ' — ' : ''}${song.title}${song.artist ? ' (' + song.artist + ')' : ''}${flags ? ' · ' + flags : ''}`;
      songSelect.appendChild(option);
    }
    if (current && state.songs.some(song => song.filename === current)) songSelect.value = current;
    else if (state.song && state.songs.some(song => song.filename === state.song.filename)) songSelect.value = state.song.filename;
    updateHeader();
    updateSaveButton();
  } catch (err) {
    songSelect.innerHTML = '';
    const option = document.createElement('option');
    option.disabled = true;
    option.selected = true;
    option.value = '';
    option.textContent = 'Failed to load the song list';
    songSelect.appendChild(option);
    setStatus(`Failed to load songs: ${err.message || err}`, 'error');
  }
}
async function loadSong(song) {
  if (!song || !(await confirmDiscard())) return;
  const token = ++state.loadToken;
  state.song = song;
  state.loadedStem = null;
  state.cues = [];
  state.wordsLoaded = false;
  state.wordsMeta = null;
  state.selectedIndex = -1;
  state.history = [];
  state.dirty = false;
  state.duration = 0;
  state.activeCueIndex = -1;
  state.activeWordIndex = -1;
  renderAll();
  setStatus(`Loading ${song.title}…`, 'muted');
  updateHeader();

  const stem = fileStem(song.filename);
  const responses = await Promise.allSettled([
    fetch(`/lyrics/${encodeURIComponent(stem)}.srt`),
    fetch(`/lyrics/${encodeURIComponent(stem)}.words.json`)
  ]);
  const srtResponse = responses[0];
  const wordsResponse = responses[1];
  const srtText = (srtResponse.status === 'fulfilled' && srtResponse.value.ok)
    ? await srtResponse.value.text().catch(() => null) : null;
  const wordsJson = (wordsResponse.status === 'fulfilled' && wordsResponse.value.ok)
    ? await wordsResponse.value.json().catch(() => null) : null;
  if (token !== state.loadToken) return;

  const srtCues = srtText ? parseSRT(srtText) : [];
  const parsedWords = wordsJson ? parseWordsData(wordsJson) : null;

  if (parsedWords && parsedWords.cues && parsedWords.cues.length) {
    state.cues = parsedWords.cues.map(cloneCue);
    state.wordsMeta = parsedWords.meta || null;
    state.wordsLoaded = true;
    state.loadedStem = stem;
    if (parsedWords.warnings && parsedWords.warnings.length) {
      setStatus(`Loaded word timings with ${parsedWords.warnings.length} warning(s): ${parsedWords.warnings[0]}`, 'warn');
    } else if (srtCues.length && !cuesMatch(srtCues, state.cues)) {
      setStatus('Loaded word timings. The .srt on disk differs, so saving will regenerate it from these cues.', 'warn');
    } else if (srtCues.length) {
      setStatus('Loaded .srt + word timings.', 'ok');
    } else {
      setStatus('Loaded word timings; no matching .srt on disk.', 'ok');
    }
  } else if (srtCues.length) {
    state.cues = srtCues.map(cloneCue);
    state.wordsLoaded = false;
    state.wordsMeta = null;
    state.loadedStem = stem;
    if (wordsJson) {
      setStatus('Loaded .srt only. The word timing file could not be used and will be replaced on save.', 'warn');
    } else {
      setStatus('Loaded .srt only (no word timings).', 'warn');
    }
  } else if (wordsJson) {
    setStatus('The word timing file is invalid, so nothing was loaded.', 'error');
  } else {
    setStatus('No lyrics found for this song yet. Add cues or import a file, then save.', 'warn');
  }
  state.selectedIndex = state.cues.length ? 0 : -1;
  renderAll();
  loadAudio(`/songs/${encodeURIComponent(song.filename)}`, song.filename);
  updateHeader();
}

function matchSongByStem(stem) {
  return state.songs.find(song => fileStem(song.filename) === stem) || null;
}

async function importSrtFile(file) {
  if (!(await confirmDiscard())) return;
  try {
    const text = await file.text();
    const cues = parseSRT(text);
    if (!cues.length) { setStatus('No cues found in that .srt file.', 'error'); return; }
    state.cues = cues.map(cloneCue);
    state.wordsLoaded = false;
    state.wordsMeta = null;
    state.loadedStem = lyricsStem(file.name);
    state.selectedIndex = 0;
    state.history = [];
    state.dirty = true;
    renderAll();
    const song = matchSongByStem(lyricsStem(file.name));
    if (song) {
      songSelect.value = song.filename;
      state.song = song;
      updateHeader();
      setStatus(`Imported ${cues.length} cues and matched them to ${song.title}. Pick "Audio only" if you want to hear this song.`, 'ok');
    } else {
      state.song = null;
      songSelect.value = '';
      updateHeader();
      setStatus(`Imported ${cues.length} cues. No catalog song matched that filename; pick a destination before saving.`, 'warn');
    }
  } catch (err) {
    setStatus(`Could not read ${file.name}: ${err.message || err}`, 'error');
  }
}

async function importWordsFile(file) {
  if (!(await confirmDiscard())) return;
  try {
    const text = await file.text();
    let parsedJson;
    try { parsedJson = JSON.parse(text); } catch (e) { setStatus(`That file is not valid JSON: ${e.message}`, 'error'); return; }
    const result = parseWordsData(parsedJson);
    if (!result.cues || !result.cues.length) { setStatus(result.error || 'No cues found in that word timing file.', 'error'); return; }
    state.cues = result.cues.map(cloneCue);
    state.wordsLoaded = true;
    state.wordsMeta = result.meta ? deepClone(result.meta) : null;
    if (!state.wordsMeta) state.wordsMeta = { version: 1, song: '', duration: 0, generated_by: 'Imported Sync Editor' };
    state.loadedStem = lyricsStem(file.name);
    state.selectedIndex = 0;
    state.history = [];
    state.dirty = true;
    renderAll();
    if (result.warnings && result.warnings.length) setStatus(`Imported ${result.cues.length} cues with warnings: ${result.warnings[0]}`, 'warn');
    else setStatus(`Imported ${result.cues.length} cues with word timings.`, 'ok');
    const song = matchSongByStem(lyricsStem(file.name));
    if (song) {
      songSelect.value = song.filename;
      state.song = song;
      updateHeader();
    } else {
      state.song = null;
      songSelect.value = '';
      updateHeader();
      setStatus(`Imported ${result.cues.length} cues with word timings. No catalog song matched; pick a destination before saving.`, 'warn');
    }
  } catch (err) {
    setStatus(`Could not read ${file.name}: ${err.message || err}`, 'error');
  }
}

/* ---------- save ---------- */
async function saveLyrics() {
  if (!state.song) { setStatus('Pick a destination song first.', 'error'); return; }
  const { errors } = validateCues();
  if (errors.length) { setStatus(`Fix ${errors.length} validation error(s) before saving.`, 'error'); updateValidationView(); return; }
  if (!state.cues.length) { setStatus('Nothing to save yet.', 'error'); return; }

  const selectedStem = fileStem(state.song.filename);
  if (state.loadedStem && selectedStem !== state.loadedStem) {
    const confirmed = await karaokeConfirm({
      title: 'Save to a different song?',
      message: `These lyrics were loaded for "${state.loadedStem}", but you are about to save them to "${selectedStem}".`,
      confirmLabel: 'Save here',
      tone: 'danger',
      focus: 'cancel'
    });
    if (!confirmed) return;
  }
  if (!state.wordsLoaded && state.song.words) {
    const confirmed = await karaokeConfirm({
      title: 'Remove word timings?',
      message: 'This song currently has word timings, but none are loaded in the editor. Saving will remove them.',
      confirmLabel: 'Save without words',
      tone: 'danger',
      focus: 'cancel'
    });
    if (!confirmed) return;
  }

  const savedSnapshot = JSON.stringify({ cues: state.cues, wordsLoaded: state.wordsLoaded });
  state.saving = true;
  updateSaveButton();
  setStatus('Saving…', 'muted');
  try {
    const body = {
      song_id: state.song.filename,
      lyrics: buildSRT(),
    };
    if (state.wordsLoaded) body.words = buildWordsJSON();
    const response = await fetch('/api/save-lyrics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    if (JSON.stringify({ cues: state.cues, wordsLoaded: state.wordsLoaded }) === savedSnapshot) {
      state.dirty = false;
    }
    state.loadedStem = selectedStem;
    state.song.words = !!data.words;
    state.song.lyrics = true;
    if (data.words) {
      setStatus(`Saved ${data.saved} + ${data.words}`, 'ok');
    } else {
      setStatus(`Saved ${data.saved} (word timings removed)`, 'ok');
    }
    await loadSongOptions(state.song.filename);
    updateHeader();
  } catch (err) {
    setStatus(`Save failed: ${err.message || err}`, 'error');
  } finally {
    state.saving = false;
    updateSaveButton();
  }
}
function buildSRT() {
  return state.cues.map((cue, index) => {
    const text = cue.instrumental ? '[Instrumental]' : (cue.text || '').trim();
    return `${index + 1}\n${formatTime(cue.start)} --> ${formatTime(cue.end)}\n${text}\n`;
  }).join('\n');
}
function buildWordsJSON() {
  const meta = state.wordsMeta || {};
  const data = {
    version: meta.version ?? 1,
    song: state.song ? fileStem(state.song.filename) : (meta.song || ''),
    generated_by: meta.generated_by || 'Sync Editor',
    lines: state.cues.map(cue => ({
      start: roundTime(cue.start),
      end: roundTime(cue.end),
      text: cue.instrumental ? '[Instrumental]' : (cue.text || ''),
      instrumental: cue.instrumental,
      words: cue.instrumental ? [] : cue.words.map(word => ({
        text: word.text,
        start: roundTime(word.start),
        end: roundTime(word.end)
      }))
    }))
  };
  if (Number.isFinite(Number(meta.duration)) && Number(meta.duration) > 0) data.duration = Number(meta.duration);
  else if (state.duration > 0) data.duration = roundTime(state.duration);
  return data;
}

/* ---------- downloads ---------- */
function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type: type || 'text/plain' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
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
  return 'lyrics';
}

/* ---------- keyboard ---------- */
document.addEventListener('keydown', evt => {
  const isInteractive = evt.target instanceof Element &&
    evt.target.closest('button, a, input, textarea, select, [role=\"slider\"]');
  if (isInteractive) return;
  if (evt.code === 'Space') { evt.preventDefault(); if (wavesurfer && state.audioReady) wavesurfer.playPause(); return; }
  if (evt.key === 'ArrowLeft') { evt.preventDefault(); seekTo(currentTime() - 1, false); return; }
  if (evt.key === 'ArrowRight') { evt.preventDefault(); seekTo(currentTime() + 1, false); return; }
  if (evt.key === '[' && state.selectedIndex >= 0) { evt.preventDefault(); nudgeCue(state.selectedIndex, -0.1); return; }
  if (evt.key === ']' && state.selectedIndex >= 0) { evt.preventDefault(); nudgeCue(state.selectedIndex, 0.1); return; }
  if ((evt.key === 's' || evt.key === 'S') && state.selectedIndex >= 0) { evt.preventDefault(); if (requireAudio()) setCueStart(state.selectedIndex, wavesurfer.getCurrentTime()); return; }
  if ((evt.key === 'e' || evt.key === 'E') && state.selectedIndex >= 0) { evt.preventDefault(); if (requireAudio()) setCueEnd(state.selectedIndex, wavesurfer.getCurrentTime()); return; }
  if ((evt.ctrlKey || evt.metaKey) && (evt.key === 'z' || evt.key === 'Z')) { evt.preventDefault(); undo(); return; }
});

/* ---------- event wiring ---------- */
$('loadSongBtn').addEventListener('click', () => {
  const song = state.songs.find(s => s.filename === songSelect.value);
  if (!song) { setStatus('Pick a song first.', 'warn'); return; }
  loadSong(song);
});
$('loadAudioBtn').addEventListener('click', () => {
  const song = state.songs.find(s => s.filename === songSelect.value);
  if (!song) { setStatus('Pick a song first.', 'warn'); return; }
  state.song = song;
  loadAudio(`/songs/${encodeURIComponent(song.filename)}`, song.filename);
  updateHeader();
});
$('reloadSongsBtn').addEventListener('click', () => loadSongOptions(songSelect.value));
songSelect.addEventListener('change', () => {
  const song = state.songs.find(s => s.filename === songSelect.value);
  state.song = song || state.song;
  updateHeader();
  updateSaveButton();
});

$('srtFile').addEventListener('change', evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = '';
  if (file) importSrtFile(file);
});
$('wordsFile').addEventListener('change', evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = '';
  if (file) importWordsFile(file);
});
$('audioFile').addEventListener('change', evt => {
  const file = evt.target.files && evt.target.files[0];
  evt.target.value = '';
  if (!file) return;
  const url = URL.createObjectURL(file);
  loadAudio(url, file.name);
  state.localAudioUrl = url;
  const song = matchSongByStem(lyricsStem(file.name));
  if (song) {
    songSelect.value = song.filename;
    state.song = song;
    updateHeader();
    setStatus(`Loaded local audio for ${file.name} and matched it to ${song.title}.`, 'ok');
  } else {
    setStatus(`Loaded local audio: ${file.name}. Pick a destination song before saving.`, 'muted');
  }
});

$('addCueBtn').addEventListener('click', () => addCue(false));
$('addInstrumentalBtn').addEventListener('click', () => addCue(true));
$('clearCuesBtn').addEventListener('click', clearCues);
toggleWordModeBtn.addEventListener('click', dropWordTimings);
undoBtn.addEventListener('click', undo);
$('rippleToggle').addEventListener('change', evt => { state.ripple = !!evt.target.checked; });
$('followToggle').addEventListener('change', evt => { state.follow = !!evt.target.checked; });
playPauseBtn.addEventListener('click', () => { if (wavesurfer && state.audioReady) wavesurfer.playPause(); else setStatus('Load audio first.', 'warn'); });
$('stopBtn').addEventListener('click', () => { if (wavesurfer && state.audioReady) { try { wavesurfer.stop(); } catch {} } });
$('applyOffsetBtn').addEventListener('click', () => {
  const value = parseFloat($('offsetInput').value);
  if (!Number.isFinite(value)) { setStatus('Offset must be a number.', 'error'); return; }
  applyShift(value);
});
document.querySelectorAll('button[data-shift]').forEach(button => {
  button.addEventListener('click', () => applyShift(parseFloat(button.dataset.shift)));
});
saveBtn.addEventListener('click', saveLyrics);
$('previewSrtBtn').addEventListener('click', () => {
  srtOutput.textContent = buildSRT();
  srtOutput.classList.toggle('d-none');
});
$('downloadSrtBtn').addEventListener('click', () => downloadFile(`${stemForDownload()}.srt`, buildSRT(), 'text/plain'));
$('downloadWordsBtn').addEventListener('click', () => downloadFile(`${stemForDownload()}.words.json`, JSON.stringify(buildWordsJSON(), null, 2) + '\n', 'application/json'));
window.addEventListener('beforeunload', evt => {
  if (!state.dirty) return;
  evt.preventDefault();
  evt.returnValue = '';
});

/* ---------- bootstrap ---------- */
(async function () {
  await loadSongOptions();
  // Auto-load the first song if exactly one is present, which is convenient
  // for a single-song working directory; otherwise leave the choice to the user.
  if (state.songs.length === 1) {
    loadSong(state.songs[0]);
  }
  renderAll();
  setStatus('Select a song and click "Load song", or import local files.', 'muted');
})();
