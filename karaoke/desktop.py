"""Lightweight Windows desktop shell for KARAOKE APP.

The browser application remains a Flask/Waitress service.  This module only
provides a native launcher, startup feedback, tray behaviour, and coordinated
shutdown.  The web application and the development entry point (``app.py``)
remain unchanged.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from waitress.server import create_server

from karaoke import create_app
from karaoke.http import base_url, local_ip
from karaoke.logging import logger, setup_logging
from karaoke.transposition import (
    cancel_active_transpositions,
    check_ffmpeg,
)
from karaoke.version import APP_NAME, version_label

HOST = "0.0.0.0"
PORT_START = 8080
PORT_END = 8099
DEFAULT_THREADS = 16
MUTEX_NAME = "Local\\KaraokeApp-5D7B52F1-0A0E-4E2C-9A3C-6F4B7D1E2A90"


class StartupError(RuntimeError):
    """A user-visible startup failure."""


def app_root() -> Path:
    """Return the directory that owns the portable distribution."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """Return the root containing bundled templates and static assets."""
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return app_root()


def ensure_data_directories(base_dir: Path) -> dict[str, Path]:
    """Create and verify the three writable portable media directories."""
    directories = {
        "songs": base_dir / "songs",
        "lyrics": base_dir / "lyrics",
        "transposed": base_dir / "transposed",
    }
    for name, directory in directories.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".karaoke-write-test-{os.getpid()}-{time.time_ns()}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise StartupError(
                f"The {name} folder is not writable:\n{directory}\n\n"
                "Extract KARAOKE APP to a writable folder, such as your "
                "Documents or Desktop, and try again."
            ) from exc
    return directories


def fallback_log_path() -> Path:
    """Return the per-user fallback log path used when the app folder is read-only."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "KaraokeApp" / "logs" / "karaoke.log"


def configure_logging(base_dir: Path) -> Path:
    """Configure file logging beside the executable, then use LOCALAPPDATA fallback."""
    level_name = os.environ.get("KARAOKE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    preferred = base_dir / "karaoke.log"
    try:
        setup_logging(log_file=str(preferred), level=level)
        return preferred
    except OSError as first_error:
        fallback = fallback_log_path()
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            setup_logging(log_file=str(fallback), level=level)
            logger.warning(
                "Could not write %s (%s); logging to %s",
                preferred, first_error, fallback,
            )
            return fallback
        except OSError as second_error:
            raise StartupError(
                "Could not create a log file in either location:\n"
                f"{preferred}\n{fallback}\n\n{second_error}"
            ) from second_error


def create_server_with_fallback(
        app: Any, *, host: str = HOST,
        ports: range = range(PORT_START, PORT_END + 1),
        threads: int = DEFAULT_THREADS,
) -> Any:
    """Bind the first available port, returning the actual Waitress server."""
    errors: list[str] = []
    for port in ports:
        try:
            return create_server(app, host=host, port=port, threads=threads)
        except OSError as exc:
            errors.append(f"{port}: {exc}")
    detail = "\n".join(errors) if errors else "no candidate ports"
    raise StartupError(
        "Could not start the karaoke server on any port from "
        f"{ports.start} to {ports.stop - 1}.\n\n{detail}"
    )


def urls_for_port(port: int) -> dict[str, str]:
    """Build the GUI URLs from the actual server port and detected LAN IP."""
    lan_ip = local_ip()
    join_url = base_url(lan_ip, port)
    return {
        "lan_ip": lan_ip,
        "local_player": base_url("localhost", port) + "/player",
        "phone_remote": join_url + "/remote",
        "other_devices": join_url,
        "lan_available": lan_ip not in {"127.0.0.1", "localhost", ""},
    }


def check_server_ready(port: int, timeout: float = 10.0) -> bool:
    """Wait until the local server answers /api/info."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/info"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.1)
    return False


def check_packaged_assets(port: int) -> None:
    """Verify that bundled templates and static assets are reachable."""
    checks = (
        ("/player", b"KARAOKE"),
        ("/remote", b"/static/remote.js"),
        ("/static/scripts.js", b"SHARED KARAOKE SESSION"),
        ("/static/remote.css", b""),
        ("/api/songs", b"songs"),
    )
    for path, marker in checks:
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urlopen(url, timeout=3.0) as response:
                body = response.read()
                if response.status != 200 or marker not in body:
                    raise StartupError(
                        f"Packaged asset check failed for {path} "
                        f"(status {response.status}).")
        except (OSError, URLError, ValueError) as exc:
            raise StartupError(
                f"Packaged asset check failed for {path}: {exc}") from exc


class SingleInstance:
    """Windows named mutex guard against accidental duplicate launches."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._handle = None
        self.already_running = False
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = kernel32.CreateMutexW(None, False, name)
        if not self._handle:
            raise StartupError("Could not create the single-instance guard.")
        self.already_running = ctypes.get_last_error() == 183
        self._kernel32 = kernel32

    def close(self) -> None:
        if self._handle:
            try:
                self._kernel32.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None


class DesktopApp:
    """Tk main window, splash/status surface, tray icon, and server ownership."""

    def __init__(self, base_dir: Path, single_instance: SingleInstance,
                 smoke_test: bool = False) -> None:
        self.base_dir = base_dir
        self.single_instance = single_instance
        self.smoke_test = smoke_test
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.app = None
        self.tray = None
        self.tray_available = False
        self.port: int | None = None
        self.urls: dict[str, str] = {}
        self.log_path: Path | None = None
        self.ffmpeg_description = "Not checked"
        self._closing = False
        self._startup_thread: threading.Thread | None = None
        self._ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._splash_photo = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.root.report_callback_exception = self._report_callback_exception
        self._apply_window_icon()
        self._build_splash()
        self.root.after(50, self._start_startup)
        self.root.after(50, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # Splash and main window
    # ------------------------------------------------------------------
    def _apply_window_icon(self) -> None:
        icon_path = resource_root() / "static" / "karaoke-app.ico"
        try:
            if icon_path.is_file():
                self.root.iconbitmap(default=str(icon_path))
                return
            from PIL import Image, ImageTk
            image = Image.open(resource_root() / "static" / "logo.webp")
            photo = ImageTk.PhotoImage(image.convert("RGBA").resize((64, 64)))
            self._window_icon_photo = photo
            self.root.iconphoto(True, photo)
        except Exception:
            # The executable icon and in-window logo still provide branding.
            pass

    def _build_splash(self) -> None:
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.configure(bg="#0f0f13")
        frame = tk.Frame(self.root, bg="#0f0f13", padx=30, pady=24)
        frame.pack(fill="both", expand=True)
        self._add_logo(frame, size=(96, 96))
        tk.Label(
            frame, text=APP_NAME, bg="#0f0f13", fg="#ffffff",
            font=("Segoe UI", 20, "bold"),
        ).pack(pady=(12, 2))
        tk.Label(
            frame, text=f"Version: {version_label()}", bg="#0f0f13",
            fg="#b8b8c7", font=("Segoe UI", 10),
        ).pack()
        self.splash_status = tk.Label(
            frame, text="Starting KARAOKE APP…", bg="#0f0f13",
            fg="#ff4d6d", font=("Segoe UI", 10, "bold"),
        )
        self.splash_status.pack(pady=(18, 4))
        tk.Label(
            frame, text="Loading bundled runtime and preparing folders",
            bg="#0f0f13", fg="#858596", font=("Segoe UI", 9),
        ).pack()
        self.root.update_idletasks()
        width = max(390, self.root.winfo_reqwidth())
        height = max(250, self.root.winfo_reqheight())
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()

    def _add_logo(self, parent: tk.Widget, size: tuple[int, int],
                  background: str = "#0f0f13") -> None:
        """Add the logo using Pillow, falling back to text if unavailable."""
        try:
            from PIL import Image, ImageTk
            image = Image.open(resource_root() / "static" / "logo.webp")
            image = image.convert("RGBA").resize(size)
            photo = ImageTk.PhotoImage(image)
            self._splash_photo = photo
            tk.Label(parent, image=photo, bg=background).pack(side="left")
        except Exception:
            tk.Label(
                parent, text="K", bg="#ff4d6d", fg="#ffffff",
                font=("Segoe UI", 28, "bold"), width=3, height=1,
            ).pack(side="left")

    def _build_main_window(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self.root.overrideredirect(False)
        self.root.resizable(True, False)
        self.root.configure(bg="#f4f4f7")
        self.root.title(APP_NAME)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        outer = ttk.Frame(self.root, padding=(24, 20))
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        self._add_logo(heading, size=(58, 58), background="#f4f4f7")
        title_box = ttk.Frame(heading)
        title_box.pack(side="left", padx=(14, 0), fill="x", expand=True)
        ttk.Label(
            title_box, text=APP_NAME, font=("Segoe UI", 20, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            title_box, text=f"Version: {version_label()}",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 0))
        self.status_var = tk.StringVar(value="Status: Running")
        ttk.Label(
            title_box, textvariable=self.status_var,
            font=("Segoe UI", 10, "bold"), foreground="#16803c",
        ).pack(anchor="w", pady=(4, 0))

        ttk.Separator(outer).pack(fill="x", pady=16)
        self._add_url_block(
            outer, "Player on this PC:", self.urls["local_player"])
        if self.urls["lan_available"]:
            self._add_url_block(
                outer, "Phone remotes:", self.urls["phone_remote"])
            self._add_url_block(
                outer, "Other devices on your network:", self.urls["other_devices"])
        else:
            ttk.Label(
                outer,
                text="Phone remotes: no LAN address detected. "
                     "Connect this computer to the local network to enable remotes.",
                foreground="#9a4b00", wraplength=680,
            ).pack(anchor="w", pady=(0, 10))

        ttk.Label(
            outer,
            text="Use KARAOKE APP only on a trusted private network. "
                 "Do not expose it through router port forwarding to the internet.",
            foreground="#666677", wraplength=680,
        ).pack(anchor="w", pady=(4, 12))
        ttk.Label(
            outer, text=f"Pitch shifting: {self.ffmpeg_description}",
            foreground="#666677", wraplength=680,
        ).pack(anchor="w")
        ttk.Label(
            outer, text=f"Log: {self.log_path}", foreground="#666677",
            wraplength=680,
        ).pack(anchor="w", pady=(4, 12))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            actions, text="Open player", command=self._open_player,
        ).pack(side="left")
        ttk.Button(
            actions, text="Minimize to tray", command=self._hide_to_tray,
        ).pack(side="left", padx=8)
        ttk.Button(
            actions, text="Exit", command=self._request_shutdown,
        ).pack(side="right")

        self.root.update_idletasks()
        width = max(700, self.root.winfo_reqwidth())
        height = max(430, self.root.winfo_reqheight())
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _add_url_block(self, parent: ttk.Frame, label: str, url: str) -> None:
        block = ttk.Frame(parent)
        block.pack(fill="x", pady=(0, 12))
        ttk.Label(block, text=label, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        entry = ttk.Entry(block, width=70)
        entry.insert(0, url)
        entry.configure(state="readonly")
        entry.pack(anchor="w", pady=(3, 0))
        entry.bind("<Button-1>", lambda _event: entry.selection_range(0, "end"))
        ttk.Button(
            block, text="Copy", width=7,
            command=lambda: self._copy(url),
        ).pack(anchor="w", pady=(4, 0))

    def _copy(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status_var.set("Status: Running · copied link")

    # ------------------------------------------------------------------
    # Startup worker
    # ------------------------------------------------------------------
    def _start_startup(self) -> None:
        self._startup_thread = threading.Thread(
            target=self._startup_worker, name="karaoke-startup", daemon=True)
        self._startup_thread.start()

    def _post(self, kind: str, payload: Any = None) -> None:
        self._ui_queue.put((kind, payload))

    def _startup_worker(self) -> None:
        try:
            self._post("status", "Preparing external data folders…")
            ensure_data_directories(self.base_dir)

            self._post("status", "Configuring logging…")
            self.log_path = configure_logging(self.base_dir)
            logger.info("Starting %s %s", APP_NAME, version_label())

            self._post("status", "Loading application…")
            self.app = create_app(
                songs_dir=self.base_dir / "songs",
                lyrics_dir=self.base_dir / "lyrics",
                transposed_dir=self.base_dir / "transposed",
            )

            if self._closing:
                return
            self._post("status", "Starting local server…")
            self.server = create_server_with_fallback(self.app)
            if self._closing:
                self.server.close()
                return
            self.port = int(self.server.effective_port)
            self.urls = urls_for_port(self.port)
            self.server_thread = threading.Thread(
                target=self._serve, name="karaoke-waitress", daemon=False)
            self.server_thread.start()
            if self._closing:
                self.server.close()
                return

            self._post("status", f"Waiting for server on port {self.port}…")
            if not check_server_ready(self.port):
                raise StartupError(
                    "The server started but did not answer on localhost. "
                    "Check karaoke.log for details."
                )

            ok, description = check_ffmpeg()
            self.ffmpeg_description = description
            (logger.info if ok else logger.warning)(
                "ffmpeg: %s", description)
            self._post("ready")
        except Exception as exc:
            logger.exception("Desktop startup failed")
            self._post("startup_error", str(exc))

    def _serve(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:  # report unexpected server death
            logger.exception("Waitress server stopped unexpectedly")
            if not self._closing:
                self._post("server_error", str(exc))

    # ------------------------------------------------------------------
    # UI event loop
    # ------------------------------------------------------------------
    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "status":
                    self.splash_status.configure(text=str(payload))
                elif kind == "ready":
                    self._show_main_window()
                elif kind == "startup_error":
                    self._show_error(str(payload))
                elif kind == "server_error":
                    self.status_var.set("Status: Stopped — check the log")
                    messagebox.showerror(
                        APP_NAME,
                        f"The karaoke server stopped unexpectedly.\n\n{payload}",
                        parent=self.root,
                    )
                elif kind == "restore":
                    self._show_window()
                elif kind == "open_player":
                    self._open_player()
                elif kind == "exit":
                    self._request_shutdown()
        except queue.Empty:
            pass
        if not self._closing:
            try:
                self.root.after(50, self._drain_ui_queue)
            except tk.TclError:
                pass

    def _show_main_window(self) -> None:
        self._build_main_window()
        self._start_tray()
        if self.smoke_test:
            self.root.after(1200, self._request_shutdown)

    def _show_error(self, message: str) -> None:
        if self._closing:
            return
        messagebox.showerror(APP_NAME, message, parent=self.root)
        self._request_shutdown(exit_code=1)

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        logger.exception("GUI callback failed", exc_info=(exc_type, exc_value, exc_tb))
        if not self._closing:
            messagebox.showerror(APP_NAME, str(exc_value), parent=self.root)

    # ------------------------------------------------------------------
    # Tray and commands
    # ------------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
            image = Image.open(resource_root() / "static" / "logo.webp")
            image = image.convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Restore", lambda icon, item: self._post("restore"),
                    default=True),
                pystray.MenuItem(
                    "Open player", lambda icon, item: self._post("open_player")),
                pystray.MenuItem(
                    "Exit", lambda icon, item: self._post("exit")),
            )
            self.tray = pystray.Icon("karaoke-app", image, APP_NAME, menu)
            if hasattr(self.tray, "run_detached"):
                self.tray.run_detached()
            else:
                threading.Thread(
                    target=self.tray.run, name="karaoke-tray", daemon=True).start()
            self.tray_available = True
        except Exception as exc:
            logger.warning("System tray unavailable: %s", exc)
            self.tray_available = False
            if hasattr(self, "status_var"):
                self.status_var.set("Status: Running (tray unavailable)")

    def _show_window(self) -> None:
        if self._closing:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self) -> None:
        if self.tray_available:
            self.root.withdraw()
        else:
            self._request_shutdown()

    def _on_close_request(self) -> None:
        if self.tray_available and not self._closing:
            self.root.withdraw()
        else:
            self._request_shutdown()

    def _open_player(self) -> None:
        try:
            webbrowser.open(self.urls["local_player"])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the browser:\n{exc}",
                                 parent=self.root)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _request_shutdown(self, exit_code: int = 0) -> None:
        if self._closing:
            return
        self._closing = True
        if hasattr(self, "status_var"):
            self.status_var.set("Status: Stopping…")
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

        if self.app is not None:
            try:
                shutdown_event = self.app.extensions.get("shutdown_event")
                if shutdown_event is not None:
                    shutdown_event.set()
                playback = self.app.extensions.get("playback")
                if playback is not None:
                    playback.close_subscribers()
            except Exception:
                logger.exception("Could not signal SSE shutdown")
        try:
            cancel_active_transpositions()
        except Exception:
            logger.exception("Could not stop FFmpeg cleanly")

        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                logger.exception("Could not close Waitress server")
            if self.server_thread is not None:
                self.server_thread.join(timeout=6.0)
                if self.server_thread.is_alive():
                    logger.warning("Waitress thread did not stop; cancelling tasks")
                    try:
                        self.server.task_dispatcher.shutdown(
                            cancel_pending=True, timeout=2.0)
                    except Exception:
                        logger.exception("Could not shut down Waitress tasks")
                    self.server_thread.join(timeout=2.0)
        if self._startup_thread is not None and self._startup_thread.is_alive():
            self._startup_thread.join(timeout=2.0)

        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                logger.exception("Could not stop tray icon")
        self.single_instance.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        logger.info("Desktop exit requested (code=%s)", exit_code)
        self._exit_code = exit_code

    def run(self) -> int:
        self.root.mainloop()
        return getattr(self, "_exit_code", 0)


def _show_already_running() -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        APP_NAME,
        "KARAOKE APP is already running. Use its system tray icon to restore "
        "the window.",
    )
    root.destroy()


def run_self_test() -> int:
    """Exercise packaged imports, Tk data, server startup, and graceful shutdown."""
    base_dir = app_root()
    server = None
    server_thread = None
    try:
        ensure_data_directories(base_dir)
        configure_logging(base_dir)

        # Importing tkinter is not enough; creating a root verifies that the
        # bundled Tcl/Tk data files were collected correctly.
        probe_root = tk.Tk()
        probe_root.withdraw()
        probe_root.update_idletasks()
        probe_root.destroy()

        # Importing pystray resolves the dynamic Windows backend. This catches
        # a packaging mistake even when the self-test is run without Explorer.
        import pystray
        assert pystray.Icon is not None

        app = create_app(
            songs_dir=base_dir / "songs",
            lyrics_dir=base_dir / "lyrics",
            transposed_dir=base_dir / "transposed",
        )
        server = create_server_with_fallback(app)
        port = int(server.effective_port)
        server_thread = threading.Thread(
            target=server.run, name="karaoke-self-test", daemon=False)
        server_thread.start()
        if not check_server_ready(port):
            raise StartupError("Self-test server did not answer /api/info.")
        check_packaged_assets(port)
        ok, description = check_ffmpeg()
        if not ok or "rubberband filter available" not in description:
            raise StartupError(f"FFmpeg self-test failed: {description}")

        logger.info("Self-test passed on port %s; %s", port, description)
        app.extensions["shutdown_event"].set()
        app.extensions["playback"].close_subscribers()
        server.close()
        server_thread.join(timeout=6.0)
        if server_thread.is_alive():
            raise StartupError("Self-test server did not shut down cleanly.")
        print(f"self-test passed: {APP_NAME} {version_label()} on port {port}")
        return 0
    except Exception as exc:
        logger.exception("Self-test failed")
        print(f"self-test failed: {exc}", file=sys.stderr)
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        if server_thread is not None:
            server_thread.join(timeout=2.0)
        cancel_active_transpositions()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run KARAOKE APP as a desktop application.")
    parser.add_argument("--version", action="store_true",
                        help="print the application version and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="run bundled server/Tk/FFmpeg checks without the GUI and exit")
    parser.add_argument("--smoke-test-gui", action="store_true",
                        help="open the GUI briefly, then shut down cleanly and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(version_label())
        return 0
    if args.self_test:
        return run_self_test()

    # The Windows GUI looks blurry without an explicit DPI awareness setting.
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    try:
        single_instance = SingleInstance()
    except Exception as exc:
        logger.exception("Could not create the single-instance guard")
        try:
            messagebox.showerror(APP_NAME, str(exc))
        except Exception:
            pass
        return 1

    if single_instance.already_running:
        _show_already_running()
        single_instance.close()
        return 1

    try:
        app = DesktopApp(
            app_root(), single_instance, smoke_test=args.smoke_test_gui)
        return app.run()
    except Exception as exc:
        logger.exception("Could not start the desktop application")
        try:
            messagebox.showerror(APP_NAME, str(exc))
        except Exception:
            pass
        single_instance.close()
        return 1
