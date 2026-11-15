#!/usr/bin/env python3
"""
LIVE FX  -  Stereo Space Processor (Reverb + Delay + Echo) for any audio on your PC (YouTube etc.)
Retro studio-rack interface  |  F11 full screen

Install :  python -m pip install -r requirements.txt
Run     :  python main.py               (add --list to print audio devices)

SIGNAL PATH
  YouTube/browser -> virtual audio cable -> LIVE FX (this program) -> your real speakers/headphones

ROUTING SETUP (one time)
  Windows : install VB-CABLE (vb-audio.com/Cable). Set Windows sound output (or the browser's output in
            Volume Mixer) to "CABLE Input". In the app: Input = "CABLE Output", Output = your speakers.
            Use the SAME host API (e.g. WASAPI) for both.
  macOS   : install BlackHole 2ch. Set system output to BlackHole. Input = BlackHole, Output = speakers.
  Linux   : pactl load-module module-null-sink sink_name=fx
            Play YouTube to "fx"; Input = "Monitor of fx", Output = your speakers.

CONTROLS
  Knobs   : drag up/down, mouse wheel, hold SHIFT for fine control, double-click = reset to default
  Keys    : F11 full screen | Esc exit full screen | Ctrl+P power | Ctrl+B bypass | Ctrl+M mute | Ctrl+R record
  Settings are remembered in ~/.livefx_settings.json ; recordings go to ~/LiveFX_Recordings/
"""
import datetime
import json
import math
import queue
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
except Exception as e:  # pragma: no cover
    sys.exit("Missing dependency. Run:  pip install numpy sounddevice\n(%s)" % e)

CHUNK = 256  # internal processing chunk (must be <= shortest delay line)


# =========================================================================== DSP
class Ring:
    """Circular buffer with vectorised block read/write."""

    def __init__(self, size, channels=None):
        shape = (size,) if channels is None else (size, channels)
        self.buf = np.zeros(shape, np.float32)
        self.n = size

    def read(self, pos, delay, ar):
        return self.buf[(pos - delay + ar) % self.n]

    def write(self, pos, x, ar):
        self.buf[(pos + ar) % self.n] = x


class Effects:
    """Freeverb-style reverb (pre-delay, width) + ping-pong delay + toned feedback echo + limiter."""

    COMB = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
    ALLPASS = (556, 441)
    SPREAD = 23

    def __init__(self, sr, params):
        self.sr = sr
        self.p = params
        self.pos = 0
        self.ar = np.arange(CHUNK)
        k = sr / 44100.0

        def build(spread):
            combs = [(Ring(int(d * k) + spread + CHUNK + 2), int(d * k) + spread) for d in self.COMB]
            aps = [(Ring(int(d * k) + spread + CHUNK + 2), int(d * k) + spread) for d in self.ALLPASS]
            return combs, aps

        self.rev = [build(0), build(self.SPREAD)]
        self.pre = Ring(int(sr * 0.25) + CHUNK + 2)
        size = int(sr * 2.0) + CHUNK + 2
        self.dring = Ring(size, 2)
        self.ering = Ring(size, 2)
        self.lim_g = 1.0
        # metering (read by the GUI thread)
        self.scope = np.zeros(2048, np.float32)
        self.in_rms = self.out_rms = 0.0
        self.in_pk = self.out_pk = 0.0
        self.clipped = False

    def _reverb(self, x, ch, ar):
        p = self.p
        g = 0.7 + 0.28 * p["r_room"]
        d = 0.5 * p["r_damp"]
        combs, aps = self.rev[ch]
        acc = np.zeros(len(ar), np.float32)
        for ring, D in combs:
            a = ring.read(self.pos, D, ar)
            b = ring.read(self.pos, D + 1, ar)
            y = x + g * ((1.0 - d) * a + d * b)
            ring.write(self.pos, y, ar)
            acc += y
        for ring, D in aps:
            wd = ring.read(self.pos, D, ar)
            w = acc + 0.5 * wd
            ring.write(self.pos, w, ar)
            acc = wd - 0.5 * w
        return acc * 3.0

    def _chunk(self, x):
        n = len(x)
        ar = self.ar[:n]
        p, sr = self.p, self.sr

        # ---- reverb (with pre-delay and stereo width)
        mono = (x[:, 0] + x[:, 1]) * 0.015
        dp = int(p["r_pre"] * sr / 1000.0)
        m_in = self.pre.read(self.pos, dp, ar) if dp >= CHUNK else mono
        self.pre.write(self.pos, mono, ar)
        rev = np.stack([self._reverb(m_in, 0, ar), self._reverb(m_in, 1, ar)], axis=1)
        w = p["r_width"]
        if abs(w - 1.0) > 1e-3:
            m = (rev[:, 0] + rev[:, 1]) * 0.5
            s = (rev[:, 0] - rev[:, 1]) * 0.5 * w
            rev = np.stack([m + s, m - s], axis=1)

        # ---- delay (single repeat, optional ping-pong)
        D = max(CHUNK, int(p["d_time"] * sr / 1000.0))
        dly = self.dring.read(self.pos, D, ar)
        self.dring.write(self.pos, x, ar)
        if p["pingpong"]:
            dly = dly[:, ::-1]

        # ---- echo (feedback repeats with tone control in the loop)
        D2 = max(CHUNK, int(p["e_time"] * sr / 1000.0))
        ed = self.ering.read(self.pos, D2, ar)
        ed2 = self.ering.read(self.pos, D2 + 1, ar)
        t = 0.5 * p["e_tone"]
        self.ering.write(self.pos, x + p["e_fb"] * ((1.0 - t) * ed + t * ed2), ar)

        out = x if p["bypass"] else (p["dry"] * x + p["r_mix"] * rev + p["d_mix"] * dly + p["e_mix"] * ed)
        self.pos += n
        return out

    def process(self, x):
        p = self.p
        x = x * np.float32(10.0 ** (p["in_db"] / 20.0))
        out = np.empty_like(x)
        for i in range(0, len(x), CHUNK):
            out[i:i + CHUNK] = self._chunk(x[i:i + CHUNK])
        out *= np.float32(10.0 ** (p["out_db"] / 20.0))

        self.in_pk = float(np.abs(x).max()) if x.size else 0.0
        self.in_rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0

        pk = float(np.abs(out).max()) if out.size else 0.0
        if pk > 1.0:
            self.clipped = True
        if p["limiter"]:
            thr = 0.97
            need = thr / pk if pk > thr else 1.0
            if need < self.lim_g:                      # instant attack
                self.lim_g = need
                out *= np.float32(need)
            else:                                      # smooth release
                g_new = min(need, self.lim_g + (1.0 - self.lim_g) * 0.15 + 0.002, 1.0)
                ramp = np.linspace(self.lim_g, g_new, len(out), dtype=np.float32)[:, None]
                out *= ramp
                self.lim_g = g_new
        np.clip(out, -1.0, 1.0, out=out)
        if p["mute"]:
            out[:] = 0.0

        self.out_pk = float(np.abs(out).max()) if out.size else 0.0
        self.out_rms = float(np.sqrt(np.mean(out * out))) if out.size else 0.0
        mono_out = out.mean(axis=1)
        if len(mono_out) >= len(self.scope):
            self.scope = mono_out[-len(self.scope):].copy()
        else:
            self.scope = np.concatenate((self.scope[len(mono_out):], mono_out))
        return out


# =========================================================================== settings / presets
DEFAULTS = dict(in_db=0.0, dry=1.0, out_db=-3.0,
                r_mix=0.25, r_room=0.70, r_damp=0.40, r_pre=0.0, r_width=1.0,
                d_time=300.0, d_mix=0.0, pingpong=False,
                e_time=450.0, e_fb=0.40, e_mix=0.0, e_tone=0.30,
                limiter=True, mute=False, bypass=False)

FX_KEYS = ("r_mix", "r_room", "r_damp", "r_pre", "r_width", "d_time", "d_mix", "pingpong",
           "e_time", "e_fb", "e_mix", "e_tone")
BOOL_KEYS = ("pingpong", "limiter", "mute", "bypass")

PRESETS = {
    "Clean (no FX)":  dict(r_mix=0, d_mix=0, e_mix=0),
    "Small Room":     dict(r_mix=0.25, r_room=0.40, r_damp=0.50),
    "Concert Hall":   dict(r_mix=0.40, r_room=0.75, r_damp=0.35, r_pre=20),
    "Cathedral":      dict(r_mix=0.55, r_room=0.93, r_damp=0.25, r_pre=35, r_width=1.3),
    "Slapback":       dict(r_mix=0.10, d_time=120, d_mix=0.55),
    "Tape Echo":      dict(r_mix=0.15, e_time=380, e_fb=0.50, e_mix=0.40, e_tone=0.65),
    "Dub Echo":       dict(r_mix=0.30, e_time=375, e_fb=0.72, e_mix=0.55, e_tone=0.50),
    "Ping-Pong":      dict(r_mix=0.15, d_time=350, d_mix=0.50, pingpong=True),
    "Stadium":        dict(r_mix=0.50, r_room=0.90, r_damp=0.30, r_pre=60, e_time=600, e_fb=0.35, e_mix=0.35),
    "Ambient Wash":   dict(r_mix=0.60, r_room=0.95, r_damp=0.50, r_pre=40, r_width=1.4,
                           e_time=700, e_fb=0.50, e_mix=0.30, e_tone=0.50),
}

SETTINGS_PATH = Path.home() / ".livefx_settings.json"
PREFERRED_INPUT = ("cable output", "blackhole", "monitor", "stereo mix", "loopback", "voicemeeter", "vb-audio")


# =========================================================================== GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    # ---- retro palette: dark faceplate, walnut cheeks, amber displays, cream legends
    CASE, PANEL, PANEL_HI, EDGE = "#14110F", "#2B2824", "#3A352F", "#0A0908"
    CREAM, AMBER, AMBER_DIM = "#E8DCC0", "#FFB000", "#7A5500"
    RED, GREEN, ORANGE, FACE = "#FF3B1F", "#35E04A", "#C2410C", "#E6D9A8"
    LCD = "#0B0A09"
    MONO = "Courier New"

    PCT = lambda v: "%d%%" % round(v * 100)
    DB = lambda v: "%+.1f dB" % v
    MS = lambda v: "%d ms" % round(v)
    NUM = lambda v: "%.2f" % v

    # ------------------------------------------------------------------ widgets
    class Knob(tk.Frame):
        def __init__(self, parent, label, var, lo, hi, fmt, default, size=70, bg=PANEL):
            super().__init__(parent, bg=bg)
            self.var, self.lo, self.hi, self.fmt, self.default, self.S = var, lo, hi, fmt, default, size
            c = size / 2.0
            cv = self.cv = tk.Canvas(self, width=size, height=size, bg=bg, highlightthickness=0, cursor="hand2")
            cv.pack()
            for i in range(11):                                   # scale ticks
                a = math.radians(-135 + 27 * i)
                major = i in (0, 5, 10)
                r1, r2 = size * 0.40, size * (0.48 if major else 0.45)
                cv.create_line(c + r1 * math.sin(a), c - r1 * math.cos(a),
                               c + r2 * math.sin(a), c - r2 * math.cos(a),
                               fill=CREAM if major else "#8A8068", width=2 if major else 1)
            r = size * 0.31
            cv.create_oval(c - r + 2, c - r + 3, c + r + 2, c + r + 3, fill="#000000", outline="")
            cv.create_oval(c - r, c - r, c + r, c + r, fill="#1E1B18", outline=EDGE, width=2)
            r2 = r * 0.84
            cv.create_oval(c - r2, c - r2, c + r2, c + r2, fill="#3B3732", outline="#4A453E")
            r3 = r * 0.58
            cv.create_oval(c - r3, c - r3, c + r3, c + r3, fill="#55504A", outline="#2A2723")
            cv.create_oval(c - r3 * 0.75, c - r3 * 0.85, c - r3 * 0.05, c - r3 * 0.2, fill="#6E685F", outline="")
            self.ptr = cv.create_line(c, c, c, c - r, fill="#FFE9B0", width=3, capstyle="round")
            tk.Label(self, text=label, bg=bg, fg=CREAM, font=(MONO, 8, "bold")).pack()
            self.val = tk.Label(self, bg=LCD, fg=AMBER, font=(MONO, 9, "bold"), width=8,
                                relief="sunken", bd=2)
            self.val.pack(pady=(2, 0))
            cv.bind("<ButtonPress-1>", self._press)
            cv.bind("<B1-Motion>", self._drag)
            cv.bind("<Double-Button-1>", lambda e: self.var.set(self.default))
            cv.bind("<MouseWheel>", lambda e: self._step(1 if e.delta > 0 else -1, e))
            cv.bind("<Button-4>", lambda e: self._step(1, e))
            cv.bind("<Button-5>", lambda e: self._step(-1, e))
            var.trace_add("write", lambda *a: self.refresh())
            self.refresh()

        def _press(self, e):
            self._y0, self._v0 = e.y, self.var.get()

        def _drag(self, e):
            fine = 0.25 if (e.state & 0x0001) else 1.0
            self._set(self._v0 + (self._y0 - e.y) / 160.0 * (self.hi - self.lo) * fine)

        def _step(self, d, e):
            fine = 0.25 if (e.state & 0x0001) else 1.0
            self._set(self.var.get() + d * (self.hi - self.lo) / 50.0 * fine)

        def _set(self, v):
            self.var.set(max(self.lo, min(self.hi, v)))

        def refresh(self):
            try:
                v = float(self.var.get())
            except tk.TclError:
                return
            frac = max(0.0, min(1.0, (v - self.lo) / (self.hi - self.lo)))
            a = math.radians(-135 + 270 * frac)
            c, r = self.S / 2.0, self.S * 0.29
            self.cv.coords(self.ptr, c + 0.2 * r * math.sin(a), c - 0.2 * r * math.cos(a),
                           c + r * math.sin(a), c - r * math.cos(a))
            self.val.config(text=self.fmt(v))

    class Toggle(tk.Frame):
        def __init__(self, parent, text, var, color=AMBER, bg=PANEL, width=9):
            super().__init__(parent, bg=bg)
            self.var, self.color = var, color
            self.led = tk.Canvas(self, width=14, height=14, bg=bg, highlightthickness=0)
            self.led.pack(side="left", padx=(0, 5))
            self.dot = self.led.create_oval(2, 2, 12, 12, fill="#2A1A00", outline=EDGE)
            tk.Checkbutton(self, text=text, variable=var, indicatoron=False, font=(MONO, 8, "bold"),
                           fg=CREAM, bg=PANEL_HI, selectcolor="#4A3A1E", activebackground="#4B453D",
                           activeforeground=CREAM, relief="raised", bd=3, padx=4, pady=1, width=width,
                           cursor="hand2").pack(side="left")
            var.trace_add("write", lambda *a: self._upd())
            self._upd()

        def _upd(self):
            self.led.itemconfig(self.dot, fill=self.color if self.var.get() else "#2A1A00")

    class Led(tk.Canvas):
        def __init__(self, parent, color, bg=CASE):
            super().__init__(parent, width=16, height=16, bg=bg, highlightthickness=0)
            self.color = color
            self.dot = self.create_oval(2, 2, 14, 14, fill="#1E1B18", outline=EDGE, width=2)
            self.on = None
            self.set(False)

        def set(self, on):
            on = bool(on)
            if on != self.on:
                self.on = on
                self.itemconfig(self.dot, fill=self.color if on else "#1E1B18")

    class VU(tk.Canvas):
        LO, HI = -40.0, 0.0

        def __init__(self, parent, title):
            super().__init__(parent, bg=CASE, highlightthickness=2, highlightbackground=EDGE,
                             width=210, height=140)
            self.title, self.val, self.geom = title, self.LO, None
            self.bind("<Configure>", lambda e: self.draw())

        def _ang(self, db):
            f = (max(self.LO, min(self.HI, db)) - self.LO) / (self.HI - self.LO)
            return math.radians(-50 + 100 * f)

        def draw(self):
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w < 60 or h < 60:
                return
            self.create_rectangle(3, 3, w - 3, h - 3, fill=FACE, outline=EDGE, width=2)
            R = min(0.95 * h, 0.62 * w)
            px, py = w / 2.0, 0.16 * h + R
            self.geom = (px, py, R)
            self.create_arc(px - R, py - R, px + R, py + R, start=40, extent=100, style="arc",
                            outline="#222222", width=2)
            a0, a1 = self._ang(-6), self._ang(0)
            self.create_arc(px - R * 0.97, py - R * 0.97, px + R * 0.97, py + R * 0.97,
                            start=90 - math.degrees(a1), extent=math.degrees(a1 - a0), style="arc",
                            outline="#C0392B", width=6)
            for db in (-40, -30, -20, -10, -6, -3, 0):
                a = self._ang(db)
                s, c = math.sin(a), math.cos(a)
                self.create_line(px + R * 0.88 * s, py - R * 0.88 * c, px + R * 1.0 * s, py - R * 1.0 * c,
                                 fill="#222222", width=2)
                if db != -3:
                    self.create_text(px + R * 0.76 * s, py - R * 0.76 * c, text=str(db), fill="#222222",
                                     font=(MONO, 8, "bold"))
            self.create_text(px, 0.50 * h, text="dB", fill="#222222", font=(MONO, 9, "bold"))
            self.create_line(px, py, px, py - R * 0.9, fill="#111111", width=2, tags="needle")
            self.create_rectangle(3, h - 24, w - 3, h - 3, fill=CASE, outline=EDGE, width=2, tags="strip")
            self.create_text(w / 2, h - 13, text=self.title, fill=AMBER, font=(MONO, 9, "bold"), tags="strip")
            self.smooth(self.val, 0.0)

        def smooth(self, db, k=0.3):
            self.val += (max(self.LO, db) - self.val) * (k if k else 1.0)
            if not self.geom:
                return
            px, py, R = self.geom
            a = self._ang(self.val)
            self.coords("needle", px, py, px + 0.9 * R * math.sin(a), py - 0.9 * R * math.cos(a))
            self.tag_raise("strip")

    class Spectrum(tk.Canvas):
        NB = 48

        def __init__(self, parent):
            super().__init__(parent, bg=LCD, highlightthickness=2, highlightbackground=EDGE,
                             width=420, height=140)
            self.lv = np.zeros(self.NB)
            self.cap = np.zeros(self.NB)
            self.rects, self.caps = [], []
            self.bind("<Configure>", lambda e: self.layout())

        def layout(self):
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w < 80 or h < 60:
                return
            self.pl, self.pr, self.pt, self.pb = 30, w - 10, 16, h - 20
            H = self.pb - self.pt
            for f, lab in ((0.25, "-60"), (0.5, "-40"), (0.75, "-20"), (1.0, "0")):
                y = self.pb - H * f
                self.create_line(self.pl, y, self.pr, y, fill="#3A2A00", dash=(2, 4))
                self.create_text(self.pl - 4, y, text=lab, anchor="e", fill=AMBER_DIM, font=(MONO, 7))
            for f, lab in ((100, "100"), (1000, "1k"), (10000, "10k")):
                x = self.pl + (self.pr - self.pl) * math.log(f / 40.0) / math.log(16000 / 40.0)
                self.create_line(x, self.pt, x, self.pb, fill="#2A1F00")
                self.create_text(x, self.pb + 9, text=lab, fill=AMBER_DIM, font=(MONO, 7))
            self.create_text(self.pl, 7, text="SPECTRUM ANALYZER", anchor="w", fill=AMBER_DIM, font=(MONO, 7, "bold"))
            self.rects = [self.create_rectangle(0, 0, 0, 0, fill=AMBER, outline="") for _ in range(self.NB)]
            self.caps = [self.create_line(0, 0, 0, 0, fill=CREAM, width=2) for _ in range(self.NB)]
            self.draw()

        def update_levels(self, lv):
            self.lv = lv
            self.cap = np.maximum(lv, self.cap - 0.015)
            self.draw()

        def draw(self):
            if not self.rects:
                return
            bw = (self.pr - self.pl) / self.NB
            H = self.pb - self.pt
            for i, (r, c) in enumerate(zip(self.rects, self.caps)):
                x0, x1 = self.pl + i * bw + 1, self.pl + (i + 1) * bw - 1
                y, yc = self.pb - H * float(self.lv[i]), self.pb - H * float(self.cap[i])
                self.coords(r, x0, y, x1, self.pb)
                self.coords(c, x0, yc, x1, yc)
                self.itemconfig(r, fill=RED if self.lv[i] > 0.93 else AMBER)

    def screw(parent, bg=CASE, size=16):
        c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
        c.create_oval(1, 1, size - 1, size - 1, fill="#8E8B84", outline=EDGE)
        c.create_oval(3, 3, size - 3, size - 3, fill="#B8B4AA", outline="")
        c.create_line(4, size - 5, size - 4, 5, fill="#3A3833", width=2)
        return c

    def module(parent, title):
        outer = tk.Frame(parent, bg=PANEL, highlightbackground=EDGE, highlightthickness=2)
        hdr = tk.Frame(outer, bg=PANEL)
        hdr.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(hdr, text=title, bg=PANEL, fg=AMBER, font=(MONO, 10, "bold")).pack(side="left")
        tk.Frame(hdr, bg=AMBER_DIM, height=2).pack(side="left", fill="x", expand=True, padx=(8, 0), pady=(9, 0))
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        return outer, body

    def rbutton(parent, text, cmd, **kw):
        return tk.Button(parent, text=text, command=cmd, font=(MONO, 8, "bold"), bg=PANEL_HI, fg=CREAM,
                         activebackground="#4B453D", activeforeground=CREAM, relief="raised", bd=3,
                         padx=8, pady=2, cursor="hand2", **kw)

    # ------------------------------------------------------------------ application
    class App:
        def __init__(self, root):
            self.root = root
            root.title("LIVE FX - Stereo Space Processor")
            root.configure(bg=CASE)
            root.minsize(1080, 700)
            root.option_add("*TCombobox*Listbox.background", LCD)
            root.option_add("*TCombobox*Listbox.foreground", AMBER)
            root.option_add("*TCombobox*Listbox.selectBackground", AMBER)
            root.option_add("*TCombobox*Listbox.selectForeground", "#000000")
            root.option_add("*TCombobox*Listbox.font", (MONO, 9, "bold"))
            self.params = dict(DEFAULTS)
            self.saved = {}
            self.vars = {}
            self.stream = self.fx = None
            self.out_ch = 2
            self.fs = False
            self.rec = False
            self.rec_q = self.rec_thread = self.rec_path = None
            self.rec_t0 = 0.0
            self.clip_until = 0.0
            self.taps = []
            self.win = np.hanning(2048).astype(np.float32)
            self.bins, self.bins_sr = None, None
            self.sp_prev = np.zeros(Spectrum.NB)
            self.devs = sd.query_devices()
            self.apis = sd.query_hostapis()
            self._load_settings()
            self._style()
            self._build()
            self._refresh_devices()
            self._bind_keys()
            self._tick()
            root.protocol("WM_DELETE_WINDOW", self.close)

        # ---------- settings
        def _load_settings(self):
            try:
                data = json.loads(SETTINGS_PATH.read_text())
            except Exception:
                return
            self._apply_values(data.get("params", {}))
            self.params["mute"] = False
            self.saved = {k: data.get(k) for k in ("api", "in", "out", "bs")}

        def _apply_values(self, vals):
            for k, v in vals.items():
                if k not in DEFAULTS:
                    continue
                if k in BOOL_KEYS and isinstance(v, bool):
                    self.params[k] = v
                elif k not in BOOL_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.params[k] = float(v)
                if k in self.vars:
                    self.vars[k].set(self.params[k])

        def _save_settings(self):
            try:
                SETTINGS_PATH.write_text(json.dumps({
                    "params": self.params, "api": self.api_var.get(),
                    "in": self.in_var.get().split(": ", 1)[-1], "out": self.out_var.get().split(": ", 1)[-1],
                    "bs": self.bs_var.get()}, indent=1))
            except Exception:
                pass

        def save_preset(self):
            f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Live FX preset", "*.json")],
                                             initialfile="my_preset.json", title="Save preset")
            if f:
                Path(f).write_text(json.dumps({"params": {k: self.params[k] for k in DEFAULTS
                                                          if k not in ("mute", "bypass")}}, indent=1))

        def load_preset(self):
            f = filedialog.askopenfilename(filetypes=[("Live FX preset", "*.json")], title="Load preset")
            if not f:
                return
            try:
                self._apply_values(json.loads(Path(f).read_text()).get("params", {}))
            except Exception as e:
                messagebox.showerror("Live FX", "Could not load preset.\n\n%s" % e)

        # ---------- style
        def _style(self):
            st = ttk.Style()
            try:
                st.theme_use("clam")
            except Exception:
                pass
            st.configure("Retro.TCombobox", fieldbackground=LCD, background=PANEL_HI, foreground=AMBER,
                         arrowcolor=AMBER, bordercolor=EDGE, lightcolor=PANEL_HI, darkcolor=EDGE,
                         selectbackground=LCD, selectforeground=AMBER)
            st.map("Retro.TCombobox", fieldbackground=[("readonly", LCD)], foreground=[("readonly", AMBER)],
                   selectbackground=[("readonly", LCD)], selectforeground=[("readonly", AMBER)])

        # ---------- build
        def _var(self, key):
            is_bool = key in BOOL_KEYS
            var = tk.BooleanVar(value=self.params[key]) if is_bool else tk.DoubleVar(value=self.params[key])
            self.vars[key] = var

            def cb(*a):
                try:
                    self.params[key] = bool(var.get()) if is_bool else float(var.get())
                except tk.TclError:
                    pass
            var.trace_add("write", cb)
            return var

        def _knob(self, parent, col, label, key, lo, hi, fmt, row=0):
            k = Knob(parent, label, self._var(key), lo, hi, fmt, DEFAULTS[key])
            k.grid(row=row, column=col, padx=5, pady=2)
            parent.columnconfigure(col, weight=1)
            return k

        def _build(self):
            root = self.root
            root.columnconfigure(1, weight=1)
            root.rowconfigure(0, weight=1)
            for col in (0, 2):                                    # walnut side cheeks
                cv = tk.Canvas(root, width=22, bg="#5B3A22", highlightthickness=0)
                shades = ("#5B3A22", "#6B4527", "#4F321D", "#7A5230", "#5B3A22", "#47301C")
                for x in range(22):
                    cv.create_line(x, 0, x, 4000, fill=shades[(x * 5) % len(shades)])
                cv.create_line(21 if col == 0 else 0, 0, 21 if col == 0 else 0, 4000, fill=EDGE, width=2)
                cv.grid(row=0, column=col, sticky="ns")
            main = tk.Frame(root, bg=CASE)
            main.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
            main.columnconfigure(0, weight=1)
            main.rowconfigure(3, weight=1)

            # ---- header
            hd = tk.Frame(main, bg=CASE)
            hd.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            hd.columnconfigure(2, weight=1)
            screw(hd).grid(row=0, column=0, padx=(2, 8))
            tk.Label(hd, text="LIVE FX", bg=CASE, fg=CREAM, font=(MONO, 24, "bold")).grid(row=0, column=1)
            tt = tk.Frame(hd, bg=CASE)
            tt.grid(row=0, column=2, sticky="w", padx=12)
            tk.Label(tt, text="STEREO SPACE PROCESSOR", bg=CASE, fg=AMBER, font=(MONO, 10, "bold")).pack(anchor="w")
            tk.Label(tt, text="REVERB  \u00b7  DELAY  \u00b7  ECHO", bg=CASE, fg=AMBER_DIM,
                     font=(MONO, 8, "bold")).pack(anchor="w")
            tk.Label(hd, text=" MK II ", bg=ORANGE, fg=CREAM, font=(MONO, 9, "bold")).grid(row=0, column=3, padx=8)
            leds = tk.Frame(hd, bg=CASE)
            leds.grid(row=0, column=4, padx=8)
            self.sig_led, self.clip_led, self.rec_led = Led(leds, GREEN), Led(leds, RED), Led(leds, RED)
            for i, (led, t) in enumerate(((self.sig_led, "SIG"), (self.clip_led, "CLIP"), (self.rec_led, "REC"))):
                led.grid(row=0, column=i, padx=8)
                tk.Label(leds, text=t, bg=CASE, fg=CREAM, font=(MONO, 7, "bold")).grid(row=1, column=i)
            self.fs_btn = rbutton(hd, "FULL SCREEN [F11]", self.toggle_fs)
            self.fs_btn.grid(row=0, column=5, padx=8)
            screw(hd).grid(row=0, column=6, padx=(0, 2))

            # ---- routing + presets
            r1 = tk.Frame(main, bg=CASE)
            r1.grid(row=1, column=0, sticky="ew", pady=3)
            r1.columnconfigure(0, weight=3)
            r1.columnconfigure(1, weight=2)
            mod, g = module(r1, "AUDIO ROUTING")
            mod.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            g.columnconfigure(1, weight=1)
            self.api_var, self.in_var, self.out_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
            self.sr_var = tk.StringVar(value="48000")
            self.bs_var = tk.StringVar(value=self.saved.get("bs") or "512")
            lab = lambda p, t: tk.Label(p, text=t, bg=PANEL, fg=CREAM, font=(MONO, 8, "bold"))
            lab(g, "HOST API").grid(row=0, column=0, sticky="w")
            self.api_cb = ttk.Combobox(g, textvariable=self.api_var, state="readonly", style="Retro.TCombobox",
                                       values=[a["name"] for a in self.apis], font=(MONO, 9, "bold"))
            self.api_cb.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
            self.api_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_devices(keep_api=True))
            lab(g, "INPUT (CABLE)").grid(row=1, column=0, sticky="w")
            self.in_cb = ttk.Combobox(g, textvariable=self.in_var, state="readonly", style="Retro.TCombobox",
                                      font=(MONO, 9, "bold"))
            self.in_cb.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
            lab(g, "OUTPUT (SPEAKERS)").grid(row=2, column=0, sticky="w")
            self.out_cb = ttk.Combobox(g, textvariable=self.out_var, state="readonly", style="Retro.TCombobox",
                                       font=(MONO, 9, "bold"))
            self.out_cb.grid(row=2, column=1, sticky="ew", padx=6, pady=2)
            self.out_cb.bind("<<ComboboxSelected>>", lambda e: self._set_rate())
            rw = tk.Frame(g, bg=PANEL)
            rw.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            lab(rw, "SAMPLE RATE").pack(side="left")
            ttk.Combobox(rw, textvariable=self.sr_var, state="readonly", width=6, style="Retro.TCombobox",
                         values=["44100", "48000"], font=(MONO, 9, "bold")).pack(side="left", padx=(6, 14))
            lab(rw, "BUFFER").pack(side="left")
            ttk.Combobox(rw, textvariable=self.bs_var, state="readonly", width=5, style="Retro.TCombobox",
                         values=["256", "512", "1024", "2048"], font=(MONO, 9, "bold")).pack(side="left", padx=6)
            self.btn = tk.Button(rw, text="\u25B6  POWER ON", command=self.toggle, width=14,
                                 font=(MONO, 11, "bold"), fg=CREAM, bg="#2F5E2A", activebackground="#3C7A35",
                                 activeforeground=CREAM, relief="raised", bd=4, padx=4, pady=2, cursor="hand2")
            self.btn.pack(side="right")

            mod, g = module(r1, "PRESETS")
            mod.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
            g.columnconfigure(0, weight=1)
            self.pre_var = tk.StringVar(value="Concert Hall")
            pcb = ttk.Combobox(g, textvariable=self.pre_var, state="readonly", values=list(PRESETS),
                               style="Retro.TCombobox", font=(MONO, 10, "bold"))
            pcb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(2, 6))
            pcb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset(self.pre_var.get()))
            rbutton(g, "SAVE...", self.save_preset).grid(row=1, column=0, sticky="ew", padx=(0, 3))
            rbutton(g, "LOAD...", self.load_preset).grid(row=1, column=1, sticky="ew", padx=(3, 0))
            g.columnconfigure(1, weight=1)
            tk.Label(g, text="Double-click a knob to reset it", bg=PANEL, fg=AMBER_DIM,
                     font=(MONO, 8)).grid(row=2, column=0, columnspan=2, pady=(8, 0))

            # ---- effect rack
            rack = tk.Frame(main, bg=CASE)
            rack.grid(row=2, column=0, sticky="ew", pady=3)
            for i, wgt in enumerate((2, 5, 3, 4)):
                rack.columnconfigure(i, weight=wgt)
            mod, g = module(rack, "INPUT")
            mod.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            self._knob(g, 0, "TRIM", "in_db", -24, 24, DB)
            self._knob(g, 1, "DRY", "dry", 0, 1, PCT)

            mod, g = module(rack, "REVERB")
            mod.grid(row=0, column=1, sticky="nsew", padx=4)
            self._knob(g, 0, "MIX", "r_mix", 0, 1, PCT)
            self._knob(g, 1, "SIZE", "r_room", 0, 1, PCT)
            self._knob(g, 2, "DAMP", "r_damp", 0, 1, PCT)
            self._knob(g, 3, "PRE-DLY", "r_pre", 0, 200, MS)
            self._knob(g, 4, "WIDTH", "r_width", 0, 1.5, PCT)

            mod, g = module(rack, "DELAY")
            mod.grid(row=0, column=2, sticky="nsew", padx=4)
            self._knob(g, 0, "TIME", "d_time", 20, 1000, MS)
            self._knob(g, 1, "MIX", "d_mix", 0, 1, PCT)
            Toggle(g, "PING-PONG", self._var("pingpong"), width=12).grid(row=1, column=0, columnspan=2, pady=(4, 0))

            mod, g = module(rack, "ECHO")
            mod.grid(row=0, column=3, sticky="nsew", padx=(4, 0))
            self._knob(g, 0, "TIME", "e_time", 50, 1500, MS)
            self._knob(g, 1, "REPEATS", "e_fb", 0, 0.9, PCT)
            self._knob(g, 2, "TONE", "e_tone", 0, 1, PCT)
            self._knob(g, 3, "MIX", "e_mix", 0, 1, PCT)
            tp = tk.Frame(g, bg=PANEL)
            tp.grid(row=1, column=0, columnspan=4, pady=(4, 0))
            rbutton(tp, "TAP TEMPO", self.tap).pack(side="left", padx=6)
            self.bpm_lbl = tk.Label(tp, text="-- BPM", bg=LCD, fg=AMBER, font=(MONO, 9, "bold"), width=9,
                                    relief="sunken", bd=2)
            self.bpm_lbl.pack(side="left")

            # ---- meters + output
            row3 = tk.Frame(main, bg=CASE)
            row3.grid(row=3, column=0, sticky="nsew", pady=3)
            row3.rowconfigure(0, weight=1)
            row3.columnconfigure(0, weight=2, uniform="m")
            row3.columnconfigure(1, weight=5, uniform="m")
            row3.columnconfigure(2, weight=2, uniform="m")
            row3.columnconfigure(3, weight=0)
            self.vu_in = VU(row3, "INPUT  LEVEL")
            self.vu_in.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            self.spec = Spectrum(row3)
            self.spec.grid(row=0, column=1, sticky="nsew", padx=4)
            self.vu_out = VU(row3, "OUTPUT  LEVEL")
            self.vu_out.grid(row=0, column=2, sticky="nsew", padx=4)
            mod, g = module(row3, "OUTPUT")
            mod.grid(row=0, column=3, sticky="nsew", padx=(4, 0))
            Knob(g, "LEVEL", self._var("out_db"), -30, 6, DB, DEFAULTS["out_db"]).grid(row=0, column=0, rowspan=4,
                                                                                    padx=(0, 10))
            Toggle(g, "LIMITER", self._var("limiter"), color=GREEN, width=8).grid(row=0, column=1, pady=2, sticky="w")
            Toggle(g, "BYPASS", self._var("bypass"), color=AMBER, width=8).grid(row=1, column=1, pady=2, sticky="w")
            Toggle(g, "MUTE", self._var("mute"), color=RED, width=8).grid(row=2, column=1, pady=2, sticky="w")
            self.rec_btn = rbutton(g, "\u25CF REC", self.toggle_rec, width=10)
            self.rec_btn.grid(row=3, column=1, pady=2, sticky="w", padx=(19, 0))

            # ---- status bar
            sb = tk.Frame(main, bg=CASE)
            sb.grid(row=4, column=0, sticky="ew", pady=(4, 0))
            sb.columnconfigure(0, weight=1)
            self.status = tk.Label(sb, text=" READY - set Input to your virtual cable, Output to your speakers, press POWER ON",
                                   anchor="w", bg=LCD, fg=AMBER, font=(MONO, 9, "bold"), relief="sunken", bd=2)
            self.status.grid(row=0, column=0, sticky="ew")
            self.state = tk.Label(sb, text=" STOPPED ", bg=LCD, fg=AMBER, font=(MONO, 9, "bold"), relief="sunken",
                                  bd=2, width=18)
            self.state.grid(row=0, column=1, padx=(4, 0))
            tk.Label(main, text="F11 FULL SCREEN   ESC EXIT   CTRL+P POWER   CTRL+B BYPASS   CTRL+M MUTE   CTRL+R REC"
                                "   |   SHIFT+DRAG = FINE", bg=CASE, fg=AMBER_DIM, font=(MONO, 7, "bold")
                     ).grid(row=5, column=0, pady=(3, 0))

        def _bind_keys(self):
            r = self.root
            r.bind("<F11>", self.toggle_fs)
            r.bind("<Escape>", lambda e: self.toggle_fs() if self.fs else None)
            r.bind("<Control-p>", lambda e: self.toggle())
            r.bind("<Control-b>", lambda e: self.vars["bypass"].set(not self.vars["bypass"].get()))
            r.bind("<Control-m>", lambda e: self.vars["mute"].set(not self.vars["mute"].get()))
            r.bind("<Control-r>", lambda e: self.toggle_rec())

        def toggle_fs(self, e=None):
            self.fs = not self.fs
            self.root.attributes("-fullscreen", self.fs)
            self.fs_btn.config(text="EXIT FULL SCREEN [ESC]" if self.fs else "FULL SCREEN [F11]")

        # ---------- devices
        def _refresh_devices(self, keep_api=False):
            names = [a["name"] for a in self.apis]
            if not keep_api or not self.api_var.get():
                saved = self.saved.get("api")
                if saved in names:
                    self.api_var.set(saved)
                else:
                    try:
                        api = sd.query_devices(kind="output")["hostapi"]
                    except Exception:
                        api = sd.default.hostapi
                    self.api_var.set(names[api])
            api = names.index(self.api_var.get())
            ins = [(i, d) for i, d in enumerate(self.devs) if d["hostapi"] == api and d["max_input_channels"] > 0]
            outs = [(i, d) for i, d in enumerate(self.devs) if d["hostapi"] == api and d["max_output_channels"] > 0]
            fmt = lambda i, d: "%d: %s" % (i, d["name"])
            self.in_cb["values"] = [fmt(i, d) for i, d in ins]
            self.out_cb["values"] = [fmt(i, d) for i, d in outs]

            pick = None
            for i, d in ins:
                if d["name"] == self.saved.get("in"):
                    pick = fmt(i, d)
            if not pick:
                for i, d in ins:
                    if any(k in d["name"].lower() for k in PREFERRED_INPUT):
                        pick = fmt(i, d)
                        break
            self.in_var.set(pick or (fmt(*ins[0]) if ins else ""))

            try:
                default_out = sd.query_devices(kind="output")["index"]
            except Exception:
                default_out = sd.default.device[1]
            pick = None
            for i, d in outs:
                if d["name"] == self.saved.get("out"):
                    pick = fmt(i, d)
            if not pick:
                for i, d in outs:
                    if i == default_out:
                        pick = fmt(i, d)
            self.out_var.set(pick or (fmt(*outs[0]) if outs else ""))
            self._set_rate()

        def _set_rate(self):
            try:
                idx = int(self.out_var.get().split(":")[0])
                sr = int(self.devs[idx]["default_samplerate"])
                self.sr_var.set(str(sr if sr in (44100, 48000) else 48000))
            except Exception:
                pass

        # ---------- presets / tap tempo
        def apply_preset(self, name):
            vals = {k: DEFAULTS[k] for k in FX_KEYS}
            vals.update(PRESETS[name])
            for k, v in vals.items():
                self.vars[k].set(bool(v) if k in BOOL_KEYS else float(v))

        def tap(self):
            now = time.time()
            if self.taps and now - self.taps[-1] > 2.0:
                self.taps = []
            self.taps = (self.taps + [now])[-6:]
            if len(self.taps) >= 2:
                iv = (self.taps[-1] - self.taps[0]) / (len(self.taps) - 1)
                self.bpm_lbl.config(text="%d BPM" % round(60.0 / iv))
                self.vars["e_time"].set(max(50.0, min(1500.0, iv * 1000.0)))

        # ---------- audio
        def audio_cb(self, indata, outdata, frames, time_info, status):
            x = indata
            if x.shape[1] == 1:
                x = np.repeat(x, 2, axis=1)
            y = self.fx.process(x)
            outdata[:] = y if self.out_ch == 2 else y.mean(axis=1, keepdims=True)
            if self.rec:
                self.rec_q.put((y * 32767.0).astype("<i2").tobytes())

        def toggle(self):
            self.stop() if self.stream else self.start()

        def start(self):
            try:
                i_idx = int(self.in_var.get().split(":")[0])
                o_idx = int(self.out_var.get().split(":")[0])
                sr = int(self.sr_var.get())
                in_ch = min(2, self.devs[i_idx]["max_input_channels"])
                self.out_ch = min(2, self.devs[o_idx]["max_output_channels"])
                self.fx = Effects(sr, self.params)
                self.stream = sd.Stream(device=(i_idx, o_idx), samplerate=sr, blocksize=int(self.bs_var.get()),
                                        dtype="float32", channels=(in_ch, self.out_ch), latency="low",
                                        callback=self.audio_cb)
                self.stream.start()
            except Exception as e:
                self.stream = None
                messagebox.showerror("Live FX",
                                     "Could not start audio.\n\n%s\n\nTips:\n- Use the same Host API for input and output\n"
                                     "- Make sure the sample rate is supported by both devices\n"
                                     "- Increase the buffer size if audio crackles" % e)
                return
            self.btn.config(text="\u25A0  POWER OFF", bg="#8C2A1A", activebackground="#A8361F")
            self.status.config(text=" RUNNING - play something on YouTube (routed to the input device)")
            self.state.config(text=" POWER ON ")

        def stop(self):
            if self.rec:
                self._rec_stop()
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
            self.stream = None
            self.btn.config(text="\u25B6  POWER ON", bg="#2F5E2A", activebackground="#3C7A35")
            self.status.config(text=" READY")
            self.state.config(text=" STOPPED ")

        # ---------- recording (processed output -> 16-bit stereo WAV)
        @staticmethod
        def _rec_worker(path, sr, q):
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                while True:
                    b = q.get()
                    if b is None:
                        break
                    wf.writeframes(b)

        def toggle_rec(self):
            if self.rec:
                self._rec_stop()
                return
            if not self.stream:
                messagebox.showinfo("Live FX", "Press POWER ON first, then start recording.")
                return
            d = Path.home() / "LiveFX_Recordings"
            d.mkdir(parents=True, exist_ok=True)
            self.rec_path = d / datetime.datetime.now().strftime("livefx_%Y%m%d_%H%M%S.wav")
            self.rec_q = queue.Queue()
            self.rec_thread = threading.Thread(target=self._rec_worker,
                                               args=(self.rec_path, self.fx.sr, self.rec_q), daemon=True)
            self.rec_thread.start()
            self.rec_t0 = time.time()
            self.rec = True
            self.rec_btn.config(text="\u25A0 STOP REC", fg=RED)

        def _rec_stop(self):
            self.rec = False
            self.rec_q.put(None)
            self.rec_thread.join(timeout=5)
            self.rec_btn.config(text="\u25CF REC", fg=CREAM)
            self.rec_led.set(False)
            self.status.config(text=" SAVED: %s" % self.rec_path)

        # ---------- GUI refresh
        def _spectrum_levels(self):
            fx = self.fx
            if fx is None or not self.stream:
                return np.zeros(Spectrum.NB)
            if self.bins is None or self.bins_sr != fx.sr:
                edges = np.geomspace(40, 16000, Spectrum.NB + 1)
                self.bins = np.clip((edges * 2048 / fx.sr).astype(int), 0, 1024)
                self.bins_sr = fx.sr
            sig = fx.scope
            db = 20.0 * np.log10(np.abs(np.fft.rfft(sig * self.win)) / 512.0 + 1e-9)
            b = self.bins
            lv = np.array([db[b[i]:max(b[i + 1], b[i] + 1)].max() for i in range(Spectrum.NB)])
            return np.clip((lv + 80.0) / 80.0, 0.0, 1.0)

        def _tick(self):
            now = time.time()
            fx = self.fx if self.stream else None
            if fx:
                iv = 20.0 * math.log10(fx.in_rms + 1e-9)
                ov = 20.0 * math.log10(fx.out_rms + 1e-9)
                self.sig_led.set(fx.in_pk > 0.003)
                if fx.clipped:
                    self.clip_until = now + 1.2
                    fx.clipped = False
            else:
                iv = ov = -99.0
                self.sig_led.set(False)
            self.vu_in.smooth(iv)
            self.vu_out.smooth(ov)
            self.clip_led.set(now < self.clip_until)
            self.sp_prev = np.maximum(self._spectrum_levels(), self.sp_prev * 0.82)
            self.spec.update_levels(self.sp_prev)
            if self.rec:
                self.rec_led.set(int(now * 2) % 2 == 0)
                el = int(now - self.rec_t0)
                self.state.config(text=" REC %02d:%02d " % (el // 60, el % 60))
            elif self.stream:
                self.state.config(text=" POWER ON ")
            self.root.after(40, self._tick)

        def close(self):
            self.stop()
            self._save_settings()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(sd.query_devices())
        sys.exit(0)
    run_gui()