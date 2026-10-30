"""Browser tests for the novice-first Lyrics Editor.

These tests use the same real threaded Flask server as the rest of the browser
suite. They intentionally exercise the user-facing labels and flows rather than
implementation-only hooks.
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

pytestmark = pytest.mark.browser


def _words_payload(lines):
    return json.dumps({"lines": lines})


def _line(text, start, end, words=None):
    return {
        "start": start,
        "end": end,
        "text": text,
        "instrumental": False,
        "words": words or [],
    }


def _word(text, start, end):
    return {"text": text, "start": start, "end": end}


def _open_editor(page, live_server, filename, wait_audio=True):
    page.goto(
        live_server.url(
            "/sync_editor.html?song=" + urllib.parse.quote(filename)),
        wait_until="networkidle",
    )
    page.wait_for_function(
        "() => document.querySelector('#workspace')"
        " && !document.querySelector('#workspace').classList.contains('d-none')",
        timeout=15000,
    )
    page.wait_for_function(
        "(filename) => state.song && state.song.filename === filename",
        arg=filename,
        timeout=15000,
    )
    if wait_audio:
        page.wait_for_function("() => state.audioReady === true", timeout=15000)


def _select_line(page, index):
    page.locator("#cues .lyric-row").nth(index).locator(".lyric-number").click()
    page.wait_for_function(
        "(index) => state.selectedIndex === index", arg=index, timeout=5000)


def _line_textarea(page, index):
    return page.locator("#cues .lyric-row").nth(index).locator(
        "textarea.lyric-text")


def _commit_text(page, index, text):
    textarea = _line_textarea(page, index)
    textarea.fill(text)
    textarea.evaluate("element => element.blur()")
    page.wait_for_function(
        "(index) => !state.cues[index].needsReconcile",
        arg=index,
        timeout=5000,
    )


def _confirm(page):
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    page.click(".confirm-dialog-confirm")
    dialog.wait_for(state="hidden")


def _reconcile_dialog(page):
    dialog = page.locator("#reconcileDialog")
    dialog.wait_for(state="visible")
    return dialog


def test_editor_without_a_song_shows_the_picker(
        browser_context, live_server):
    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_selector("#emptyState:not(.d-none)", timeout=10000)
    assert page.locator("#workspace").is_hidden()
    assert page.locator("#emptyState h2").inner_text() == "Choose a song to edit"
    assert page.locator("#songSelect option").count() > 1
    page.close()


def test_default_is_text_first_and_song_action_opens_the_song(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nHello from the test\n",
    )

    page = browser_context.new_page()
    page.goto(live_server.url("/player"), wait_until="networkidle")
    page.wait_for_selector(".song-row", timeout=10000)
    edit_link = page.locator(".song-row").first.locator(".edit-lyrics-button")
    assert edit_link.count() == 1
    assert "?song=" in edit_link.get_attribute("href")

    edit_link.click()
    page.wait_for_url("**/sync_editor.html?song=*", timeout=10000)
    page.wait_for_selector("#cues .lyric-row", timeout=15000)
    page.wait_for_function("() => state.song && state.audioReady", timeout=15000)

    assert page.locator("h1").inner_text() == "Lyrics Editor"
    assert page.locator("#statusChip").inner_text() == "Published · live"
    assert page.locator("#cues textarea.lyric-text").first.is_editable()
    assert page.locator("#cues .cue-start").count() == 0
    assert page.locator("#cues .word-chip").count() == 0
    assert page.locator("#rippleToggle").is_visible() is False
    assert page.get_by_text("Preview SRT", exact=True).is_visible() is False
    assert page.locator("#srtOutput").is_hidden()
    assert "Drop all word timings" not in page.content()
    page.click("#moreMenu summary")
    page.click("#previewSrtBtn")
    assert page.locator("#srtOutput").is_visible()
    assert "-->" in page.locator("#srtOutput").inner_text()
    page.close()


def test_typo_on_word_timed_line_preserves_timing_without_unlock(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("Walk away", 0, 2, [
            _word("Walk", 0, 0.9),
            _word("away", 1.0, 2.0),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    before = page.evaluate("() => state.cues[0].words")
    _commit_text(page, 0, "Walk home")
    after = page.evaluate("() => state.cues[0].words")

    assert page.evaluate("() => state.cues[0].text") == "Walk home"
    assert after[1]["text"] == "home"
    assert after[1]["start"] == before[1]["start"]
    assert after[1]["end"] == before[1]["end"]
    assert page.evaluate("() => state.cues[0].review") is None
    assert page.locator("button", has_text="Unlock").count() == 0
    page.close()


def test_insert_and_delete_preserve_anchors_and_mark_review(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("one two three", 0, 6, [
            _word("one", 0, 1),
            _word("two", 1.1, 2),
            _word("three", 5, 6),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _commit_text(page, 0, "one two and three")
    inserted = page.evaluate("() => state.cues[0].words")
    assert [word["text"] for word in inserted] == ["one", "two", "and", "three"]
    assert inserted[0]["start"] == 0
    assert inserted[1]["start"] == 1.1
    assert inserted[3]["start"] == 5
    assert page.evaluate("() => state.cues[0].review") is not None
    assert "updated automatically" in page.locator(
        "#cues .reconcile-notice").inner_text()
    assert page.locator("#cues .review-chip").count() == 1

    _commit_text(page, 0, "one and three")
    deleted = page.evaluate("() => state.cues[0].words")
    assert [word["text"] for word in deleted] == ["one", "and", "three"]
    assert deleted[0]["start"] == 0
    assert deleted[2]["start"] == 5
    assert page.evaluate("() => state.cues[0].review") is not None
    page.close()


def test_unsafe_reconciliation_offers_cancel_spread_and_line_highlighting(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("one two", 0, 1, [
            _word("one", 0, 0.5),
            _word("two", 0.5, 1),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)

    textarea = _line_textarea(page, 0)
    textarea.fill("one big two")
    textarea.evaluate("element => element.blur()")
    dialog = _reconcile_dialog(page)
    assert "fit the new words" in dialog.inner_text()
    page.click("#reconcileCancelBtn")
    dialog.wait_for(state="hidden")
    assert page.evaluate("() => state.cues[0].text") == "one two"
    assert len(page.evaluate("() => state.cues[0].words")) == 2
    assert page.evaluate("() => state.dirty") is False
    assert page.locator("#statusChip").inner_text() == "Published · live"

    textarea = _line_textarea(page, 0)
    textarea.fill("one big two")
    textarea.evaluate("element => element.blur()")
    dialog = _reconcile_dialog(page)
    page.check('input[name="reconcileChoice"][value="spread"]')
    page.click("#reconcileApplyBtn")
    dialog.wait_for(state="hidden")
    assert page.evaluate("() => state.cues[0].text") == "one big two"
    assert len(page.evaluate("() => state.cues[0].words")) == 3
    assert page.evaluate("() => state.cues[0].review") is not None

    page.click("#undoBtn")
    page.wait_for_function("() => state.cues[0].text === 'one two'")

    textarea = _line_textarea(page, 0)
    textarea.fill("one big two")
    textarea.evaluate("element => element.blur()")
    dialog = _reconcile_dialog(page)
    page.check('input[name="reconcileChoice"][value="line"]')
    page.click("#reconcileApplyBtn")
    dialog.wait_for(state="hidden")
    assert page.evaluate("() => state.cues[0].text") == "one big two"
    assert page.evaluate("() => state.cues[0].words") == []
    page.close()


def test_empty_line_blocks_saving_and_publishing(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nA lyric line\n",
    )

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    textarea = _line_textarea(page, 0)
    textarea.fill("")
    textarea.evaluate("element => element.blur()")
    page.wait_for_function("() => state.validationErrors.length > 0")
    assert page.locator("#saveBtn").is_disabled()
    assert page.locator("#publishBtn").is_disabled()
    assert "empty" in page.locator("#validation").inner_text()
    page.close()


def test_split_line_level_text_without_word_timing(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:04,000\none two three four\n",
    )

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    page.evaluate("() => wavesurfer.setTime(2.2)")
    textarea = _line_textarea(page, 0)
    textarea.evaluate(
        "element => { element.focus(); element.setSelectionRange(8, 8); }")
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Split line").click()
    page.wait_for_function("() => state.cues.length === 2")
    assert page.locator("#cues .lyric-row").count() == 2
    assert page.evaluate("() => state.cues[0].text") == "one two"
    assert page.evaluate("() => state.cues[1].text") == "three four"
    assert page.evaluate("() => state.cues[0].end") == 2.2
    assert page.evaluate("() => state.cues[1].start") == 2.2
    assert page.evaluate("() => state.cues[0].words") == []
    assert page.evaluate("() => state.cues[1].words") == []
    page.close()


def test_add_remove_and_merge_line_level_lines(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nFirst line\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nSecond line\n",
    )

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Add line below").click()
    page.wait_for_function("() => state.cues.length === 3")
    _commit_text(page, 1, "Middle line")
    assert page.evaluate("() => state.cues[1].text") == "Middle line"

    _select_line(page, 1)
    page.locator("#cues .lyric-row.selected summary",
                 has_text="Line actions").click()
    page.locator("#cues .lyric-row.selected button",
                 has_text="Remove line").click()
    _confirm(page)
    page.wait_for_function("() => state.cues.length === 2")
    assert [cue["text"] for cue in page.evaluate("() => state.cues")] == [
        "First line", "Second line"]

    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Merge with next").click()
    page.wait_for_function("() => state.cues.length === 1")
    assert page.evaluate("() => state.cues[0].text") == "First line Second line"
    page.close()


def test_add_line_level_line_while_word_timing_exists(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("One two", 0, 2, [_word("One", 0, 0.9), _word("two", 1, 2)]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Add line below").click()
    page.wait_for_function("() => state.cues.length === 2")
    _commit_text(page, 1, "New line")
    assert len(page.evaluate("() => state.cues[0].words")) == 2
    assert page.evaluate("() => state.cues[1].words") == []

    page.click("#saveBtn")
    page.wait_for_function(
        "() => document.querySelector('#statusChip').textContent"
        ".includes('Draft')", timeout=15000)
    draft = page.request.get(
        live_server.url(
            f"/api/editor/draft?song_id={urllib.parse.quote(filename)}")).json()
    assert draft["words"]["lines"][1]["text"] == "New line"
    assert draft["words"]["lines"][1]["words"] == []
    page.close()


def test_split_and_merge_word_timed_line(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("One two three", 0, 3, [
            _word("One", 0, 0.8),
            _word("two", 0.9, 2),
            _word("three", 2.1, 3),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    page.evaluate("() => wavesurfer.setTime(2.05)")
    textarea = _line_textarea(page, 0)
    textarea.evaluate(
        "element => { element.focus(); element.setSelectionRange(7, 7); }")
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Split line").click()
    page.wait_for_function("() => state.cues.length === 2")
    assert page.evaluate("() => state.cues[0].text") == "One two"
    assert page.evaluate("() => state.cues[1].text") == "three"
    assert len(page.evaluate("() => state.cues[0].words")) == 2
    assert len(page.evaluate("() => state.cues[1].words")) == 1

    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Merge with next").click()
    page.wait_for_function("() => state.cues.length === 1")
    assert page.evaluate("() => state.cues[0].text") == "One two three"
    assert len(page.evaluate("() => state.cues[0].words")) == 3
    page.close()


def test_mixed_merge_asks_before_changing_timing(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("One two", 0, 2, [_word("One", 0, 0.9), _word("two", 1, 2)]),
        _line("Three four", 3, 5, []),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Merge with next").click()
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert "Create word timing" in dialog.locator(
        ".confirm-dialog-title").inner_text()
    page.click(".confirm-dialog-confirm")
    page.wait_for_function("() => state.cues.length === 1")
    assert len(page.evaluate("() => state.cues[0].words")) == 4
    assert page.evaluate("() => state.cues[0].review") is not None
    page.close()


def test_line_timing_whole_song_shift_and_overlap_prompt(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nFirst line\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nSecond line\n",
    )

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    page.click("#modeTimingBtn")
    page.wait_for_selector("#timingPanel:not(.d-none)", timeout=5000)
    _select_line(page, 0)
    page.locator("#cues .lyric-row.selected .line-actions button",
                 has_text="Later 0.1s").click()
    page.wait_for_function("() => state.cues[0].start === 0.1")

    page.evaluate("() => { nudgeCue(0, 3); }")
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert "room before the next line" in dialog.locator(
        ".confirm-dialog-title").inner_text()
    page.click(".confirm-dialog-cancel")
    dialog.wait_for(state="hidden")
    assert page.evaluate("() => state.cues[0].start") == 0.1

    page.evaluate("() => { nudgeCue(0, 3); }")
    dialog.wait_for(state="visible")
    page.click(".confirm-dialog-confirm")
    page.wait_for_function("() => state.cues[0].start === 3.1")
    assert page.evaluate("() => state.cues[1].start") == 6

    before = page.evaluate("() => state.cues.map(cue => cue.start)")
    page.locator('button[data-shift="0.1"]').click()
    page.wait_for_function(
        "(before) => state.cues.every((cue, index) => "
        "Math.abs(cue.start - (before[index] + 0.1)) < 0.001)",
        arg=before,
    )
    shifted = page.evaluate("() => state.cues.map(cue => cue.start)")
    page.evaluate("() => { applyShift(-100); }")
    assert page.evaluate("() => state.cues.map(cue => cue.start)") == shifted
    assert "before the song begins" in page.locator("#saveStatus").inner_text()
    page.close()


def test_fine_tune_one_word_and_stop_word_highlighting_on_one_line(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("One two", 0, 3, [_word("One", 0, 1), _word("two", 1.5, 2.5)]),
        _line("Three four", 4, 6, [
            _word("Three", 4, 5),
            _word("four", 5, 6),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    page.click("#modeWordsBtn")
    page.wait_for_selector("#wordPanel:not(.d-none)", timeout=5000)
    page.locator("#wordChips .word-chip").first.click()
    before_first = page.evaluate("() => state.cues[0].words[0]")
    before_second = page.evaluate("() => state.cues[1].words")
    page.locator("#wordInspector button", has_text="+0.1s").click()
    page.wait_for_function(
        "() => Math.abs(state.cues[0].words[0].start - 0.1) < 0.001")
    assert page.evaluate("() => state.cues[1].words") == before_second
    assert page.evaluate("() => state.cues[0].words[0].end") > before_first["end"]

    page.click("#wordMoreMenu summary")
    page.click("#stopWordTimingBtn")
    _confirm(page)
    page.wait_for_function("() => state.cues[0].words.length === 0")
    assert len(page.evaluate("() => state.cues[1].words")) == 2

    page.click("#undoBtn")
    page.wait_for_function("() => state.cues[0].words.length === 2")
    page.close()


def test_draft_reload_publish_and_review_confirmation(
        browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    add_lyrics(stem + ".words.json", _words_payload([
        _line("one two three", 0, 6, [
            _word("one", 0, 1),
            _word("two", 1.1, 2),
            _word("three", 5, 6),
        ]),
    ]))

    page = browser_context.new_page()
    _open_editor(page, live_server, filename)
    _commit_text(page, 0, "one two and three")
    assert page.evaluate("() => state.cues[0].review") is not None
    assert "newer edits" in page.locator("#statusChip").inner_text()

    page.click("#saveBtn")
    page.wait_for_function(
        "() => document.querySelector('#statusChip').textContent"
        ".includes('Draft')", timeout=15000)
    assert "not live" in page.locator("#saveStatus").inner_text()
    draft = page.request.get(
        live_server.url(
            f"/api/editor/draft?song_id={urllib.parse.quote(filename)}")).json()
    assert draft["source"] == "draft"
    assert draft["review"] == [{"line": 1, "reason": "words-added"}]
    assert page.request.get(
        live_server.url(f"/lyrics/{stem}.srt")).status == 404

    _open_editor(page, live_server, filename)
    page.wait_for_function(
        "() => state.cues[0].text === 'one two and three'", timeout=15000)
    assert page.locator("#statusChip").inner_text().startswith("Draft")
    assert page.locator("#cues .review-chip").count() == 1

    page.click("#publishBtn")
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    message = dialog.locator(".confirm-dialog-message").inner_text()
    assert "timing check" in message
    assert "Line 1" in message or "1" in message
    page.click(".confirm-dialog-confirm")
    page.wait_for_function(
        "() => document.querySelector('#statusChip').textContent"
        ".includes('Published')", timeout=15000)

    response = page.request.get(live_server.url(f"/lyrics/{stem}.srt"))
    assert response.status == 200
    assert "one two and three" in response.text()
    active_words = json.loads(
        page.request.get(
            live_server.url(f"/lyrics/{stem}.words.json")).text())
    assert len(active_words["lines"][0]["words"]) == 4
    assert "review" not in active_words
    after = page.request.get(
        live_server.url(
            f"/api/editor/draft?song_id={urllib.parse.quote(filename)}")).json()
    assert after["draft_revision"] is None
    page.close()
