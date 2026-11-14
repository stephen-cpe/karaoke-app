const test = require("node:test");
const assert = require("node:assert/strict");
const LyricsDisplay = require("../../static/lyrics_display.js");

test("a line without word timings renders as one fully-sung span", () => {
  const result = LyricsDisplay.linePresentation({
    text: "Hikaru kumo o tsukinuke",
    words: [],
  });
  assert.deepEqual(result, {
    mode: "full-line",
    text: "Hikaru kumo o tsukinuke",
    className: "word sung",
  });
});

test("a line with word timings keeps the word list for the wipe", () => {
  const words = [{ text: "Hello", start: 0, end: 1 }];
  const result = LyricsDisplay.linePresentation({ text: "Hello", words });
  assert.equal(result.mode, "words");
  assert.equal(result.words, words);
});
