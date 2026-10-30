const test = require("node:test");
const assert = require("node:assert/strict");
const Reconcile = require("../../static/lyric_reconcile.js");

function line(text, words, start = 0, end = 4) {
  return { start, end, text, instrumental: false, words };
}

function word(text, start, end) {
  return { text, start, end };
}

test("punctuation and capitalization changes keep the same intervals", () => {
  const source = line("Hello world", [
    word("Hello", 0, 1.5),
    word("world", 1.6, 4),
  ]);
  const result = Reconcile.reconcileLine(source, "Hello, World");
  assert.equal(result.status, "ok");
  assert.equal(result.review, null);
  assert.deepEqual(result.words, [
    word("Hello,", 0, 1.5),
    word("World", 1.6, 4),
  ]);
});

test("one-to-one replacement keeps the old interval", () => {
  const source = line("Hello blue world", [
    word("Hello", 0, 1),
    word("blue", 1, 2),
    word("world", 2, 4),
  ]);
  const result = Reconcile.reconcileLine(source, "Hello red world");
  assert.equal(result.status, "ok");
  assert.equal(result.review, null);
  assert.deepEqual(result.words[1], word("red", 1, 2));
  assert.deepEqual(result.words[0], word("Hello", 0, 1));
  assert.deepEqual(result.words[2], word("world", 2, 4));
});

test("insertion preserves anchors and marks review", () => {
  const source = line("never knew", [
    word("never", 0, 1),
    word("knew", 2, 4),
  ]);
  const result = Reconcile.reconcileLine(source, "never ever knew");
  assert.equal(result.status, "ok");
  assert.ok(result.review);
  assert.deepEqual(result.words[0], word("never", 0, 1));
  assert.deepEqual(result.words[2], word("knew", 2, 4));
  assert.equal(result.words[1].text, "ever");
  assert.ok(result.words[1].start >= 1);
  assert.ok(result.words[1].end <= 2);
});

test("deletion preserves the remaining anchors", () => {
  const source = line("one very good two", [
    word("one", 0, 0.5),
    word("very", 0.5, 1),
    word("good", 1, 2),
    word("two", 2, 4),
  ]);
  const result = Reconcile.reconcileLine(source, "one good two");
  assert.equal(result.status, "ok");
  assert.ok(result.review);
  assert.deepEqual(result.words, [
    word("one", 0, 0.5),
    word("good", 1, 2),
    word("two", 2, 4),
  ]);
});

test("repeated words still produce valid non-overlapping timing", () => {
  const source = line("no no", [
    word("no", 0, 1),
    word("no", 1, 2),
  ]);
  const result = Reconcile.reconcileLine(source, "no no no");
  assert.equal(result.status, "ok");
  assert.ok(result.review);
  assert.equal(result.words.length, 3);
  assert.equal(Reconcile.validateWords(result.words, source, "no no no"), true);
});

test("an insertion with no room is unsafe instead of guessed", () => {
  const source = line("hi there", [
    word("hi", 0, 0.1),
    word("there", 0.1, 0.2),
  ], 0, 0.2);
  const result = Reconcile.reconcileLine(source, "hi big there");
  assert.equal(result.status, "unsafe");
  assert.equal(result.reason, "no-room");
});

test("empty text is reported, not silently converted", () => {
  const source = line("hello", [word("hello", 0, 1)]);
  const result = Reconcile.reconcileLine(source, "   ");
  assert.equal(result.status, "empty");
  assert.equal(result.words.length, 1);
});

test("word timing can be created for a line-level line", () => {
  const source = line("one two three", [], 0, 3);
  const result = Reconcile.createWordTimings(source);
  assert.equal(result.status, "ok");
  assert.equal(result.words.length, 3);
  assert.ok(result.review);
  assert.equal(Reconcile.validateWords(result.words, source, source.text), true);
});

test("split works on line-level text without word timing", () => {
  const source = line("one two three four", [], 0, 4);
  const result = Reconcile.splitLine(source, "one two ".length, 2.2);
  assert.equal(result.status, "ok");
  assert.equal(result.first.text, "one two");
  assert.equal(result.second.text, "three four");
  assert.equal(result.first.words.length, 0);
  assert.equal(result.second.words.length, 0);
  assert.equal(result.first.end, 2.2);
  assert.equal(result.second.start, 2.2);
});

test("split at the caret estimate still works without audio", () => {
  const source = line("one two three four", [], 0, 4);
  const result = Reconcile.splitLine(source, "one two ".length, null);
  assert.equal(result.status, "ok");
  assert.equal(result.estimated, true);
  assert.ok(result.first.review);
  assert.ok(result.second.review);
});

test("instrumental break split uses the playhead", () => {
  const source = {
    start: 0,
    end: 4,
    text: "[Instrumental]",
    instrumental: true,
    words: [],
  };
  const result = Reconcile.splitLine(source, 0, 2.5);
  assert.equal(result.status, "ok");
  assert.equal(result.first.end, 2.5);
  assert.equal(result.second.start, 2.5);
  assert.equal(result.first.instrumental, true);
  assert.equal(result.second.instrumental, true);
});

test("split preserves word-timed content at a word boundary", () => {
  const source = line("One two three", [
    word("One", 0, 0.8),
    word("two", 0.9, 2),
    word("three", 2.1, 3),
  ], 0, 3);
  const result = Reconcile.splitLine(source, "One two ".length, 2.05);
  assert.equal(result.status, "ok");
  assert.equal(result.first.text, "One two");
  assert.equal(result.second.text, "three");
  assert.equal(result.first.words.length, 2);
  assert.equal(result.second.words.length, 1);
  assert.equal(result.first.end, result.second.start);
});

test("split inside a word splits its text and timing", () => {
  const source = line("hello world", [
    word("hello", 0, 1),
    word("world", 1.2, 3),
  ], 0, 3);
  const result = Reconcile.splitLine(source, 2, 1.6);
  assert.equal(result.status, "ok");
  assert.equal(result.first.text, "he");
  assert.equal(result.second.text, "llo world");
  assert.equal(result.first.words[0].text, "he");
  assert.equal(result.second.words[0].text, "llo");
  assert.equal(result.first.end, 0.4);
  assert.equal(result.second.start, 0.4);
});

test("mixed merge asks before changing timing", () => {
  const first = line("word line", [word("word", 0, 1), word("line", 1, 2)], 0, 2);
  const second = line("plain line", [], 2.5, 4);
  const result = Reconcile.mergeLines(first, second);
  assert.equal(result.status, "mixed");
});

test("mixed merge can create timing when explicitly chosen", () => {
  const first = line("word line", [word("word", 0, 1), word("line", 1, 2)], 0, 2);
  const second = line("plain line", [], 2.5, 4);
  const result = Reconcile.mergeLines(first, second, "create");
  assert.equal(result.status, "ok");
  assert.equal(result.cue.words.length, 4);
  assert.ok(result.cue.review);
  assert.equal(Reconcile.validateWords(result.cue.words, result.cue, result.cue.text), true);
});

test("merge can explicitly choose line highlighting", () => {
  const first = line("word line", [word("word", 0, 1), word("line", 1, 2)], 0, 2);
  const second = line("plain line", [], 2.5, 4);
  const result = Reconcile.mergeLines(first, second, "line");
  assert.equal(result.status, "ok");
  assert.deepEqual(result.cue.words, []);
  assert.equal(result.cue.text, "word line plain line");
});

test("a split that would leave a tiny line is rejected", () => {
  const source = line("a b", [], 0, 0.1);
  const result = Reconcile.splitLine(source, 1, null);
  assert.equal(result.status, "error");
});
