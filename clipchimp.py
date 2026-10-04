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
import queue
import struct
import tempfile
import threading
import time
import winreg

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab, ImageTk
import tkinter as tk
from tkinter import colorchooser, simpledialog
import win32api
import win32clipboard
import win32con
import win32gui


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
ZOOM_STEP = 1.25            # one wheel notch
ZOOM_MIN, ZOOM_MAX = 0.2, 8.0
ZOOM_MAX_PIXELS = 12_000_000  # the zoomed frame never grows past this many pixels (keeps every redraw quick)
ZOOM_MIN_SIDE = 24          # nor shrinks below this many pixels a side

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
        self.paused = threading.Event()          # set from the tray: middle clicks pass straight through
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
                if self.annotation_active or self.paused.is_set():
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
    return TOOLBAR_LIGHT if light else TOOLBAR_DARK


# The toolbar ("Bold"): big 44 px buttons, heavy icons, the chosen tool a solid blue tile.
TOOLBAR_DARK = {"bar": "#121418", "border": "#262930", "ink": "#f2f2f0", "hover": "#23262d",
                "press": "#2c3038", "chosen": "#3d7bf2", "chosen_ink": "#ffffff", "divider": "#262930",
                "ring": "#f2f2f0", "track": "#2c3038", "fill": "#3d7bf2", "knob": "#ffffff",
                "knob_edge": "#121418"}
TOOLBAR_LIGHT = {"bar": "#ffffff", "border": "#d9dbe0", "ink": "#15171b", "hover": "#eef0f3",
                 "press": "#e3e6ea", "chosen": "#2f6ae6", "chosen_ink": "#ffffff", "divider": "#e3e5e9",
                 "ring": "#15171b", "track": "#e3e6ea", "fill": "#2f6ae6", "knob": "#ffffff",
                 "knob_edge": "#9ea3ab"}
TOOLBAR_GROUPS = (("pen", "highlighter", "eraser"), ("rect", "ellipse", "arrow", "line"), ("text", "crop"),
                  ("undo", "redo"), ("colour", "size"))
TB_BUTTON, TB_GAP, TB_PAD, TB_GROUP_GAP = 44, 4, 8, 14     # button, gap inside a group, edge, gap between groups
TB_RADIUS, TB_TILE_RADIUS, TB_ICON = 14, 10, 22
TB_SWATCH, TB_SLIDER, TB_TRACK, TB_KNOB = 22, 90, 6, 16     # colour square; size slider length, track, knob


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


_icon_cache: dict[tuple[str, str, int], Image.Image] = {}


def load_icon(name: str, colour: str, size: int = 24) -> Image.Image:
    key = (name, colour, size)
    if key in _icon_cache:
        return _icon_cache[key]
    path = Path(__file__).with_name("icons") / f"{name}.png"
    try:
        source = Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        alpha = source.getchannel("A")
        tinted = Image.new("RGBA", source.size, colour)
        tinted.putalpha(alpha)
    except Exception:
        logging.warning("using fallback icon: %s", name)
        tinted = fallback_icon(name, colour).resize((size, size), Image.Resampling.LANCZOS)
    _icon_cache[key] = tinted
    return tinted


CURSOR_INK = "#1f2328"
TIP_TOOLS = ("pen", "highlighter")          # hotspot = the writing tip of the icon
CENTRE_TOOLS = ("eraser",)                  # hotspot = the middle of the icon
# every other tool needs an exact point: a small crosshair marks it and the icon sits beside it


def cursor_art(name: str) -> tuple[Image.Image, Point]:
    """The tool's icon as a 32x32 cursor image (ink with a white outline) and its hotspot."""
    size = 32
    ink = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if name in TIP_TOOLS or name in CENTRE_TOOLS:
        icon = load_icon(name, CURSOR_INK)
        ink.alpha_composite(icon, (4, 4))
        alpha = icon.getchannel("A")
        solid = [(x, y) for y in range(icon.height) for x in range(icon.width) if alpha.getpixel((x, y)) > 128]
        if not solid:
            raise ValueError(f"empty icon: {name}")
        if name in TIP_TOOLS:
            x, y = min(solid, key=lambda p: (p[0] - p[1], -p[1]))       # bottom-most-left pixel
        else:
            x0, y0, x1, y1 = alpha.getbbox()
            x, y = (x0 + x1) // 2, (y0 + y1) // 2
        hotspot = (4 + x, 4 + y)
    else:
        icon = load_icon(name, CURSOR_INK)
        icon = icon.crop(icon.getchannel("A").getbbox() or (0, 0, 24, 24))   # the drawing, not its padding
        scale = 19 / max(icon.size)
        icon = icon.resize((max(1, round(icon.width * scale)), max(1, round(icon.height * scale))),
                           Image.Resampling.LANCZOS)
        ink.alpha_composite(icon, (11 + (19 - icon.width) // 2, 11 + (19 - icon.height) // 2))
        draw = ImageDraw.Draw(ink)
        draw.line((0, 4, 8, 4), fill=CURSOR_INK, width=1)
        draw.line((4, 0, 4, 8), fill=CURSOR_INK, width=1)
        hotspot = (4, 4)
    outline = ink.getchannel("A").point(lambda v: 255 if v > 40 else 0).filter(ImageFilter.MaxFilter(3))
    art = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    art.putalpha(outline)
    art.alpha_composite(ink)
    return art, hotspot


def write_cursor(image: Image.Image, hotspot: Point, path: Path) -> None:
    """Write a 32-bpp .cur (BGRA bitmap + AND mask), the layout Windows loads for a custom cursor."""
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    colour = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            colour += bytes((b, g, r, a))
    row = ((width + 31) // 32) * 4
    mask = bytearray()
    for y in range(height - 1, -1, -1):
        bits = bytearray(row)
        for x in range(width):
            if pixels[x, y][3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, len(colour) + len(mask), 0, 0, 0, 0)
    data = header + bytes(colour) + bytes(mask)
    directory = struct.pack("<HHH", 0, 2, 1) + struct.pack(
        "<BBBBHHII", width, height, 0, 0, hotspot[0], hotspot[1], len(data), 22)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(directory + data)
    os.replace(tmp, path)


def build_tool_cursors(names) -> dict[str, str]:
    """One .cur per tool in a per-user temp folder; a tool whose cursor fails keeps the crosshair."""
    folder = Path(tempfile.gettempdir()) / "clipchimp-cursors"
    cursors = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.warning("no cursor folder: %s", folder)
        return cursors
    for name in names:
        try:
            art, hotspot = cursor_art(name)
            path = folder / f"{name}.cur"
            write_cursor(art, hotspot, path)
            cursors[name] = "@" + str(path).replace("\\", "/")   # Tcl reads backslashes as escapes
        except Exception:
            logging.exception("cursor for %s", name)
    return cursors


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
        # The image is the source of truth; the frame shows it at `zoom` with its top-left at `origin`.
        # `offset` = where the current image sits inside the original capture (moves with each crop).
        self.origin: Point = (screen_rect[0], screen_rect[1])
        self.zoom = 1.0
        self.offset: Point = (0, 0)
        self.moving = None
        self.outline = outline
        self.on_finish = on_finish
        self.on_cancel = on_cancel
        self.clickaway_control = clickaway_control
        self.regions_changed = regions_changed
        self.automated = automated
        self.tool = "rect"
        self.color = "#ff3b30"
        self.sizes = {name: 8 if name == "eraser" else 2 for name in self.TOOL_NAMES}
        self.cursors = build_tool_cursors(self.TOOL_NAMES)
        self.start: Point | None = None
        self.last: Point | None = None
        self.history: list[tuple[Image.Image, Image.Image, Rect]] = []
        self.future: list[tuple[Image.Image, Image.Image, Rect]] = []
        self.photo = None
        self.theme = windows_theme()
        self.pointer: Point | None = None        # the image pixel under the pointer (for the eraser ring)
        self.placing: dict | None = None         # a typed text still following the pointer, not yet dropped

        self.frame = tk.Toplevel(owner)
        self.frame.overrideredirect(True)
        self.frame.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<Leave>", self.pointer_left)
        # the canvas only: a wheel event also reaches the Toplevel, which would zoom twice
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<ButtonPress-3>", self.begin_frame_move)
        self.canvas.bind("<B3-Motion>", self.frame_move)
        self.canvas.bind("<ButtonRelease-3>", self.end_frame_move)

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
            window.bind("<Escape>", lambda _e: self.escape())
            window.bind("<Control-z>", lambda _e: self.undo())
            window.bind("<Control-y>", lambda _e: self.redo())
            window.bind("<Return>", lambda _e: self.drop_text())
            for key, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
                window.bind(f"<{key}>", lambda _e, d=(dx, dy): self.nudge_text(*d, 1))
                window.bind(f"<Shift-{key}>", lambda _e, d=(dx, dy): self.nudge_text(*d, 10))
        self.set_cursor()
        self.place_all()
        self.refresh()
        self.frame.after_idle(self.take_focus)
        if automated:
            self.frame.after(350, self.automate)

    # ---------- the toolbar: ONE drawn picture, hit-tested by position ----------
    def build_toolbar(self) -> None:
        items, dividers, pos = {}, [], TB_PAD
        for index, group in enumerate(TOOLBAR_GROUPS):
            for name in group:
                length = TB_SLIDER if name == "size" else TB_BUTTON
                items[name] = (pos, TB_PAD, pos + length, TB_PAD + TB_BUTTON)
                pos += length + TB_GAP
            pos -= TB_GAP
            if index < len(TOOLBAR_GROUPS) - 1:
                dividers.append(pos + TB_GROUP_GAP / 2)
                pos += TB_GROUP_GAP
        self.toolbar_items, self.toolbar_dividers = items, dividers    # rects are toolbar-local
        self.toolbar_size = (pos + TB_PAD, TB_BUTTON + 2 * TB_PAD)
        self.toolbar_canvas.configure(width=self.toolbar_size[0], height=self.toolbar_size[1])
        self.tb_hover = self.tb_press = None
        self.tb_drag = False
        self.toolbar_photo = None
        self.toolbar_image = self.toolbar_canvas.create_image(0, 0, anchor="nw")
        self.toolbar_canvas.bind("<Motion>", self.toolbar_motion)
        self.toolbar_canvas.bind("<Enter>", self.toolbar_motion)    # a pointer that jumps in lights its button too
        self.toolbar_canvas.bind("<B1-Motion>", self.toolbar_motion)
        self.toolbar_canvas.bind("<Leave>", self.toolbar_leave)
        self.toolbar_canvas.bind("<ButtonPress-1>", self.toolbar_press)
        self.toolbar_canvas.bind("<ButtonRelease-1>", self.toolbar_release)
        self.draw_toolbar()

    def toolbar_picture(self) -> Image.Image:
        """The whole toolbar in its current state. Inside it is anti-aliased; its outer edge is hard,
        because the corners around it are keyed out (TRANSPARENT_KEY) and a soft edge would fringe."""
        theme, R = self.theme, 4
        W, H = self.toolbar_size
        img = Image.new("RGBA", (W * R, H * R), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        radius = min(TB_RADIUS, min(W, H) / 2)
        d.rounded_rectangle((0, 0, W * R - 1, H * R - 1), radius * R, fill=theme["border"])
        d.rounded_rectangle((R, R, W * R - 1 - R, H * R - 1 - R), (radius - 1) * R, fill=theme["bar"])
        for p in self.toolbar_dividers:
            d.rectangle((p * R - R // 2, (TB_PAD + TB_BUTTON * 0.24) * R, p * R + R // 2,
                         (TB_PAD + TB_BUTTON * 0.76) * R), fill=theme["divider"])
        for name, rect in self.toolbar_items.items():
            x0, y0, x1, y1 = (c * R for c in rect)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            chosen = name == self.tool
            tile = theme["press"] if self.tb_press == name else theme["hover"] if self.tb_hover == name else None
            if chosen:
                tile = theme["press"] if self.tb_press == name else theme["chosen"]
            if tile and name != "size" and not self.toolbar_inert(name):
                d.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), TB_TILE_RADIUS * R, fill=tile)
            if name == "colour" and self.toolbar_inert(name):
                pass                                     # erasing: colour means nothing, the slot shows the eraser
            elif name == "colour":
                r = TB_SWATCH / 2 * R
                d.rounded_rectangle((cx - r, cy - r, cx + r, cy + r), 6 * R, fill=self.color,
                                    outline=theme["ring"], width=int(1.5 * R))
            elif name == "size":
                frac = (self.size - 1) / 29
                t, k = TB_TRACK * R / 2, TB_KNOB * R / 2
                a, b = x0 + 8 * R, x1 - 8 * R
                kx = a + (b - a) * frac
                d.rounded_rectangle((a, cy - t, b, cy + t), t, fill=theme["track"])
                d.rounded_rectangle((a, cy - t, max(a + 2 * t, kx), cy + t), t, fill=theme["fill"])
                k *= 1.15 if (self.tb_hover == "size" or self.tb_drag) else 1.0
                d.ellipse((kx - k, cy - k, kx + k, cy + k), fill=theme["knob"],
                          outline=theme["knob_edge"], width=int(1.25 * R))
        img = img.resize((W, H), Image.Resampling.LANCZOS)
        img.putalpha(img.getchannel("A").point(lambda a: 255 if a >= 128 else 0))
        for name, (x0, y0, x1, y1) in self.toolbar_items.items():
            if name == "size" or (name == "colour" and not self.toolbar_inert(name)):
                continue
            ink = theme["chosen_ink"] if name == self.tool else theme["ink"]
            icon = load_icon("eraser" if name == "colour" else name, ink, TB_ICON)
            img.alpha_composite(icon, (int((x0 + x1 - icon.width) / 2), int((y0 + y1 - icon.height) / 2)))
        return img

    def draw_toolbar(self) -> None:
        picture = Image.new("RGBA", self.toolbar_size, TRANSPARENT_KEY)
        picture.alpha_composite(self.toolbar_picture())
        self.toolbar_photo = ImageTk.PhotoImage(picture.convert("RGB"))
        self.toolbar_canvas.itemconfigure(self.toolbar_image, image=self.toolbar_photo)

    def toolbar_inert(self, name: str) -> bool:
        """While erasing, the colour square shows the eraser and does nothing."""
        return name == "colour" and self.tool == "eraser"

    def toolbar_hit(self, x: int, y: int) -> str | None:
        for name, (x0, y0, x1, y1) in self.toolbar_items.items():
            if x0 <= x < x1 and y0 <= y < y1:
                return None if self.toolbar_inert(name) else name
        return None

    def set_size_from(self, x: int) -> None:
        x0, _, x1, _ = self.toolbar_items["size"]
        frac = min(1.0, max(0.0, (x - x0 - 8) / max(1, x1 - x0 - 16)))
        self.sizes[self.tool] = int(round(1 + 29 * frac))
        self.draw_overlays()                     # a floating text or the eraser ring follows the new size

    def toolbar_motion(self, event) -> None:
        if self.tb_drag:
            self.set_size_from(event.x); self.draw_toolbar(); return
        hot = self.toolbar_hit(event.x, event.y)
        if hot != self.tb_hover:
            self.tb_hover = hot
            self.toolbar_canvas.configure(cursor="hand2" if hot else "")
            self.draw_toolbar()

    def toolbar_leave(self, _event) -> None:
        if not self.tb_drag and self.tb_hover:
            self.tb_hover = None; self.draw_toolbar()

    def toolbar_press(self, event) -> None:
        name = self.toolbar_hit(event.x, event.y)
        if not name:
            return
        self.tb_press = name
        if name == "size":
            self.tb_drag = True; self.set_size_from(event.x)
        self.draw_toolbar()
        if name == "undo": self.undo()
        elif name == "redo": self.redo()
        elif name == "colour":
            self.choose_color()
            self.tb_press = None; self.draw_toolbar()
        elif name != "size": self.choose_tool(name)

    def toolbar_release(self, _event) -> None:
        self.tb_press, self.tb_drag = None, False
        self.draw_toolbar()

    def update_selected(self) -> None:
        self.draw_toolbar()

    @property
    def size(self) -> int:
        return self.sizes[self.tool]

    def choose_tool(self, name: str) -> None:
        if self.placing:
            self.drop_text()                     # picking any tool drops the floating text where it is
        self.tool = name                         # each tool keeps its own size (self.sizes)
        self.set_cursor()
        self.update_selected()
        self.draw_overlays()                     # the eraser ring shows only while the eraser is in hand

    def set_cursor(self) -> None:
        """The pointer over the clip is the selected tool's icon (the crosshair if that cursor is missing)."""
        try:
            self.canvas.configure(cursor=self.cursors.get(self.tool, "crosshair"))
        except tk.TclError:
            logging.warning("cursor refused for %s", self.tool)
            self.canvas.configure(cursor="crosshair")

    def choose_color(self) -> None:
        self.clickaway_control(True)
        try:
            picked = colorchooser.askcolor(self.color, parent=self.toolbar)[1]
        finally:
            self.clickaway_control(False)
        if picked:
            self.color = picked
            self.draw_toolbar()
            self.draw_overlays()

    def view_rect(self) -> Rect:
        """Where the frame is on screen: the image at `zoom`, top-left at `origin`."""
        width = max(1, round(self.base.width * self.zoom))
        height = max(1, round(self.base.height * self.zoom))
        return self.origin[0], self.origin[1], self.origin[0] + width, self.origin[1] + height

    def toolbar_rect(self) -> Rect:
        vx0, vy0, vx1, vy1 = virtual_screen()
        width, height = self.toolbar_size
        view = self.view_rect()
        center = (view[0] + view[2]) // 2
        left = min(max(vx0, center - width // 2), vx1 - width)
        if view[1] - height - 8 >= vy0:
            top = view[1] - height - 8
        else:
            top = min(vy1 - height, view[3] + 8)
        return left, top, left + width, top + height

    def place_all(self) -> None:
        view = self.view_rect()
        place_window(self.frame, view, activate=True)
        toolbar_rect = self.toolbar_rect()
        place_window(self.toolbar, toolbar_rect)
        self.outline.show(view)
        self.regions_changed(view, toolbar_rect)

    def take_focus(self) -> None:
        self.frame.lift()
        self.toolbar.lift()
        self.frame.focus_force()

    def state(self):
        return self.base.copy(), self.layer.copy(), self.offset

    def checkpoint(self) -> None:
        self.history.append(self.state())
        if len(self.history) > 50:
            self.history.pop(0)
        self.future.clear()

    def restore(self, saved) -> None:
        # Re-place relative to where the frame is NOW, so moving or zooming is never undone by accident.
        base, layer, offset = saved
        dx, dy = offset[0] - self.offset[0], offset[1] - self.offset[1]
        self.origin = (self.origin[0] + round(dx * self.zoom), self.origin[1] + round(dy * self.zoom))
        self.base, self.layer, self.offset = base, layer, offset
        low, high = self.zoom_limits()
        self.zoom = min(high, max(low, self.zoom))
        self.place_all(); self.refresh()

    def undo(self) -> None:
        if self.placing:                         # the floating text is not placed yet: undo just drops it
            self.placing = None; self.refresh(); return
        if self.history:
            self.future.append(self.state())
            self.restore(self.history.pop())

    def redo(self) -> None:
        if self.future and not self.placing:
            self.history.append(self.state())
            self.restore(self.future.pop())

    def composite(self) -> Image.Image:
        return Image.alpha_composite(self.base, self.layer)

    def refresh(self, temporary: Image.Image | None = None) -> None:
        shown = temporary or self.composite()
        if self.zoom != 1.0:
            size = (self.view_rect()[2] - self.origin[0], self.view_rect()[3] - self.origin[1])
            # enlarged: every image pixel stays a crisp block; shrunk: smoothed
            shown = shown.resize(size, Image.Resampling.NEAREST if self.zoom > 1 else Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.configure(width=shown.width, height=shown.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.draw_overlays()

    # ---------- overlays: the eraser ring and the floating text (canvas items, never in the image) ----------
    def draw_overlays(self) -> None:
        self.canvas.delete("overlay")
        if self.placing:
            self.draw_floating_text()
        elif self.tool == "eraser" and self.pointer and not self.moving:
            # exactly what one dab of the eraser clears: a circle 3 × size across (see draw_freehand)
            r = max(1, self.size) * 1.5 * self.zoom
            cx, cy = self.pointer[0] * self.zoom, self.pointer[1] * self.zoom
            box = (cx - r, cy - r, cx + r, cy + r)
            self.canvas.create_oval(*box, outline="#ffffff", width=3, tags="overlay")   # reads on dark pixels
            self.canvas.create_oval(*box, outline=CURSOR_INK, width=1, tags="overlay")  # and on light ones

    def draw_text(self, layer: Image.Image, point: Point, text: str) -> None:
        ImageDraw.Draw(layer).text(point, text, fill=self.rgba(), font=self.get_font(max(12, self.size * 4)),
                                   stroke_width=max(0, self.size // 5), stroke_fill=(0, 0, 0, 210))

    def draw_floating_text(self) -> None:
        p = self.placing
        key = (p["text"], self.color, self.size, self.zoom)
        if p.get("key") != key:                   # rebuild the picture only when its look changes
            font, stroke = self.get_font(max(12, self.size * 4)), max(0, self.size // 5)
            box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), p["text"], font=font, stroke_width=stroke)
            sprite = Image.new("RGBA", (max(1, box[2] - box[0]), max(1, box[3] - box[1])), (0, 0, 0, 0))
            self.draw_text(sprite, (-box[0], -box[1]), p["text"])
            if self.zoom != 1.0:
                size = (max(1, round(sprite.width * self.zoom)), max(1, round(sprite.height * self.zoom)))
                sprite = sprite.resize(size, Image.Resampling.NEAREST if self.zoom > 1 else Image.Resampling.BILINEAR)
            p.update(key=key, box=box, photo=ImageTk.PhotoImage(sprite), size=sprite.size)
        x = round((p["pos"][0] + p["box"][0]) * self.zoom)
        y = round((p["pos"][1] + p["box"][1]) * self.zoom)
        w, h = p["size"]
        self.canvas.create_image(x, y, image=p["photo"], anchor="nw", tags="overlay")
        # a thin dashed frame says "still moving": it follows the pointer until a click drops it
        frame = (x - 4, y - 4, x + w + 3, y + h + 3)
        self.canvas.create_rectangle(*frame, outline="#ffffff", width=1, tags="overlay")
        self.canvas.create_rectangle(*frame, outline=TOOLBAR_DARK["chosen"], width=1, dash=(4, 3), tags="overlay")

    def hover(self, event) -> None:
        point = self.point(event)
        if self.placing:
            if point and point != self.placing["pos"]:
                self.placing["pos"] = point; self.draw_overlays()
            return
        self.pointer = point
        if self.tool == "eraser":
            self.draw_overlays()

    def pointer_left(self, _event) -> None:
        self.pointer = None
        if not self.placing:
            self.draw_overlays()

    # ---------- placing text: it follows the pointer; click (or Enter) drops it, arrows nudge, Esc drops it out ----------
    def drop_text(self) -> None:
        if not self.placing:
            return
        p, self.placing = self.placing, None
        self.checkpoint()
        self.draw_text(self.layer, p["pos"], p["text"])
        self.refresh()

    def nudge_text(self, dx: int, dy: int, step: int) -> None:
        if not self.placing:
            return
        x, y = self.placing["pos"]
        self.placing["pos"] = (min(self.base.width - 1, max(0, x + dx * step)),
                               min(self.base.height - 1, max(0, y + dy * step)))
        self.draw_overlays()

    def escape(self) -> None:
        if self.placing:
            self.placing = None; self.refresh()          # only the floating text goes; the clip stays open
        else:
            self.cancel()

    def to_image(self, event) -> Point:
        """Canvas (screen) pixels → image pixels."""
        return int(event.x / self.zoom), int(event.y / self.zoom)

    def point(self, event) -> Point | None:
        x, y = self.to_image(event)
        if 0 <= event.x and 0 <= event.y and x < self.base.width and y < self.base.height:
            return x, y
        return None

    # ---------- zoom (wheel) and move (right button) ----------
    def zoom_limits(self) -> tuple[float, float]:
        width, height = self.base.size
        high = min(ZOOM_MAX, math.sqrt(ZOOM_MAX_PIXELS / (width * height)))
        low = max(ZOOM_MIN, ZOOM_MIN_SIDE / min(width, height))
        return min(1.0, low), max(1.0, high)

    def wheel(self, event) -> None:
        if self.start or self.moving or not event.delta:
            return
        low, high = self.zoom_limits()
        new = min(high, max(low, self.zoom * ZOOM_STEP ** (event.delta / 120)))
        if abs(new - 1.0) < 1e-6:
            new = 1.0
        if abs(new - self.zoom) < 1e-9:
            return
        # the image pixel under the pointer stays under the pointer
        ratio = new / self.zoom
        self.origin = (round(self.origin[0] + event.x - event.x * ratio),
                       round(self.origin[1] + event.y - event.y * ratio))
        self.zoom = new
        self.place_all(); self.refresh()

    def begin_frame_move(self, event) -> None:
        if self.start:
            return
        self.moving = (event.x_root, event.y_root, self.origin)
        self.canvas.configure(cursor="fleur")

    def frame_move(self, event) -> None:
        if not self.moving:
            return
        x0, y0, (ox, oy) = self.moving
        self.origin = (ox + event.x_root - x0, oy + event.y_root - y0)
        self.place_all()

    def end_frame_move(self, event) -> None:
        if self.moving:
            self.frame_move(event)
            self.moving = None
            self.set_cursor()

    def rgba(self, alpha: int = 255):
        value = self.color.lstrip("#")
        return int(value[:2], 16), int(value[2:4], 16), int(value[4:], 16), alpha

    def press(self, event) -> None:
        point = self.point(event)
        if self.moving:
            return
        if self.placing:                         # the click that drops the floating text
            if point:
                self.placing["pos"] = point
            self.drop_text()
            return
        if point is None:
            return
        if self.tool == "text":
            self.clickaway_control(True)
            try:
                text = simpledialog.askstring(APP_NAME, "Text to add:", parent=self.frame)
            finally:
                self.clickaway_control(False)
            if text:
                self.placing = {"text": text, "pos": point}    # it now follows the pointer until dropped
                self.frame.focus_force()                        # so Enter / arrows / Esc reach it
                self.draw_overlays()
            return
        self.checkpoint()
        self.start = self.last = point
        if self.tool == "eraser":                # a single click rubs out exactly the ring shown
            self.draw_freehand(point, point); self.refresh()

    def move(self, event) -> None:
        point = self.point(event)
        if point is not None:
            self.pointer = point                 # the eraser ring rides along while rubbing out
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
            x, y = self.to_image(event)
            point = (min(self.base.width - 1, max(0, x)), min(self.base.height - 1, max(0, y)))
        if self.tool in ("rect", "ellipse", "arrow", "line"):
            self.draw_shape(self.layer, self.tool, self.start, point)
        elif self.tool == "crop":
            x0, x1 = sorted((self.start[0], point[0])); y0, y1 = sorted((self.start[1], point[1]))
            if x1 - x0 >= 4 and y1 - y0 >= 4:
                self.base = self.base.crop((x0, y0, x1, y1))
                self.layer = self.layer.crop((x0, y0, x1, y1))
                self.origin = (self.origin[0] + round(x0 * self.zoom), self.origin[1] + round(y0 * self.zoom))
                self.offset = (self.offset[0] + x0, self.offset[1] + y0)
                low, high = self.zoom_limits()
                self.zoom = min(high, max(low, self.zoom))
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
        self.drop_text()                         # a floating text is saved where it floats, never lost
        self.on_finish(self.composite())

    def cancel(self) -> None:
        self.on_cancel()

    def destroy(self) -> None:
        self.frame.destroy(); self.toolbar.destroy()


def settings() -> dict:
    """Per-user settings (optional): %APPDATA%\\ClipChimp\\settings.json, e.g. {"open": "<program or folder>"}."""
    import json
    path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception("could not read %s", path)
        return {}


def launch_as_user(target: str) -> None:
    """Open a folder or start a program as the signed-in user, never as administrator: Explorer's own desktop
    shell does the launch, so whatever opens gets Explorer's rights, not ClipChimp's."""
    import pythoncom
    import win32com.client
    from win32com.client import VARIANT
    pythoncom.CoInitialize()
    try:
        windows = win32com.client.dynamic.Dispatch("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}")   # ShellWindows
        desktop = windows.FindWindowSW(VARIANT(pythoncom.VT_I4, 0), VARIANT(pythoncom.VT_EMPTY, None), 8,
                                       VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0), 1)
        desktop.Document.Application.ShellExecute(target, "", "", "open", 1)
    finally:
        pythoncom.CoUninitialize()


def resource(name: str) -> Path:
    return Path(__file__).with_name(name)


class Tray(threading.Thread):
    """The tray chimp. Left-click opens; right-click: Open · Pause clipping · Quit. Actions go to the Tk thread
    through a queue (Tk is not thread-safe)."""
    CALLBACK = win32con.WM_APP + 1
    OPEN, PAUSE, QUIT = 1, 2, 3

    def __init__(self, actions: "queue.Queue[str]", paused: threading.Event):
        super().__init__(name="ClipChimpTray", daemon=True)
        self.actions, self.paused = actions, paused
        self.hwnd = 0
        self.taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

    def run(self) -> None:
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = "ClipChimpTray"
            wc.lpfnWndProc = self.wndproc
            win32gui.RegisterClass(wc)
            self.hwnd = win32gui.CreateWindow(wc.lpszClassName, APP_NAME, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
            # Running as administrator, Windows filters the shell's messages to us unless we let them in.
            for message in (self.CALLBACK, self.taskbar_created):
                user32.ChangeWindowMessageFilterEx(self.hwnd, message, 1, None)
            self.add()
            win32gui.PumpMessages()
        except Exception:
            logging.exception("tray failed")

    def icon(self):
        size = user32.GetSystemMetrics(49)   # SM_CXSMICON, already scaled for this display
        try:
            return win32gui.LoadImage(0, str(resource("clipchimp.ico")), win32con.IMAGE_ICON, size, size,
                                      win32con.LR_LOADFROMFILE)
        except Exception:
            logging.exception("tray icon missing")
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def add(self) -> None:
        tip = APP_NAME + (" (paused)" if self.paused.is_set() else "")
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self.hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE |
                                                     win32gui.NIF_TIP, self.CALLBACK, self.icon(), tip))

    def refresh(self) -> None:
        tip = APP_NAME + (" (paused)" if self.paused.is_set() else "")
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (self.hwnd, 0, win32gui.NIF_TIP, self.CALLBACK, 0, tip))

    def menu(self) -> None:
        m = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(m, win32con.MF_STRING, self.OPEN, "Open")
        win32gui.AppendMenu(m, win32con.MF_STRING | (win32con.MF_CHECKED if self.paused.is_set() else 0),
                            self.PAUSE, "Pause clipping")
        win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(m, win32con.MF_STRING, self.QUIT, "Quit")
        win32gui.SetMenuDefaultItem(m, self.OPEN, False)
        win32gui.SetForegroundWindow(self.hwnd)
        x, y = win32gui.GetCursorPos()
        choice = win32gui.TrackPopupMenu(m, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(m)
        if choice == self.OPEN:
            self.actions.put("open")
        elif choice == self.PAUSE:
            self.paused.clear() if self.paused.is_set() else self.paused.set()
            self.refresh()
        elif choice == self.QUIT:
            self.actions.put("quit")

    def wndproc(self, hwnd, message, wparam, lparam):
        if message == self.CALLBACK:
            if lparam == win32con.WM_LBUTTONUP:
                self.actions.put("open")
            elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                self.menu()
            return 0
        if message == self.taskbar_created:   # Explorer restarted: put the chimp back
            self.add()
            return 0
        if message == win32con.WM_DESTROY:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def stop(self) -> None:
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)


class ClipChimpApp:
    def __init__(self, selftest_path: Path | None = None):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = self.report_tk_exception
        self.selftest_path = selftest_path
        self.selftest = selftest_path is not None
        self.hook: HookThread | None = None
        self.tray: Tray | None = None
        self.actions: "queue.Queue[str]" = queue.Queue()
        self.outline = DottedOutline(self.root)
        self.annotator: FrameAnnotator | None = None
        self.last_drag_revision = -1
        self.pending_rect: Rect | None = None
        if self.selftest:
            self.root.after(100, self.begin_selftest)
        else:
            self.hook = HookThread(); self.hook.start()
            self.tray = Tray(self.actions, self.hook.paused); self.tray.start()
            self.root.after(25, self.poll)

    def poll(self) -> None:
        if self.hook.failure.is_set():
            self.report_error(RuntimeError("Could not install the global mouse hook")); self.root.destroy(); return
        while not self.actions.empty():          # from the tray (its own thread)
            action = self.actions.get_nowait()
            if action == "quit":
                self.root.destroy(); return
            if action == "open":
                self.open_clips()
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

    def open_clips(self) -> None:
        """The tray's Open: the clips folder, or the program/folder named by \"open\" in settings.json."""
        target = settings().get("open")
        try:
            if not target:
                clips_directory().mkdir(parents=True, exist_ok=True)
                target = str(clips_directory())
            launch_as_user(target)
        except Exception as exc:
            self.report_error(exc)

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
            if self.tray: self.tray.stop()


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
