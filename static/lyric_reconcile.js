/*
 * Text/word-timing reconciliation for the local Lyrics Editor.
 *
 * The editor keeps line text directly editable. When a line has word timings,
 * text changes must keep those timings coherent: matched words keep their
 * intervals, one-to-one replacements keep the old interval, and inserted or
 * deleted words are handled only inside safe spans. The active .words.json
 * schema is unchanged; this module only produces editor cue data.
 *
 * This file has no runtime dependencies and is also loadable by Node's test
 * runner so the reconciliation rules can be unit-tested.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.LyricReconcile = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MIN_WORD_DURATION = 0.08;
  const MIN_LINE_DURATION = 0.12;

  function roundTime(value) {
    return Math.round(Number(value) * 1000) / 1000;
  }

  function normalizeText(text) {
    return String(text ?? "").replace(/\s+/g, " ").trim();
  }

  function tokenize(text) {
    const normalized = normalizeText(text);
    return normalized ? normalized.split(" ") : [];
  }

  function coreKey(token) {
    const text = String(token ?? "");
    let key = text
      .replace(/^[\p{P}\p{S}]+/u, "")
      .replace(/[\p{P}\p{S}]+$/u, "")
      .toLocaleLowerCase();
    if (!key) key = text.toLocaleLowerCase();
    return key;
  }

  function wordsJoin(words) {
    return normalizeText(words.map(word => word.text).join(" "));
  }

  function stableLcs(oldWords, newTokens) {
    const oldCount = oldWords.length;
    const newCount = newTokens.length;
    const table = Array.from(
      { length: oldCount + 1 },
      () => new Array(newCount + 1).fill(0)
    );
    for (let i = oldCount - 1; i >= 0; i -= 1) {
      for (let j = newCount - 1; j >= 0; j -= 1) {
        if (coreKey(oldWords[i].text) === coreKey(newTokens[j])) {
          table[i][j] = table[i + 1][j + 1] + 1;
        } else {
          table[i][j] = Math.max(table[i + 1][j], table[i][j + 1]);
        }
      }
    }
    const anchors = [];
    let i = 0;
    let j = 0;
    while (i < oldCount && j < newCount) {
      if (coreKey(oldWords[i].text) === coreKey(newTokens[j])) {
        anchors.push({ oldIndex: i, newIndex: j });
        i += 1;
        j += 1;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        i += 1;
      } else {
        j += 1;
      }
    }
    return anchors;
  }

  function allocateWords(tokens, start, end) {
    if (!tokens.length) return [];
    const startMs = Math.round(Number(start) * 1000);
    const endMs = Math.round(Number(end) * 1000);
    const spanMs = endMs - startMs;
    const minimumMs = Math.round(MIN_WORD_DURATION * 1000);
    if (!Number.isFinite(spanMs) || spanMs < tokens.length * minimumMs) {
      return null;
    }
    const weights = tokens.map(token => Math.max(1, coreKey(token).length || token.length));
    const totalWeight = weights.reduce((total, weight) => total + weight, 0);
    const distributable = spanMs - tokens.length * minimumMs;
    let cursor = startMs;
    let assignedExtra = 0;
    const words = [];
    tokens.forEach((token, index) => {
      const isLast = index === tokens.length - 1;
      const extra = isLast
        ? distributable - assignedExtra
        : Math.floor((distributable * weights[index]) / totalWeight);
      if (!isLast) assignedExtra += extra;
      const duration = minimumMs + extra;
      words.push({
        text: token,
        start: roundTime(cursor / 1000),
        end: roundTime((cursor + duration) / 1000),
      });
      cursor += duration;
    });
    return words;
  }

  function validateWords(words, line, text) {
    if (!Array.isArray(words) || !words.length) return false;
    if (wordsJoin(words) !== normalizeText(text)) return false;
    let previousEnd = -Infinity;
    for (const word of words) {
      if (!Number.isFinite(word.start) || !Number.isFinite(word.end)) return false;
      if (word.end <= word.start + 0.0005) return false;
      if (word.start < line.start - 0.001) return false;
      if (word.end > line.end + 0.001) return false;
      if (word.start < previousEnd - 0.001) return false;
      if (!normalizeText(word.text)) return false;
      previousEnd = Math.max(previousEnd, word.end);
    }
    return true;
  }

  function gapWords(oldGap, newGap, previousEnd, nextStart) {
    if (!oldGap.length && !newGap.length) {
      return { words: [], review: false };
    }
    if (!newGap.length) {
      return { words: [], review: true, reason: "words-removed" };
    }
    if (oldGap.length === newGap.length) {
      if (oldGap.length === 1) {
        return {
          words: [{ text: newGap[0], start: oldGap[0].start, end: oldGap[0].end }],
          review: false,
        };
      }
      return {
        words: newGap.map((text, index) => ({
          text,
          start: oldGap[index].start,
          end: oldGap[index].end,
        })),
        review: true,
        reason: "words-replaced",
      };
    }
    const spanStart = oldGap.length ? oldGap[0].start : previousEnd;
    const spanEnd = oldGap.length
      ? oldGap[oldGap.length - 1].end
      : nextStart;
    const words = allocateWords(newGap, spanStart, spanEnd);
    if (words === null) return { error: "no-room" };
    return {
      words,
      review: true,
      reason: oldGap.length ? "words-redistributed" : "words-added",
    };
  }

  function reconcileLine(line, newText) {
    const normalized = normalizeText(newText);
    const originalWords = Array.isArray(line.words) ? line.words : [];

    if (line.instrumental) {
      return {
        status: "ok",
        text: "[Instrumental]",
        words: [],
        review: null,
        message: "Instrumental break preserved.",
      };
    }
    if (!normalized) {
      return {
        status: "empty",
        text: "",
        words: originalWords.map(word => ({ ...word })),
        review: line.review || null,
        message: "This line is empty. Type some words or remove the line.",
      };
    }
    if (!originalWords.length) {
      return {
        status: "ok",
        text: normalized,
        words: [],
        review: line.review || null,
        message: "Line text updated. Whole-line highlighting is unchanged.",
      };
    }

    const oldWords = originalWords.map(word => ({
      text: String(word.text ?? ""),
      start: Number(word.start),
      end: Number(word.end),
    }));
    if (!validateWords(oldWords, line, line.text)) {
      return {
        status: "unsafe",
        reason: "invalid-source",
        message: "This line's current word timing is invalid. Fix it before editing the text.",
        text: normalized,
        words: oldWords,
        review: line.review || null,
      };
    }

    const tokens = tokenize(normalized);
    const anchors = stableLcs(oldWords, tokens);
    const words = [];
    let review = false;
    let reason = null;
    let oldPosition = 0;
    let newPosition = 0;

    for (let anchorIndex = 0; anchorIndex <= anchors.length; anchorIndex += 1) {
      const anchor = anchors[anchorIndex] || null;
      const oldEnd = anchor ? anchor.oldIndex : oldWords.length;
      const newEnd = anchor ? anchor.newIndex : tokens.length;
      const oldGap = oldWords.slice(oldPosition, oldEnd);
      const newGap = tokens.slice(newPosition, newEnd);
      const previousEnd = words.length ? words[words.length - 1].end : line.start;
      const nextStart = anchor ? oldWords[anchor.oldIndex].start : line.end;
      const gap = gapWords(oldGap, newGap, previousEnd, nextStart);
      if (gap.error) {
        return {
          status: "unsafe",
          reason: gap.error,
          message: "There isn't enough time to fit the new words without overlapping.",
          text: normalized,
          words: oldWords,
          review: line.review || null,
        };
      }
      words.push(...gap.words);
      if (gap.review) {
        review = true;
        if (!reason) reason = gap.reason;
      }
      if (anchor) {
        words.push({
          text: tokens[anchor.newIndex],
          start: oldWords[anchor.oldIndex].start,
          end: oldWords[anchor.oldIndex].end,
        });
        oldPosition = anchor.oldIndex + 1;
        newPosition = anchor.newIndex + 1;
      }
    }

    if (!validateWords(words, line, normalized)) {
      return {
        status: "unsafe",
        reason: "invalid-result",
        message: "The text change could not be fitted into this line safely.",
        text: normalized,
        words: oldWords,
        review: line.review || null,
      };
    }

    const added = Math.max(0, tokens.length - oldWords.length);
    const removed = Math.max(0, oldWords.length - tokens.length);
    let message = "Timing preserved.";
    if (added) {
      message = `Timing updated automatically: ${added} new ${added === 1 ? "word" : "words"} fitted into this line.`;
    } else if (removed) {
      message = `Timing updated automatically: ${removed} ${removed === 1 ? "word" : "words"} removed from this line.`;
    } else if (review) {
      message = "Word timings were redistributed for this line.";
    }

    return {
      status: "ok",
      text: normalized,
      words,
      review: review ? (reason || "words-changed") : (line.review || null),
      message,
    };
  }

  function createWordTimings(line) {
    const tokens = tokenize(line.text);
    if (!tokens.length) {
      return { status: "error", message: "Type the line first, then add word timing." };
    }
    const words = allocateWords(tokens, line.start, line.end);
    if (words === null) {
      return {
        status: "unsafe",
        message: "This line is too short to fit word-by-word timing. Keep whole-line highlighting or make the line longer.",
      };
    }
    return {
      status: "ok",
      words,
      review: "word-timing-created",
      message: "Word-by-word timing was added for this line only.",
    };
  }

  function splitTextAtCaret(text, caretOffset) {
    const raw = String(text ?? "");
    const caret = Math.max(0, Math.min(raw.length, Number(caretOffset) || 0));
    const left = normalizeText(raw.slice(0, caret));
    const right = normalizeText(raw.slice(caret));
    return { left, right, caret };
  }

  function splitLine(line, caretOffset, playhead) {
    if (line.instrumental) {
      const at = Number(playhead);
      const estimated = !(Number.isFinite(at)
        && at > line.start + MIN_LINE_DURATION
        && at < line.end - MIN_LINE_DURATION);
      const boundary = estimated
        ? roundTime((line.start + line.end) / 2)
        : roundTime(at);
      if (boundary <= line.start + MIN_LINE_DURATION
          || boundary >= line.end - MIN_LINE_DURATION) {
        return { status: "error", message: "This break is too short to split." };
      }
      const review = estimated ? "split-estimated" : (line.review || null);
      return {
        status: "ok",
        first: {
          start: line.start,
          end: boundary,
          text: "[Instrumental]",
          instrumental: true,
          words: [],
          review,
        },
        second: {
          start: boundary,
          end: line.end,
          text: "[Instrumental]",
          instrumental: true,
          words: [],
          review,
        },
        estimated,
        message: estimated
          ? "Instrumental break split at the midpoint. Check the timing."
          : "Instrumental break split at the playhead.",
      };
    }

    const { left, right } = splitTextAtCaret(line.text, caretOffset);
    if (!left || !right) {
      return {
        status: "error",
        message: "Place the cursor between two words before splitting.",
      };
    }

    const words = Array.isArray(line.words) ? line.words : [];
    if (!words.length) {
      const at = Number(playhead);
      const usablePlayhead = Number.isFinite(at)
        && at > line.start + MIN_LINE_DURATION
        && at < line.end - MIN_LINE_DURATION;
      const totalChars = Math.max(1, left.length + right.length);
      const estimated = !usablePlayhead;
      const boundary = estimated
        ? roundTime(line.start + (line.end - line.start) * (left.length / totalChars))
        : roundTime(at);
      if (boundary <= line.start + MIN_LINE_DURATION
          || boundary >= line.end - MIN_LINE_DURATION) {
        return { status: "error", message: "This line is too short to split." };
      }
      const review = estimated ? "split-estimated" : (line.review || null);
      return {
        status: "ok",
        first: {
          start: line.start,
          end: boundary,
          text: left,
          instrumental: false,
          words: [],
          review,
        },
        second: {
          start: boundary,
          end: line.end,
          text: right,
          instrumental: false,
          words: [],
          review,
        },
        estimated,
        message: estimated
          ? "Line split based on the cursor position. Check the timing."
          : "Line split at the playhead.",
      };
    }

    const joined = words.map(word => word.text).join(" ");
    const splitPosition = normalizeText(joined.slice(0, left.length)).length;
    if (!joined || splitPosition <= 0 || splitPosition >= joined.length) {
      return { status: "error", message: "Place the cursor between two words before splitting." };
    }

    let wordIndex = -1;
    let insideWord = false;
    let insideOffset = 0;
    let cursor = 0;
    for (let index = 0; index < words.length; index += 1) {
      const wordText = String(words[index].text ?? "");
      const start = cursor;
      const end = cursor + wordText.length;
      if (splitPosition > start && splitPosition < end) {
        wordIndex = index;
        insideWord = true;
        insideOffset = splitPosition - start;
        break;
      }
      if (splitPosition <= start) {
        wordIndex = index;
        insideWord = false;
        break;
      }
      if (splitPosition === end) {
        wordIndex = index;
        insideWord = false;
        break;
      }
      cursor = end + 1;
    }

    if (wordIndex < 0) {
      return { status: "error", message: "Place the cursor between two words before splitting." };
    }

    let leftWords;
    let rightWords;
    let boundary;
    let estimated = false;
    let sawWordSplit = false;

    if (insideWord) {
      const word = words[wordIndex];
      const offset = Math.max(1, Math.min(String(word.text).length - 1, insideOffset));
      const at = Number(playhead);
      const usablePlayhead = Number.isFinite(at)
        && at > Number(word.start) + MIN_WORD_DURATION
        && at < Number(word.end) - MIN_WORD_DURATION;
      const ratio = offset / Math.max(1, String(word.text).length);
      const splitAt = usablePlayhead
        ? at
        : Number(word.start) + (Number(word.end) - Number(word.start)) * ratio;
      if (splitAt - Number(word.start) < MIN_WORD_DURATION
          || Number(word.end) - splitAt < MIN_WORD_DURATION) {
        return {
          status: "error",
          message: "This word is too short to split. Place the cursor between words instead.",
        };
      }
      boundary = roundTime(splitAt);
      estimated = !usablePlayhead;
      sawWordSplit = true;
      leftWords = words.slice(0, wordIndex).map(item => ({ ...item }));
      rightWords = words.slice(wordIndex + 1).map(item => ({ ...item }));
      leftWords.push({
        text: String(word.text).slice(0, offset),
        start: word.start,
        end: boundary,
      });
      rightWords.unshift({
        text: String(word.text).slice(offset),
        start: boundary,
        end: word.end,
      });
    } else {
      const splitAfter = splitPosition >= cursor + String(words[wordIndex].text).length;
      const leftCount = splitAfter ? wordIndex + 1 : wordIndex;
      leftWords = words.slice(0, leftCount).map(item => ({ ...item }));
      rightWords = words.slice(leftCount).map(item => ({ ...item }));
      if (!leftWords.length || !rightWords.length) {
        return { status: "error", message: "Place the cursor between two words before splitting." };
      }
      const validStart = leftWords[leftWords.length - 1].end;
      const validEnd = rightWords[0].start;
      const at = Number(playhead);
      const usablePlayhead = Number.isFinite(at)
        && at >= validStart - 0.001
        && at <= validEnd + 0.001;
      boundary = roundTime(usablePlayhead ? at : validStart);
      estimated = !usablePlayhead;
    }

    if (boundary <= line.start + MIN_LINE_DURATION
        || boundary >= line.end - MIN_LINE_DURATION) {
      return { status: "error", message: "The split would leave a line too short to use." };
    }

    const firstText = wordsJoin(leftWords);
    const secondText = wordsJoin(rightWords);
    if (firstText !== left || secondText !== right) {
      return { status: "error", message: "The cursor position does not match the line text. Try again." };
    }

    const splitReview = (estimated || sawWordSplit)
      ? "split-needs-review"
      : (line.review || null);
    const first = {
      start: line.start,
      end: boundary,
      text: firstText,
      instrumental: false,
      words: leftWords,
      review: splitReview,
    };
    const second = {
      start: boundary,
      end: line.end,
      text: secondText,
      instrumental: false,
      words: rightWords,
      review: splitReview,
    };
    if (!validateWords(first.words, first, first.text)
        || !validateWords(second.words, second, second.text)) {
      return { status: "error", message: "The split could not preserve word timing safely." };
    }

    return {
      status: "ok",
      first,
      second,
      estimated: estimated || sawWordSplit,
      message: sawWordSplit
        ? "Word timing was split at the cursor. Check the result."
        : (estimated
          ? "Line split at the nearest safe word boundary. Check the timing."
          : "Line split at the playhead."),
    };
  }

  function mergeLines(first, second, mode) {
    if (!first || !second) {
      return { status: "error", message: "Choose two adjacent lines to merge." };
    }
    if (first.instrumental || second.instrumental) {
      if (!first.instrumental || !second.instrumental) {
        return {
          status: "error",
          message: "An instrumental break can only be merged with another instrumental break.",
        };
      }
      return {
        status: "ok",
        cue: {
          start: first.start,
          end: second.end,
          text: "[Instrumental]",
          instrumental: true,
          words: [],
          review: null,
        },
        message: "Instrumental breaks merged.",
      };
    }

    const text = normalizeText(`${first.text} ${second.text}`);
    const firstHasWords = Array.isArray(first.words) && first.words.length > 0;
    const secondHasWords = Array.isArray(second.words) && second.words.length > 0;

    if (firstHasWords !== secondHasWords && mode !== "create" && mode !== "line") {
      return {
        status: "mixed",
        message: "One line has word-by-word timing and the other uses whole-line highlighting.",
      };
    }

    let words = [];
    let review = (first.review || second.review) ? "merge-needs-review" : null;
    if (firstHasWords && secondHasWords) {
      words = first.words.concat(second.words).map(word => ({ ...word }));
    } else if (!firstHasWords && !secondHasWords) {
      words = [];
    } else if (mode === "line") {
      words = [];
      review = null;
    } else {
      const missing = firstHasWords ? second : first;
      const created = createWordTimings(missing);
      if (created.status !== "ok") {
        return {
          status: "error",
          message: created.message || "Could not create word timing for the merged line.",
        };
      }
      const firstWords = firstHasWords ? first.words : created.words;
      words = firstWords.concat(secondHasWords ? second.words : created.words)
        .map(word => ({ ...word }));
      review = "merge-created";
    }

    const cue = {
      start: first.start,
      end: second.end,
      text,
      instrumental: false,
      words,
      review,
    };
    if (words.length && !validateWords(words, cue, text)) {
      return { status: "error", message: "The merged line's word timing could not be preserved safely." };
    }
    return {
      status: "ok",
      cue,
      message: words.length
        ? "Lines merged; word timing preserved."
        : "Lines merged with whole-line highlighting.",
    };
  }

  return {
    MIN_WORD_DURATION,
    MIN_LINE_DURATION,
    normalizeText,
    tokenize,
    coreKey,
    reconcileLine,
    createWordTimings,
    splitLine,
    mergeLines,
    validateWords,
  };
});
