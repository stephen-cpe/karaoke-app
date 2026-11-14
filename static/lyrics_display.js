/*
 * Player lyric presentation shared by the TV and remote pages.
 *
 * A line with word timings renders one span per word so it can be wiped. A
 * line without word timings renders as one fully-sung span for its whole cue;
 * guessing a wipe would imply timing the pipeline does not have.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.LyricsDisplay = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function linePresentation(line) {
    const text = String(line && line.text ? line.text : "");
    const words = line && Array.isArray(line.words) ? line.words : [];
    if (!words.length) {
      return { mode: "full-line", text, className: "word sung" };
    }
    return { mode: "words", words };
  }

  return { linePresentation };
});
