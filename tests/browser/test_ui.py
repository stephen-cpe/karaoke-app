"""Browser-level UI tests for the player, remote, and confirmation dialog."""
from __future__ import annotations

import json
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


def _touch_drag(
        page, start, end, *, hold_ms=550, steps=8, cancel=False, during=None):
    """Send a real touch gesture through Chromium's DevTools protocol."""
    session = page.context.new_cdp_session(page)
    point = {
        "x": start[0],
        "y": start[1],
        "id": 1,
        "radiusX": 2,
        "radiusY": 2,
        "force": 1,
    }
    session.send("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [point],
    })
    page.wait_for_timeout(hold_ms)
    if during is not None:
        during()
        page.wait_for_timeout(200)
    for step in range(1, steps + 1):
        x = start[0] + (end[0] - start[0]) * step / steps
        y = start[1] + (end[1] - start[1]) * step / steps
        session.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{
                "x": x,
                "y": y,
                "id": 1,
                "radiusX": 2,
                "radiusY": 2,
                "force": 1,
            }],
        })
        page.wait_for_timeout(25)
    session.send("Input.dispatchTouchEvent", {
        "type": "touchCancel" if cancel else "touchEnd",
        "touchPoints": [],
    })


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


def test_host_join_qr_matches_visible_url_and_sits_below_note(host_page):
    host_page.wait_for_function(
        "() => document.querySelector('#host-join-qr svg') && "
        "document.querySelector('#host-join-url')?.textContent.startsWith('http')",
        timeout=10000,
    )

    details = host_page.evaluate("""() => {
      const label = document.querySelector('.host-join-label');
      const note = document.querySelector('.host-join-note');
      const qr = document.querySelector('#host-join-qr');
      const row = document.querySelector('.host-join-row');
      const url = document.querySelector('#host-join-url')?.textContent.trim() || '';
      const svg = qr?.querySelector('svg');
      const path = svg?.querySelector('path');
      let matches = false;
      let expectedViewBox = '';
      if (url && window.qrcode) {
        const generated = window.qrcode(0, 'M');
        generated.addData(url);
        generated.make();
        const holder = document.createElement('div');
        holder.innerHTML = generated.createSvgTag({
          cellSize: 8,
          margin: 32,
          scalable: true,
          alt: 'expected QR'
        });
        const expected = holder.querySelector('svg');
        expectedViewBox = expected?.getAttribute('viewBox') || '';
        matches = expected?.querySelector('path')?.getAttribute('d') ===
          path?.getAttribute('d');
      }
      return {
        ordered: label?.nextElementSibling === note &&
          note?.nextElementSibling === qr &&
          qr?.nextElementSibling === row,
        note: note?.textContent.trim() || '',
        url,
        qrUrl: qr?.dataset.joinUrl || '',
        hasSvg: !!svg,
        hasWhiteBackground: svg?.querySelector('rect')?.getAttribute('fill') === 'white',
        quietZone: (path?.getAttribute('d') || '').startsWith('M32,32'),
        viewBox: svg?.getAttribute('viewBox') || '',
        expectedViewBox,
        matches
      };
    }""")

    assert details["ordered"]
    assert details["note"] == (
        "Connect your phone to the same Wi-Fi or network as this computer, "
        "then scan the QR code."
    )
    assert details["hasSvg"]
    assert details["hasWhiteBackground"]
    assert details["quietZone"]
    assert details["viewBox"] == details["expectedViewBox"]
    assert details["url"] == details["qrUrl"]
    assert details["matches"]

    host_page.evaluate("""() => {
      document.querySelector('#host-join-url').textContent = 'Finding this server…';
    }""")
    host_page.wait_for_function(
        """() => {
          const qr = document.querySelector('#host-join-qr');
          return qr?.classList.contains('is-placeholder') && !qr.querySelector('svg');
        }""",
        timeout=5000,
    )

    updated_url = "http://192.0.2.55:5000"
    host_page.evaluate(
        "url => { document.querySelector('#host-join-url').textContent = url; }",
        updated_url,
    )
    host_page.wait_for_function(
        "(url) => document.querySelector('#host-join-qr')?.dataset.joinUrl === url",
        arg=updated_url,
        timeout=5000,
    )
    assert host_page.locator("#host-join-qr svg").count() == 1


def test_remote_touch_long_press_reorders_queue(host_page, browser, live_server):
    _add_songs(live_server, 3)
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
    )
    try:
        remote = _open_remote(context, live_server)
        _wait_for_queue(remote, 2)
        before = _state(live_server)["queue"]
        assert len(before) == 2

        rows = remote.locator("#queue-list .queue-row")
        first = rows.nth(0).bounding_box()
        second = rows.nth(1).bounding_box()
        assert first and second
        def add_song_mid_drag():
            _request(live_server, "/api/control", {
                "action": "add",
                "filename": live_server.songs[0],
            })

        _touch_drag(
            remote,
            (first["x"] + first["width"] / 2, first["y"] + first["height"] / 2),
            (second["x"] + second["width"] / 2, second["y"] + second["height"] - 4),
            during=add_song_mid_drag,
        )

        remote.wait_for_function(
            """([firstTitle, secondTitle]) => {
              const titles = Array.from(
                document.querySelectorAll('#queue-list .queue-row .queue-title'),
                node => node.textContent);
              return titles[0] === secondTitle && titles[1] === firstTitle;
            }""",
            arg=[before[0]["title"], before[1]["title"]],
            timeout=10000,
        )
        after = _state(live_server)["queue"]
        assert [song["filename"] for song in after[:2]] == [
            before[1]["filename"],
            before[0]["filename"],
        ]
        assert len(after) == 3
        assert remote.locator(".queue-drag-ghost").count() == 0
        assert remote.locator(".queue-drop-indicator").count() == 0
    finally:
        context.close()


def test_remote_touch_drag_moves_last_queue_row_to_top(
        host_page, browser, live_server):
    _add_songs(live_server, 3)
    extra_songs = [
        live_server.songs[0],
        live_server.songs[1],
        live_server.songs[2],
        live_server.songs[0],
        live_server.songs[1],
    ]
    for filename in extra_songs:
        _request(live_server, "/api/control", {
            "action": "add",
            "filename": filename,
        })

    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
    )
    try:
        remote = _open_remote(context, live_server)
        _wait_for_queue(remote, 7)
        before = _state(live_server)["queue"]
        assert len(before) == 7

        remote.evaluate(
            "() => document.querySelector('#queue-list').scrollIntoView({ block: 'center' })")
        remote.wait_for_timeout(150)
        rows = remote.locator("#queue-list .queue-row")
        first = rows.nth(0).bounding_box()
        last = rows.nth(6).bounding_box()
        assert first and last

        _touch_drag(
            remote,
            (last["x"] + last["width"] / 2, last["y"] + last["height"] / 2),
            (first["x"] + first["width"] / 2, first["y"] + 4),
        )

        expected_titles = [before[6]["title"]] + [
            song["title"] for song in before[:6]
        ]
        remote.wait_for_function(
            """(expected) => {
              const titles = Array.from(
                document.querySelectorAll('#queue-list .queue-row .queue-title'),
                node => node.textContent);
              return titles.length === expected.length &&
                titles.every((title, index) => title === expected[index]);
            }""",
            arg=expected_titles,
            timeout=10000,
        )
        after = _state(live_server)["queue"]
        assert [song["filename"] for song in after] == [
            before[6]["filename"],
            *[song["filename"] for song in before[:6]],
        ]
    finally:
        context.close()


def test_remote_quick_touch_swipe_does_not_reorder_queue(
        host_page, browser, live_server):
    _add_songs(live_server, 3)
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
    )
    try:
        remote = _open_remote(context, live_server)
        _wait_for_queue(remote, 2)
        before = _state(live_server)["queue"]

        row = remote.locator("#queue-list .queue-row").nth(0).bounding_box()
        assert row
        start = (row["x"] + row["width"] / 2, row["y"] + row["height"] / 2)
        _touch_drag(
            remote,
            start,
            (start[0], start[1] - 120),
            hold_ms=20,
            steps=4,
        )
        remote.wait_for_timeout(500)

        after = _state(live_server)["queue"]
        assert [song["filename"] for song in after] == [
            song["filename"] for song in before
        ]
        assert remote.locator(".queue-drag-ghost").count() == 0
    finally:
        context.close()


def test_remote_touch_drag_feedback_cancels_on_pointer_cancel(
        host_page, browser, live_server):
    _add_songs(live_server, 3)
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
    )
    try:
        remote = _open_remote(context, live_server)
        _wait_for_queue(remote, 2)
        before = _state(live_server)["queue"]

        rows = remote.locator("#queue-list .queue-row")
        first = rows.nth(0).bounding_box()
        second = rows.nth(1).bounding_box()
        assert first and second

        session = context.new_cdp_session(remote)
        start = (first["x"] + first["width"] / 2, first["y"] + first["height"] / 2)
        end = (second["x"] + second["width"] / 2, second["y"] + second["height"] - 4)
        session.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": start[0], "y": start[1], "id": 1}],
        })
        remote.wait_for_timeout(550)
        session.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": end[0], "y": end[1], "id": 1}],
        })
        remote.wait_for_timeout(100)

        assert remote.locator(".queue-drag-ghost").count() == 1
        indicator = remote.locator(".queue-drop-indicator")
        assert indicator.count() == 1
        assert indicator.evaluate("element => !element.hidden")

        session.send("Input.dispatchTouchEvent", {
            "type": "touchCancel",
            "touchPoints": [],
        })
        remote.wait_for_timeout(300)

        assert remote.locator(".queue-drag-ghost").count() == 0
        assert remote.locator(".queue-drop-indicator").count() == 0
        assert remote.evaluate(
            "() => !document.body.classList.contains('is-reordering-queue')")
        after = _state(live_server)["queue"]
        assert [song["filename"] for song in after] == [
            song["filename"] for song in before
        ]
    finally:
        context.close()


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
