"""
Map Poster Generator - Full GUI Mode (Tkinter)
Custom dark web-style interface with Poppins font.

Launch with:  python create_map_poster.py GUI
"""

import io
import json
import math
import os
import platform
import random
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

# ---------------------------------------------------------------------------
# Constants / Colours
# ---------------------------------------------------------------------------
THEMES_DIR = "themes"
POSTERS_DIR = "posters"
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
FILE_ENCODING = "utf-8"

C_BG       = "#111117"
C_PANEL    = "#1a1a24"
C_SECTION  = "#22222e"
C_HEADER   = "#2a2a38"
C_HOVER    = "#32324a"
C_ACCENT   = "#6c5ce7"
C_ACCENT_H = "#7f70f0"
C_TEXT     = "#e0e0e8"
C_TEXT_DIM = "#888898"
C_TEXT_MUT = "#555566"
C_BORDER   = "#2e2e3e"
C_INPUT_BG = "#181822"
C_INPUT_FG = "#d0d0dc"
C_GREEN    = "#00b894"
C_RED      = "#e74c3c"
C_ORANGE   = "#f39c12"
C_BLUE_NET = "#0984e3"

PRESET_CITIES = [
    ("New York", "USA", 12000), ("Paris", "France", 10000),
    ("London", "UK", 15000), ("Tokyo", "Japan", 15000),
    ("Barcelona", "Spain", 8000), ("Venice", "Italy", 4000),
    ("Amsterdam", "Netherlands", 6000), ("Dubai", "UAE", 15000),
    ("Rome", "Italy", 8000), ("Moscow", "Russia", 12000),
    ("San Francisco", "USA", 10000), ("Sydney", "Australia", 12000),
    ("Mumbai", "India", 18000), ("Budapest", "Hungary", 8000),
    ("Marrakech", "Morocco", 5000), ("Istanbul", "Turkey", 12000),
    ("Berlin", "Germany", 12000), ("Singapore", "Singapore", 10000),
    ("Hong Kong", "China", 8000), ("Chicago", "USA", 12000),
    ("Los Angeles", "USA", 18000), ("Cairo", "Egypt", 12000),
    ("Bangkok", "Thailand", 12000), ("Seoul", "South Korea", 12000),
    ("Melbourne", "Australia", 12000), ("Toronto", "Canada", 12000),
    ("Lisbon", "Portugal", 8000), ("Prague", "Czech Republic", 8000),
    ("Vienna", "Austria", 10000), ("Athens", "Greece", 10000),
]

OUTPUT_FORMATS = ["png", "svg", "pdf"]

DISTANCE_PRESETS = {
    "Neighborhood (3 km)": 3000, "Small / Dense (5 km)": 5000,
    "Medium Downtown (8 km)": 8000, "City Center (10 km)": 10000,
    "Full City (15 km)": 15000, "Metro Area (18 km)": 18000,
    "Wide Area (25 km)": 25000, "Regional (30 km)": 30000,
    "Large Metro (40 km)": 40000, "Greater City (50 km)": 50000,
    "Conurbation (60 km)": 60000, "Mega Region (70 km)": 70000,
    "Super Region (80 km)": 80000, "Giant Region (90 km)": 90000,
    "Max Region (100 km)": 100000, "Country Scale (120 km)": 120000,
    "Wide Country (150 km)": 150000, "Macro Region (200 km)": 200000,
}

ZOOM_LABELS = {
    3000: "Street-level", 5000: "Neighborhood", 8000: "District",
    10000: "City center", 12000: "Inner city", 15000: "Full city",
    18000: "Metro overview", 25000: "Wide-area", 30000: "Regional",
    40000: "Large metro", 50000: "Greater city",
    60000: "Conurbation", 70000: "Mega region",
    80000: "Super region", 90000: "Giant region", 100000: "Max region",
    120000: "Country scale", 150000: "Wide country", 200000: "Macro region",
}

DPI_OPTIONS = [150, 200, 300, 400, 600]

FONT_FAMILY = "Poppins"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_all_themes():
    themes = {}
    if not os.path.isdir(THEMES_DIR):
        return themes
    for fname in sorted(os.listdir(THEMES_DIR)):
        if fname.endswith(".json"):
            path = os.path.join(THEMES_DIR, fname)
            try:
                with open(path, "r", encoding=FILE_ENCODING) as fh:
                    themes[fname[:-5]] = json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
    return themes


def _zoom_label(dist):
    best = "Custom"
    for d, label in sorted(ZOOM_LABELS.items()):
        if dist >= d:
            best = label
    return best


def _estimated_filesize(fmt, w, h, dpi):
    if fmt == "png":
        mb = w * dpi * h * dpi * 3 / (1024 * 1024) * 0.15
    elif fmt == "svg":
        mb = w * h * 0.08
    else:
        mb = w * h * 0.25
    return f"~{mb*1024:.0f} KB" if mb < 1 else f"~{mb:.1f} MB"


def _open_path(path):
    if platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    elif platform.system() == "Windows":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def _is_dark(hex_color):
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (r * 0.299 + g * 0.587 + b * 0.114) < 128
    except Exception:
        return True


def _format_bytes(n):
    if n < 1024:
        return f"{n:.0f} B"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    else:
        return f"{n/(1024*1024):.2f} MB"


def _deg2tile_float(lat, lon, zoom):
    """Return exact (float) tile coordinates for lat/lon at zoom."""
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x_f = (lon + 180.0) / 360.0 * n
    y_f = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x_f, y_f


def _deg2tile(lat, lon, zoom):
    x_f, y_f = _deg2tile_float(lat, lon, zoom)
    return int(x_f), int(y_f)


def _fetch_tile_preview(lat, lon, dist_m, canvas_w, canvas_h):
    """Fetch OSM tiles covering the area bbox and return (PIL Image, zoom, cx, cy).
    cx/cy are the canvas pixel coordinates of the exact lat/lon point.
    Returns (None, 0, 0, 0) on failure."""
    try:
        import requests
        from PIL import Image
    except ImportError:
        return None, 0, 0, 0

    # Pick zoom so the full diameter fits in canvas_w pixels.
    lat_r = math.radians(lat)
    zoom = 12
    for z in range(17, 3, -1):
        mpp = 156543.03 * math.cos(lat_r) / (2 ** z)  # meters per pixel
        if dist_m * 2.4 / mpp <= canvas_w:
            zoom = z
            break

    # Exact float tile position of the coordinate
    x_f, y_f = _deg2tile_float(lat, lon, zoom)
    tx_c, ty_c = int(x_f), int(y_f)

    half_tx = math.ceil(canvas_w / 2 / 256) + 1
    half_ty = math.ceil(canvas_h / 2 / 256) + 1

    n = 2 ** zoom
    x0 = max(0, tx_c - half_tx)
    x1 = min(n - 1, tx_c + half_tx)
    y0 = max(0, ty_c - half_ty)
    y1 = min(n - 1, ty_c + half_ty)

    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    mosaic = Image.new("RGB", (cols * 256, rows * 256), (30, 30, 40))

    headers = {"User-Agent": "maptoposter/1.0 (area preview)"}
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            try:
                resp = requests.get(url, headers=headers, timeout=8)
                if resp.status_code == 200:
                    tile = Image.open(io.BytesIO(resp.content)).convert("RGB")
                    mosaic.paste(tile, ((tx - x0) * 256, (ty - y0) * 256))
            except Exception:
                pass

    # Exact pixel in mosaic for the target coordinate
    px = (x_f - x0) * 256
    py = (y_f - y0) * 256

    # Crop canvas_w × canvas_h centred on the exact coordinate pixel
    left = px - canvas_w / 2
    top  = py - canvas_h / 2
    left = max(0.0, min(left, cols * 256 - canvas_w))
    top  = max(0.0, min(top,  rows * 256 - canvas_h))
    left, top = int(left), int(top)
    cropped = mosaic.crop((left, top, left + canvas_w, top + canvas_h))

    # The coordinate lands at this pixel inside the cropped canvas
    cx = int(px - left)
    cy = int(py - top)
    return cropped, zoom, cx, cy


# ---------------------------------------------------------------------------
# Network tracker
# ---------------------------------------------------------------------------

class NetTracker:
    """
    OS-level network tracker using psutil (falls back to urllib3 hook if unavailable).
    psutil.net_io_counters() gives real system bytes so we catch ALL traffic
    regardless of which HTTP lib / streaming method osmnx uses internally.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._use_psutil = False
        self._baseline_recv = 0
        self._baseline_sent = 0
        self._prev_recv = 0
        self._prev_sent = 0
        self.dl = 0       # total bytes received since reset
        self.ul = 0       # total bytes sent since reset
        self.dl_rate = 0.0
        self.ul_rate = 0.0
        # urllib3 fallback counters
        self._ub_dl = 0
        self._ub_prev_dl = 0

        try:
            import psutil
            c = psutil.net_io_counters()
            self._baseline_recv = c.bytes_recv
            self._baseline_sent = c.bytes_sent
            self._prev_recv = c.bytes_recv
            self._prev_sent = c.bytes_sent
            self._use_psutil = True
        except Exception:
            pass

    def reset(self):
        """Call at the start of a generation to zero the counters."""
        with self._lock:
            if self._use_psutil:
                try:
                    import psutil
                    c = psutil.net_io_counters()
                    self._baseline_recv = c.bytes_recv
                    self._baseline_sent = c.bytes_sent
                    self._prev_recv = c.bytes_recv
                    self._prev_sent = c.bytes_sent
                except Exception:
                    pass
            self.dl = 0
            self.ul = 0
            self._ub_dl = 0
            self._ub_prev_dl = 0

    def add_download(self, n):
        """urllib3 fallback path."""
        with self._lock:
            self._ub_dl += n

    def tick(self, interval):
        with self._lock:
            if self._use_psutil:
                try:
                    import psutil
                    c = psutil.net_io_counters()
                    self.dl = c.bytes_recv - self._baseline_recv
                    self.ul = c.bytes_sent - self._baseline_sent
                    self.dl_rate = (c.bytes_recv - self._prev_recv) / max(interval, 0.1)
                    self.ul_rate = (c.bytes_sent - self._prev_sent) / max(interval, 0.1)
                    self._prev_recv = c.bytes_recv
                    self._prev_sent = c.bytes_sent
                except Exception:
                    pass
            else:
                self.dl = self._ub_dl
                self.dl_rate = (self._ub_dl - self._ub_prev_dl) / max(interval, 0.1)
                self._ub_prev_dl = self._ub_dl

    def snapshot(self):
        with self._lock:
            return self.dl, self.ul, self.dl_rate, self.ul_rate


NET = NetTracker()

_patched = False


def _install_net_hooks():
    """Fallback urllib3 patch — only used when psutil is unavailable."""
    global _patched
    if _patched or NET._use_psutil:
        return
    _patched = True
    try:
        import urllib3
        _orig_read = urllib3.response.HTTPResponse.read

        def _tracked_read(self, amt=None, **kw):
            data = _orig_read(self, amt, **kw)
            if data:
                NET.add_download(len(data))
            return data

        urllib3.response.HTTPResponse.read = _tracked_read

        # Also patch read_chunked for chunked transfer encoding
        if hasattr(urllib3.response.HTTPResponse, 'read_chunked'):
            _orig_rc = urllib3.response.HTTPResponse.read_chunked

            def _tracked_read_chunked(self, amt=None, **kw):
                for chunk in _orig_rc(self, amt, **kw):
                    if chunk:
                        NET.add_download(len(chunk))
                    yield chunk

            urllib3.response.HTTPResponse.read_chunked = _tracked_read_chunked
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Custom flat widgets
# ---------------------------------------------------------------------------

class FlatButton(tk.Frame):
    def __init__(self, parent, text="", command=None, bg=C_ACCENT, fg="#ffffff",
                 hover_bg=C_ACCENT_H, font_size=11, padx=18, pady=8, **kw):
        super().__init__(parent, bg=bg, cursor="hand2", **kw)
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg
        self._disabled = False
        self._label = tk.Label(
            self, text=text, bg=bg, fg=fg,
            font=(FONT_FAMILY, font_size, "bold"),
            padx=padx, pady=pady, cursor="hand2")
        self._label.pack(fill=tk.BOTH, expand=True)
        for w in (self, self._label):
            w.bind("<Enter>", lambda e: self._on_enter())
            w.bind("<Leave>", lambda e: self._on_leave())
            w.bind("<ButtonRelease-1>", lambda e: self._on_click())

    def _on_enter(self):
        if not self._disabled:
            self.config(bg=self._hover_bg)
            self._label.config(bg=self._hover_bg)

    def _on_leave(self):
        if not self._disabled:
            self.config(bg=self._bg)
            self._label.config(bg=self._bg)

    def _on_click(self):
        if not self._disabled and self._cmd:
            self._cmd()

    def set_disabled(self, val):
        self._disabled = val
        self._label.config(fg=C_TEXT_MUT if val else self._fg)

    def configure_text(self, text):
        self._label.config(text=text)


class FlatEntry(tk.Entry):
    def __init__(self, parent, textvariable=None, width=20, **kw):
        super().__init__(parent, textvariable=textvariable, width=width,
                         bg=C_INPUT_BG, fg=C_INPUT_FG, insertbackground=C_TEXT,
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
                         font=(FONT_FAMILY, 11), **kw)


class FlatOptionMenu(tk.Frame):
    def __init__(self, parent, variable, values, command=None, width=20):
        super().__init__(parent, bg=C_INPUT_BG, highlightthickness=1,
                         highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
        self._var = variable
        self._cmd = command
        self._values = list(values)

        self._label = tk.Label(self, textvariable=variable, bg=C_INPUT_BG,
                               fg=C_INPUT_FG, font=(FONT_FAMILY, 11),
                               anchor="w", padx=6, width=width)
        self._label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._arrow = tk.Label(self, text="\u25be", bg=C_INPUT_BG, fg=C_TEXT_DIM,
                               font=(FONT_FAMILY, 10), padx=4)
        self._arrow.pack(side=tk.RIGHT)

        for w in (self, self._label, self._arrow):
            w.bind("<Button-1>", self._open_menu)

    def _open_menu(self, event=None):
        menu = tk.Menu(self, tearoff=0, bg=C_SECTION, fg=C_TEXT,
                       activebackground=C_ACCENT, activeforeground="#fff",
                       font=(FONT_FAMILY, 11), bd=0, relief="flat")
        for v in self._values:
            menu.add_command(label=v, command=lambda val=v: self._select(val))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _select(self, val):
        self._var.set(val)
        if self._cmd:
            self._cmd()

    def set_values(self, vals):
        self._values = list(vals)


class FlatCheck(tk.Frame):
    def __init__(self, parent, text="", variable=None, command=None):
        parent_bg = C_PANEL
        try:
            parent_bg = parent["bg"]
        except Exception:
            pass
        super().__init__(parent, bg=parent_bg)
        self._var = variable or tk.BooleanVar(value=False)
        self._cmd = command

        self._box = tk.Canvas(self, width=16, height=16, bg=C_INPUT_BG,
                              highlightthickness=1, highlightbackground=C_BORDER, bd=0)
        self._box.pack(side=tk.LEFT, padx=(0, 6))
        self._box.bind("<Button-1>", self._toggle)

        self._lbl = tk.Label(self, text=text, bg=self["bg"], fg=C_TEXT,
                             font=(FONT_FAMILY, 11))
        self._lbl.pack(side=tk.LEFT)
        self._lbl.bind("<Button-1>", self._toggle)

        self._var.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _toggle(self, e=None):
        self._var.set(not self._var.get())
        if self._cmd:
            self._cmd()

    def _redraw(self):
        self._box.delete("all")
        if self._var.get():
            self._box.create_rectangle(0, 0, 16, 16, fill=C_ACCENT, outline="")
            self._box.create_text(8, 8, text="\u2713", fill="#fff",
                                  font=(FONT_FAMILY, 10, "bold"))
        else:
            self._box.create_rectangle(0, 0, 16, 16, fill=C_INPUT_BG, outline=C_BORDER)


class FlatRadio(tk.Frame):
    def __init__(self, parent, values, variable, command=None):
        super().__init__(parent, bg=parent["bg"])
        self._var = variable
        self._cmd = command
        self._buttons = []
        for val in values:
            lbl = tk.Label(self, text=val, bg=C_INPUT_BG, fg=C_TEXT_DIM,
                           font=(FONT_FAMILY, 10), padx=12, pady=4, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=(0, 2))
            lbl.bind("<Button-1>", lambda e, v=val: self._select(v))
            self._buttons.append((lbl, val))
        self._var.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _select(self, val):
        self._var.set(val)
        if self._cmd:
            self._cmd()

    def _redraw(self):
        cur = self._var.get()
        for lbl, val in self._buttons:
            if val == cur:
                lbl.config(bg=C_ACCENT, fg="#fff")
            else:
                lbl.config(bg=C_INPUT_BG, fg=C_TEXT_DIM)


class FlatScale(tk.Frame):
    def __init__(self, parent, from_=0, to=100, variable=None, command=None,
                 fmt="{:.0f}"):
        super().__init__(parent, bg=parent["bg"])
        self._var = variable
        self._cmd = command
        self._fmt = fmt

        self._scale = tk.Scale(self, from_=from_, to=to, orient=tk.HORIZONTAL,
                               variable=variable, showvalue=False,
                               bg=C_SECTION, fg=C_TEXT, troughcolor=C_INPUT_BG,
                               highlightthickness=0, bd=0, sliderrelief="flat",
                               activebackground=C_ACCENT, sliderlength=14,
                               font=(FONT_FAMILY, 9), command=self._on_change)
        self._scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._lbl = tk.Label(self, text="", bg=self["bg"], fg=C_TEXT_DIM,
                             font=(FONT_FAMILY, 10), width=8, anchor="w")
        self._lbl.pack(side=tk.LEFT, padx=(6, 0))
        self._on_change(None)

    def _on_change(self, val):
        try:
            self._lbl.config(text=self._fmt.format(self._var.get()))
        except Exception:
            pass
        if self._cmd:
            self._cmd()


# ---------------------------------------------------------------------------
# Collapsible Accordion Section
# ---------------------------------------------------------------------------

class AccordionSection(tk.Frame):
    def __init__(self, parent, title, preview_func=None, open_by_default=True):
        super().__init__(parent, bg=C_PANEL)
        self._title = title
        self._preview_func = preview_func
        self._is_open = open_by_default

        self._header = tk.Frame(self, bg=C_HEADER, cursor="hand2")
        self._header.pack(fill=tk.X)

        self._arrow = tk.Label(
            self._header,
            text="\u25be" if self._is_open else "\u25b8",
            bg=C_HEADER, fg=C_ACCENT,
            font=(FONT_FAMILY, 12, "bold"), padx=8)
        self._arrow.pack(side=tk.LEFT)

        self._title_lbl = tk.Label(
            self._header, text=title, bg=C_HEADER, fg=C_TEXT,
            font=(FONT_FAMILY, 12, "bold"), pady=8)
        self._title_lbl.pack(side=tk.LEFT)

        self._preview_lbl = tk.Label(
            self._header, text="", bg=C_HEADER, fg=C_TEXT_DIM,
            font=(FONT_FAMILY, 10), padx=10)
        self._preview_lbl.pack(side=tk.RIGHT, padx=8)

        for w in (self._header, self._arrow, self._title_lbl, self._preview_lbl):
            w.bind("<Button-1>", self._toggle)

        self._body = tk.Frame(self, bg=C_PANEL, padx=12, pady=8)
        if self._is_open:
            self._body.pack(fill=tk.X)

        tk.Frame(self, bg=C_BORDER, height=1).pack(fill=tk.X)

    @property
    def body(self):
        return self._body

    def _toggle(self, event=None):
        self._is_open = not self._is_open
        self._arrow.config(text="\u25be" if self._is_open else "\u25b8")
        if self._is_open:
            self._body.pack(fill=tk.X, after=self._header)
            self._preview_lbl.config(text="")
        else:
            self._body.pack_forget()
            if self._preview_func:
                self._preview_lbl.config(text=self._preview_func())


    def update_preview(self):
        if not self._is_open and self._preview_func:
            self._preview_lbl.config(text=self._preview_func())


# ---------------------------------------------------------------------------
# Main GUI
# ---------------------------------------------------------------------------

class MapPosterGUI:

    def __init__(self, root):
        self.root = root
        self.root.title("Map Poster Generator")
        self.root.geometry("1200x860")
        self.root.minsize(1000, 700)
        self.root.configure(bg=C_BG)

        self.all_themes = _load_all_themes()
        self.generation_history = []
        self.last_output_file = None
        self.is_generating = False
        self._dot_idx = 0
        self._start_time = None
        self._cancel_event = threading.Event()
        self._area_preview_photo = None  # prevent GC

        self.var_city = tk.StringVar()
        self.var_country = tk.StringVar()
        self.var_theme = tk.StringVar(value="terracotta")
        self.var_distance = tk.IntVar(value=18000)
        self.var_width = tk.DoubleVar(value=12.0)
        self.var_height = tk.DoubleVar(value=16.0)
        self.var_format = tk.StringVar(value="png")
        self.var_dpi = tk.IntVar(value=300)
        self.var_lat = tk.StringVar()
        self.var_lon = tk.StringVar()
        self.var_display_city = tk.StringVar()
        self.var_display_country = tk.StringVar()
        self.var_country_label = tk.StringVar()
        self.var_font_family = tk.StringVar(value="Poppins")
        self.var_output_dir = tk.StringVar(value=os.path.abspath(POSTERS_DIR))
        self.var_custom_filename = tk.StringVar()
        self.var_all_themes = tk.BooleanVar(value=False)
        self.var_show_water = tk.BooleanVar(value=True)
        self.var_show_parks = tk.BooleanVar(value=True)
        self.var_show_coastline = tk.BooleanVar(value=True)
        self.var_show_gradient = tk.BooleanVar(value=True)
        self.var_show_attribution = tk.BooleanVar(value=True)
        self.var_road_width_mult = tk.DoubleVar(value=1.0)
        self.var_orientation = tk.StringVar(value="Portrait")
        self.var_zoom_label = tk.StringVar(value=_zoom_label(18000))
        self.var_est_size = tk.StringVar(value="")
        self.var_bg_override = tk.StringVar()
        self.var_text_override = tk.StringVar()
        self.var_border_size = tk.DoubleVar(value=0.05)
        self.var_preset_city = tk.StringVar()
        self.var_distance_preset = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="Idle")

        self._build_ui()
        self._draw_theme_preview()
        self._update_estimates()
        self._tick_stats()

    # ==================================================================
    # BUILD UI
    # ==================================================================
    def _build_ui(self):
        title_bar = tk.Frame(self.root, bg=C_PANEL, height=48)
        title_bar.pack(fill=tk.X)
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text="MAP POSTER GENERATOR", bg=C_PANEL, fg=C_TEXT,
                 font=(FONT_FAMILY, 14, "bold"), padx=16).pack(side=tk.LEFT)

        tb = tk.Frame(title_bar, bg=C_PANEL)
        tb.pack(side=tk.RIGHT, padx=8)
        for txt, cmd in [("Export", self._export_settings),
                         ("Import", self._import_settings),
                         ("Batch", self._show_batch_dialog),
                         ("Clear Cache", self._clear_cache)]:
            b = tk.Label(tb, text=txt, bg=C_PANEL, fg=C_TEXT_DIM,
                         font=(FONT_FAMILY, 10), padx=10, pady=6, cursor="hand2")
            b.pack(side=tk.LEFT)
            b.bind("<Enter>", lambda e, w=b: w.config(fg=C_TEXT, bg=C_HOVER))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=C_TEXT_DIM, bg=C_PANEL))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill=tk.X)

        main = tk.Frame(self.root, bg=C_BG)
        main.pack(fill=tk.BOTH, expand=True)

        left_outer = tk.Frame(main, bg=C_PANEL)
        left_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        lh = tk.Frame(left_outer, bg=C_PANEL)
        lh.pack(fill=tk.X)
        tk.Label(lh, text="GENERATION CONTROLS", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), padx=12, pady=8).pack(side=tk.LEFT)
        tk.Frame(left_outer, bg=C_BORDER, height=1).pack(fill=tk.X)

        left_scroll_area = tk.Frame(left_outer, bg=C_PANEL)
        left_scroll_area.pack(fill=tk.BOTH, expand=True)

        self._left_scrollbar = tk.Scrollbar(
            left_scroll_area, orient=tk.VERTICAL,
            bg=C_HEADER, troughcolor=C_INPUT_BG,
            activebackground=C_ACCENT, highlightthickness=0, bd=0,
            relief="flat", width=14)
        self._left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._left_canvas = tk.Canvas(
            left_scroll_area, bg=C_PANEL, highlightthickness=0, bd=0,
            yscrollcommand=self._left_scrollbar.set)
        self._left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._left_scrollbar.config(command=self._left_canvas.yview)

        self._left_scroll_frame = tk.Frame(self._left_canvas, bg=C_PANEL)
        self._left_canvas_win = self._left_canvas.create_window(
            (0, 0), window=self._left_scroll_frame, anchor="nw")

        self._left_scroll_frame.bind("<Configure>", self._on_left_configure)
        self._left_canvas.bind("<Configure>", self._on_left_canvas_resize)

        tk.Frame(main, bg=C_BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        right = tk.Frame(main, bg=C_BG, width=440)
        right.pack(side=tk.RIGHT, fill=tk.BOTH)
        right.pack_propagate(False)

        right_scroll_area = tk.Frame(right, bg=C_BG)
        right_scroll_area.pack(fill=tk.BOTH, expand=True)

        self._right_scrollbar = tk.Scrollbar(
            right_scroll_area, orient=tk.VERTICAL,
            bg=C_HEADER, troughcolor=C_INPUT_BG,
            activebackground=C_ACCENT, highlightthickness=0, bd=0,
            relief="flat", width=14)
        self._right_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Right panel scrollable canvas
        self._right_canvas = tk.Canvas(
            right_scroll_area, bg=C_BG, highlightthickness=0, bd=0,
            yscrollcommand=self._right_scrollbar.set)
        self._right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._right_scrollbar.config(command=self._right_canvas.yview)

        self._right_scroll_frame = tk.Frame(self._right_canvas, bg=C_BG)
        self._right_canvas_win = self._right_canvas.create_window(
            (0, 0), window=self._right_scroll_frame, anchor="nw")

        self._right_scroll_frame.bind("<Configure>", self._on_right_configure)
        self._right_canvas.bind("<Configure>", self._on_right_canvas_resize)

        # Global mousewheel handler
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel)
        self.root.bind_all("<Button-4>", self._on_global_mousewheel)
        self.root.bind_all("<Button-5>", self._on_global_mousewheel)

        self._build_right_panel(self._right_scroll_frame)

        self._build_left_sections()

        gen_bar = tk.Frame(left_outer, bg=C_PANEL, pady=0)
        gen_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(gen_bar, bg=C_BORDER, height=1).pack(fill=tk.X)

        btn_row = tk.Frame(gen_bar, bg=C_PANEL, pady=8, padx=12)
        btn_row.pack(fill=tk.X)
        self.btn_generate = FlatButton(
            btn_row, text="GENERATE POSTER", command=self._generate,
            bg=C_ACCENT, hover_bg=C_ACCENT_H, font_size=12, padx=20, pady=10)
        self.btn_generate.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_cancel = FlatButton(
            btn_row, text="✕ CANCEL", command=self._cancel,
            bg=C_RED, hover_bg="#c0392b", font_size=11, padx=14, pady=10)
        # hidden until generation starts

        sub_row = tk.Frame(gen_bar, bg=C_PANEL, padx=12)
        sub_row.pack(fill=tk.X, pady=(0, 8))
        for txt, cmd in [("Copy CLI", self._copy_cli_command),
                         ("Open Folder", self._open_output_folder),
                         ("Open Last", self._open_last_poster)]:
            b = tk.Label(sub_row, text=txt, bg=C_SECTION, fg=C_TEXT_DIM,
                         font=(FONT_FAMILY, 10), padx=10, pady=5, cursor="hand2")
            b.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=C_HOVER, fg=C_TEXT))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=C_SECTION, fg=C_TEXT_DIM))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        self.root.bind("<Command-Return>", lambda e: self._generate())
        self.root.bind("<Control-Return>", lambda e: self._generate())

    def _on_left_configure(self, event):
        self._left_canvas.configure(
            scrollregion=self._left_canvas.bbox("all"))

    def _on_left_canvas_resize(self, event):
        self._left_canvas.itemconfig(self._left_canvas_win, width=event.width)

    # --- Right panel scroll helpers ---
    def _on_right_configure(self, event):
        self._right_canvas.configure(
            scrollregion=self._right_canvas.bbox("all"))

    def _on_right_canvas_resize(self, event):
        self._right_canvas.itemconfig(self._right_canvas_win, width=event.width)

    # --- Global mousewheel: detect which panel cursor is over, scroll it ---
    def _is_child_of(self, widget, parent):
        """Walk up the widget tree to check if widget is a descendant of parent."""
        w = widget
        while w is not None:
            if w is parent:
                return True
            try:
                w = w.master
            except Exception:
                break
        return False

    def _on_global_mousewheel(self, event):
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return
        if w is None:
            return

        # Determine which canvas to scroll
        canvas = None
        if (self._is_child_of(w, self._left_canvas) or
                self._is_child_of(w, self._left_scroll_frame) or
                w is self._left_canvas):
            canvas = self._left_canvas
        elif (self._is_child_of(w, self._right_canvas) or
              self._is_child_of(w, self._right_scroll_frame) or
              w is self._right_canvas):
            canvas = self._right_canvas

        if canvas is None:
            return

        if getattr(event, 'num', None) == 4:
            canvas.yview_scroll(-3, "units")
        elif getattr(event, 'num', None) == 5:
            canvas.yview_scroll(3, "units")
        else:
            delta = getattr(event, 'delta', 0)
            if platform.system() == "Darwin":
                # macOS: delta is in pixels, use fraction of total height
                canvas.yview_scroll(int(-1 * (delta / 10)), "units")
            else:
                canvas.yview_scroll(-1 * (delta // 120), "units")

    # ==================================================================
    # LEFT: 7 accordion sections
    # ==================================================================
    def _build_left_sections(self):
        f = self._left_scroll_frame

        self.sec_location = AccordionSection(
            f, "Location",
            preview_func=lambda: (
                f"{self.var_city.get() or '\u2014'}, "
                f"{self.var_country.get() or '\u2014'}"),
            open_by_default=True)
        self.sec_location.pack(fill=tk.X)
        self._build_location(self.sec_location.body)

        self.sec_labels = AccordionSection(
            f, "Display Labels",
            preview_func=lambda: self.var_display_city.get() or "default",
            open_by_default=False)
        self.sec_labels.pack(fill=tk.X)
        self._build_labels(self.sec_labels.body)

        self.sec_theme = AccordionSection(
            f, "Theme",
            preview_func=lambda: self.var_theme.get(),
            open_by_default=True)
        self.sec_theme.pack(fill=tk.X)
        self._build_theme_section(self.sec_theme.body)

        self.sec_map = AccordionSection(
            f, "Map Settings",
            preview_func=lambda: (
                f"{self.var_distance.get()}m  "
                f"{_zoom_label(self.var_distance.get())}"),
            open_by_default=False)
        self.sec_map.pack(fill=tk.X)
        self._build_map_settings(self.sec_map.body)

        self.sec_output = AccordionSection(
            f, "Output",
            preview_func=lambda: (
                f"{self.var_format.get().upper()} "
                f"{self.var_width.get():.0f}x{self.var_height.get():.0f}in"),
            open_by_default=False)
        self.sec_output.pack(fill=tk.X)
        self._build_output(self.sec_output.body)

        self.sec_typo = AccordionSection(
            f, "Typography",
            preview_func=lambda: self.var_font_family.get() or "Poppins",
            open_by_default=False)
        self.sec_typo.pack(fill=tk.X)
        self._build_typography(self.sec_typo.body)

        self.sec_adv = AccordionSection(
            f, "Advanced",
            preview_func=lambda: (
                f"water={'on' if self.var_show_water.get() else 'off'} "
                f"coast={'on' if self.var_show_coastline.get() else 'off'} "
                f"road x{self.var_road_width_mult.get():.1f}"),
            open_by_default=False)
        self.sec_adv.pack(fill=tk.X)
        self._build_advanced(self.sec_adv.body)



    def _row(self, parent, label_text, widget_factory, label_width=14):
        r = tk.Frame(parent, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text=label_text, bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=label_width,
                 anchor="w").pack(side=tk.LEFT)
        w = widget_factory(r)
        if w:
            w.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return r

    def _build_location(self, body):
        self._row(body, "Preset", lambda p: FlatOptionMenu(
            p, self.var_preset_city,
            [f"{c}, {co}" for c, co, _ in PRESET_CITIES],
            command=self._on_preset_selected, width=28))
        self._row(body, "City",
                  lambda p: FlatEntry(p, self.var_city, width=28))
        self._row(body, "Country",
                  lambda p: FlatEntry(p, self.var_country, width=28))

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Lat", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=5,
                 anchor="w").pack(side=tk.LEFT)
        FlatEntry(r, self.var_lat, width=12).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(r, text="Lon", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=5,
                 anchor="w").pack(side=tk.LEFT)
        FlatEntry(r, self.var_lon, width=12).pack(side=tk.LEFT)

    def _build_labels(self, body):
        self._row(body, "Display City",
                  lambda p: FlatEntry(p, self.var_display_city))
        self._row(body, "Display Country",
                  lambda p: FlatEntry(p, self.var_display_country))
        self._row(body, "Country Label",
                  lambda p: FlatEntry(p, self.var_country_label))

    def _build_theme_section(self, body):
        self._row(body, "Theme", lambda p: FlatOptionMenu(
            p, self.var_theme, list(self.all_themes.keys()),
            command=self._draw_theme_preview, width=24))

        FlatCheck(body, text="Generate ALL themes",
                  variable=self.var_all_themes).pack(anchor="w", pady=4)

        self.lbl_theme_desc = tk.Label(
            body, text="", bg=C_PANEL, fg=C_TEXT_MUT,
            font=(FONT_FAMILY, 10), wraplength=400,
            anchor="w", justify="left")
        self.lbl_theme_desc.pack(anchor="w", pady=(0, 4))

        self.swatch_canvas = tk.Canvas(
            body, height=28, bg=C_PANEL, highlightthickness=0, bd=0)
        self.swatch_canvas.pack(fill=tk.X, pady=(0, 6))

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=2)
        tk.Label(r, text="BG", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT)
        FlatEntry(r, self.var_bg_override, width=8).pack(
            side=tk.LEFT, padx=4)
        bp = tk.Label(r, text="\u25fc", bg=C_PANEL, fg=C_ACCENT,
                      font=(FONT_FAMILY, 14), cursor="hand2")
        bp.pack(side=tk.LEFT)
        bp.bind("<Button-1>", lambda e: self._pick_color(self.var_bg_override))
        tk.Label(r, text="   Text", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT)
        FlatEntry(r, self.var_text_override, width=8).pack(
            side=tk.LEFT, padx=4)
        tp = tk.Label(r, text="\u25fc", bg=C_PANEL, fg=C_ACCENT,
                      font=(FONT_FAMILY, 14), cursor="hand2")
        tp.pack(side=tk.LEFT)
        tp.bind("<Button-1>",
                lambda e: self._pick_color(self.var_text_override))

    def _build_map_settings(self, body):
        self._row(body, "Distance Preset", lambda p: FlatOptionMenu(
            p, self.var_distance_preset, list(DISTANCE_PRESETS.keys()),
            command=self._on_distance_preset, width=24))

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Distance (m)", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatScale(r, from_=1000, to=200000, variable=self.var_distance,
                  command=self._on_distance_change,
                  fmt="{:.0f} m").pack(side=tk.LEFT, fill=tk.X, expand=True)

        r2 = tk.Frame(body, bg=C_PANEL)
        r2.pack(fill=tk.X, pady=2)
        tk.Label(r2, text="Zoom", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        self._zoom_lbl = tk.Label(
            r2, textvariable=self.var_zoom_label, bg=C_PANEL, fg=C_ACCENT,
            font=(FONT_FAMILY, 11))
        self._zoom_lbl.pack(side=tk.LEFT)

    def _build_output(self, body):
        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Orientation", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatRadio(r, ["Portrait", "Landscape", "Square"],
                  self.var_orientation,
                  command=self._on_orientation_change).pack(side=tk.LEFT)

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Size (in)", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        tk.Label(r, text="W", bg=C_PANEL, fg=C_TEXT_MUT,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT)
        FlatEntry(r, self.var_width, width=5).pack(
            side=tk.LEFT, padx=(2, 8))
        tk.Label(r, text="H", bg=C_PANEL, fg=C_TEXT_MUT,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT)
        FlatEntry(r, self.var_height, width=5).pack(side=tk.LEFT, padx=2)

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Format", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatRadio(r, ["png", "svg", "pdf"], self.var_format,
                  command=self._update_estimates).pack(side=tk.LEFT)

        self._dpi_str = tk.StringVar(value=str(self.var_dpi.get()))
        self._dpi_str.trace_add("write", lambda *_: self._set_dpi())

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="DPI (PNG)", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatOptionMenu(r, self._dpi_str,
                       [str(d) for d in DPI_OPTIONS],
                       width=8).pack(side=tk.LEFT)
        tk.Label(r, text="  Est:", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(r, textvariable=self.var_est_size, bg=C_PANEL,
                 fg=C_ORANGE,
                 font=(FONT_FAMILY, 10)).pack(side=tk.LEFT, padx=4)

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Border (in)", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatEntry(r, self.var_border_size, width=6).pack(side=tk.LEFT)

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=3)
        tk.Label(r, text="Output Dir", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatEntry(r, self.var_output_dir, width=24).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        br = tk.Label(r, text="Browse", bg=C_SECTION, fg=C_TEXT_DIM,
                      font=(FONT_FAMILY, 10), padx=8, cursor="hand2")
        br.pack(side=tk.LEFT)
        br.bind("<Button-1>", lambda e: self._browse_output_dir())

        self._row(body, "Filename",
                  lambda p: FlatEntry(p, self.var_custom_filename, width=24))
        tk.Label(body, text="Leave blank for auto-generated filename",
                 bg=C_PANEL, fg=C_TEXT_MUT,
                 font=(FONT_FAMILY, 9), anchor="w").pack(anchor="w")

    def _set_dpi(self):
        try:
            self.var_dpi.set(int(self._dpi_str.get()))
        except ValueError:
            pass
        self._update_estimates()

    def _build_typography(self, body):
        self._row(body, "Google Font",
                  lambda p: FlatEntry(p, self.var_font_family))
        tk.Label(
            body,
            text="Leave blank for default Roboto. e.g. Noto Sans JP, Open Sans",
            bg=C_PANEL, fg=C_TEXT_MUT,
            font=(FONT_FAMILY, 9), wraplength=400,
            anchor="w").pack(anchor="w")

    def _build_advanced(self, body):
        FlatCheck(body, text="Water features",
                  variable=self.var_show_water).pack(anchor="w", pady=2)
        FlatCheck(body, text="Parks / green spaces",
                  variable=self.var_show_parks).pack(anchor="w", pady=2)
        FlatCheck(body, text="Coastline",
                  variable=self.var_show_coastline).pack(anchor="w", pady=2)
        FlatCheck(body, text="Gradient fade",
                  variable=self.var_show_gradient).pack(anchor="w", pady=2)
        FlatCheck(body, text="Attribution text",
                  variable=self.var_show_attribution).pack(anchor="w", pady=2)

        r = tk.Frame(body, bg=C_PANEL)
        r.pack(fill=tk.X, pady=4)
        tk.Label(r, text="Road width x", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 11), width=14,
                 anchor="w").pack(side=tk.LEFT)
        FlatScale(
            r, from_=0.3, to=3.0, variable=self.var_road_width_mult,
            fmt="{:.1f}x").pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ==================================================================
    # RIGHT PANEL
    # ==================================================================
    def _build_right_panel(self, parent):
        sec = tk.Frame(parent, bg=C_PANEL)
        sec.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(sec, text="THEME PREVIEW", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(anchor="w")
        tk.Frame(sec, bg=C_BORDER, height=1).pack(fill=tk.X)
        self.preview_canvas = tk.Canvas(
            sec, height=200, bg="#181822", highlightthickness=0, bd=0)
        self.preview_canvas.pack(fill=tk.X, padx=0, pady=0)

        prog = tk.Frame(parent, bg=C_PANEL)
        prog.pack(fill=tk.X, padx=8, pady=4)
        hdr = tk.Frame(prog, bg=C_PANEL)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="PROGRESS", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(side=tk.LEFT)
        self._pct_label = tk.Label(
            hdr, text="0%", bg=C_PANEL, fg=C_ACCENT,
            font=(FONT_FAMILY, 11, "bold"), padx=8)
        self._pct_label.pack(side=tk.RIGHT)
        tk.Frame(prog, bg=C_BORDER, height=1).pack(fill=tk.X)

        self._prog_canvas = tk.Canvas(
            prog, height=8, bg=C_INPUT_BG, highlightthickness=0, bd=0)
        self._prog_canvas.pack(fill=tk.X, padx=8, pady=(8, 2))

        self._prog_status = tk.Label(
            prog, textvariable=self.progress_text, bg=C_PANEL,
            fg=C_TEXT_MUT, font=(FONT_FAMILY, 10), padx=8, anchor="w")
        self._prog_status.pack(anchor="w", pady=(0, 6))

        log_sec = tk.Frame(parent, bg=C_PANEL)
        log_sec.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        tk.Label(log_sec, text="CONSOLE", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(anchor="w")
        tk.Frame(log_sec, bg=C_BORDER, height=1).pack(fill=tk.X)

        self.log_text = tk.Text(
            log_sec, height=10, bg="#0d0d14", fg="#8a8a9a",
            font=(FONT_FAMILY, 10), relief="flat", bd=0,
            insertbackground=C_TEXT, padx=8, pady=6,
            wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        stats_sec = tk.Frame(parent, bg=C_SECTION)
        stats_sec.pack(fill=tk.X, padx=8, pady=4)
        tk.Label(stats_sec, text="LIVE STATS", bg=C_SECTION, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(anchor="w")
        tk.Frame(stats_sec, bg=C_BORDER, height=1).pack(fill=tk.X)

        inner = tk.Frame(stats_sec, bg=C_SECTION, padx=10, pady=8)
        inner.pack(fill=tk.X)

        self._dots_label = tk.Label(
            inner, text=".", bg=C_SECTION, fg=C_ACCENT,
            font=(FONT_FAMILY, 14, "bold"), anchor="w")
        self._dots_label.pack(anchor="w")

        net_grid = tk.Frame(inner, bg=C_SECTION)
        net_grid.pack(fill=tk.X, pady=(6, 0))

        tk.Label(net_grid, text="\u2193 Download", bg=C_SECTION,
                 fg=C_TEXT_MUT, font=(FONT_FAMILY, 9), width=14,
                 anchor="w").grid(row=0, column=0, sticky="w")
        self._dl_rate_lbl = tk.Label(
            net_grid, text="0 B/s", bg=C_SECTION, fg=C_BLUE_NET,
            font=(FONT_FAMILY, 10, "bold"), anchor="w")
        self._dl_rate_lbl.grid(row=0, column=1, sticky="w", padx=(4, 16))

        tk.Label(net_grid, text="\u2191 Upload", bg=C_SECTION,
                 fg=C_TEXT_MUT, font=(FONT_FAMILY, 9), width=10,
                 anchor="w").grid(row=0, column=2, sticky="w")
        self._ul_rate_lbl = tk.Label(
            net_grid, text="0 B/s", bg=C_SECTION, fg=C_GREEN,
            font=(FONT_FAMILY, 10, "bold"), anchor="w")
        self._ul_rate_lbl.grid(row=0, column=3, sticky="w")

        tk.Label(net_grid, text="Total \u2193", bg=C_SECTION,
                 fg=C_TEXT_MUT, font=(FONT_FAMILY, 9), width=14,
                 anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._dl_total_lbl = tk.Label(
            net_grid, text="0 B", bg=C_SECTION, fg=C_BLUE_NET,
            font=(FONT_FAMILY, 10), anchor="w")
        self._dl_total_lbl.grid(
            row=1, column=1, sticky="w", padx=(4, 16), pady=(4, 0))

        tk.Label(net_grid, text="Total \u2191", bg=C_SECTION,
                 fg=C_TEXT_MUT, font=(FONT_FAMILY, 9), width=10,
                 anchor="w").grid(row=1, column=2, sticky="w", pady=(4, 0))
        self._ul_total_lbl = tk.Label(
            net_grid, text="0 B", bg=C_SECTION, fg=C_GREEN,
            font=(FONT_FAMILY, 10), anchor="w")
        self._ul_total_lbl.grid(row=1, column=3, sticky="w", pady=(4, 0))

        self._elapsed_lbl = tk.Label(
            inner, text="", bg=C_SECTION, fg=C_TEXT_MUT,
            font=(FONT_FAMILY, 9), anchor="w")
        self._elapsed_lbl.pack(anchor="w", pady=(6, 0))

        self._eta_lbl = tk.Label(
            inner, text="", bg=C_SECTION, fg=C_TEXT_MUT,
            font=(FONT_FAMILY, 9), anchor="w")
        self._eta_lbl.pack(anchor="w", pady=(2, 0))

        hist_sec = tk.Frame(parent, bg=C_PANEL)
        hist_sec.pack(fill=tk.X, padx=8, pady=(4, 8))
        tk.Label(hist_sec, text="HISTORY", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(anchor="w")
        tk.Frame(hist_sec, bg=C_BORDER, height=1).pack(fill=tk.X)

        self._hist_container = tk.Frame(
            hist_sec, bg=C_PANEL, padx=4, pady=4)
        self._hist_container.pack(fill=tk.X)

        self._hist_empty = tk.Label(
            self._hist_container, text="No posters generated yet",
            bg=C_PANEL, fg=C_TEXT_MUT, font=(FONT_FAMILY, 10), pady=8)
        self._hist_empty.pack()

        # ---- AREA PREVIEW -----------------------------------------------
        ap_sec = tk.Frame(parent, bg=C_PANEL)
        ap_sec.pack(fill=tk.X, padx=8, pady=(4, 12))

        ap_hdr = tk.Frame(ap_sec, bg=C_PANEL)
        ap_hdr.pack(fill=tk.X)
        tk.Label(ap_hdr, text="AREA PREVIEW", bg=C_PANEL, fg=C_TEXT_DIM,
                 font=(FONT_FAMILY, 10, "bold"), pady=6,
                 padx=8).pack(side=tk.LEFT)
        self._preview_area_btn = FlatButton(
            ap_hdr, text="Preview Area", command=self._preview_area,
            bg=C_SECTION, hover_bg=C_HOVER, font_size=9, padx=10, pady=4)
        self._preview_area_btn.pack(side=tk.RIGHT, padx=8, pady=4)

        tk.Frame(ap_sec, bg=C_BORDER, height=1).pack(fill=tk.X)

        self._area_canvas = tk.Canvas(
            ap_sec, height=220, bg="#181822",
            highlightthickness=0, bd=0)
        self._area_canvas.pack(fill=tk.X, padx=0, pady=0)

        self._area_status_lbl = tk.Label(
            ap_sec, text="Enter city/country or coordinates, then click Preview Area",
            bg=C_PANEL, fg=C_TEXT_MUT, font=(FONT_FAMILY, 9),
            wraplength=360, justify="center", pady=4)
        self._area_status_lbl.pack()

    # ==================================================================
    # Theme preview
    # ==================================================================
    def _draw_theme_preview(self):
        name = self.var_theme.get()
        theme = self.all_themes.get(name, {})
        desc = theme.get("description", "")
        display_name = theme.get("name", name)
        self.lbl_theme_desc.config(
            text=f"{display_name} \u2014 {desc}" if desc else display_name)

        sc = self.swatch_canvas
        sc.delete("all")
        sc.update_idletasks()
        cw = max(sc.winfo_width(), 300)
        keys = ["bg", "text", "water", "parks", "road_motorway",
                "road_primary", "road_secondary", "road_tertiary",
                "road_residential"]
        avail = [(k, theme[k]) for k in keys if k in theme]
        if avail:
            sw = cw / len(avail)
            for i, (k, color) in enumerate(avail):
                x0 = i * sw
                sc.create_rectangle(
                    x0, 0, x0 + sw, 28, fill=color, outline="")
                tc = "#FFF" if _is_dark(color) else "#000"
                sc.create_text(
                    x0 + sw / 2, 14,
                    text=k.replace("road_", "r:").replace("_", " "),
                    fill=tc, font=(FONT_FAMILY, 7))

        c = self.preview_canvas
        c.delete("all")
        c.update_idletasks()
        cw = max(c.winfo_width(), 400)
        ch = max(c.winfo_height(), 200)

        bg = theme.get("bg", "#333")
        text_color = theme.get("text", "#FFF")
        water = theme.get("water", "#1a1a3a")
        parks = theme.get("parks", "#1a3a1a")
        roads = [theme.get(f"road_{t}", "#666") for t in
                 ["motorway", "primary", "secondary",
                  "tertiary", "residential"]]

        c.create_rectangle(0, 0, cw, ch, fill=bg, outline="")
        c.create_oval(
            cw * 0.55, ch * 0.08, cw * 0.95, ch * 0.48,
            fill=water, outline="")

        for px, py in [(0.08, 0.12), (0.3, 0.5), (0.65, 0.6)]:
            c.create_rectangle(
                cw * px, ch * py, cw * (px + 0.1), ch * (py + 0.06),
                fill=parks, outline="")

        rng = random.Random(42)
        for _ in range(25):
            rc = rng.choice(roads)
            if rng.random() > 0.5:
                y = rng.randint(int(ch * 0.04), int(ch * 0.7))
                c.create_line(
                    0, y, cw, y, fill=rc, width=rng.choice([1, 1, 2]))
            else:
                x = rng.randint(0, cw)
                c.create_line(
                    x, 0, x, int(ch * 0.75), fill=rc,
                    width=rng.choice([1, 1, 2]))
        for _ in range(6):
            rc = rng.choice(roads)
            x1 = rng.randint(0, cw)
            y1 = rng.randint(0, int(ch * 0.7))
            c.create_line(
                x1, y1, x1 + rng.randint(-120, 120),
                y1 + rng.randint(-80, 80), fill=rc)

        gc = theme.get("gradient_color", bg)
        try:
            rv, gv, bv = (
                int(gc[1:3], 16), int(gc[3:5], 16), int(gc[5:7], 16))
        except (ValueError, IndexError):
            rv, gv, bv = 30, 30, 30
        for i in range(30):
            blended = f"#{rv:02x}{gv:02x}{bv:02x}"
            ys = ch * 0.68 + i * (ch * 0.32 / 30)
            c.create_rectangle(
                0, ys, cw, ys + ch * 0.32 / 30 + 1,
                fill=blended, outline="")

        city = self.var_city.get() or "CITY NAME"
        c.create_text(cw / 2, ch * 0.82, text=city.upper(),
                      fill=text_color, font=(FONT_FAMILY, 16, "bold"))
        country = self.var_country.get() or "COUNTRY"
        c.create_text(cw / 2, ch * 0.91, text=country.upper(),
                      fill=text_color, font=(FONT_FAMILY, 10))
        c.create_line(
            cw * 0.35, ch * 0.86, cw * 0.65, ch * 0.86,
            fill=text_color, width=1)

    # ==================================================================
    # Progress bar drawing
    # ==================================================================
    def _draw_progress_bar(self):
        c = self._prog_canvas
        c.delete("all")
        c.update_idletasks()
        w = max(c.winfo_width(), 100)
        pct = self.progress_var.get() / 100.0
        c.create_rectangle(0, 0, w, 8, fill=C_INPUT_BG, outline="")
        if pct > 0:
            c.create_rectangle(0, 0, w * pct, 8, fill=C_ACCENT, outline="")
        self._pct_label.config(text=f"{self.progress_var.get():.0f}%")

    # ==================================================================
    # Live stats tick (runs every 500ms)
    # ==================================================================
    def _tick_stats(self):
        if self.is_generating:
            NET.tick(0.5)
            dl, ul, dl_rate, ul_rate = NET.snapshot()
            self._dl_rate_lbl.config(text=f"{_format_bytes(dl_rate)}/s")
            self._ul_rate_lbl.config(text=f"{_format_bytes(ul_rate)}/s")
            self._dl_total_lbl.config(text=_format_bytes(dl))
            self._ul_total_lbl.config(text=_format_bytes(ul))
        else:
            self._dl_rate_lbl.config(text="\u2014")
            self._ul_rate_lbl.config(text="\u2014")

        if self.is_generating:
            patterns = [".", "..", "...", "..", "."]
            self._dot_idx = (self._dot_idx + 1) % len(patterns)
            self._dots_label.config(
                text=patterns[self._dot_idx], fg=C_ACCENT)
            if self._start_time:
                elapsed = time.time() - self._start_time
                m, s = divmod(int(elapsed), 60)
                self._elapsed_lbl.config(text=f"Elapsed: {m:02d}:{s:02d}")
                # ETA: linear model from two calibration points:
                #   70 km  -> 12 min,  200 km -> 30 min
                # slope = (30-12)/(200000-70000) min/m
                _slope = 18.0 / 130_000
                dist_m = getattr(self, '_eta_dist_m', self.var_distance.get())
                est_total = (_slope * dist_m + 12.0 - _slope * 70_000) * 60
                remaining = max(0.0, est_total - elapsed)
                rm, rs = divmod(int(remaining), 60)
                pct = min(100, int(elapsed / max(est_total, 1) * 100))
                self._eta_lbl.config(
                    text=f"ETA: {rm:02d}:{rs:02d} remaining  ({pct}%)")
        else:
            self._dots_label.config(text="\u2014", fg=C_TEXT_MUT)
            self._eta_lbl.config(text="")

        self._draw_progress_bar()
        self.root.after(500, self._tick_stats)

    # ==================================================================
    # Event handlers
    # ==================================================================
    def _on_preset_selected(self):
        val = self.var_preset_city.get()
        for c, co, d in PRESET_CITIES:
            if f"{c}, {co}" == val:
                self.var_city.set(c)
                self.var_country.set(co)
                self.var_distance.set(d)
                self._on_distance_change()
                self._draw_theme_preview()
                self.sec_location.update_preview()
                break

    def _on_distance_preset(self):
        name = self.var_distance_preset.get()
        val = DISTANCE_PRESETS.get(name)
        if val:
            self.var_distance.set(val)
            self._on_distance_change()

    def _on_distance_change(self):
        self.var_zoom_label.set(_zoom_label(self.var_distance.get()))
        self._update_estimates()
        self.sec_map.update_preview()

    def _on_orientation_change(self):
        o = self.var_orientation.get()
        if o == "Portrait":
            self.var_width.set(12)
            self.var_height.set(16)
        elif o == "Landscape":
            self.var_width.set(16)
            self.var_height.set(12)
        else:
            self.var_width.set(14)
            self.var_height.set(14)
        self._update_estimates()
        self.sec_output.update_preview()

    def _update_estimates(self):
        self.var_est_size.set(_estimated_filesize(
            self.var_format.get(), self.var_width.get(),
            self.var_height.get(), self.var_dpi.get()))

    def _pick_color(self, var):
        initial = var.get() if var.get() else "#000000"
        result = colorchooser.askcolor(
            initialcolor=initial, title="Pick color")
        if result and result[1]:
            var.set(result[1])

    def _browse_output_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_output_dir.get())
        if d:
            self.var_output_dir.set(d)

    # ==================================================================
    # Logging
    # ==================================================================
    def _log(self, msg):
        def _do():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ==================================================================
    # Generation
    # ==================================================================
    def _generate(self):
        if self.is_generating:
            messagebox.showwarning(
                "Busy", "A poster is already being generated.")
            return
        city = self.var_city.get().strip()
        country = self.var_country.get().strip()
        if not city or not country:
            messagebox.showerror(
                "Missing input", "City and Country are required.")
            return

        self.is_generating = True
        self._start_time = time.time()
        self._eta_dist_m = self.var_distance.get()
        self._cancel_event.clear()
        NET.reset()
        self.btn_generate.set_disabled(True)
        self.btn_cancel.pack(side=tk.LEFT, padx=(6, 0))
        self.progress_var.set(0)
        self.progress_text.set("Starting...")
        self._log(f"--- Starting: {city}, {country} ---")
        threading.Thread(
            target=self._generate_worker, daemon=True).start()

    def _generate_worker(self):
        try:
            _install_net_hooks()

            import matplotlib
            matplotlib.use("Agg")
            import create_map_poster as cmp
            from font_management import load_fonts

            city = self.var_city.get().strip()
            country = self.var_country.get().strip()
            theme_name = self.var_theme.get()
            dist = self.var_distance.get()
            width = self.var_width.get()
            height = self.var_height.get()
            fmt = self.var_format.get()
            dpi = self.var_dpi.get()
            lat = self.var_lat.get().strip()
            lon = self.var_lon.get().strip()
            display_city = self.var_display_city.get().strip() or None
            display_country = self.var_display_country.get().strip() or None
            country_label = self.var_country_label.get().strip() or None
            font_family = self.var_font_family.get().strip() or "Poppins"
            show_water = self.var_show_water.get()
            show_parks = self.var_show_parks.get()
            show_coastline = self.var_show_coastline.get()
            show_gradient = self.var_show_gradient.get()
            show_attribution = self.var_show_attribution.get()
            road_mult = self.var_road_width_mult.get()
            bg_override = self.var_bg_override.get().strip() or None
            text_override = self.var_text_override.get().strip() or None
            border_size = self.var_border_size.get()
            output_dir = self.var_output_dir.get().strip()
            custom_filename = self.var_custom_filename.get().strip()

            themes_list = (list(self.all_themes.keys())
                           if self.var_all_themes.get() else [theme_name])

            self._set_progress(5, "Resolving coordinates...")
            if lat and lon:
                from lat_lon_parser import parse as llparse
                coords = (llparse(lat), llparse(lon))
                self._log(f"Coordinates (manual): {coords}")
            else:
                coords = cmp.get_coordinates(city, country)
                self._log(
                    f"Coordinates: {coords[0]:.4f}, {coords[1]:.4f}")

            if self._cancel_event.is_set():
                self._log("Cancelled.")
                return

            total = len(themes_list)
            for t_idx, t_name in enumerate(themes_list):
                base = 10 + (t_idx / total) * 80
                self._set_progress(
                    base, f"Theme {t_idx+1}/{total}: {t_name}")
                self._log(f"\n>> Theme: {t_name}")

                cmp.THEME = cmp.load_theme(t_name)
                if bg_override:
                    cmp.THEME["bg"] = bg_override
                    cmp.THEME["gradient_color"] = bg_override
                if text_override:
                    cmp.THEME["text"] = text_override

                custom_fonts = None
                if font_family:
                    custom_fonts = load_fonts(font_family)
                    if not custom_fonts:
                        self._log(
                            f"Warning: Failed to load "
                            f"'{font_family}', using default")

                if (output_dir and
                        output_dir != os.path.abspath(POSTERS_DIR)):
                    os.makedirs(output_dir, exist_ok=True)
                    if custom_filename:
                        out_file = os.path.join(
                            output_dir, custom_filename)
                    else:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        slug = city.lower().replace(" ", "_")
                        out_file = os.path.join(
                            output_dir,
                            f"{slug}_{t_name}_{ts}.{fmt}")
                else:
                    if custom_filename:
                        os.makedirs(POSTERS_DIR, exist_ok=True)
                        out_file = os.path.join(
                            POSTERS_DIR, custom_filename)
                    else:
                        out_file = cmp.generate_output_filename(
                            city, t_name, fmt)

                self._set_progress(
                    base + 5, "Downloading street network...")
                compensated_dist = (
                    dist * (max(height, width) / min(height, width)) / 4)
                g = cmp.fetch_graph(coords, compensated_dist)
                if g is None:
                    self._log("FAIL: Failed to fetch street network")
                    continue
                self._log("OK: Street network loaded")

                if self._cancel_event.is_set():
                    self._log("Cancelled.")
                    return

                self._set_progress(
                    base + 20, "Downloading water features...")
                water_data = None
                if show_water:
                    water_data = cmp.fetch_features(
                        coords, compensated_dist,
                        tags={"natural": ["water", "bay", "strait"],
                              "waterway": "riverbank"},
                        name="water")
                    self._log(
                        "OK: Water features"
                        if water_data is not None else "  (no water)")

                self._set_progress(base + 30, "Downloading parks...")
                parks_data = None
                if show_parks:
                    parks_data = cmp.fetch_features(
                        coords, compensated_dist,
                        tags={"leisure": "park", "landuse": "grass"},
                        name="parks")
                    self._log(
                        "OK: Parks"
                        if parks_data is not None else "  (no parks)")

                coastline_data = None
                if show_coastline:
                    self._set_progress(base + 35, "Downloading coastline...")
                    coastline_data = cmp.fetch_features(
                        coords, compensated_dist,
                        tags={"natural": "coastline"},
                        name="coastline")
                    self._log(
                        "OK: Coastline"
                        if coastline_data is not None else "  (no coastline)")

                self._set_progress(base + 40, "Rendering poster...")
                if self._cancel_event.is_set():
                    self._log("Cancelled.")
                    return
                import matplotlib.pyplot as plt
                import osmnx as ox

                fig, ax = plt.subplots(
                    figsize=(width, height),
                    facecolor=cmp.THEME["bg"])
                ax.set_facecolor(cmp.THEME["bg"])
                ax.set_position((0.0, 0.0, 1.0, 1.0))
                g_proj = ox.project_graph(g)

                if (water_data is not None and not water_data.empty
                        and show_water):
                    wp = water_data[water_data.geometry.type.isin(
                        ["Polygon", "MultiPolygon"])]
                    if not wp.empty:
                        try:
                            wp = ox.projection.project_gdf(wp)
                        except Exception:
                            wp = wp.to_crs(g_proj.graph['crs'])
                        wp.plot(ax=ax,
                                facecolor=cmp.THEME['water'],
                                edgecolor='none', zorder=0.5)

                if (parks_data is not None and not parks_data.empty
                        and show_parks):
                    pp = parks_data[parks_data.geometry.type.isin(
                        ["Polygon", "MultiPolygon"])]
                    if not pp.empty:
                        try:
                            pp = ox.projection.project_gdf(pp)
                        except Exception:
                            pp = pp.to_crs(g_proj.graph['crs'])
                        pp.plot(ax=ax,
                                facecolor=cmp.THEME['parks'],
                                edgecolor='none', zorder=0.8)

                if (coastline_data is not None
                        and not coastline_data.empty
                        and show_coastline):
                    cl = coastline_data[
                        coastline_data.geometry.type.isin(
                            ["LineString", "MultiLineString",
                             "Polygon", "MultiPolygon"])]
                    if not cl.empty:
                        try:
                            cl = ox.projection.project_gdf(cl)
                        except Exception:
                            cl = cl.to_crs(g_proj.graph['crs'])
                        coast_color = cmp.THEME.get(
                            'water', '#4a90d9')
                        cl.plot(ax=ax,
                                edgecolor=coast_color,
                                facecolor='none',
                                linewidth=1.2, zorder=1.5)

                self._set_progress(base + 50, "Drawing roads...")
                crop_xlim, crop_ylim = cmp.get_crop_limits(
                    g_proj, coords, fig, compensated_dist)
                cmp.plot_roads_layered(
                    g_proj, ax, compensated_dist,
                    road_width_mult=road_mult)
                ax.set_aspect("equal", adjustable="box")
                ax.set_xlim(crop_xlim)
                ax.set_ylim(crop_ylim)

                if show_gradient:
                    cmp.create_gradient_fade(
                        ax, cmp.THEME['gradient_color'],
                        location='bottom', zorder=10)
                    cmp.create_gradient_fade(
                        ax, cmp.THEME['gradient_color'],
                        location='top', zorder=10)

                self._set_progress(base + 60, "Adding typography...")
                scale_factor = min(height, width) / 12.0
                active_fonts = custom_fonts or cmp.FONTS
                from matplotlib.font_manager import FontProperties

                if active_fonts:
                    font_sub = FontProperties(
                        fname=active_fonts["light"],
                        size=22 * scale_factor)
                    font_coords_fp = FontProperties(
                        fname=active_fonts["regular"],
                        size=14 * scale_factor)
                    font_attr = FontProperties(
                        fname=active_fonts["light"], size=8)
                else:
                    font_sub = FontProperties(
                        family="monospace", weight="normal",
                        size=22 * scale_factor)
                    font_coords_fp = FontProperties(
                        family="monospace",
                        size=14 * scale_factor)
                    font_attr = FontProperties(
                        family="monospace", size=8)

                dc = display_city or city
                dco = display_country or country_label or country
                spaced = ("  ".join(list(dc.upper()))
                          if cmp.is_latin_script(dc) else dc)

                base_adj = 60 * scale_factor
                adj_size = (
                    max(base_adj * (10 / len(dc)),
                        10 * scale_factor)
                    if len(dc) > 10 else base_adj)
                if active_fonts:
                    font_main = FontProperties(
                        fname=active_fonts["bold"], size=adj_size)
                else:
                    font_main = FontProperties(
                        family="monospace", weight="bold",
                        size=adj_size)

                ax.text(
                    0.5, 0.14, spaced, transform=ax.transAxes,
                    color=cmp.THEME["text"], ha="center",
                    fontproperties=font_main, zorder=11)
                ax.text(
                    0.5, 0.10, dco.upper(), transform=ax.transAxes,
                    color=cmp.THEME["text"], ha="center",
                    fontproperties=font_sub, zorder=11)
                lat_v, lon_v = coords
                cs = (f"{abs(lat_v):.4f} "
                      f"{'N' if lat_v >= 0 else 'S'} / "
                      f"{abs(lon_v):.4f} "
                      f"{'E' if lon_v >= 0 else 'W'}")
                ax.text(
                    0.5, 0.07, cs, transform=ax.transAxes,
                    color=cmp.THEME["text"], alpha=0.7,
                    ha="center", fontproperties=font_coords_fp,
                    zorder=11)
                ax.plot(
                    [0.4, 0.6], [0.125, 0.125],
                    transform=ax.transAxes,
                    color=cmp.THEME["text"],
                    linewidth=1 * scale_factor, zorder=11)
                if show_attribution:
                    ax.text(
                        0.98, 0.02,
                        "OpenStreetMap contributors",
                        transform=ax.transAxes,
                        color=cmp.THEME["text"], alpha=0.5,
                        ha="right", va="bottom",
                        fontproperties=font_attr, zorder=11)

                self._set_progress(
                    base + 70, f"Saving {fmt.upper()}...")
                save_kw = dict(
                    facecolor=cmp.THEME["bg"],
                    bbox_inches="tight",
                    pad_inches=border_size)
                if fmt == "png":
                    save_kw["dpi"] = dpi
                plt.savefig(out_file, format=fmt, **save_kw)
                plt.close(fig)

                self.last_output_file = out_file
                self._log(f"OK: Saved: {out_file}")
                self.root.after(
                    0, self._add_history, city, country,
                    t_name, out_file)

            self._set_progress(100, "Done!")
            self._log("\nGeneration complete!")
            self.root.after(
                0, lambda: messagebox.showinfo(
                    "Done",
                    f"Poster(s) generated!\n{self.last_output_file}"))

        except Exception as exc:
            import traceback
            self._log(
                f"\nError: {exc}\n{traceback.format_exc()}")
            self.root.after(
                0, lambda: messagebox.showerror("Error", str(exc)))
        finally:
            self.is_generating = False
            self._start_time = None
            self.root.after(0, lambda: self.btn_generate.set_disabled(False))
            self.root.after(0, self.btn_cancel.pack_forget)
            self.root.after(0, self._clear_net_display)

    def _cancel(self):
        if self.is_generating:
            self._cancel_event.set()
            self._log("Cancelling — waiting for current step to finish...")
            self.btn_cancel.set_disabled(True)

    def _clear_net_display(self):
        self._dl_rate_lbl.config(text="\u2014")
        self._ul_rate_lbl.config(text="\u2014")
        self._dl_total_lbl.config(text="\u2014")
        self._ul_total_lbl.config(text="\u2014")
        self._elapsed_lbl.config(text="")
        self._eta_lbl.config(text="")

    def _preview_area(self):
        """Resolve coordinates then fetch OSM tiles for an area overview."""
        city = self.var_city.get().strip()
        country = self.var_country.get().strip()
        lat_s = self.var_lat.get().strip()
        lon_s = self.var_lon.get().strip()

        if not (city and country) and not (lat_s and lon_s):
            messagebox.showerror(
                "Missing input",
                "Enter a city + country or manual coordinates first.")
            return

        self._area_status_lbl.config(text="Fetching coordinates…")
        self._preview_area_btn.set_disabled(True)
        self._area_canvas.delete("all")

        def worker():
            try:
                if lat_s and lon_s:
                    from lat_lon_parser import parse as llparse
                    coords = (llparse(lat_s), llparse(lon_s))
                else:
                    import create_map_poster as cmp
                    coords = cmp.get_coordinates(city, country)

                lat, lon = coords
                dist = self.var_distance.get()

                self.root.after(0, lambda: self._area_status_lbl.config(
                    text=f"Loading tiles for {lat:.4f}, {lon:.4f}…"))

                self._area_canvas.update_idletasks()
                cw = max(self._area_canvas.winfo_width(), 360)
                ch = max(self._area_canvas.winfo_height(), 220)

                img = _fetch_tile_preview(lat, lon, dist, cw, ch)

                if img is None:
                    self.root.after(0, lambda: self._area_status_lbl.config(
                        text="Tile fetch failed — check internet connection."))
                    return
                img, used_zoom, pin_x, pin_y = img

                # Draw bbox rectangle and crosshair using exact pin position
                from PIL import ImageDraw
                draw = ImageDraw.Draw(img)
                lat_r = math.radians(lat)
                mpp = 156543.03 * math.cos(lat_r) / (2 ** used_zoom)
                half_px = int(dist / mpp)
                rx0 = max(0, pin_x - half_px)
                ry0 = max(0, pin_y - half_px)
                rx1 = min(cw, pin_x + half_px)
                ry1 = min(ch, pin_y + half_px)
                draw.rectangle([rx0, ry0, rx1, ry1],
                                outline="#6c5ce7", width=2)
                # Crosshair exactly on the coordinate
                arm = 12
                draw.line([(pin_x - arm, pin_y), (pin_x + arm, pin_y)],
                          fill="#e74c3c", width=2)
                draw.line([(pin_x, pin_y - arm), (pin_x, pin_y + arm)],
                          fill="#e74c3c", width=2)
                draw.ellipse([pin_x - 4, pin_y - 4, pin_x + 4, pin_y + 4],
                              outline="#e74c3c", width=2)

                from PIL import ImageTk
                photo = ImageTk.PhotoImage(img)

                def _show():
                    self._area_preview_photo = photo
                    self._area_canvas.config(
                        width=cw, height=ch)
                    self._area_canvas.create_image(
                        0, 0, anchor="nw", image=photo)
                    dist_km = dist / 1000
                    self._area_status_lbl.config(
                        text=(f"{lat:.4f} {'N' if lat>=0 else 'S'}, "
                              f"{lon:.4f} {'E' if lon>=0 else 'W'} "
                              f"— radius {dist_km:.0f} km"))
                    self._preview_area_btn.set_disabled(False)

                self.root.after(0, _show)

            except Exception as exc:
                self.root.after(0, lambda: self._area_status_lbl.config(
                    text=f"Error: {exc}"))
                self.root.after(0, lambda: self._preview_area_btn.set_disabled(False))

        threading.Thread(target=worker, daemon=True).start()

    def _set_progress(self, value, text):
        v = min(value, 100)
        self.progress_var.set(v)
        self.progress_text.set(text)
        self._log(f"[{v:.0f}%] {text}")

    # ==================================================================
    # History
    # ==================================================================
    def _add_history(self, city, country, theme, path):
        ts = datetime.now().strftime("%H:%M:%S")
        self.generation_history.append({
            "city": city, "country": country,
            "theme": theme, "path": path, "time": ts})

        if self._hist_empty.winfo_ismapped():
            self._hist_empty.pack_forget()

        row = tk.Frame(self._hist_container, bg=C_SECTION)
        row.pack(fill=tk.X, pady=2)

        tk.Label(
            row,
            text=f"{ts}  {city}, {country} \u2014 {theme}",
            bg=C_SECTION, fg=C_TEXT_DIM,
            font=(FONT_FAMILY, 10), anchor="w",
            padx=6, pady=4).pack(
                side=tk.LEFT, fill=tk.X, expand=True)

        btn = tk.Label(
            row, text="Preview", bg=C_ACCENT, fg="#fff",
            font=(FONT_FAMILY, 9, "bold"), padx=10, pady=4,
            cursor="hand2")
        btn.pack(side=tk.RIGHT, padx=2)
        btn.bind("<Enter>", lambda e, w=btn: w.config(bg=C_ACCENT_H))
        btn.bind("<Leave>", lambda e, w=btn: w.config(bg=C_ACCENT))
        btn.bind(
            "<Button-1>",
            lambda e, p=path: (
                _open_path(p) if os.path.exists(p)
                else messagebox.showwarning(
                    "Not Found", f"File not found:\n{p}")))

    # ==================================================================
    # CLI builder
    # ==================================================================
    def _copy_cli_command(self):
        city = self.var_city.get().strip()
        country = self.var_country.get().strip()
        if not city or not country:
            messagebox.showinfo("CLI", "Enter city and country first.")
            return
        parts = [
            "python", "create_map_poster.py",
            "-c", f'"{city}"', "-C", f'"{country}"',
            "-t", self.var_theme.get(),
            "-d", str(self.var_distance.get()),
            "-W", str(self.var_width.get()),
            "-H", str(self.var_height.get()),
            "-f", self.var_format.get()]
        for flag, var in [
            ("-lat", self.var_lat),
            ("-long", self.var_lon),
            ("-dc", self.var_display_city),
            ("-dC", self.var_display_country),
            ("--country-label", self.var_country_label),
            ("--font-family", self.var_font_family),
        ]:
            v = var.get().strip()
            if v:
                parts += [flag, f'"{v}"']
        if self.var_all_themes.get():
            parts.append("--all-themes")
        cmd = " ".join(parts)
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self._log(f"Copied: {cmd}")

    # ==================================================================
    # Settings export / import
    # ==================================================================
    def _get_settings(self):
        return {k: v.get() for k, v in [
            ("city", self.var_city),
            ("country", self.var_country),
            ("theme", self.var_theme),
            ("distance", self.var_distance),
            ("width", self.var_width),
            ("height", self.var_height),
            ("format", self.var_format),
            ("dpi", self.var_dpi),
            ("latitude", self.var_lat),
            ("longitude", self.var_lon),
            ("display_city", self.var_display_city),
            ("display_country", self.var_display_country),
            ("country_label", self.var_country_label),
            ("font_family", self.var_font_family),
            ("output_dir", self.var_output_dir),
            ("custom_filename", self.var_custom_filename),
            ("all_themes", self.var_all_themes),
            ("show_water", self.var_show_water),
            ("show_parks", self.var_show_parks),
            ("show_coastline", self.var_show_coastline),
            ("show_gradient", self.var_show_gradient),
            ("show_attribution", self.var_show_attribution),
            ("road_width_mult", self.var_road_width_mult),
            ("orientation", self.var_orientation),
            ("bg_override", self.var_bg_override),
            ("text_override", self.var_text_override),
            ("border_size", self.var_border_size),
        ]}

    def _apply_settings(self, s):
        str_keys = [
            "city", "country", "theme", "format", "latitude",
            "longitude", "display_city", "display_country",
            "country_label", "font_family", "output_dir",
            "custom_filename", "orientation",
            "bg_override", "text_override"]
        for k in str_keys:
            if k in s:
                getattr(self, f"var_{k}").set(s[k])
        for k in ["distance", "dpi"]:
            if k in s:
                getattr(self, f"var_{k}").set(int(s[k]))
        for k in ["width", "height", "road_width_mult", "border_size"]:
            if k in s:
                getattr(self, f"var_{k}").set(float(s[k]))
        for k in ["all_themes", "show_water", "show_parks",
                   "show_coastline", "show_gradient", "show_attribution"]:
            if k in s:
                getattr(self, f"var_{k}").set(bool(s[k]))
        self._draw_theme_preview()
        self._on_distance_change()

    def _export_settings(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            title="Export Settings")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._get_settings(), f, indent=2)
            self._log(f"Exported: {path}")

    def _import_settings(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json")],
            title="Import Settings")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._apply_settings(json.load(f))
                self._log(f"Imported: {path}")
            except Exception as e:
                messagebox.showerror("Import Error", str(e))

    # ==================================================================
    # Utility actions
    # ==================================================================
    def _open_output_folder(self):
        d = self.var_output_dir.get()
        os.makedirs(d, exist_ok=True)
        _open_path(d)

    def _open_last_poster(self):
        if self.last_output_file and os.path.exists(
                self.last_output_file):
            _open_path(self.last_output_file)
        else:
            messagebox.showinfo(
                "No Poster", "No poster generated in this session.")

    def _clear_cache(self):
        if not messagebox.askyesno(
                "Clear Cache",
                f"Delete all cached data in '{CACHE_DIR}'?"):
            return
        try:
            if os.path.isdir(CACHE_DIR):
                count = sum(
                    1 for f in os.listdir(CACHE_DIR)
                    if os.path.isfile(os.path.join(CACHE_DIR, f)))
                for fname in os.listdir(CACHE_DIR):
                    fp = os.path.join(CACHE_DIR, fname)
                    if os.path.isfile(fp):
                        os.remove(fp)
                self._log(f"Cleared {count} cache files")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ==================================================================
    # Batch dialog
    # ==================================================================
    def _show_batch_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Batch Generate")
        dlg.geometry("520x440")
        dlg.configure(bg=C_PANEL)
        dlg.transient(self.root)

        tk.Label(
            dlg, text="Enter cities (one per line: City, Country)",
            bg=C_PANEL, fg=C_TEXT, font=(FONT_FAMILY, 12),
            padx=12, pady=10).pack(anchor="w")
        text = tk.Text(
            dlg, bg=C_INPUT_BG, fg=C_INPUT_FG,
            font=(FONT_FAMILY, 11), relief="flat", bd=0,
            padx=8, pady=6, insertbackground=C_TEXT)
        text.pack(fill=tk.BOTH, expand=True, padx=12)
        text.insert(
            "1.0", "New York, USA\nParis, France\nTokyo, Japan\n")

        btn_row = tk.Frame(dlg, bg=C_PANEL, pady=10)
        btn_row.pack(fill=tk.X, padx=12)
        FlatButton(
            btn_row, text="Generate All",
            command=lambda: self._run_batch(
                text.get("1.0", tk.END), dlg),
            bg=C_ACCENT, hover_bg=C_ACCENT_H).pack(side=tk.RIGHT)
        FlatButton(
            btn_row, text="Cancel", command=dlg.destroy,
            bg=C_SECTION, hover_bg=C_HOVER,
            fg=C_TEXT_DIM).pack(side=tk.RIGHT, padx=(0, 8))

    def _run_batch(self, text, dialog):
        dialog.destroy()
        lines = [
            l.strip() for l in text.strip().split("\n") if l.strip()]
        cities = []
        for line in lines:
            parts = line.split(",", 1)
            if len(parts) == 2:
                cities.append((parts[0].strip(), parts[1].strip()))
        if not cities:
            messagebox.showwarning("Batch", "No valid cities.")
            return
        self._log(f"Batch: {len(cities)} cities queued")

        def worker():
            for i, (city, country) in enumerate(cities):
                self.var_city.set(city)
                self.var_country.set(country)
                self._set_progress(
                    i / len(cities) * 100,
                    f"Batch {i+1}/{len(cities)}: {city}")
                self._generate_worker()
            self._set_progress(
                100, f"Batch done -- {len(cities)} cities")

        self.is_generating = True
        self._start_time = time.time()
        self._eta_dist_m = self.var_distance.get()
        NET.reset()
        self.btn_generate.set_disabled(True)
        threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def launch_gui():
    root = tk.Tk()

    try:
        import tkinter.font as tkfont
        available = tkfont.families(root)
        global FONT_FAMILY
        for candidate in ["Poppins", "Segoe UI", "SF Pro Text",
                          "Helvetica Neue", "Helvetica", "Arial"]:
            if candidate in available:
                FONT_FAMILY = candidate
                break
    except Exception:
        pass

    _app = MapPosterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
