"""Browser-level UI tests for the player, remote, and confirmation dialog."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.browser


def _request(live_server, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        live_server.url(path), data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _state(live_server):
    return _request(live_server, "/api/state")["state"]


def _add_songs(live_server, count=2):
    for filename in live_server.songs[:count]:
        _request(live_server, "/api/control", {
            "action": "add",
            "filename": filename,
        })


def _open_remote(browser_context, live_server):
    page = browser_context.new_page()
    page.goto(live_server.url("/remote"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#connection-text')?.textContent === 'Player online'",
        timeout=10000,
    )
    return page


def _wait_for_queue(page, count):
    page.wait_for_function(
        "(expected) => document.querySelector('#queue-tab-count')?.textContent === String(expected)",
        arg=count,
        timeout=10000,
    )


@pytest.fixture
def host_page(browser_context, live_server):
    page = browser_context.new_page()
    page.goto(live_server.url("/player"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#host-status-text')?.textContent.includes('Player online')",
        timeout=15000,
    )
    try:
        yield page
    finally:
        page.close()


def test_host_page_loads_library_and_omits_redundant_heading_note(host_page, live_server):
    host_page.wait_for_function(
        "(expected) => document.querySelectorAll('#song-list-content .song-row').length === expected",
        arg=len(live_server.songs),
        timeout=10000,
    )
    assert host_page.locator(".heading-note").count() == 0


def test_remote_clear_queue_confirmation_preserves_current_song(
        host_page, browser_context, live_server):
    _add_songs(live_server, 2)
    remote = _open_remote(browser_context, live_server)
    _wait_for_queue(remote, 1)
    remote.wait_for_function(
        "() => { const button = document.querySelector('#clear-queue'); "
        "return button && !button.disabled; }",
        timeout=10000,
    )

    remote.click("#clear-queue")
    dialog = remote.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert dialog.locator(".confirm-dialog-title").inner_text() == "Clear the queue?"
    assert "Remove 1 queued song" in dialog.locator(".confirm-dialog-message").inner_text()
    assert dialog.evaluate("el => el.classList.contains('is-destructive')")
    assert remote.evaluate(
        "() => document.activeElement.classList.contains('confirm-dialog-cancel')")

    # Tab wrapping: Shift+Tab from Cancel moves to the destructive action,
    # and Tab from there wraps back to Cancel.
    remote.keyboard.press("Shift+Tab")
    assert remote.evaluate(
        "() => document.activeElement.classList.contains('confirm-dialog-confirm')")
    remote.keyboard.press("Tab")
    assert remote.evaluate(
        "() => document.activeElement.classList.contains('confirm-dialog-cancel')")

    remote.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert remote.evaluate("() => document.activeElement.id") == "clear-queue"
    state = _state(live_server)
    assert state["current"] is not None
    assert len(state["queue"]) == 1

    remote.click("#clear-queue")
    dialog.wait_for(state="visible")
    remote.click(".confirm-dialog-confirm")
    _wait_for_queue(remote, 0)
    state = _state(live_server)
    assert state["current"] is not None
    assert state["queue"] == []
    remote.close()


def test_remote_stop_and_clear_confirmation_clears_current_and_queue(
        host_page, browser_context, live_server):
    _add_songs(live_server, 2)
    remote = _open_remote(browser_context, live_server)
    _wait_for_queue(remote, 1)
    remote.wait_for_function(
        "() => { const button = document.querySelector('#stop-clear'); "
        "return button && !button.disabled; }",
        timeout=10000,
    )

    remote.click("#stop-clear")
    dialog = remote.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert dialog.locator(".confirm-dialog-title").inner_text() == "Stop and clear?"
    remote.click(".confirm-dialog-confirm")
    remote.wait_for_function(
        "() => document.querySelector('#now-title')?.textContent.trim() === 'Choose a song'",
        timeout=10000,
    )
    state = _state(live_server)
    assert state["current"] is None
    assert state["queue"] == []
    remote.close()


def test_host_clear_queue_confirmation_clears_only_the_queue(host_page, live_server):
    _add_songs(live_server, 2)
    host_page.wait_for_function(
        "() => document.querySelector('#queue-count')?.textContent === '1'",
        timeout=10000,
    )
    host_page.wait_for_function(
        "() => { const button = document.querySelector('#queue-clear'); "
        "return button && !button.hidden; }",
        timeout=10000,
    )

    host_page.click("#queue-clear")
    dialog = host_page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert dialog.locator(".confirm-dialog-title").inner_text() == "Clear the queue?"
    assert host_page.evaluate(
        "() => document.activeElement.classList.contains('confirm-dialog-cancel')")
    host_page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")

    state = _state(live_server)
    assert state["current"] is not None
    assert len(state["queue"]) == 1

    host_page.click("#queue-clear")
    dialog.wait_for(state="visible")
    host_page.click(".confirm-dialog-confirm")
    host_page.wait_for_function(
        "() => document.querySelector('#queue-count')?.textContent === '0'",
        timeout=10000,
    )
    state = _state(live_server)
    assert state["current"] is not None
    assert state["queue"] == []


def test_confirmation_dialog_respects_reduced_motion(browser, live_server):
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        page.goto(live_server.url("/remote"), wait_until="networkidle")
        page.wait_for_function(
            "() => document.querySelector('#connection-text') !== null",
            timeout=10000,
        )
        _request(live_server, "/api/control", {
            "action": "add",
            "filename": live_server.songs[0],
        })
        page.wait_for_function(
            "() => document.querySelector('#queue-tab-count')?.textContent === '1'",
            timeout=10000,
        )
        page.click("#stop-clear")
        dialog = page.locator("dialog.confirm-dialog")
        dialog.wait_for(state="visible")
        assert dialog.evaluate(
            "el => getComputedStyle(el).animationName") == "none"
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
    finally:
        context.close()


def test_host_player_parses_unnumbered_dot_decimal_srt(host_page):
    parsed = host_page.evaluate("""() => {
      parseAndSyncLyrics(
        "00:00:01.000 --> 00:00:02.500\\nHello dotted world");
      return lyricsTimeline.map(line => ({
        start: line.start,
        end: line.end,
        text: line.text
      }));
    }""")

    assert parsed == [{
        "start": 1.0,
        "end": 2.5,
        "text": "Hello dotted world",
    }]


def test_sync_editor_save_roundtrip(browser_context, live_server, add_lyrics):
    stem = live_server.songs[0].rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nHello from the test\n",
    )

    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", live_server.songs[0])
    page.click("#loadSongBtn")
    page.wait_for_selector("#cues .cue-row", timeout=15000)
    page.locator("#cues .cue-row").first.locator(
        "button", has_text="+0.1").click()

    page.click("#saveBtn")
    page.wait_for_function(
        "() => document.querySelector('#saveStatus')?.textContent.toLowerCase().includes('saved')",
        timeout=15000,
    )

    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", live_server.songs[0])
    page.click("#loadSongBtn")
    page.wait_for_selector("#cues .cue-row", timeout=15000)

    assert page.locator("#cues .cue-start").first.input_value() == "00:00:00,100"
    page.close()


def test_sync_editor_draft_is_not_live_until_approved(browser_context, live_server):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]

    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", filename)
    page.click("#loadSongBtn")
    page.wait_for_function(
        "() => document.querySelector('#audioStatus')?.textContent.startsWith('Loaded')",
        timeout=15000,
    )
    page.click("#addCueBtn")
    page.locator("#cues .cue-text").first.fill("Draft only lyric")

    page.evaluate("() => saveDraft({ autosave: true })")
    page.wait_for_function(
        "() => document.querySelector('#saveStatus')?.textContent.toLowerCase().includes('autosaved')",
        timeout=15000,
    )
    assert "draft" in page.locator("#loadBadge").inner_text().lower()
    assert page.request.get(
        live_server.url(f"/lyrics/{stem}.srt")).status == 404

    page.click("#approveBtn")
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    page.click(".confirm-dialog-confirm")
    page.wait_for_function(
        "() => document.querySelector('#saveStatus')?.textContent.includes('Published')",
        timeout=15000,
    )

    response = page.request.get(live_server.url(f"/lyrics/{stem}.srt"))
    assert response.status == 200
    assert "Draft only lyric" in response.text()
    page.close()


def test_sync_editor_word_editing_and_mixed_cues(browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    words = {
        "lines": [
            {
                "start": 0,
                "end": 2,
                "text": "First line",
                "instrumental": False,
                "words": [
                    {"text": "First", "start": 0, "end": 0.8},
                    {"text": "line", "start": 0.9, "end": 2},
                ],
            },
            {
                "start": 3,
                "end": 5,
                "text": "Second line",
                "instrumental": False,
                "words": [
                    {"text": "Second", "start": 3, "end": 4},
                    {"text": "line", "start": 4.1, "end": 5},
                ],
            },
        ],
    }
    add_lyrics(stem + ".words.json", json.dumps(words))

    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", filename)
    page.click("#loadSongBtn")
    page.wait_for_selector("#cues .cue-row", timeout=15000)
    page.select_option("#rateSelect", "0.5")
    page.click("#zoomInBtn")
    page.click("#loopCueBtn")
    assert page.locator("#loopCueBtn").inner_text() == "Stop loop"
    page.click("#loopCueBtn")
    assert page.locator("#loopCueBtn").inner_text() == "Loop cue"
    page.locator("#cues .cue-row").first.locator(".word-chip").first.click()
    assert "First" in page.locator("#wordInspector").inner_text()

    before = page.locator("#wordInspector").inner_text()
    page.locator("#wordInspector button", has_text="+0.1").click()
    after = page.locator("#wordInspector").inner_text()
    assert after != before

    page.evaluate("() => wavesurfer.setTime(2.5)")
    page.click("#addCueBtn")
    assert page.locator("#cues .cue-row").count() == 3
    page.locator("#cues .cue-row.selected .cue-text").fill("New line-level cue")
    page.wait_for_function(
        "() => { const b = document.querySelector('#saveBtn'); return b && !b.disabled; }",
        timeout=10000,
    )

    page.click("#saveBtn")
    page.wait_for_function(
        "() => document.querySelector('#saveStatus')?.textContent.toLowerCase().includes('saved')",
        timeout=15000,
    )

    draft = page.request.get(
        live_server.url(
            f"/api/editor/draft?song_id={urllib.parse.quote(filename)}")).json()
    assert draft["source"] == "draft"
    assert len(draft["words"]["lines"]) == 3
    new_line = next(
        line for line in draft["words"]["lines"]
        if line["text"] == "New line-level cue")
    assert new_line["words"] == []

    unlock = page.locator("#cues .cue-row").first.locator(
        "button", has_text="Unlock")
    unlock.click()
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    page.click(".confirm-dialog-confirm")
    assert page.locator("#cues .cue-row").nth(0).locator(".word-chip").count() == 0
    assert page.locator("#cues .cue-row").nth(2).locator(".word-chip").count() > 0

    page.click("#undoBtn")
    assert page.locator("#cues .cue-row").nth(0).locator(".word-chip").count() > 0
    page.click("#redoBtn")
    assert page.locator("#cues .cue-row").nth(0).locator(".word-chip").count() == 0
    page.close()


def test_sync_editor_splits_and_merges_word_cues(browser_context, live_server, add_lyrics):
    filename = live_server.songs[0]
    stem = filename.rsplit(".", 1)[0]
    words = {
        "lines": [{
            "start": 0,
            "end": 3,
            "text": "One two three",
            "instrumental": False,
            "words": [
                {"text": "One", "start": 0, "end": 0.8},
                {"text": "two", "start": 0.9, "end": 2},
                {"text": "three", "start": 2.1, "end": 3},
            ],
        }],
    }
    add_lyrics(stem + ".words.json", json.dumps(words))

    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", filename)
    page.click("#loadSongBtn")
    page.wait_for_selector("#cues .cue-row", timeout=15000)
    page.wait_for_function(
        "() => document.querySelector('#audioStatus')?.textContent.startsWith('Loaded')",
        timeout=15000,
    )
    page.evaluate("() => wavesurfer.setTime(0.85)")
    page.locator("#cues .cue-row").first.locator(
        "button", has_text="Split").click()
    assert page.locator("#cues .cue-row").count() == 2
    page.locator("#cues .cue-row").first.locator(
        "button", has_text="Merge").click()
    assert page.locator("#cues .cue-row").count() == 1
    page.close()


def test_sync_editor_confirmation_dialogs(browser_context, live_server, add_lyrics):
    stem = live_server.songs[0].rsplit(".", 1)[0]
    add_lyrics(
        stem + ".srt",
        "1\n00:00:00,000 --> 00:00:02,000\nHello from the test\n",
    )

    page = browser_context.new_page()
    page.goto(live_server.url("/sync_editor.html"), wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#songSelect option').length > 1",
        timeout=10000,
    )
    page.select_option("#songSelect", live_server.songs[0])
    page.click("#loadSongBtn")
    page.wait_for_selector("#cues .cue-row", timeout=15000)
    assert page.locator("#cues .cue-row").count() == 1

    # Dirty the editor, then attempt to load another song.
    page.locator("#cues .cue-row").first.locator("button", has_text="+0.1").click()
    page.select_option("#songSelect", live_server.songs[1])
    page.click("#loadSongBtn")
    dialog = page.locator("dialog.confirm-dialog")
    dialog.wait_for(state="visible")
    assert dialog.locator(".confirm-dialog-title").inner_text() == "Discard unsaved changes?"
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert page.locator("#cues .cue-row").count() == 1
    page.select_option("#songSelect", live_server.songs[0])

    # Deleting a cue uses the same themed confirmation dialog.
    delete_button = page.locator("#cues .cue-row").first.locator(
        "button", has_text="Delete")
    delete_button.click()
    dialog.wait_for(state="visible")
    assert dialog.locator(".confirm-dialog-title").inner_text() == "Delete cue 1?"
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert page.locator("#cues .cue-row").count() == 1

    delete_button.click()
    dialog.wait_for(state="visible")
    page.click(".confirm-dialog-confirm")
    page.wait_for_function(
        "() => document.querySelectorAll('#cues .cue-row').length === 0",
        timeout=10000,
    )
    page.close()
