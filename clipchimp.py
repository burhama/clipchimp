"""ClipChimp: hold-drag screen capture with in-place annotation on Windows."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import io
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import winreg

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk
import tkinter as tk
from tkinter import colorchooser, simpledialog
import win32clipboard
import win32con


APP_NAME = "ClipChimp"
DOUBLE_CLICK_SECONDS = 0.400
MUTEX_NAME = "Local\\ClipChimpCapture-0E2B49A7"
LOG_PATH = Path(tempfile.gettempdir()) / "clipchimp.log"
DEBUG = bool(os.environ.get("CLIPCHIMP_DEBUG"))
ACCEPT_INJECTED = bool(os.environ.get("CLIPCHIMP_ACCEPT_INJECTED"))
REPLAY_TAG = 0x43434D50

WH_MOUSE_LL = 14
HC_ACTION = 0
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
LLMHF_INJECTED = 0x00000001
INPUT_MOUSE = 0
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
TRANSPARENT_KEY = "#ff00ff"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.DWORD)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT)
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

logging.basicConfig(filename=LOG_PATH, level=logging.DEBUG if DEBUG else logging.ERROR,
                    format="%(asctime)s %(levelname)s %(message)s")


Rect = tuple[int, int, int, int]
Point = tuple[int, int]


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen() -> Rect:
    x, y, width, height = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    return x, y, x + width, y + height


def normalize_rect(a: Point, b: Point) -> Rect:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def point_in_rect(point: Point, rect: Rect | None) -> bool:
    return bool(rect and rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3])


def native_toplevel(window: tk.Misc) -> int:
    window.update_idletasks()
    child = window.winfo_id()
    return user32.GetParent(child) or child


def place_window(window: tk.Misc, rect: Rect, activate: bool = False) -> None:
    left, top, right, bottom = rect
    flags = SWP_SHOWWINDOW | (0 if activate else 0x0010)
    user32.SetWindowPos(native_toplevel(window), HWND_TOPMOST, left, top,
                        max(1, right - left), max(1, bottom - top), flags)


def make_clickthrough(window: tk.Misc) -> None:
    hwnd = native_toplevel(window)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT |
                          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


def capture_region(rect: Rect) -> Image.Image:
    image = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGBA")
    expected = (rect[2] - rect[0], rect[3] - rect[1])
    if image.size != expected:
        raise RuntimeError(f"Capture size {image.size} did not match region {expected}")
    return image


def pictures_directory() -> Path:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    fid = GUID(0x33E28130, 0x4E1E, 0x4676, (ctypes.c_ubyte * 8)(
        0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB))
    out = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None,
                                                     ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return Path.home() / "Pictures"


def clips_directory() -> Path:
    return pictures_directory() / "ClipChimp"


def unique_output_path() -> Path:
    folder = clips_directory()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    candidate = folder / f"ClipChimp_{stamp}.png"
    number = 2
    while candidate.exists():
        candidate = folder / f"ClipChimp_{stamp}_{number}.png"
        number += 1
    return candidate


def write_latest_pointer(path: Path) -> None:
    """Atomically record the newest clip (path + hash) so other tools can find it reliably."""
    import hashlib, json
    data = path.read_bytes()
    record = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
              "bytes": len(data), "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    pointer = clips_directory() / "latest.json"
    tmp = pointer.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, pointer)


def set_image_clipboard(image: Image.Image, path: Path) -> None:
    bmp = io.BytesIO()
    image.convert("RGB").save(bmp, "BMP")
    png = io.BytesIO()
    image.save(png, "PNG")
    png_format = win32clipboard.RegisterClipboardFormat("PNG")
    last_error: Exception | None = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB, bmp.getvalue()[14:])
                win32clipboard.SetClipboardData(png_format, png.getvalue())
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, str(path))
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError("Could not open the Windows clipboard") from last_error


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


class HookThread(threading.Thread):
    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    def __init__(self):
        super().__init__(name="ClipChimpMouseHook", daemon=True)
        self.hook = None
        self.thread_id = 0
        self.lock = threading.Lock()
        self.failure = threading.Event()
        self.finalize_event = threading.Event()
        self.first_down = 0.0
        self.first_up = False
        self.dragging = False
        self.drag_anchor: Point | None = None
        self.drag_current: Point | None = None
        self.drag_revision = 0
        self.completed_rect: Rect | None = None
        self.annotation_active = False
        self.frame_rect: Rect | None = None
        self.toolbar_rect: Rect | None = None
        self.timer: threading.Timer | None = None
        prototype = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)
        self.callback_ref = prototype(self.callback)

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self.callback_ref,
                                             kernel32.GetModuleHandleW(None), 0)
        if not self.hook:
            logging.error("SetWindowsHookEx failed: %s", ctypes.WinError())
            self.failure.set()
            return
        logging.debug("mouse hook installed: %#x", self.hook)
        try:
            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError()
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            logging.exception("mouse hook thread failed")
            self.failure.set()
        finally:
            if self.hook:
                user32.UnhookWindowsHookEx(self.hook)

    def callback(self, code, wparam, lparam):
        if code != HC_ACTION:
            return user32.CallNextHookEx(self.hook, code, wparam, lparam)
        info = ctypes.cast(lparam, ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
        injected = bool(info.flags & LLMHF_INJECTED)
        # Replayed single clicks always bypass recognition, even in diagnostic mode.
        if injected and int(info.dwExtraInfo or 0) == REPLAY_TAG:
            return user32.CallNextHookEx(self.hook, code, wparam, lparam)
        if injected and not ACCEPT_INJECTED:
            return user32.CallNextHookEx(self.hook, code, wparam, lparam)
        point = (info.pt.x, info.pt.y)
        now = time.monotonic()
        with self.lock:
            if wparam == WM_LBUTTONDOWN and self.annotation_active:
                if not point_in_rect(point, self.frame_rect) and not point_in_rect(point, self.toolbar_rect):
                    logging.debug("click-away point=%s frame=%s toolbar=%s",
                                  point, self.frame_rect, self.toolbar_rect)
                    self.annotation_active = False
                    self.finalize_event.set()
                    return 1
            if wparam == WM_MOUSEMOVE and self.dragging:
                self.drag_current = point
                self.drag_revision += 1
                return user32.CallNextHookEx(self.hook, code, wparam, lparam)
            if wparam == WM_MBUTTONDOWN:
                if self.annotation_active:
                    return user32.CallNextHookEx(self.hook, code, wparam, lparam)
                if self.first_down and now - self.first_down <= DOUBLE_CLICK_SECONDS:
                    if self.timer:
                        self.timer.cancel()
                    self.first_down = 0.0
                    self.first_up = False
                    self.dragging = True
                    self.drag_anchor = self.drag_current = point
                    self.drag_revision += 1
                    logging.debug("drag anchor 1: %s", point)
                    return 1
                self.first_down = now
                self.first_up = False
                if self.timer:
                    self.timer.cancel()
                self.timer = threading.Timer(DOUBLE_CLICK_SECONDS, self.replay_single)
                self.timer.daemon = True
                self.timer.start()
                return 1
            if wparam == WM_MBUTTONUP:
                if self.dragging:
                    self.dragging = False
                    self.drag_current = point
                    self.completed_rect = normalize_rect(self.drag_anchor, point)
                    self.drag_revision += 1
                    logging.debug("drag anchor 2: %s rect=%s", point, self.completed_rect)
                    return 1
                if self.first_down:
                    self.first_up = True
                    return 1
        return user32.CallNextHookEx(self.hook, code, wparam, lparam)

    def replay_single(self) -> None:
        with self.lock:
            if not self.first_down:
                return
            was_released = self.first_up
            self.first_down = 0.0
            self.first_up = False
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_MOUSE
        inputs[0].mi.dwFlags = MOUSEEVENTF_MIDDLEDOWN
        inputs[0].mi.dwExtraInfo = REPLAY_TAG
        count = 1
        if was_released:
            inputs[1].type = INPUT_MOUSE
            inputs[1].mi.dwFlags = MOUSEEVENTF_MIDDLEUP
            inputs[1].mi.dwExtraInfo = REPLAY_TAG
            count = 2
        user32.SendInput(count, ctypes.byref(inputs), ctypes.sizeof(INPUT))

    def drag_snapshot(self) -> tuple[int, Point | None, Point | None, bool]:
        with self.lock:
            return self.drag_revision, self.drag_anchor, self.drag_current, self.dragging

    def pop_completed(self) -> Rect | None:
        with self.lock:
            result, self.completed_rect = self.completed_rect, None
            return result

    def set_annotation_regions(self, frame: Rect | None, toolbar: Rect | None,
                               enabled: bool = True) -> None:
        with self.lock:
            self.frame_rect = frame
            self.toolbar_rect = toolbar
            self.annotation_active = enabled and frame is not None

    def suspend_clickaway(self, suspended: bool) -> None:
        with self.lock:
            self.annotation_active = not suspended and self.frame_rect is not None

    def stop(self) -> None:
        with self.lock:
            if self.timer:
                self.timer.cancel()
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)


class DottedOutline:
    def __init__(self, owner: tk.Tk):
        self.window = tk.Toplevel(owner)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=TRANSPARENT_KEY)
        self.window.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.window, bg=TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.rect: Rect | None = None
        self.phase = 0
        self.running = True
        self.window.after(120, self.animate)

    def show(self, rect: Rect) -> None:
        self.rect = rect
        left, top, right, bottom = rect
        outer = (left - 3, top - 3, right + 3, bottom + 3)
        self.window.deiconify()
        place_window(self.window, outer)
        make_clickthrough(self.window)
        self.redraw()

    def redraw(self) -> None:
        if not self.rect:
            return
        width = max(7, self.rect[2] - self.rect[0] + 6)
        height = max(7, self.rect[3] - self.rect[1] + 6)
        self.canvas.delete("all")
        self.canvas.create_rectangle(2, 2, width - 3, height - 3,
                                     outline="#ffffff", width=2,
                                     dash=(4, 4), dashoffset=self.phase)
        self.canvas.create_rectangle(3, 3, width - 4, height - 4,
                                     outline="#15171a", width=1,
                                     dash=(4, 4), dashoffset=self.phase + 4)

    def animate(self) -> None:
        if not self.running:
            return
        if self.rect:
            self.phase = (self.phase + 2) % 8
            self.redraw()
        self.window.after(120, self.animate)

    def hide(self) -> None:
        self.rect = None
        self.window.withdraw()

    def destroy(self) -> None:
        self.running = False
        self.window.destroy()


def windows_theme() -> dict[str, str]:
    light = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light = bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        pass
    if light:
        return {"pill": "#f4f4f5", "button": "#f4f4f5", "hover": "#e4e4e7",
                "selected": "#dbeafe", "icon": "#202124", "muted": "#71717a"}
    return {"pill": "#202124", "button": "#202124", "hover": "#35363a",
            "selected": "#174a72", "icon": "#f5f5f5", "muted": "#a1a1aa"}


def fallback_icon(name: str, colour: str) -> Image.Image:
    image = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    c, w = colour, 2
    if name == "pen":
        draw.line((5, 18, 16, 7), fill=c, width=w); draw.polygon((4, 20, 6, 15, 9, 18), outline=c)
    elif name == "highlighter":
        draw.polygon((5, 17, 15, 7, 19, 11, 9, 21), outline=c); draw.line((4, 21, 14, 21), fill=c, width=w)
    elif name == "eraser":
        draw.polygon((5, 15, 14, 6, 20, 12, 11, 21), outline=c); draw.line((8, 18, 17, 9), fill=c, width=w)
    elif name == "rect":
        draw.rectangle((4, 6, 20, 18), outline=c, width=w)
    elif name == "ellipse":
        draw.ellipse((4, 6, 20, 18), outline=c, width=w)
    elif name == "arrow":
        draw.line((4, 12, 19, 12), fill=c, width=w); draw.line((14, 7, 19, 12, 14, 17), fill=c, width=w)
    elif name == "line":
        draw.line((5, 19, 19, 5), fill=c, width=w)
    elif name == "text":
        draw.line((5, 6, 19, 6), fill=c, width=w); draw.line((12, 6, 12, 19), fill=c, width=w)
    elif name == "crop":
        draw.line((7, 3, 7, 17, 21, 17), fill=c, width=w); draw.line((3, 7, 17, 7, 17, 21), fill=c, width=w)
    elif name in ("undo", "redo"):
        if name == "undo":
            draw.arc((5, 6, 20, 20), 190, 530, fill=c, width=w); draw.line((4, 10, 8, 5, 10, 11), fill=c, width=w)
        else:
            draw.arc((4, 6, 19, 20), 10, 350, fill=c, width=w); draw.line((20, 10, 16, 5, 14, 11), fill=c, width=w)
    return image


def load_icon(name: str, colour: str) -> Image.Image:
    path = Path(__file__).with_name("icons") / f"{name}.png"
    try:
        source = Image.open(path).convert("RGBA").resize((24, 24), Image.Resampling.LANCZOS)
        alpha = source.getchannel("A")
        tinted = Image.new("RGBA", source.size, colour)
        tinted.putalpha(alpha)
        return tinted
    except Exception:
        logging.warning("using fallback icon: %s", name)
        return fallback_icon(name, colour)


class FrameAnnotator:
    TOOL_NAMES = ("pen", "highlighter", "eraser", "rect", "ellipse",
                  "arrow", "line", "text", "crop")
    ALL_ICONS = TOOL_NAMES + ("undo", "redo")

    def __init__(self, owner: tk.Tk, image: Image.Image, screen_rect: Rect,
                 outline: DottedOutline, on_finish, on_cancel,
                 clickaway_control, regions_changed, automated: bool = False):
        self.owner = owner
        self.base = image.convert("RGBA")
        self.layer = Image.new("RGBA", self.base.size, (0, 0, 0, 0))
        self.screen_rect = screen_rect
        self.outline = outline
        self.on_finish = on_finish
        self.on_cancel = on_cancel
        self.clickaway_control = clickaway_control
        self.regions_changed = regions_changed
        self.automated = automated
        self.tool = "rect"
        self.color = "#ff3b30"
        self.size = 2
        self.start: Point | None = None
        self.last: Point | None = None
        self.history: list[tuple[Image.Image, Image.Image, Rect]] = []
        self.future: list[tuple[Image.Image, Image.Image, Rect]] = []
        self.photo = None
        self.icon_photos: dict[str, ImageTk.PhotoImage] = {}
        self.icon_widgets: dict[str, tk.Label] = {}
        self.theme = windows_theme()

        self.frame = tk.Toplevel(owner)
        self.frame.overrideredirect(True)
        self.frame.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.release)

        self.toolbar = tk.Toplevel(owner)
        self.toolbar.overrideredirect(True)
        self.toolbar.attributes("-topmost", True)
        self.toolbar.configure(bg=TRANSPARENT_KEY)
        self.toolbar.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        self.toolbar_canvas = tk.Canvas(self.toolbar, bg=TRANSPARENT_KEY,
                                        highlightthickness=0, bd=0)
        self.toolbar_canvas.pack(fill="both", expand=True)
        self.build_toolbar()
        for window in (self.frame, self.toolbar):
            window.bind("<Escape>", lambda _e: self.cancel())
            window.bind("<Control-z>", lambda _e: self.undo())
            window.bind("<Control-y>", lambda _e: self.redo())
        self.place_all()
        self.refresh()
        self.frame.after_idle(self.take_focus)
        if automated:
            self.frame.after(350, self.automate)

    def build_toolbar(self) -> None:
        width, height = 11 * 36 + 44 + 104 + 16, 48
        self.toolbar_size = (width, height)
        self.toolbar_canvas.configure(width=width, height=height)
        self.toolbar_canvas.create_polygon(
            8, 0, width - 8, 0, width, 8, width, height - 8,
            width - 8, height, 8, height, 0, height - 8, 0, 8,
            fill=self.theme["pill"], smooth=True)
        x = 8
        for name in self.ALL_ICONS:
            icon = ImageTk.PhotoImage(load_icon(name, self.theme["icon"]))
            self.icon_photos[name] = icon
            label = tk.Label(self.toolbar_canvas, image=icon, bg=self.theme["button"],
                             bd=0, cursor="hand2", width=34, height=34)
            action = self.undo if name == "undo" else self.redo if name == "redo" else lambda n=name: self.choose_tool(n)
            label.bind("<Button-1>", lambda _e, fn=action: fn())
            label.bind("<Enter>", lambda _e, n=name: self.hover(n, True))
            label.bind("<Leave>", lambda _e, n=name: self.hover(n, False))
            self.toolbar_canvas.create_window(x, 7, window=label, anchor="nw", width=36, height=34)
            self.icon_widgets[name] = label
            x += 36
        self.color_image = ImageTk.PhotoImage(self.color_dot())
        self.color_widget = tk.Label(self.toolbar_canvas, image=self.color_image,
                                     bg=self.theme["button"], cursor="hand2", bd=0)
        self.color_widget.bind("<Button-1>", lambda _e: self.choose_color())
        self.toolbar_canvas.create_window(x + 3, 7, window=self.color_widget,
                                          anchor="nw", width=36, height=34)
        x += 44
        self.size_var = tk.IntVar(value=self.size)
        self.size_scale = tk.Scale(self.toolbar_canvas, from_=1, to=30, orient="horizontal",
                                   variable=self.size_var, command=self.change_size,
                                   length=92, showvalue=False, sliderlength=14, bd=0,
                                   highlightthickness=0, troughcolor=self.theme["muted"],
                                   bg=self.theme["pill"], activebackground=self.color)
        self.toolbar_canvas.create_window(x, 10, window=self.size_scale,
                                          anchor="nw", width=96, height=28)
        self.update_selected()

    def color_dot(self) -> Image.Image:
        icon = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        ImageDraw.Draw(icon).ellipse((5, 5, 19, 19), fill=self.color,
                                     outline=self.theme["icon"], width=1)
        return icon

    def hover(self, name: str, inside: bool) -> None:
        bg = self.theme["selected"] if name == self.tool else self.theme["hover"] if inside else self.theme["button"]
        self.icon_widgets[name].configure(bg=bg)

    def update_selected(self) -> None:
        for name, widget in self.icon_widgets.items():
            widget.configure(bg=self.theme["selected"] if name == self.tool else self.theme["button"])

    def choose_tool(self, name: str) -> None:
        self.tool = name
        self.canvas.configure(cursor="xterm" if name == "text" else "crosshair")
        self.update_selected()

    def choose_color(self) -> None:
        self.clickaway_control(True)
        try:
            picked = colorchooser.askcolor(self.color, parent=self.toolbar)[1]
        finally:
            self.clickaway_control(False)
        if picked:
            self.color = picked
            self.color_image = ImageTk.PhotoImage(self.color_dot())
            self.color_widget.configure(image=self.color_image)
            self.size_scale.configure(activebackground=self.color)

    def change_size(self, value) -> None:
        self.size = int(float(value))

    def toolbar_rect(self) -> Rect:
        vx0, vy0, vx1, vy1 = virtual_screen()
        width, height = self.toolbar_size
        center = (self.screen_rect[0] + self.screen_rect[2]) // 2
        left = min(max(vx0, center - width // 2), vx1 - width)
        if self.screen_rect[1] - height - 8 >= vy0:
            top = self.screen_rect[1] - height - 8
        else:
            top = min(vy1 - height, self.screen_rect[3] + 8)
        return left, top, left + width, top + height

    def place_all(self) -> None:
        place_window(self.frame, self.screen_rect, activate=True)
        toolbar_rect = self.toolbar_rect()
        place_window(self.toolbar, toolbar_rect)
        self.outline.show(self.screen_rect)
        self.regions_changed(self.screen_rect, toolbar_rect)

    def take_focus(self) -> None:
        self.frame.lift()
        self.toolbar.lift()
        self.frame.focus_force()

    def state(self):
        return self.base.copy(), self.layer.copy(), self.screen_rect

    def checkpoint(self) -> None:
        self.history.append(self.state())
        if len(self.history) > 50:
            self.history.pop(0)
        self.future.clear()

    def undo(self) -> None:
        if self.history:
            self.future.append(self.state())
            self.base, self.layer, self.screen_rect = self.history.pop()
            self.place_all(); self.refresh()

    def redo(self) -> None:
        if self.future:
            self.history.append(self.state())
            self.base, self.layer, self.screen_rect = self.future.pop()
            self.place_all(); self.refresh()

    def composite(self) -> Image.Image:
        return Image.alpha_composite(self.base, self.layer)

    def refresh(self, temporary: Image.Image | None = None) -> None:
        shown = temporary or self.composite()
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.configure(width=shown.width, height=shown.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

    def point(self, event) -> Point | None:
        if 0 <= event.x < self.base.width and 0 <= event.y < self.base.height:
            return event.x, event.y
        return None

    def rgba(self, alpha: int = 255):
        value = self.color.lstrip("#")
        return int(value[:2], 16), int(value[2:4], 16), int(value[4:], 16), alpha

    def press(self, event) -> None:
        point = self.point(event)
        if point is None:
            return
        if self.tool == "text":
            self.clickaway_control(True)
            try:
                text = simpledialog.askstring(APP_NAME, "Text to add:", parent=self.frame)
            finally:
                self.clickaway_control(False)
            if text:
                self.checkpoint()
                ImageDraw.Draw(self.layer).text(point, text, fill=self.rgba(),
                    font=self.get_font(max(12, self.size * 4)),
                    stroke_width=max(0, self.size // 5), stroke_fill=(0, 0, 0, 210))
                self.refresh()
            return
        self.checkpoint()
        self.start = self.last = point

    def move(self, event) -> None:
        point = self.point(event)
        if not self.start or point is None:
            return
        if self.tool in ("pen", "highlighter", "eraser"):
            self.draw_freehand(self.last, point); self.last = point; self.refresh()
        elif self.tool in ("rect", "ellipse", "arrow", "line", "crop"):
            self.refresh(self.preview_image(point))

    def release(self, event) -> None:
        point = self.point(event)
        if not self.start:
            self.last = None
            self.refresh()
            return
        if point is None:
            point = (min(self.base.width - 1, max(0, event.x)),
                     min(self.base.height - 1, max(0, event.y)))
        if self.tool in ("rect", "ellipse", "arrow", "line"):
            self.draw_shape(self.layer, self.tool, self.start, point)
        elif self.tool == "crop":
            x0, x1 = sorted((self.start[0], point[0])); y0, y1 = sorted((self.start[1], point[1]))
            if x1 - x0 >= 4 and y1 - y0 >= 4:
                left, top = self.screen_rect[:2]
                self.base = self.base.crop((x0, y0, x1, y1))
                self.layer = self.layer.crop((x0, y0, x1, y1))
                self.screen_rect = left + x0, top + y0, left + x1, top + y1
                self.place_all()
        self.start = self.last = None
        self.refresh()

    def draw_freehand(self, start: Point, end: Point) -> None:
        width = max(1, self.size)
        if self.tool == "eraser":
            mask = Image.new("L", self.layer.size, 0)
            draw = ImageDraw.Draw(mask)
            draw.line((start, end), fill=255, width=width * 3, joint="curve")
            draw.ellipse((end[0] - width * 1.5, end[1] - width * 1.5,
                          end[0] + width * 1.5, end[1] + width * 1.5), fill=255)
            self.layer.paste((0, 0, 0, 0), mask=mask)
        else:
            alpha = 95 if self.tool == "highlighter" else 255
            ImageDraw.Draw(self.layer, "RGBA").line(
                (start, end), fill=self.rgba(alpha),
                width=width * (3 if self.tool == "highlighter" else 1), joint="curve")

    def preview_image(self, point: Point) -> Image.Image:
        if self.tool == "crop":
            image = Image.alpha_composite(self.composite(), Image.new("RGBA", self.base.size, (0, 0, 0, 105)))
            x0, x1 = sorted((self.start[0], point[0])); y0, y1 = sorted((self.start[1], point[1]))
            image.alpha_composite(self.composite().crop((x0, y0, x1, y1)), (x0, y0))
            ImageDraw.Draw(image).rectangle((x0, y0, x1, y1), outline="#38bdf8", width=2)
            return image
        layer = self.layer.copy()
        self.draw_shape(layer, self.tool, self.start, point)
        return Image.alpha_composite(self.base, layer)

    def draw_shape(self, layer: Image.Image, tool: str, start: Point, end: Point) -> None:
        draw = ImageDraw.Draw(layer, "RGBA"); fill = self.rgba(); width = max(1, self.size)
        x0, x1 = sorted((start[0], end[0])); y0, y1 = sorted((start[1], end[1])); box = (x0, y0, x1, y1)
        if tool == "rect": draw.rectangle(box, outline=fill, width=width)
        elif tool == "ellipse": draw.ellipse(box, outline=fill, width=width)
        elif tool == "line": draw.line((start, end), fill=fill, width=width)
        elif tool == "arrow":
            draw.line((start, end), fill=fill, width=width)
            angle = math.atan2(end[1] - start[1], end[0] - start[0]); length = max(12, width * 4); spread = math.pi / 7
            p1 = (end[0] - length * math.cos(angle - spread), end[1] - length * math.sin(angle - spread))
            p2 = (end[0] - length * math.cos(angle + spread), end[1] - length * math.sin(angle + spread))
            draw.polygon((end, p1, p2), fill=fill)

    @staticmethod
    def get_font(size: int):
        for name in ("segoeui.ttf", "arial.ttf"):
            try: return ImageFont.truetype(name, size)
            except OSError: pass
        return ImageFont.load_default()

    def automate(self) -> None:
        self.checkpoint()
        self.draw_shape(self.layer, "arrow", (max(1, self.base.width // 6), max(1, self.base.height // 4)),
                        (max(2, self.base.width * 5 // 6), max(2, self.base.height * 3 // 4)))
        self.refresh()
        self.frame.after(350, self.finish)

    def finish(self) -> None:
        self.on_finish(self.composite())

    def cancel(self) -> None:
        self.on_cancel()

    def destroy(self) -> None:
        self.frame.destroy(); self.toolbar.destroy()


class ClipChimpApp:
    def __init__(self, selftest_path: Path | None = None):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = self.report_tk_exception
        self.selftest_path = selftest_path
        self.selftest = selftest_path is not None
        self.hook: HookThread | None = None
        self.outline = DottedOutline(self.root)
        self.annotator: FrameAnnotator | None = None
        self.last_drag_revision = -1
        self.pending_rect: Rect | None = None
        if self.selftest:
            self.root.after(100, self.begin_selftest)
        else:
            self.hook = HookThread(); self.hook.start(); self.root.after(25, self.poll)

    def poll(self) -> None:
        if self.hook.failure.is_set():
            self.report_error(RuntimeError("Could not install the global mouse hook")); self.root.destroy(); return
        revision, anchor, current, dragging = self.hook.drag_snapshot()
        if revision != self.last_drag_revision:
            self.last_drag_revision = revision
            if dragging and anchor and current:
                self.outline.show(normalize_rect(anchor, current))
        rect = self.hook.pop_completed()
        if rect:
            self.outline.hide()
            if rect[2] - rect[0] >= 4 and rect[3] - rect[1] >= 4:
                self.pending_rect = rect
                self.root.after(35, self.open_pending_region)
        if self.hook.finalize_event.is_set():
            self.hook.finalize_event.clear()
            if self.annotator: self.annotator.finish()
        self.root.after(25, self.poll)

    def open_pending_region(self) -> None:
        rect, self.pending_rect = self.pending_rect, None
        if rect is None or self.annotator:
            return
        try:
            self.open_annotator(capture_region(rect), rect)
        except Exception as exc:
            self.report_error(exc)

    def begin_selftest(self) -> None:
        try:
            image = Image.open(self.selftest_path).convert("RGBA")
            vx0, vy0, vx1, vy1 = virtual_screen()
            left = vx0 + max(20, (vx1 - vx0 - image.width) // 2)
            top = vy0 + max(80, (vy1 - vy0 - image.height) // 2)
            self.open_annotator(image, (left, top, left + image.width, top + image.height))
        except Exception as exc:
            self.report_error(exc); self.root.destroy()

    def open_annotator(self, image: Image.Image, rect: Rect) -> None:
        self.annotator = FrameAnnotator(
            self.root, image, rect, self.outline, self.finish, self.cancel,
            self.suspend_clickaway, self.update_regions, self.selftest)

    def update_regions(self, frame: Rect, toolbar: Rect) -> None:
        if self.hook: self.hook.set_annotation_regions(frame, toolbar)

    def suspend_clickaway(self, suspended: bool) -> None:
        if self.hook: self.hook.suspend_clickaway(suspended)

    def finish(self, image: Image.Image) -> None:
        try:
            if self.selftest:
                path = Path(tempfile.gettempdir()) / "ClipChimp_selftest_result.png"
                image.save(path, "PNG")
                print(f"SELFTEST PASS: {path}", flush=True)
            else:
                path = unique_output_path(); image.save(path, "PNG"); write_latest_pointer(path)
                set_image_clipboard(image, path)
                logging.debug("saved %s", path)
        except Exception as exc:
            self.report_error(exc)
        self.close_annotation()

    def cancel(self) -> None:
        self.close_annotation()

    def close_annotation(self) -> None:
        if self.hook: self.hook.set_annotation_regions(None, None, False)
        if self.annotator:
            self.annotator.destroy(); self.annotator = None
        self.outline.hide()
        if self.selftest: self.root.destroy()

    @staticmethod
    def report_error(exc: Exception) -> None:
        logging.error("ClipChimp error", exc_info=(type(exc), exc, exc.__traceback__))
        user32.MessageBoxW(None, str(exc), APP_NAME, 0x10)

    @staticmethod
    def report_tk_exception(exc_type, exc, traceback) -> None:
        logging.error("Unhandled Tk callback", exc_info=(exc_type, exc, traceback))
        user32.MessageBoxW(None, str(exc), APP_NAME, 0x10)

    def run(self) -> None:
        try: self.root.mainloop()
        finally:
            if self.hook: self.hook.stop()


def acquire_mutex():
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle or kernel32.GetLastError() == 183:
        if handle: kernel32.CloseHandle(handle)
        return None
    return handle


def prepare_selftest(argument: str | None) -> Path:
    if argument:
        path = Path(argument).resolve()
        if not path.is_file(): raise FileNotFoundError(path)
        return path
    path = Path(tempfile.gettempdir()) / "ClipChimp_selftest_source.png"
    ImageGrab.grab(all_screens=True).save(path, "PNG")
    return path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", nargs="?", const="", metavar="PNG",
                        help="exercise the frame annotator on a saved image without the hook")
    return parser.parse_args()


def main() -> int:
    enable_dpi_awareness()
    args = parse_args()
    if args.selftest is not None:
        ClipChimpApp(prepare_selftest(args.selftest or None)).run(); return 0
    mutex = acquire_mutex()
    if mutex is None: return 0
    try: ClipChimpApp().run()
    finally:
        kernel32.ReleaseMutex(mutex); kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception:
        logging.exception("Unhandled top-level exception")
        raise
