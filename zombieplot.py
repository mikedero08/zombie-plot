#!/usr/bin/env python3
"""
zombiePlot: phone control for Roland CAMM-1 vinyl cutters (CAMM-GL III / HP-GL).
https://github.com/mikedero08/zombie-plot

One file, Python standard library only (3.9+). It does three things:
  1. Reads SVG files and converts them to cutter commands.
  2. Sends those commands to the cutter over a USB-to-parallel cable (or USB-to-serial).
  3. Serves a phone-sized web page to pick a file, rotate / mirror / copy, and cut.

Run:  python3 zombieplot.py --port 80 --data /var/lib/zombieplot --library /mnt/plots
"""

import argparse
import errno
import glob
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import select
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
try:
    import fcntl
    import termios
except ImportError:       # Windows: only --car-thing-install is meant to run there
    fcntl = termios = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.7"
UNITS_PER_MM = 40.0          # CAMM-GL III step = 0.025 mm
FLATTEN_TOL_MM = 0.03        # curve-to-line tolerance (about one cutter step)
MAX_UPLOAD = 25 * 1024 * 1024
KEEP_UPLOADS = 50


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

DEFAULTS = {
    "link": "auto",            # auto | parallel | serial
    "port": "auto",            # "auto" or a device path
    "baud": 9600,
    "flow": "hardware",        # hardware | xonxoff | none
    "cut_width_mm": 584.0,     # widest the plotter can cut (PNC-1210: 585 mm / 23 in)
    "vinyl_width_mm": 584.0,   # usable width of the vinyl loaded now
    "start_corner": "origin",  # origin | far: which edge of the vinyl jobs start from
    "copy_gap_mm": 5.0,
    "overcut_mm": 1.0,         # matches the 1 mm overcut set in Inkcut
    "end_action": "origin",    # origin | next | advance
    "advance_mm": 25.0,
    "swap_axes": False,
    "mirror_carriage": False,
    "mirror_feed": False,
    "px_per_inch": 96.0,       # only used for SVGs that carry no real-world units
    "units": "in",             # display units: in | mm
    "init": "IN;PA;",
    "auto_update": False,
    "thing_sleep_min": 5,      # Car Thing screen goes dark after this many idle minutes (0 = never)
    "pin_hash": "",            # set through /api/pin only
    "steps_per_mm": 40.0,      # plotter units per mm (HP-GL / Roland CAMM-GL: 40)
}


def _raise():
    raise ValueError("not valid")


def _num(lo, hi):
    def check(v):
        v = float(v)
        if not (lo <= v <= hi) or math.isnan(v):
            raise ValueError("out of range")
        return v
    return check


def _choice(*opts):
    def check(v):
        if v not in opts:
            raise ValueError("not one of %s" % (opts,))
        return v
    return check


def _bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("not true/false")


def _port(v):
    v = str(v).strip()
    if v == "auto" or (v.startswith("/dev/") and ".." not in v and len(v) < 200):
        return v
    raise ValueError("must be 'auto' or a /dev/ path")


def _init(v):
    v = str(v)
    if len(v) > 200 or not all(32 <= ord(c) < 127 for c in v):
        raise ValueError("plain text only")
    return v


VALIDATORS = {
    "link": _choice("auto", "parallel", "serial"),
    "port": _port,
    "baud": lambda v: int(_choice(2400, 4800, 9600, 19200)(int(v))),
    "flow": _choice("hardware", "xonxoff", "none"),
    "cut_width_mm": _num(20, 3000),
    "vinyl_width_mm": _num(20, 3000),
    "start_corner": _choice("origin", "far"),
    "steps_per_mm": _num(1, 1000),
    "copy_gap_mm": _num(0, 500),
    "overcut_mm": _num(0, 10),
    "end_action": _choice("origin", "next", "advance"),
    "advance_mm": _num(0, 1000),
    "swap_axes": _bool,
    "mirror_carriage": _bool,
    "mirror_feed": _bool,
    "px_per_inch": _num(10, 2400),
    "units": _choice("in", "mm"),
    "init": _init,
    "auto_update": _bool,
    "thing_sleep_min": lambda v: int(_num(0, 240)(v)),
    "pin_hash": lambda v: str(v) if re.match(r"^(|pbkdf2\$\d+\$[0-9a-f]+\$[0-9a-f]+)$", str(v)) else _raise(),
}


class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            for k, v in stored.items():
                if k in VALIDATORS:
                    try:
                        self.data[k] = VALIDATORS[k](v)
                    except (ValueError, TypeError):
                        pass
        except (OSError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def get(self, key):
        with self.lock:
            return self.data[key]

    def update(self, changes):
        clean = {}
        for k, v in changes.items():
            if k not in VALIDATORS:
                continue
            try:
                clean[k] = VALIDATORS[k](v)
            except (ValueError, TypeError) as e:
                raise ValueError("Setting '%s': %s" % (k, e))
        with self.lock:
            self.data.update(clean)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            return dict(self.data)


# ----------------------------------------------------------------------------
# SVG -> polylines
# ----------------------------------------------------------------------------

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
NUM_RE = re.compile(NUM)
LEN_RE = re.compile(r"^\s*(" + NUM + r")\s*([a-zA-Z%]*)\s*$")
TRANSFORM_RE = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
HIDDEN_RE = re.compile(r"display\s*:\s*none")
IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# CSS absolute units, in CSS pixels (96 per inch)
UNIT_PX = {"": 1.0, "px": 1.0, "in": 96.0, "mm": 96.0 / 25.4, "cm": 96.0 / 2.54,
           "pt": 96.0 / 72.0, "pc": 16.0, "q": 96.0 / 101.6}


class SvgError(Exception):
    pass


def mat_mul(m, n):
    """Matrix for 'apply n first, then m'."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2,
            a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def mat_apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def parse_transform(s):
    m = IDENT
    for name, args in TRANSFORM_RE.findall(s or ""):
        v = [float(x) for x in NUM_RE.findall(args)]
        name = name.lower()
        t = None
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and len(v) in (1, 2):
            t = (1, 0, 0, 1, v[0], v[1] if len(v) == 2 else 0.0)
        elif name == "scale" and len(v) in (1, 2):
            t = (v[0], 0, 0, v[1] if len(v) == 2 else v[0], 0, 0)
        elif name == "rotate" and len(v) in (1, 3):
            a = math.radians(v[0])
            ca, sa = math.cos(a), math.sin(a)
            t = (ca, sa, -sa, ca, 0, 0)
            if len(v) == 3:
                t = mat_mul(mat_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewx" and len(v) == 1:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewy" and len(v) == 1:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        if t is not None:
            m = mat_mul(m, t)
    return m


def parse_len(s):
    """Returns (value, unit) or None."""
    if s is None:
        return None
    m = LEN_RE.match(s)
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower()


def user_len(el, name, default=0.0):
    """A length attribute inside the drawing, in user units."""
    p = parse_len(el.get(name))
    if p is None or p[1] not in UNIT_PX:
        return default
    return p[0] * UNIT_PX[p[1]]


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class PathBuilder:
    """Collects sub-paths as lists of points already transformed to mm."""

    def __init__(self, ctm, out):
        self.m = ctm
        self.out = out
        self.cur = None
        self.pos = (0.0, 0.0)      # current point, user space
        self.start = (0.0, 0.0)    # start of current sub-path, user space

    def _flush(self, closed):
        cur, self.cur = self.cur, None
        if not cur:
            return
        if closed and len(cur) > 2 and math.hypot(cur[0][0] - cur[-1][0], cur[0][1] - cur[-1][1]) < 1e-6:
            cur.pop()
        if len(cur) >= 2:
            self.out.append((cur, closed))

    def _ensure(self):
        if self.cur is None:
            self.start = self.pos
            self.cur = [mat_apply(self.m, *self.pos)]

    def move_to(self, x, y):
        self._flush(False)
        self.pos = self.start = (x, y)
        self.cur = [mat_apply(self.m, x, y)]

    def line_to(self, x, y):
        self._ensure()
        self.cur.append(mat_apply(self.m, x, y))
        self.pos = (x, y)

    def cubic_to(self, x1, y1, x2, y2, x, y):
        self._ensure()
        p0 = self.cur[-1]
        p1 = mat_apply(self.m, x1, y1)
        p2 = mat_apply(self.m, x2, y2)
        p3 = mat_apply(self.m, x, y)
        d = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
                math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
        n = min(2000, max(1, int(math.ceil(math.sqrt(0.75 * d / FLATTEN_TOL_MM)))))
        for i in range(1, n):
            t = i / n
            u = 1 - t
            a, b, c, e = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                             a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))
        self.cur.append(p3)
        self.pos = (x, y)

    def quad_to(self, x1, y1, x, y):
        px, py = self.pos
        self.cubic_to(px + 2.0 / 3 * (x1 - px), py + 2.0 / 3 * (y1 - py),
                      x + 2.0 / 3 * (x1 - x), y + 2.0 / 3 * (y1 - y), x, y)

    def arc_to(self, rx, ry, phi_deg, large, sweep, x, y):
        x1, y1 = self.pos
        if x1 == x and y1 == y:
            return
        rx, ry = abs(rx), abs(ry)
        if rx == 0 or ry == 0:
            self.line_to(x, y)
            return
        phi = math.radians(phi_deg % 360.0)
        cp, sp = math.cos(phi), math.sin(phi)
        dx, dy = (x1 - x) / 2.0, (y1 - y) / 2.0
        x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
        lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
        if lam > 1:
            s = math.sqrt(lam)
            rx, ry = rx * s, ry * s
        num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
        den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
        coef = math.sqrt(max(0.0, num / den)) if den else 0.0
        if large == sweep:
            coef = -coef
        cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
        cx = cp * cxp - sp * cyp + (x1 + x) / 2.0
        cy = sp * cxp + cp * cyp + (y1 + y) / 2.0
        th1 = math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx)
        th2 = math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx)
        dth = th2 - th1
        if sweep and dth < 0:
            dth += 2 * math.pi
        elif not sweep and dth > 0:
            dth -= 2 * math.pi
        n = max(1, int(math.ceil(abs(dth) / (math.pi / 4) - 1e-9)))   # 45 degree pieces: error ~4e-6 x radius
        seg = dth / n
        k = 4.0 / 3.0 * math.tan(seg / 4.0)

        def pt(ux, uy):
            return (cx + rx * ux * cp - ry * uy * sp, cy + rx * ux * sp + ry * uy * cp)

        for i in range(n):
            a1 = th1 + i * seg
            a2 = a1 + seg
            c1, s1, c2, s2 = math.cos(a1), math.sin(a1), math.cos(a2), math.sin(a2)
            q1 = pt(c1 - k * s1, s1 + k * c1)
            q2 = pt(c2 + k * s2, s2 - k * c2)
            q3 = (x, y) if i == n - 1 else pt(c2, s2)
            self.cubic_to(q1[0], q1[1], q2[0], q2[1], q3[0], q3[1])

    def close(self):
        if self.cur is not None:
            self._flush(True)
        self.pos = self.start

    def finish(self):
        self._flush(False)


class _Scan:
    CMDS = "MmZzLlHhVvCcSsQqTtAa"
    WS = " \t\r\n\x0c,"

    def __init__(self, s):
        self.s = s
        self.i = 0
        self.n = len(s)

    def skip(self):
        while self.i < self.n and self.s[self.i] in self.WS:
            self.i += 1

    def num(self):
        self.skip()
        m = NUM_RE.match(self.s, self.i)
        if not m:
            raise ValueError("number expected")
        self.i = m.end()
        return float(m.group())

    def flag(self):
        self.skip()
        if self.i < self.n and self.s[self.i] in "01":
            self.i += 1
            return self.s[self.i - 1] == "1"
        raise ValueError("flag expected")


def parse_path(d, pb):
    sc = _Scan(d)
    cmd = None
    ctrl = None          # last control point (for S / T)
    ctrl_kind = ""
    try:
        while True:
            sc.skip()
            if sc.i >= sc.n:
                break
            ch = sc.s[sc.i]
            if ch in _Scan.CMDS:
                cmd = ch
                sc.i += 1
                if cmd in "Zz":
                    pb.close()
                    ctrl_kind = ""
                    continue
            elif cmd is None or cmd in "Zz":
                break
            rel = cmd.islower()
            c = cmd.upper()
            ox, oy = pb.pos if rel else (0.0, 0.0)
            px, py = pb.pos
            if c == "M":
                x, y = sc.num() + ox, sc.num() + oy
                pb.move_to(x, y)
                cmd = "l" if rel else "L"
                ctrl_kind = ""
            elif c == "L":
                x, y = sc.num() + ox, sc.num() + oy
                pb.line_to(x, y)
                ctrl_kind = ""
            elif c == "H":
                pb.line_to(sc.num() + ox, py)
                ctrl_kind = ""
            elif c == "V":
                pb.line_to(px, sc.num() + oy)
                ctrl_kind = ""
            elif c == "C":
                x1, y1 = sc.num() + ox, sc.num() + oy
                x2, y2 = sc.num() + ox, sc.num() + oy
                x, y = sc.num() + ox, sc.num() + oy
                pb.cubic_to(x1, y1, x2, y2, x, y)
                ctrl, ctrl_kind = (x2, y2), "C"
            elif c == "S":
                x2, y2 = sc.num() + ox, sc.num() + oy
                x, y = sc.num() + ox, sc.num() + oy
                x1, y1 = (2 * px - ctrl[0], 2 * py - ctrl[1]) if ctrl_kind == "C" else (px, py)
                pb.cubic_to(x1, y1, x2, y2, x, y)
                ctrl, ctrl_kind = (x2, y2), "C"
            elif c == "Q":
                x1, y1 = sc.num() + ox, sc.num() + oy
                x, y = sc.num() + ox, sc.num() + oy
                pb.quad_to(x1, y1, x, y)
                ctrl, ctrl_kind = (x1, y1), "Q"
            elif c == "T":
                x, y = sc.num() + ox, sc.num() + oy
                x1, y1 = (2 * px - ctrl[0], 2 * py - ctrl[1]) if ctrl_kind == "Q" else (px, py)
                pb.quad_to(x1, y1, x, y)
                ctrl, ctrl_kind = (x1, y1), "Q"
            elif c == "A":
                rx, ry, phi = sc.num(), sc.num(), sc.num()
                large, sweep = sc.flag(), sc.flag()
                x, y = sc.num() + ox, sc.num() + oy
                pb.arc_to(rx, ry, phi, large, sweep, x, y)
                ctrl_kind = ""
    except ValueError:
        pass                 # per the SVG spec: keep what parsed before the error
    pb.finish()


def _points(s):
    v = [float(x) for x in NUM_RE.findall(s or "")]
    return [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]


def _ellipse(pb, cx, cy, rx, ry):
    if rx <= 0 or ry <= 0:
        return
    pb.move_to(cx + rx, cy)
    pb.arc_to(rx, ry, 0, False, True, cx - rx, cy)
    pb.arc_to(rx, ry, 0, False, True, cx + rx, cy)
    pb.close()
    pb.finish()


def _rect(pb, el):
    x, y = user_len(el, "x"), user_len(el, "y")
    w, h = user_len(el, "width"), user_len(el, "height")
    if w <= 0 or h <= 0:
        return
    rx = user_len(el, "rx", -1.0)
    ry = user_len(el, "ry", -1.0)
    if rx < 0 and ry < 0:
        rx = ry = 0.0
    elif rx < 0:
        rx = ry
    elif ry < 0:
        ry = rx
    rx, ry = min(rx, w / 2.0), min(ry, h / 2.0)
    if rx <= 0 or ry <= 0:
        pb.move_to(x, y)
        pb.line_to(x + w, y)
        pb.line_to(x + w, y + h)
        pb.line_to(x, y + h)
    else:
        pb.move_to(x + rx, y)
        pb.line_to(x + w - rx, y)
        pb.arc_to(rx, ry, 0, False, True, x + w, y + ry)
        pb.line_to(x + w, y + h - ry)
        pb.arc_to(rx, ry, 0, False, True, x + w - rx, y + h)
        pb.line_to(x + rx, y + h)
        pb.arc_to(rx, ry, 0, False, True, x, y + h - ry)
        pb.line_to(x, y + ry)
        pb.arc_to(rx, ry, 0, False, True, x + rx, y)
    pb.close()
    pb.finish()


def bbox(polys):
    xs = [p[0] for pts, _ in polys for p in pts]
    ys = [p[1] for pts, _ in polys for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def parse_svg(data, px_per_inch=96.0):
    """Returns (polys, width_mm, height_mm, warnings).

    polys is a list of (points, closed); points are (x, y) in mm with the
    drawing's lower-left corner at 0,0 and y pointing up.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SvgError("Not a readable SVG file (%s)." % e)
    if local(root.tag) != "svg":
        raise SvgError("Not an SVG file.")

    # Size of one user unit in mm.
    px_mm = 25.4 / px_per_inch
    scale = (px_mm, px_mm)
    warnings = []
    vb = [float(x) for x in NUM_RE.findall(root.get("viewBox") or "")]
    w, h = parse_len(root.get("width")), parse_len(root.get("height"))

    def to_mm(p):
        if p is None or p[1] not in UNIT_PX:
            return None
        if p[1] in ("", "px"):
            return p[0] * px_mm
        return p[0] * UNIT_PX[p[1]] * 25.4 / 96.0

    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        wm, hm = to_mm(w), to_mm(h)
        if wm and hm:
            sx, sy = wm / vb[2], hm / vb[3]
            if (root.get("preserveAspectRatio") or "").strip().startswith("none"):
                scale = (sx, sy)
            else:
                scale = (min(sx, sy), min(sx, sy))
        elif wm:
            scale = (wm / vb[2], wm / vb[2])
        elif hm:
            scale = (hm / vb[3], hm / vb[3])
    real_units = any(p is not None and p[1] not in ("", "px", "%") for p in (w, h))
    if not real_units:
        warnings.append("This file has no inch/mm size in it, so the size is a guess "
                        "(%g px per inch). Check the dimensions before cutting." % px_per_inch)

    ids = {}
    for el in root.iter():
        i = el.get("id")
        if i:
            ids[i] = el

    out = []
    skipped = {"text": 0, "image": 0}

    def hidden(el):
        if el.get("display") == "none" or el.get("visibility") == "hidden":
            return True
        st = el.get("style")
        return bool(st and HIDDEN_RE.search(st))

    def walk(el, ctm, depth, via_use):
        if depth > 40 or hidden(el):
            return
        tag = local(el.tag)
        t = el.get("transform")
        if t:
            ctm = mat_mul(ctm, parse_transform(t))
        if tag in ("svg", "g", "a", "switch") or (tag == "symbol" and via_use):
            if tag == "svg" and depth > 0:
                ctm = mat_mul(ctm, (1, 0, 0, 1, user_len(el, "x"), user_len(el, "y")))
            for ch in el:
                walk(ch, ctm, depth + 1, False)
        elif tag == "use":
            ref = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            target = ids.get(ref[1:]) if ref.startswith("#") else None
            if target is not None and target is not el:
                ctm = mat_mul(ctm, (1, 0, 0, 1, user_len(el, "x"), user_len(el, "y")))
                walk(target, ctm, depth + 1, True)
        elif tag == "path":
            parse_path(el.get("d") or "", PathBuilder(ctm, out))
        elif tag == "rect":
            _rect(PathBuilder(ctm, out), el)
        elif tag == "circle":
            r = user_len(el, "r")
            _ellipse(PathBuilder(ctm, out), user_len(el, "cx"), user_len(el, "cy"), r, r)
        elif tag == "ellipse":
            _ellipse(PathBuilder(ctm, out), user_len(el, "cx"), user_len(el, "cy"),
                     user_len(el, "rx"), user_len(el, "ry"))
        elif tag == "line":
            pb = PathBuilder(ctm, out)
            pb.move_to(user_len(el, "x1"), user_len(el, "y1"))
            pb.line_to(user_len(el, "x2"), user_len(el, "y2"))
            pb.finish()
        elif tag in ("polyline", "polygon"):
            pts = _points(el.get("points"))
            if len(pts) >= 2:
                pb = PathBuilder(ctm, out)
                pb.move_to(*pts[0])
                for p in pts[1:]:
                    pb.line_to(*p)
                if tag == "polygon":
                    pb.close()
                pb.finish()
        elif tag in skipped:
            skipped[tag] += 1

    walk(root, (scale[0], 0.0, 0.0, scale[1], 0.0, 0.0), 0, False)

    if skipped["text"]:
        warnings.append("%d text object(s) were skipped. Convert text to curves in your "
                        "drawing program, then export again." % skipped["text"])
    if skipped["image"]:
        warnings.append("%d picture(s) were skipped. Only vector shapes can be cut." % skipped["image"])

    # Drop zero-length junk, then put the lower-left corner at 0,0 with y up.
    polys = []
    for pts, closed in out:
        x0, y0 = pts[0]
        if any(abs(p[0] - x0) > 1e-4 or abs(p[1] - y0) > 1e-4 for p in pts):
            polys.append((pts, closed))
    if not polys:
        raise SvgError("Nothing to cut: this file has no vector shapes in it.")
    minx, miny, maxx, maxy = bbox(polys)
    polys = [([(x - minx, maxy - y) for x, y in pts], closed) for pts, closed in polys]
    return polys, maxx - minx, maxy - miny, warnings


# ----------------------------------------------------------------------------
# Layout and cutter commands
# ----------------------------------------------------------------------------

def orient(polys, rot, mirror):
    """Mirror left-right (optional), rotate counter-clockwise by rot degrees, re-zero."""
    def f(x, y):
        if mirror:
            x = -x
        if rot == 90:
            return -y, x
        if rot == 180:
            return -x, -y
        if rot == 270:
            return y, -x
        return x, y
    moved = [([f(x, y) for x, y in pts], closed) for pts, closed in polys]
    minx, miny, maxx, maxy = bbox(moved)
    return [([(x - minx, y - miny) for x, y in pts], closed) for pts, closed in moved], maxx - minx, maxy - miny


def grid(w, h, copies, cut_w, gap, layout="across"):
    if layout == "stacked":
        cols = 1
    else:
        cols = max(1, int((cut_w + gap + 1e-6) // (w + gap))) if w + gap > 0 else 1
    cols = min(cols, copies)
    rows = int(math.ceil(copies / float(cols)))
    offsets = [((i % cols) * (w + gap), (i // cols) * (h + gap)) for i in range(copies)]
    return cols, rows, offsets, cols * w + (cols - 1) * gap, rows * h + (rows - 1) * gap


def overlaps(a, b, gap=0.0):
    """Rectangles (x, y, w, h). True if they overlap or sit closer than gap."""
    e = 1e-6
    return (a[0] < b[0] + b[2] + gap - e and b[0] < a[0] + a[2] + gap - e and
            a[1] < b[1] + b[3] + gap - e and b[1] < a[1] + a[3] + gap - e)


def find_spot(used, w, h, avail, gap):
    """Lowest (then leftmost) place for a w x h block that clears every used rectangle."""
    xs = sorted({0.0} | {r[0] + r[2] + gap for r in used})
    ys = sorted({0.0} | {r[1] + r[3] + gap for r in used})
    for y in ys:
        for x in xs:
            if x + w > avail + 1e-6:
                continue
            if not any(overlaps((x, y, w, h), r, gap) for r in used):
                return x, y
    return 0.0, max([0.0] + [r[1] + r[3] + gap for r in used])


def mirror_x(r, width):
    """Rectangle as measured from the other edge of the vinyl."""
    return (width - r[0] - r[2], r[1], r[2], r[3])


def order_polys(polys):
    """Cut in bands along the feed direction so the vinyl is not run back and forth."""
    if len(polys) < 3:
        return list(polys)
    bands = {}
    for pts, closed in polys:
        bands.setdefault(int(min(p[1] for p in pts) // 50.0), []).append((pts, closed))
    out = []
    pos = (0.0, 0.0)
    for key in sorted(bands):
        rem = bands[key]
        if len(rem) > 400:                      # too many for nearest-neighbour; sweep instead
            rem.sort(key=lambda it: it[0][0][0])
            out.extend(rem)
            pos = rem[-1][0][0]
            continue
        while rem:
            best, best_d, best_rev = 0, None, False
            for i, (pts, closed) in enumerate(rem):
                d = (pts[0][0] - pos[0]) ** 2 + (pts[0][1] - pos[1]) ** 2
                rev = False
                if not closed:
                    d2 = (pts[-1][0] - pos[0]) ** 2 + (pts[-1][1] - pos[1]) ** 2
                    if d2 < d:
                        d, rev = d2, True
                if best_d is None or d < best_d:
                    best, best_d, best_rev = i, d, rev
            pts, closed = rem.pop(best)
            if best_rev:
                pts = pts[::-1]
            out.append((pts, closed))
            pos = pts[0] if closed else pts[-1]
    return out


def close_ring(pts, overcut):
    """Closed shape -> explicit point list, optionally running past the start point."""
    ring = pts + [pts[0]]
    left = overcut
    j = 0
    while left > 1e-9 and j < len(pts):
        a, b = ring[j], ring[j + 1]
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg >= left:
            t = left / seg
            ring.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
        ring.append(b)
        left -= seg
        j += 1
    return ring


def cut_length(polys):
    total = 0.0
    for pts, closed in polys:
        seq = pts + [pts[0]] if closed else pts
        for a, b in zip(seq, seq[1:]):
            total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def build_hpgl(polys, offsets, job_w, job_h, cfg, base=(0.0, 0.0), park=None):
    """polys: one copy, already oriented. base: where the job's corner goes, in mm from
    the plotter's origin (across, along). park: where to leave the knife afterwards.
    Returns bytes ready to send."""
    swap, mc, mf = cfg["swap_axes"], cfg["mirror_carriage"], cfg["mirror_feed"]
    upm = cfg.get("steps_per_mm", UNITS_PER_MM)
    bx, by = base

    def dev(x, y):
        if mc:
            x = job_w - x
        if mf:
            y = job_h - y
        u, v = int(round((x + bx) * upm)), int(round((y + by) * upm))
        return (v, u) if swap else (u, v)

    ordered = order_polys(polys)
    out = [cfg["init"]]
    for ox, oy in offsets:
        for pts, closed in ordered:
            seq = close_ring(pts, cfg["overcut_mm"]) if closed else pts
            last = None
            steps = []
            for x, y in seq:
                p = dev(x + ox, y + oy)
                if p != last:
                    steps.append(p)
                    last = p
            if len(steps) < 2:
                continue
            out.append("PU%d,%d;" % steps[0])
            out.extend("PD%d,%d;" % p for p in steps[1:])
    if park is None:
        park = (0.0, by + job_h + cfg["advance_mm"]) if cfg["end_action"] == "advance" else (0.0, 0.0)
    u, v = int(round(park[0] * upm)), int(round(park[1] * upm))
    out.append("PU%d,%d;" % ((v, u) if swap else (u, v)))
    return "".join(out).encode("ascii")


def preview_path(polys, eps):
    parts = []
    for pts, closed in polys:
        kept = [pts[0]]
        for p in pts[1:-1]:
            if math.hypot(p[0] - kept[-1][0], p[1] - kept[-1][1]) >= eps:
                kept.append(p)
        kept.append(pts[-1])
        parts.append("M" + "L".join("%.2f %.2f" % p for p in kept) + ("Z" if closed else ""))
    return "".join(parts)


TEST_SIZE = 50.8   # the zombiePlot logo: a 2 inch square with "zP" inside

# The project logo, exported from VCarve. The P shows mirroring and rotation at a glance.
TEST_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="10in" height="5in" viewBox="0 0 10 5"><path d="M 4.080801,3.565224 L 6.080801,3.565224 L 6.080801,1.565224 L 4.080801,1.565224 L 4.080801,3.565224"/><path d="M 4.467823,3.195346 C 4.464412,3.195346 4.461426,3.194067 4.458868,3.191508 C 4.456309,3.188949 4.455029,3.185538 4.455029,3.181273 L 4.456309,3.153126 C 4.456309,3.130096 4.453750,3.107493 4.448632,3.085317 C 4.446926,3.072523 4.445220,3.060155 4.443515,3.048214 C 4.441809,3.036273 4.437117,3.026250 4.429441,3.018147 C 4.421764,3.010045 4.417926,3.000022 4.417926,2.988081 L 4.419206,2.957375 C 4.419206,2.938611 4.418353,2.921125 4.416647,2.904919 C 4.414941,2.892978 4.413662,2.879757 4.412809,2.865257 C 4.412809,2.845640 4.416860,2.827728 4.424963,2.811522 C 4.433066,2.795316 4.442235,2.779963 4.452470,2.765463 L 4.516441,2.666948 C 4.557382,2.604683 4.596405,2.543271 4.633508,2.482712 C 4.670611,2.422153 4.694706,2.374815 4.705795,2.340697 L 4.705795,2.331741 C 4.705795,2.320653 4.701530,2.310205 4.693000,2.300396 C 4.684471,2.290587 4.674662,2.285682 4.663574,2.285682 L 4.643103,2.285682 L 4.566338,2.283124 C 4.544162,2.283124 4.524971,2.287602 4.508765,2.296558 C 4.492559,2.305513 4.484243,2.324705 4.483816,2.354131 C 4.483390,2.383558 4.482537,2.406374 4.481257,2.422580 C 4.479978,2.438786 4.478272,2.450301 4.476140,2.457124 C 4.474007,2.463948 4.471875,2.468426 4.469743,2.470558 C 4.467610,2.472690 4.464838,2.473756 4.461426,2.473756 C 4.454603,2.473756 4.450125,2.470131 4.447992,2.462881 C 4.445860,2.455631 4.444794,2.446036 4.444794,2.434095 L 4.446073,2.394433 C 4.446073,2.370550 4.443088,2.344536 4.437117,2.316388 C 4.435412,2.309565 4.433492,2.296344 4.431360,2.276727 C 4.429228,2.257109 4.427309,2.235999 4.425603,2.213396 C 4.423897,2.190793 4.423044,2.172241 4.423044,2.157741 C 4.423897,2.127888 4.431360,2.108057 4.445434,2.098248 C 4.459507,2.088439 4.481471,2.083535 4.511324,2.083535 L 4.593206,2.086094 C 4.613677,2.085241 4.632441,2.084175 4.649500,2.082895 C 4.666559,2.081616 4.685324,2.080976 4.705795,2.080976 L 4.749295,2.080976 C 4.784265,2.080976 4.812412,2.080123 4.833736,2.078417 L 4.873398,2.077138 C 4.899839,2.077138 4.922442,2.083535 4.941207,2.096329 C 4.945472,2.099741 4.948244,2.103792 4.949523,2.108484 C 4.950803,2.113175 4.952722,2.116800 4.955280,2.119359 C 4.966369,2.131300 4.973832,2.144520 4.977670,2.159020 C 4.981508,2.173520 4.984281,2.188020 4.985986,2.202521 C 4.986839,2.212756 4.988119,2.222351 4.989825,2.231307 C 4.991531,2.240263 4.992383,2.249006 4.992383,2.257535 C 4.992383,2.273741 4.990678,2.289947 4.987266,2.306153 C 4.983001,2.329183 4.980869,2.352212 4.980869,2.375242 L 4.982148,2.434095 C 4.982148,2.448595 4.980869,2.462881 4.978310,2.476955 C 4.975751,2.491029 4.974472,2.505315 4.974472,2.519815 L 4.974472,2.535168 C 4.974472,2.538580 4.973406,2.541352 4.971273,2.543485 C 4.969141,2.545617 4.966369,2.546683 4.962957,2.546683 C 4.956133,2.546683 4.952722,2.541992 4.952722,2.532610 C 4.951016,2.507874 4.947178,2.483139 4.941207,2.458403 C 4.937795,2.438786 4.934810,2.418315 4.932251,2.396992 C 4.932251,2.393580 4.930972,2.391021 4.928413,2.389315 C 4.900266,2.419168 4.877236,2.452859 4.859324,2.490389 C 4.853354,2.502330 4.846530,2.512779 4.838854,2.521735 C 4.831177,2.530690 4.824354,2.539860 4.818383,2.549242 C 4.813265,2.561183 4.769339,2.634963 4.686603,2.770581 L 4.684045,2.774419 C 4.679780,2.780390 4.677647,2.785081 4.677647,2.788493 C 4.677647,2.792757 4.680206,2.796595 4.685324,2.800007 C 4.690442,2.803419 4.695986,2.805125 4.701956,2.805125 L 4.737780,2.803845 L 4.810707,2.805125 C 4.833736,2.805125 4.849089,2.804272 4.856766,2.802566 L 4.868280,2.801287 C 4.890457,2.801287 4.911994,2.806191 4.932891,2.816000 C 4.953788,2.825809 4.964236,2.843934 4.964236,2.870375 C 4.964236,2.880610 4.963597,2.890419 4.962317,2.899802 C 4.961038,2.909184 4.960398,2.918140 4.960398,2.926669 L 4.961678,2.954816 C 4.961678,2.974434 4.958692,2.997890 4.952722,3.025184 C 4.946751,3.052478 4.943766,3.078920 4.943766,3.104508 L 4.945045,3.128817 C 4.945045,3.143317 4.938648,3.151420 4.925854,3.153126 C 4.920736,3.153126 4.917111,3.149714 4.914979,3.142890 C 4.912847,3.136067 4.909648,3.120287 4.905383,3.095552 C 4.896001,3.064846 4.878942,3.040537 4.854207,3.022625 C 4.851648,3.020067 4.849089,3.018787 4.846530,3.018787 L 4.820942,3.021346 C 4.813265,3.021346 4.805376,3.020920 4.797273,3.020067 C 4.789170,3.019214 4.780854,3.018787 4.772324,3.018787 C 4.762089,3.018787 4.751640,3.019640 4.740978,3.021346 C 4.730317,3.023052 4.719442,3.023905 4.708353,3.023905 C 4.691295,3.023905 4.675942,3.023265 4.662294,3.021986 C 4.648647,3.020706 4.634574,3.019640 4.620074,3.018787 C 4.606427,3.018787 4.592993,3.019640 4.579772,3.021346 C 4.566552,3.023052 4.553544,3.023905 4.540750,3.023905 L 4.517721,3.022625 L 4.508765,3.021346 C 4.498529,3.021346 4.492985,3.034567 4.492132,3.061008 L 4.490853,3.094273 C 4.490853,3.119008 4.488294,3.145023 4.483176,3.172317 L 4.480618,3.181273 C 4.477206,3.190655 4.472941,3.195346 4.467823,3.195346"/><path d="M 5.048516,1.935102 L 5.375879,1.935102 C 5.464411,1.935102 5.532483,1.942051 5.580094,1.955948 C 5.627706,1.969846 5.663479,1.989920 5.687414,2.016171 C 5.711348,2.042421 5.727562,2.074205 5.736055,2.111523 C 5.744548,2.148840 5.748794,2.206617 5.748794,2.284855 L 5.748794,2.393718 C 5.748794,2.473500 5.740559,2.531664 5.724088,2.568209 C 5.707616,2.604754 5.677377,2.632806 5.633368,2.652366 C 5.589359,2.671925 5.531839,2.681705 5.460808,2.681705 L 5.373562,2.681705 L 5.373562,3.185102 L 5.048516,3.185102 L 5.048516,1.935102"/><path d="M 5.373562,2.148969 L 5.373562,2.467066 C 5.382827,2.467581 5.390806,2.467838 5.397497,2.467838 C 5.427351,2.467838 5.448068,2.460503 5.459650,2.445834 C 5.471231,2.431164 5.477021,2.400667 5.477021,2.354342 L 5.477021,2.251655 C 5.477021,2.208934 5.470330,2.181139 5.456947,2.168271 C 5.443565,2.155403 5.415770,2.148969 5.373562,2.148969"/></svg>')


def test_design():
    polys, w, h, _ = parse_svg(TEST_SVG.encode("ascii"))
    return polys, w, h


# ----------------------------------------------------------------------------
# Plotter connection: USB-to-parallel (usblp, /dev/usb/lp*) or USB-to-serial
# ----------------------------------------------------------------------------

class PortError(Exception):
    pass


class Cancelled(Exception):
    pass


BAUDS = {2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200} if termios else {}
CHUNK = 128
NO_ADAPTER = "No plotter cable found. Check that the plotter's USB cable is plugged into this box."
LPGETSTATUS = 0x060b
LP_PERRORP, LP_PSELECD, LP_POUTPA = 0x08, 0x10, 0x20
PARALLEL_PATTERNS = ("/dev/usb/lp*", "/dev/lp[0-9]*")
SERIAL_PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*")


class Port:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.fd = None
        self.sig = None

    def device(self):
        p = self.cfg.get("port")
        if p != "auto":
            return p if os.path.exists(p) else None
        link = self.cfg.get("link")
        if link == "parallel":
            patterns = PARALLEL_PATTERNS
        elif link == "serial":
            patterns = SERIAL_PATTERNS
        else:
            patterns = PARALLEL_PATTERNS + SERIAL_PATTERNS
        for pattern in patterns:
            found = sorted(glob.glob(pattern))
            if found:
                return found[0]
        return None

    def parallel(self, dev):
        link = self.cfg.get("link")
        if link in ("parallel", "serial"):
            return link == "parallel"
        real = os.path.realpath(dev)
        return real.startswith("/dev/usb/lp") or re.match(r"^/dev/lp\d+$", real) is not None

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.sig = None

    def _open(self):
        dev = self.device()
        if not dev:
            self._close()
            raise PortError(NO_ADAPTER)
        par = self.parallel(dev)
        baud, flow = self.cfg.get("baud"), self.cfg.get("flow")
        sig = (dev, par) if par else (dev, par, baud, flow)
        if self.fd is not None and self.sig == sig:
            return self.fd
        self._close()
        try:
            if par:
                fd = os.open(dev, os.O_WRONLY | os.O_NONBLOCK)
            else:
                fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            hint = ""
            if e.errno == errno.EACCES:
                hint = " (permission denied: run install.sh again, it fixes this)"
            raise PortError("Cannot open %s: %s%s" % (dev, e.strerror, hint))
        if not par:
            try:
                a = termios.tcgetattr(fd)
                a[0] = termios.IXON if flow == "xonxoff" else 0
                a[1] = 0
                a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                if flow == "hardware":
                    a[2] |= termios.CRTSCTS
                a[3] = 0
                a[4] = a[5] = BAUDS[baud]
                a[6][termios.VMIN] = 0
                a[6][termios.VTIME] = 0
                termios.tcsetattr(fd, termios.TCSANOW, a)
            except (termios.error, OSError) as e:
                os.close(fd)
                raise PortError("Cannot set up %s: %s" % (dev, e))
        self.fd, self.sig = fd, sig
        return fd

    def is_parallel(self):
        return bool(self.sig and self.sig[1])

    @staticmethod
    def _cts(fd):
        """True / False, or None when the device cannot report it."""
        try:
            raw = fcntl.ioctl(fd, termios.TIOCMGET, b"\0\0\0\0")
        except OSError as e:
            if e.errno in (errno.ENOTTY, errno.EINVAL):
                return None
            raise
        return bool(struct.unpack("I", raw)[0] & termios.TIOCM_CTS)

    @staticmethod
    def _lp_status(fd):
        """Parallel status byte, or None when the device cannot report it."""
        try:
            return struct.unpack("i", fcntl.ioctl(fd, LPGETSTATUS, b"\0\0\0\0"))[0]
        except OSError as e:
            if e.errno in (errno.ENOTTY, errno.EINVAL, errno.EIO):
                return None
            raise

    @staticmethod
    def _queued(fd):
        try:
            return struct.unpack("I", fcntl.ioctl(fd, termios.TIOCOUTQ, b"\0\0\0\0"))[0]
        except OSError as e:
            if e.errno in (errno.ENOTTY, errno.EINVAL):
                return 0
            raise

    def status(self):
        with self.lock:
            try:
                fd = self._open()
                state, msg = "connected", ""
                if self.is_parallel():
                    st = self._lp_status(fd)
                    if st is not None:
                        if st & LP_POUTPA:
                            state, msg = "notready", "The plotter reports no vinyl loaded."
                        elif not (st & LP_PSELECD) or not (st & LP_PERRORP):
                            state, msg = "notready", ("The plotter is not ready. Check it is switched on, "
                                                      "vinyl is loaded, and it is not in a menu.")
                        else:
                            state = "ready"
                elif self.cfg.get("flow") == "hardware":
                    cts = self._cts(fd)
                    if cts is False:
                        state, msg = "notready", ("The plotter is not signalling ready. Switch it on, load vinyl "
                                                  "and check the cable. If all of that is fine, set Flow control "
                                                  "to None in Settings.")
                    else:
                        state = "ready"
                return {"state": state, "device": self.sig[0], "message": msg,
                        "link": "parallel" if self.is_parallel() else "serial"}
            except PortError as e:
                return {"state": "missing", "device": None, "message": str(e), "link": None}
            except OSError as e:
                self._close()
                return {"state": "missing", "device": None, "link": None,
                        "message": "Plotter cable error: %s" % e.strerror}

    def send(self, data, stop, progress):
        """Send data at the pace the plotter accepts it. progress(bytes_out, waiting)."""
        with self.lock:
            self._open()
            par = self.is_parallel()
        if par:
            return self._send_parallel(data, stop, progress)
        return self._send_serial(data, stop, progress)

    def _send_parallel(self, data, stop, progress):
        # The usblp driver blocks writes while the plotter holds BUSY, so the
        # pace is set by the plotter. A non-blocking fd plus select keeps Stop responsive.
        total, sent = len(data), 0
        began = moved = time.monotonic()
        while True:
            if stop.is_set():
                raise Cancelled()
            with self.lock:
                fd = self.fd
            if fd is None:
                raise PortError("The plotter cable was disconnected.")
            n = 0
            try:
                _, w, _ = select.select([], [fd], [], 0.2)
                if w:
                    if sent >= total:
                        progress(total, False)
                        return
                    n = os.write(fd, data[sent:sent + CHUNK])
            except BlockingIOError:
                n = 0
            except OSError as e:
                with self.lock:
                    self._close()
                if e.errno == errno.ENODEV:
                    raise PortError("The plotter cable was unplugged.")
                raise PortError("Lost the plotter cable (%s)." % e.strerror)
            now = time.monotonic()
            if n:
                sent += n
                moved = now
            elif sent >= total and now - moved > 30:
                progress(total, False)          # last block never confirmed; it is in the driver
                return
            if sent == 0 and now - began > 15:
                raise PortError("The plotter is not accepting data. Check it is switched on, vinyl is "
                                "loaded, and its I/O setting is AUTO or PARA.")
            progress(sent, now - moved > 3)

    def _send_serial(self, data, stop, progress):
        total = len(data)
        sent = 0
        hardware = self.cfg.get("flow") == "hardware"
        began = moved = time.monotonic()
        while True:
            if stop.is_set():
                raise Cancelled()
            n = 0
            queued = 0
            held = False
            try:
                with self.lock:
                    fd = self.fd
                    if fd is None:
                        raise PortError("The plotter cable was disconnected.")
                    queued = self._queued(fd)
                    if hardware and self._cts(fd) is False:
                        held = True
                    elif sent < total and queued <= CHUNK:
                        n = os.write(fd, data[sent:sent + CHUNK])
            except BlockingIOError:
                n = 0
            except OSError as e:
                with self.lock:
                    self._close()
                raise PortError("Lost the plotter cable (%s)." % e.strerror)
            now = time.monotonic()
            if n:
                sent += n
                moved = now
            if sent >= total and queued == 0 and not n:
                progress(total, False)
                return
            if held and sent == 0 and now - began > 15:
                raise PortError("The plotter is not signalling ready. Check that it is switched on, "
                                "vinyl is loaded, and the serial cable is seated. If it is, set "
                                "Flow control to None in Settings.")
            progress(max(0, sent - queued), now - moved > 3)
            if not n:
                time.sleep(0.02)

    def abort(self):
        """Tell the cutter to drop everything it has buffered and lift the knife."""
        with self.lock:
            fd = self._open()
            try:
                if self.is_parallel():
                    try:
                        self._write_all(fd, b"\x1b.K;PU;", 3.0)
                    except OSError:
                        raise PortError("The plotter's buffer is full, so it could not take the stop command. "
                                        "Switch the plotter off to stop it.")
                    return
                termios.tcflush(fd, termios.TCOFLUSH)
                # Flow control off, so the abort gets through while the cutter is holding us off.
                a = termios.tcgetattr(fd)
                a[0] &= ~termios.IXON
                a[2] &= ~termios.CRTSCTS
                termios.tcsetattr(fd, termios.TCSANOW, a)
                self._write_all(fd, b"\x1b.K")
                time.sleep(0.3)
                self._write_all(fd, b";PU;")
                end = time.monotonic() + 1.0
                while self._queued(fd) and time.monotonic() < end:
                    time.sleep(0.02)
            except (OSError, termios.error) as e:
                raise PortError("Could not send the stop command (%s)." % e)
            finally:
                self._close()

    @staticmethod
    def _write_all(fd, data, limit=2.0):
        end = time.monotonic() + limit
        while data:
            if time.monotonic() > end:
                raise OSError(errno.ETIMEDOUT, "timed out")
            _, w, _ = select.select([], [fd], [], 0.2)
            if w:
                try:
                    data = data[os.write(fd, data):]
                except BlockingIOError:
                    pass


# ----------------------------------------------------------------------------
# Job runner
# ----------------------------------------------------------------------------

class Jobs:
    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state = "idle"      # idle | sending | stopping | done | stopped | error
        self.name = ""
        self.total = 0
        self.sent = 0
        self.waiting = False
        self.message = ""

    def busy(self):
        return self.state in ("sending", "stopping")

    def start(self, name, data):
        with self.lock:
            if self.busy():
                raise PortError("A job is already running.")
            self.stop.clear()
            self.state, self.name, self.total, self.sent = "sending", name, len(data), 0
            self.waiting, self.message = False, ""
        threading.Thread(target=self._run, args=(data,), daemon=True).start()

    def _progress(self, sent, waiting):
        self.sent, self.waiting = sent, waiting

    def _run(self, data):
        try:
            self.port.send(data, self.stop, self._progress)
            state, msg = "done", ""
        except Cancelled:
            try:
                self.port.abort()
                state, msg = "stopped", "Stopped."
            except PortError as e:
                state, msg = "error", str(e)
        except PortError as e:
            state, msg = "error", str(e)
        except Exception as e:             # never leave the state stuck on "sending"
            state, msg = "error", "Unexpected error: %s" % e
        with self.lock:
            self.state, self.message, self.waiting = state, msg, False

    def request_stop(self):
        with self.lock:
            if self.busy():
                self.state = "stopping"
                self.stop.set()
                return
        # Nothing being sent: the cutter may still be working through its own buffer.
        try:
            self.port.abort()
            state, msg = "stopped", "Stop sent to the plotter."
        except PortError as e:
            state, msg = "error", str(e)
        with self.lock:
            if not self.busy():
                self.state, self.message = state, msg

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "name": self.name, "total": self.total,
                    "sent": self.sent, "waiting": self.waiting, "message": self.message}


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------

class UserError(Exception):
    pass


class Session:
    """Areas cut on the vinyl loaded now, in mm from the plotter's origin (across, along)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = []
        self.group = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.items = [i for i in d.get("items", []) if all(k in i for k in ("x", "y", "w", "h"))]
            self.group = int(d.get("group", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items, "group": self.group}, f)
        os.replace(tmp, self.path)

    def rects(self):
        with self.lock:
            return [(i["x"], i["y"], i["w"], i["h"]) for i in self.items]

    def snapshot(self):
        with self.lock:
            items = [dict(i) for i in self.items]
        used = max([0.0] + [i["y"] + i["h"] for i in items])
        jobs = len({i["group"] for i in items})
        return {"items": items, "used_mm": used, "jobs": jobs}

    def add(self, rects, name):
        with self.lock:
            self.group += 1
            now = time.time()
            for r in rects:
                self.items.append({"x": r[0], "y": r[1], "w": r[2], "h": r[3], "name": name,
                                   "group": self.group, "t": now})
            self._save()

    def clear(self):
        with self.lock:
            self.items = []
            self._save()

    def undo(self):
        with self.lock:
            if self.items:
                last = self.items[-1]["group"]
                self.items = [i for i in self.items if i["group"] != last]
                self._save()


class App:
    def __init__(self, data_dir, library):
        self.data_dir = data_dir
        self.uploads = os.path.join(data_dir, "uploads")
        os.makedirs(self.uploads, exist_ok=True)
        self.library = library or os.path.join(data_dir, "files")
        if not library:
            os.makedirs(self.library, exist_ok=True)
        self.cfg = Config(os.path.join(data_dir, "config.json"))
        self.port = Port(self.cfg)
        self.jobs = Jobs(self.port)
        self.session = Session(os.path.join(data_dir, "session.json"))
        self.updates = UpdateState(data_dir)
        self.auth = Auth(self.cfg, data_dir)
        self.cache = {}
        self.cache_lock = threading.Lock()

    # ---- files ----

    def roots(self):
        return {"lib": self.library, "up": self.uploads}

    def list_files(self):
        files = []
        lib_ok, lib_msg = True, ""
        for key, root in self.roots().items():
            try:
                os.listdir(root)           # fails fast, with the real reason, if the share is offline
                base_depth = root.rstrip(os.sep).count(os.sep)
                for dirpath, dirnames, filenames in os.walk(root, onerror=None):
                    dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "#", "@"))]
                    if dirpath.count(os.sep) - base_depth >= 3:
                        dirnames[:] = []
                    for name in filenames:
                        if not name.lower().endswith(".svg") or name.startswith("."):
                            continue
                        full = os.path.join(dirpath, name)
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        rel = os.path.relpath(full, root)
                        folder = os.path.dirname(rel)
                        files.append({"id": key + ":" + rel.replace(os.sep, "/"), "name": name,
                                      "folder": "Sent from phone" if key == "up" else folder,
                                      "mtime": st.st_mtime, "size": st.st_size})
            except OSError as e:
                if key == "lib":
                    lib_ok, lib_msg = False, "Cannot read the file folder %s (%s)." % (root, e.strerror)
        files.sort(key=lambda f: -f["mtime"])
        return {"files": files[:400], "library": self.library, "library_ok": lib_ok,
                "library_message": lib_msg}

    def resolve(self, file_id):
        key, _, rel = (file_id or "").partition(":")
        root = self.roots().get(key)
        if not root or not rel:
            raise UserError("Unknown file.")
        root_real = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root_real, rel))
        if not full.startswith(root_real + os.sep) or not os.path.isfile(full):
            raise UserError("That file is no longer there. Go back and refresh the list.")
        return full

    def delete_file(self, file_id):
        if file_id == "test":
            raise UserError("The test cut is built in and can't be deleted.")
        if self.jobs.busy():
            raise UserError("Wait until the plotter has finished, then delete.")
        full = self.resolve(file_id)
        try:
            os.remove(full)
        except OSError as e:
            if e.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
                raise UserError("That file is in a folder this box can only read (such as a network folder). "
                                "Delete it on the computer that shares it.")
            raise UserError("Could not delete that file (%s)." % e.strerror)
        with self.cache_lock:
            for k in [k for k in self.cache if k[0] == full]:
                self.cache.pop(k, None)
        return os.path.basename(full)

    def load(self, file_id):
        """Parsed drawing for a file id: (polys, w, h, warnings, display name)."""
        if file_id == "test":
            polys, w, h = test_design()
            return polys, w, h, [], "Test square"
        full = self.resolve(file_id)
        ppi = self.cfg.get("px_per_inch")
        try:
            st = os.stat(full)
            key = (full, st.st_mtime_ns, st.st_size, ppi)
            with self.cache_lock:
                hit = self.cache.get(key)
            if hit is None:
                if st.st_size > MAX_UPLOAD:
                    raise UserError("That file is too large.")
                with open(full, "rb") as f:
                    raw = f.read()
                hit = parse_svg(raw, ppi)
                with self.cache_lock:
                    if len(self.cache) >= 6:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[key] = hit
        except OSError as e:
            raise UserError("Cannot read that file (%s)." % e.strerror)
        except SvgError as e:
            raise UserError(str(e))
        return hit[0], hit[1], hit[2], list(hit[3]), os.path.basename(full)

    def save_upload(self, name, body):
        base = os.path.basename((name or "").replace("\\", "/"))
        base = re.sub(r"[^A-Za-z0-9 ._()+-]", "_", base).strip(" .") or "upload"
        if not base.lower().endswith(".svg"):
            raise UserError("Only .svg files can be sent.")
        try:
            parse_svg(body, self.cfg.get("px_per_inch"))
        except SvgError as e:
            raise UserError(str(e))
        path = os.path.join(self.uploads, base)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        old = sorted((os.path.join(self.uploads, n) for n in os.listdir(self.uploads)
                      if n.lower().endswith(".svg")), key=os.path.getmtime, reverse=True)
        for p in old[KEEP_UPLOADS:]:
            try:
                os.remove(p)
            except OSError:
                pass
        return "up:" + base

    # ---- jobs ----

    def plan(self, q):
        """Shared by preview and cut. q: id, rot, mirror, copies, layout, place."""
        cfg = self.cfg.snapshot()
        polys, w, h, warnings, name = self.load(q.get("id"))
        try:
            copies = max(1, min(500, int(q.get("copies", 1))))
        except (TypeError, ValueError):
            copies = 1
        mirror = str(q.get("mirror", "0")).lower() in ("1", "true")
        layout = "stacked" if q.get("layout") == "stacked" else "across"
        place = "origin" if q.get("place") == "origin" else "auto"
        rot = q.get("rot", 0)
        cut_w = cfg["cut_width_mm"]
        vinyl_w = min(cfg["vinyl_width_mm"], cut_w)
        far = cfg["start_corner"] == "far"
        gap = cfg["copy_gap_mm"]
        note = ""
        if rot == "auto":
            rot = 0
            if w > vinyl_w + 0.01 and h <= vinyl_w + 0.01:
                rot = 90
                note = "Turned sideways so it fits the vinyl width."
        try:
            rot = int(rot) % 360
        except (TypeError, ValueError):
            rot = 0
        if rot not in (0, 90, 180, 270):
            rot = 0
        polys, w, h = orient(polys, rot, mirror)
        cols, rows, offsets, job_w, job_h = grid(w, h, copies, vinyl_w, gap, layout)
        fits = job_w <= vinyl_w + 0.01
        if not fits:
            limit = "vinyl width" if vinyl_w < cut_w - 0.01 else "cutting width"
            warnings.insert(0, "Too wide: %s across, the %s is %s. Rotate it, stack the copies, "
                               "or change the vinyl width."
                            % (fmt_len(job_w, cfg), limit, fmt_len(vinyl_w, cfg)))

        # Where on the vinyl. Work from the chosen edge, store from the plotter's origin.
        used = self.session.rects()
        used_c = [mirror_x(r, vinyl_w) for r in used] if far else used
        if place == "auto" and used:
            cx, cy = find_spot(used_c, job_w, job_h, vinyl_w, gap)
        else:
            cx, cy = 0.0, 0.0
        clash = [i for i, r in enumerate(used_c) if overlaps((cx, cy, job_w, job_h), r)]
        if clash:
            items = self.session.snapshot()["items"]
            names = sorted({re.sub(r"(?i)\.svg$", "", items[i]["name"]) for i in clash if i < len(items)})
            warnings.insert(0, "This spot was already cut on this vinyl (%s). Choose Next free spot, "
                               "or tap New vinyl if you loaded fresh vinyl or set a new start point."
                            % ", ".join(names))
        bx = vinyl_w - cx - job_w if far else cx
        by = cy
        mc = cfg["mirror_carriage"]
        rects = [(bx + (job_w - ox - w if mc else ox), by + oy, w, h) for ox, oy in offsets]
        park = None
        if cfg["end_action"] == "next":
            nxt = by + job_h + gap
            park = (vinyl_w if far else 0.0, nxt)
        return {"cfg": cfg, "polys": polys, "name": name, "w": w, "h": h, "rot": rot,
                "mirror": mirror, "copies": copies, "cols": cols, "rows": rows, "layout": layout,
                "offsets": offsets, "job_w": job_w, "job_h": job_h, "fits": fits,
                "warnings": warnings, "note": note, "place": place, "base": (bx, by),
                "rects": rects, "clash": bool(clash), "vinyl_w": vinyl_w, "far": far,
                "park": park}

    def preview(self, q):
        p = self.plan(q)
        eps = max(p["w"], p["h"]) / 900.0
        return {"name": p["name"], "w": p["w"], "h": p["h"], "rot": p["rot"], "mirror": p["mirror"],
                "copies": p["copies"], "cols": p["cols"], "rows": p["rows"], "layout": p["layout"],
                "offsets": p["offsets"], "job_w": p["job_w"], "job_h": p["job_h"],
                "fits": p["fits"], "warnings": p["warnings"], "note": p["note"],
                "cut_width": p["cfg"]["cut_width_mm"], "vinyl_width": p["vinyl_w"], "far": p["far"],
                "place": p["place"], "base": p["base"], "clash": p["clash"],
                "used": self.session.snapshot()["items"],
                "shapes": len(p["polys"]) * p["copies"],
                "cut_length": cut_length(p["polys"]) * p["copies"],
                "d": preview_path(p["polys"], eps)}

    def cut(self, q):
        if self.jobs.busy():
            raise UserError("A job is already running.")
        p = self.plan(q)
        if not p["fits"]:
            raise UserError(p["warnings"][0])
        st = self.port.status()
        if st["state"] == "missing":
            raise UserError(st["message"])
        data = build_hpgl(p["polys"], p["offsets"], p["job_w"], p["job_h"], p["cfg"],
                          base=p["base"], park=p["park"])
        try:
            self.jobs.start(p["name"], data)
        except PortError as e:
            raise UserError(str(e))
        self.session.add(p["rects"], p["name"])
        return {"bytes": len(data), "seconds": len(data) * 10.0 / p["cfg"]["baud"]}

    def update_info(self):
        st = self.updates.load()
        latest = st.get("latest")
        return {"installed": VERSION, "latest": latest,
                "available": bool(latest and parse_version(latest) > parse_version(VERSION)),
                "checked": st.get("checked"), "state": st.get("state", ""), "message": st.get("message", ""),
                "notes": st.get("notes", ""), "pending": os.path.exists(self.updates.req),
                "auto": bool(self.cfg.get("auto_update")), "previous": previous_version(),
                "repo": UPDATE_REPO}

    def update_check(self):
        try:
            rel = latest_release()
        except UpdateError as e:
            self.updates.save(checked=time.time())
            raise UserError(str(e))
        self.updates.save(checked=time.time(), latest=rel["version"], tag=rel["tag"], notes=rel["notes"])
        return self.update_info()

    def update_request(self, action):
        if self.jobs.busy():
            raise UserError("Wait until the plotter has finished, then try again.")
        if os.path.exists(self.updates.req):
            raise UserError("An update is already in progress.")
        info = self.update_info()
        if action == "install" and not info["available"]:
            raise UserError("Already up to date. Tap Check for updates first.")
        if action == "rollback" and not info["previous"]:
            raise UserError("There is no previous version to go back to.")
        with open(self.updates.req, "w", encoding="utf-8") as f:
            f.write(action)
        self.updates.save(state="requested", message="Starting.")
        return self.update_info()

    def status(self):
        ses = self.session.snapshot()
        up = self.updates.load().get("latest")
        return {"port": self.port.status(), "job": self.jobs.snapshot(), "version": VERSION,
                "update": up if up and parse_version(up) > parse_version(VERSION) else None,
                "vinyl": {"width_mm": min(self.cfg.get("vinyl_width_mm"), self.cfg.get("cut_width_mm")),
                          "used_mm": ses["used_mm"], "jobs": ses["jobs"],
                          "units": self.cfg.get("units"), "start_corner": self.cfg.get("start_corner")},
                "thing_sleep_min": self.cfg.get("thing_sleep_min")}


def fmt_len(mm, cfg):
    if cfg["units"] == "mm":
        return "%.1f mm" % mm
    return "%.2f in" % (mm / 25.4)


# ----------------------------------------------------------------------------
# Updates (from releases named in latest.json on the GitHub repository)
# ----------------------------------------------------------------------------

UPDATE_REPO = os.environ.get("ZOMBIEPLOT_REPO", "mikedero08/zombie-plot")
RAW_BASE = os.environ.get("ZOMBIEPLOT_RAW_BASE", "https://raw.githubusercontent.com")
PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))


class UpdateError(Exception):
    pass


def parse_version(v):
    parts = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in parts[:4]) or (0,)


def raw_url(ref, name):
    return "%s/%s/%s/%s" % (RAW_BASE.rstrip("/"), UPDATE_REPO, ref, name)


def fetch(url, limit=8 * 1024 * 1024, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "zombiePlot/" + VERSION,
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download too large")
    return data


def latest_release():
    try:
        info = json.loads(fetch(raw_url("main", "latest.json"), 64 * 1024).decode("utf-8"))
    except Exception as e:
        raise UpdateError("Could not reach GitHub to check for updates (%s)." % e)
    if not isinstance(info, dict):
        raise UpdateError("The update information on GitHub is not readable.")
    # "ref" is the release commit (preferred, it cannot move); "tag" is accepted too.
    ver, tag = str(info.get("version", "")), str(info.get("ref") or info.get("tag") or "")
    if not re.match(r"^\d+(\.\d+){0,3}$", ver) or not re.match(r"^[A-Za-z0-9._-]{1,64}$", tag):
        raise UpdateError("The update information on GitHub is not readable.")
    return {"version": ver, "tag": tag, "notes": str(info.get("notes", ""))[:2000]}


def previous_version():
    try:
        with open(os.path.join(PROGRAM_DIR, "previous", "zombieplot.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([0-9.]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


class UpdateState:
    """update-status.json and update-request in the data folder. The web page writes
    requests; the root helper (zombieplot-update.service) acts on them."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "update-status.json")
        self.req = os.path.join(data_dir, "update-request")
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, **changes):
        # The root helper writes here too, in a folder the service account owns. A fresh
        # randomly named file plus rename never follows a link planted in that folder.
        with self.lock:
            d = self.load()
            d.update(changes)
            fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".update-status.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(d, f)
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    st = os.stat(self.data_dir)
                    os.chown(tmp, st.st_uid, st.st_gid)
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            return d


def _tail(text, n=400):
    text = " ".join(text.strip().split())
    return text[-n:]


def _web_busy():
    port = os.environ.get("ZOMBIEPLOT_HTTP_PORT", "80")
    try:
        s = json.loads(fetch("http://127.0.0.1:%s/api/status" % port, timeout=5).decode("utf-8"))
        return s["job"]["state"] in ("sending", "stopping")
    except Exception:
        return False


def _run_installer(state, folder, done_message):
    r = subprocess.run(["bash", os.path.join(folder, "install.sh"), "--upgrade"], cwd=folder,
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise UpdateError("The installer stopped with an error: " + _tail(r.stdout + r.stderr))
    state.save(state="ok", message=done_message, finished=time.time())


def _install_latest(state):
    try:
        rel = latest_release()
    except UpdateError as e:
        state.save(state="failed", message=str(e))
        return 1
    state.save(checked=time.time(), latest=rel["version"], tag=rel["tag"], notes=rel["notes"])
    if parse_version(rel["version"]) <= parse_version(VERSION):
        state.save(state="current", message="Already up to date (version %s)." % VERSION)
        return 0
    state.save(state="installing", message="Downloading version %s." % rel["version"])
    tmp = tempfile.mkdtemp(prefix="zombieplot-update-")
    try:
        for name in ("zombieplot.py", "install.sh"):
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(fetch(raw_url(rel["tag"], name)))
        r = subprocess.run([sys.executable, os.path.join(tmp, "zombieplot.py"), "--selftest"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or r.stdout.strip() != rel["version"]:
            raise UpdateError("Version %s failed its self-test, so it was not installed. %s"
                              % (rel["version"], _tail(r.stderr or r.stdout, 300)))
        state.save(message="Installing version %s. This takes a few minutes on an older Pi." % rel["version"])
        _run_installer(state, tmp, "Updated from version %s to %s." % (VERSION, rel["version"]))
        return 0
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError) as e:
        state.save(state="failed", message=str(e))
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rollback(state):
    pv = previous_version()
    if not pv:
        state.save(state="failed", message="There is no previous version to go back to.")
        return 1
    state.save(state="installing", message="Going back to version %s." % pv)
    tmp = tempfile.mkdtemp(prefix="zombieplot-rollback-")
    try:
        for name in ("zombieplot.py", "install.sh"):
            shutil.copy2(os.path.join(PROGRAM_DIR, "previous", name), os.path.join(tmp, name))
        _run_installer(state, tmp, "Went back from version %s to %s." % (VERSION, pv))
        return 0
    except (UpdateError, OSError, subprocess.SubprocessError) as e:
        state.save(state="failed", message=str(e))
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_updater(data_dir, auto=False):
    """Runs as root from zombieplot-update.service (on request) or the nightly timer (auto)."""
    state = UpdateState(data_dir)
    action = ""
    try:
        with open(state.req, "r", encoding="utf-8") as f:
            action = f.read().strip()
    except OSError:
        pass
    try:
        os.remove(state.req)
    except OSError:
        pass
    if auto and not action:
        if not Config(os.path.join(data_dir, "config.json")).get("auto_update"):
            return 0
        if _web_busy():
            state.save(state="skipped", message="Skipped the overnight update because a job was running.")
            return 0
        action = "install"
    if action == "install":
        return _install_latest(state)
    if action == "rollback":
        return _rollback(state)
    return 0


def selftest():
    polys, w, h = test_design()
    data = build_hpgl(polys, [(0.0, 0.0)], w, h, dict(DEFAULTS))
    if not data.startswith(b"IN;PA;PU"):
        raise SystemExit("self-test: bad command output")
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="1in" viewBox="0 0 10 10">'
           b'<circle cx="5" cy="5" r="4"/></svg>')
    _, sw, sh, _ = parse_svg(svg)
    if abs(sw - 20.32) > 0.01 or abs(sh - 20.32) > 0.01:
        raise SystemExit("self-test: bad SVG size")
    if not PAGE.startswith("<!doctype html>"):
        raise SystemExit("self-test: page missing")
    if not THING.startswith("<!doctype html>") or "/thing" not in car_thing_launcher():
        raise SystemExit("self-test: Car Thing page missing")
    print(VERSION)


# ----------------------------------------------------------------------------
# Access PIN (optional)
# ----------------------------------------------------------------------------

PIN_ROUNDS = 20000
COOKIE = "zp_session"


def hash_pin(pin, salt=None, rounds=PIN_ROUNDS):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), rounds).hex()
    return "pbkdf2$%d$%s$%s" % (rounds, salt, h)


def check_pin(pin, stored):
    try:
        _, rounds, salt, _h = stored.split("$")
        return hmac.compare_digest(hash_pin(pin, salt, int(rounds)), stored)
    except (ValueError, TypeError):
        return False


class Auth:
    """Without a PIN the page is open to anyone on the network. With one, each browser
    unlocks once and keeps a long-lived session cookie."""

    def __init__(self, cfg, data_dir):
        self.cfg = cfg
        self.path = os.path.join(data_dir, "sessions.json")
        self.lock = threading.Lock()
        self.fails = 0
        self.locked_until = 0.0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.tokens = set(t for t in json.load(f) if isinstance(t, str))
        except (OSError, ValueError, TypeError):
            self.tokens = set()

    def enabled(self):
        return bool(self.cfg.get("pin_hash"))

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _save(self):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".sessions.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(self.tokens), f)
        os.replace(tmp, self.path)

    def ok(self, cookie_header):
        if not self.enabled():
            return True
        token = None
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                token = v
        with self.lock:
            return bool(token) and self._digest(token) in self.tokens

    def _new_token(self):
        token = secrets.token_urlsafe(32)
        self.tokens.add(self._digest(token))
        if len(self.tokens) > 200:
            self.tokens = set(sorted(self.tokens)[-200:])
        self._save()
        return token

    def login(self, pin):
        with self.lock:
            now = time.time()
            if now < self.locked_until:
                raise UserError("Too many wrong tries. Wait %d seconds." % int(self.locked_until - now + 1))
            if not check_pin(str(pin), self.cfg.get("pin_hash")):
                self.fails += 1
                if self.fails >= 5:
                    self.locked_until = now + min(900, 30 * 2 ** (self.fails - 5))
                raise UserError("Wrong PIN.")
            self.fails = 0
            return self._new_token()

    def set_pin(self, pin):
        pin = str(pin or "")
        if pin and not (4 <= len(pin) <= 64):
            raise UserError("Use 4 to 64 characters.")
        with self.lock:
            self.cfg.update({"pin_hash": hash_pin(pin) if pin else ""})
            self.tokens = set()
            token = self._new_token() if pin else None
            self._save()
            return token


def host_allowed(host):
    """Defends against DNS rebinding: a web page on the internet pointing its own domain
    at this box. Allows addresses, local names, and names listed in ZOMBIEPLOT_ALLOWED_HOSTS."""
    if not host:
        return True
    host = host.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host:
        return True
    if host.endswith((".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")):
        return True
    extra = [h.strip().lower() for h in os.environ.get("ZOMBIEPLOT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return host in extra or host == socket.gethostname().lower()


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "zombiePlot/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _fail(self, msg, code=400, **extra):
        self._json(dict({"ok": False, "error": msg}, **extra), code)

    def _json_cookie(self, obj, token):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                             % (COOKIE, token))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n < 0 or n > MAX_UPLOAD:
            self.close_connection = True
            raise UserError("That file is too large (limit %d MB)." % (MAX_UPLOAD // 1048576))
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        raw = self._body()
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise UserError("Bad request.")
        if not isinstance(obj, dict):
            raise UserError("Bad request.")
        return obj

    def _route(self, method):
        app = self.app
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if not host_allowed(self.headers.get("Host")):
                return self._fail("Blocked: this page was opened through an unexpected name. Use the box's "
                                  "IP address or .local name, or add the name to ZOMBIEPLOT_ALLOWED_HOSTS.", 403)
            if method == "POST" and self.headers.get("X-ZombiePlot") != "1":
                # Browsers cannot add this header on requests from other websites.
                return self._fail("Blocked: this request did not come from the zombiePlot page.", 403)
            public = path in ("/", "/index.html", "/thing", "/manifest.json", "/icon.svg", "/favicon.ico", "/api/login")
            local_status = path == "/api/status" and self.client_address[0] in ("127.0.0.1", "::1")
            if not (public or local_status or app.auth.ok(self.headers.get("Cookie"))):
                return self._fail("Enter the PIN to use this page.", 401, auth=True)
            if method == "GET":
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/thing":
                    return self._send(200, THING.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/manifest.json":
                    return self._send(200, MANIFEST.encode("utf-8"), "application/manifest+json")
                if path in ("/icon.svg", "/favicon.ico"):
                    return self._send(200, ICON.encode("utf-8"), "image/svg+xml")
                if path == "/api/status":
                    return self._json(app.status())
                if path == "/api/files":
                    return self._json(app.list_files())
                if path == "/api/preview":
                    return self._json(dict(app.preview(q), ok=True))
                if path == "/api/update":
                    return self._json(app.update_info())
                if path == "/api/settings":
                    snap = app.cfg.snapshot()
                    snap.pop("pin_hash", None)
                    return self._json({"settings": snap, "library": app.library, "pin_set": app.auth.enabled(),
                                       "device": app.port.device(), "version": VERSION})
            elif method == "POST":
                if path == "/api/upload":
                    return self._json({"ok": True, "id": app.save_upload(q.get("name"), self._body())})
                body = self._json_body()
                if path == "/api/login":
                    if not app.auth.enabled():
                        return self._json({"ok": True})
                    try:
                        token = app.auth.login(body.get("pin", ""))
                    except UserError as e:
                        return self._fail(str(e), 401 if "Wrong" in str(e) else 429)
                    return self._json_cookie({"ok": True}, token)
                if path == "/api/pin":
                    token = app.auth.set_pin(body.get("pin", ""))
                    return self._json_cookie({"ok": True, "pin_set": bool(token)}, token)
                if path == "/api/delete":
                    return self._json({"ok": True, "deleted": app.delete_file(body.get("id"))})
                if path == "/api/cut":
                    return self._json(dict(app.cut(body), ok=True))
                if path == "/api/update/check":
                    return self._json(dict(app.update_check(), ok=True))
                if path == "/api/update/install":
                    return self._json(dict(app.update_request("install"), ok=True))
                if path == "/api/update/rollback":
                    return self._json(dict(app.update_request("rollback"), ok=True))
                if path == "/api/session/new":
                    app.session.clear()
                    return self._json({"ok": True})
                if path == "/api/session/undo":
                    app.session.undo()
                    return self._json({"ok": True})
                if path == "/api/stop":
                    app.jobs.request_stop()
                    return self._json({"ok": True})
                if path == "/api/settings":
                    body.pop("pin_hash", None)
                    try:
                        saved = app.cfg.update(body)
                        saved.pop("pin_hash", None)
                        return self._json({"ok": True, "settings": saved})
                    except ValueError as e:
                        raise UserError(str(e))
            self._fail("Not found.", 404)
        except UserError as e:
            self._fail(str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write("error on %s %s: %r\n" % (method, self.path, e))
            try:
                self._fail("Internal error: %s" % e, 500)
            except OSError:
                pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


MANIFEST = json.dumps({
    "name": "zombiePlot", "short_name": "zombiePlot", "start_url": "/", "display": "standalone",
    "background_color": "#15181b", "theme_color": "#15181b",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
})

ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><rect width="96" height="96" rx="20" '
        'fill="#15181b"/><path d="M22 30h52M22 30v40h52V30M34 30v-8h28v8" fill="none" stroke="#86c35f" '
        'stroke-width="5" stroke-linejoin="round"/><path d="M34 58c8-14 20 6 28-8" fill="none" '
        'stroke="#e9edf0" stroke-width="5" stroke-linecap="round"/></svg>')

# The zombiePLOT wordmark ("zombie" in the button green, "PLOT" in the text colour) and the
# pixel zombie mascot, 16 x 18, two walking frames swapped by CSS. Used on both pages.
LOGO_SVG = '<svg class="logo" viewBox="1.46 1.82 6.09 1.36" role="img" aria-label="zombiePLOT"><path fill="var(--go)" fill-rule="evenodd" d="M 1.536952,2.821760 C 1.534000,2.821760 1.531417,2.820653 1.529203,2.818439 C 1.526989,2.816225 1.525882,2.813273 1.525882,2.809583 L 1.526989,2.785229 C 1.526989,2.765302 1.524775,2.745745 1.520347,2.726557 C 1.518871,2.715487 1.517395,2.704786 1.515919,2.694454 C 1.514443,2.684122 1.510384,2.675450 1.503742,2.668439 C 1.497100,2.661428 1.493779,2.652756 1.493779,2.642424 L 1.494886,2.615856 C 1.494886,2.599620 1.494148,2.584491 1.492672,2.570469 C 1.491196,2.560136 1.490089,2.548697 1.489351,2.536151 C 1.489351,2.519177 1.492856,2.503679 1.499868,2.489657 C 1.506879,2.475635 1.514812,2.462350 1.523668,2.449804 L 1.579019,2.364564 C 1.614443,2.310690 1.648207,2.257553 1.680310,2.205155 C 1.712414,2.152756 1.733262,2.111797 1.742856,2.082277 L 1.742856,2.074528 C 1.742856,2.064933 1.739166,2.055893 1.731786,2.047406 C 1.724406,2.038919 1.715919,2.034675 1.706325,2.034675 L 1.688613,2.034675 L 1.622192,2.032461 C 1.603004,2.032461 1.586399,2.036336 1.572377,2.044085 C 1.558355,2.051834 1.551159,2.068439 1.550790,2.093900 C 1.550421,2.119362 1.549683,2.139103 1.548576,2.153125 C 1.547469,2.167147 1.545993,2.177111 1.544148,2.183015 C 1.542303,2.188919 1.540458,2.192793 1.538613,2.194638 C 1.536768,2.196483 1.534369,2.197406 1.531417,2.197406 C 1.525513,2.197406 1.521639,2.194269 1.519794,2.187996 C 1.517949,2.181723 1.517026,2.173421 1.517026,2.163088 L 1.518133,2.128771 C 1.518133,2.108107 1.515550,2.085598 1.510384,2.061243 C 1.508908,2.055339 1.507248,2.043900 1.505403,2.026926 C 1.503558,2.009952 1.501897,1.991686 1.500421,1.972129 C 1.498945,1.952572 1.498207,1.936520 1.498207,1.923974 C 1.498945,1.898144 1.505403,1.880985 1.517580,1.872498 C 1.529757,1.864011 1.548761,1.859767 1.574591,1.859767 L 1.645439,1.861981 C 1.663152,1.861243 1.679388,1.860321 1.694148,1.859214 C 1.708908,1.858107 1.725144,1.857553 1.742856,1.857553 L 1.780495,1.857553 C 1.810753,1.857553 1.835107,1.856815 1.853558,1.855339 L 1.887875,1.854232 C 1.910753,1.854232 1.930310,1.859767 1.946546,1.870838 C 1.950237,1.873790 1.952635,1.877295 1.953742,1.881354 C 1.954849,1.885413 1.956510,1.888550 1.958724,1.890764 C 1.968318,1.901096 1.974775,1.912535 1.978096,1.925081 C 1.981417,1.937627 1.983816,1.950173 1.985292,1.962719 C 1.986030,1.971576 1.987137,1.979878 1.988613,1.987627 C 1.990089,1.995376 1.990827,2.002941 1.990827,2.010321 C 1.990827,2.024343 1.989351,2.038365 1.986399,2.052387 C 1.982709,2.072314 1.980864,2.092240 1.980864,2.112166 L 1.981971,2.163088 C 1.981971,2.175635 1.980864,2.187996 1.978650,2.200173 C 1.976436,2.212350 1.975329,2.224712 1.975329,2.237258 L 1.975329,2.250542 C 1.975329,2.253494 1.974406,2.255893 1.972561,2.257738 C 1.970716,2.259583 1.968318,2.260505 1.965366,2.260505 C 1.959462,2.260505 1.956510,2.256446 1.956510,2.248328 C 1.955034,2.226926 1.951713,2.205524 1.946546,2.184122 C 1.943594,2.167147 1.941011,2.149435 1.938797,2.130985 C 1.938797,2.128033 1.937690,2.125819 1.935476,2.124343 C 1.911122,2.150173 1.891196,2.179325 1.875698,2.211797 C 1.870532,2.222129 1.864628,2.231170 1.857986,2.238919 C 1.851344,2.246668 1.845439,2.254601 1.840273,2.262719 C 1.835845,2.273052 1.797838,2.336889 1.726251,2.454232 L 1.724037,2.457553 C 1.720347,2.462719 1.718502,2.466778 1.718502,2.469731 C 1.718502,2.473421 1.720716,2.476742 1.725144,2.479694 C 1.729572,2.482646 1.734369,2.484122 1.739535,2.484122 L 1.770532,2.483015 L 1.833631,2.484122 C 1.853558,2.484122 1.866842,2.483384 1.873484,2.481908 L 1.883447,2.480801 C 1.902635,2.480801 1.921270,2.485044 1.939351,2.493531 C 1.957432,2.502018 1.966473,2.517701 1.966473,2.540579 C 1.966473,2.549435 1.965919,2.557922 1.964812,2.566040 C 1.963705,2.574159 1.963152,2.581908 1.963152,2.589288 L 1.964259,2.613642 C 1.964259,2.630616 1.961676,2.650911 1.956510,2.674528 C 1.951344,2.698144 1.948761,2.721022 1.948761,2.743162 L 1.949868,2.764195 C 1.949868,2.776742 1.944332,2.783753 1.933262,2.785229 C 1.928834,2.785229 1.925698,2.782277 1.923853,2.776373 C 1.922008,2.770469 1.919240,2.756815 1.915550,2.735413 C 1.907432,2.708845 1.892672,2.687812 1.871270,2.672314 C 1.869056,2.670100 1.866842,2.668992 1.864628,2.668992 L 1.842487,2.671207 C 1.835845,2.671207 1.829019,2.670838 1.822008,2.670100 C 1.814997,2.669362 1.807801,2.668992 1.800421,2.668992 C 1.791565,2.668992 1.782524,2.669731 1.773299,2.671207 C 1.764074,2.672683 1.754665,2.673421 1.745070,2.673421 C 1.730310,2.673421 1.717026,2.672867 1.705218,2.671760 C 1.693410,2.670653 1.681233,2.669731 1.668687,2.668992 C 1.656879,2.668992 1.645255,2.669731 1.633816,2.671207 C 1.622377,2.672683 1.611122,2.673421 1.600052,2.673421 L 1.580126,2.672314 L 1.572377,2.671207 C 1.563521,2.671207 1.558724,2.682646 1.557986,2.705524 L 1.556879,2.734306 C 1.556879,2.755708 1.554665,2.778218 1.550237,2.801834 L 1.548022,2.809583 C 1.545070,2.817701 1.541380,2.821760 1.536952,2.821760 M 2.504480,2.903679 C 2.491934,2.902203 2.485661,2.873052 2.485661,2.816225 L 2.486768,2.736520 C 2.486768,2.706262 2.485107,2.679878 2.481786,2.657369 C 2.478465,2.634860 2.470163,2.623605 2.456879,2.623605 C 2.450975,2.623605 2.443594,2.627111 2.434738,2.634122 C 2.425882,2.641133 2.413521,2.647221 2.397653,2.652387 C 2.381786,2.657553 2.354665,2.660136 2.316288,2.660136 C 2.268318,2.656446 2.234369,2.649066 2.214443,2.637996 C 2.203373,2.631354 2.194517,2.628033 2.187875,2.628033 C 2.179019,2.628033 2.173115,2.637258 2.170163,2.655708 L 2.165735,2.682277 C 2.159831,2.703679 2.156879,2.725450 2.156879,2.747590 L 2.157986,2.756446 C 2.155772,2.771207 2.150237,2.778587 2.141380,2.778587 C 2.135476,2.778587 2.129757,2.774897 2.124222,2.767516 C 2.118687,2.760136 2.115919,2.748697 2.115919,2.733199 L 2.117026,2.715487 C 2.117026,2.659398 2.107063,2.587074 2.087137,2.498513 L 2.076067,2.449804 C 2.070901,2.423236 2.067580,2.404417 2.066104,2.393347 C 2.064628,2.382277 2.063521,2.375819 2.062783,2.373974 C 2.062045,2.372129 2.060938,2.371207 2.059462,2.371207 C 2.043225,2.371207 2.035107,2.359398 2.035107,2.335782 C 2.035107,2.318070 2.038244,2.295930 2.044517,2.269362 C 2.050790,2.242793 2.058170,2.213826 2.066657,2.182461 C 2.075144,2.151096 2.083447,2.118808 2.091565,2.085598 C 2.093779,2.078218 2.098576,2.065856 2.105956,2.048513 C 2.113336,2.031170 2.122008,2.012904 2.131971,1.993716 C 2.141934,1.974528 2.152451,1.958660 2.163521,1.946114 C 2.198207,1.907738 2.230126,1.884860 2.259277,1.877480 C 2.288428,1.870100 2.316288,1.866409 2.342856,1.866409 L 2.381602,1.866409 C 2.400790,1.866409 2.423668,1.874897 2.450237,1.891871 C 2.476805,1.908845 2.497100,1.926188 2.511122,1.943900 C 2.536952,1.974897 2.558355,2.018254 2.575329,2.073974 C 2.592303,2.129694 2.600790,2.192978 2.600790,2.263826 C 2.600790,2.321391 2.593410,2.382646 2.578650,2.447590 C 2.577912,2.452756 2.577543,2.460690 2.577543,2.471391 C 2.577543,2.482092 2.577174,2.492978 2.576436,2.504048 C 2.575698,2.515118 2.574037,2.524712 2.571454,2.532830 C 2.568871,2.540948 2.564259,2.545007 2.557617,2.545007 C 2.554665,2.545007 2.552082,2.544454 2.549868,2.543347 C 2.547653,2.542240 2.545070,2.541317 2.542118,2.540579 C 2.540642,2.540579 2.539535,2.541133 2.538797,2.542240 C 2.538059,2.543347 2.537690,2.544454 2.537690,2.545561 C 2.537690,2.546668 2.537321,2.547590 2.536583,2.548328 C 2.528465,2.577849 2.524406,2.602203 2.524406,2.621391 C 2.523668,2.660505 2.522561,2.697221 2.521085,2.731539 C 2.519609,2.765856 2.518871,2.799620 2.518871,2.832830 L 2.519978,2.880432 C 2.519978,2.896668 2.515550,2.904786 2.506694,2.904786 L 2.504480,2.903679 M 2.317395,2.489657 C 2.333631,2.489657 2.350606,2.480063 2.368318,2.460874 C 2.400052,2.426188 2.415919,2.349435 2.415919,2.230616 C 2.415919,2.173790 2.410015,2.125819 2.398207,2.086705 C 2.395255,2.080063 2.386030,2.071945 2.370532,2.062350 C 2.363890,2.058660 2.357986,2.056815 2.352820,2.056815 C 2.329941,2.063457 2.316104,2.076557 2.311307,2.096114 C 2.306510,2.115671 2.304111,2.135044 2.304111,2.154232 L 2.304111,2.175266 C 2.304111,2.183384 2.303742,2.190764 2.303004,2.197406 C 2.301528,2.207000 2.297100,2.211797 2.289720,2.211797 C 2.282340,2.211797 2.277543,2.208107 2.275329,2.200727 C 2.273115,2.193347 2.272008,2.185598 2.272008,2.177480 C 2.271270,2.164195 2.268687,2.152387 2.264259,2.142055 L 2.257617,2.198513 C 2.256141,2.206631 2.255403,2.214749 2.255403,2.222867 C 2.255403,2.230985 2.255772,2.240764 2.256510,2.252203 C 2.257248,2.263642 2.257617,2.277480 2.257617,2.293716 L 2.262045,2.340210 C 2.262045,2.347590 2.260938,2.354232 2.258724,2.360136 C 2.256510,2.366040 2.255403,2.371207 2.255403,2.375635 C 2.255403,2.383753 2.258355,2.391133 2.264259,2.397775 C 2.267211,2.402941 2.270347,2.409214 2.273668,2.416594 C 2.276989,2.423974 2.278650,2.433937 2.278650,2.446483 C 2.278650,2.459029 2.282709,2.469362 2.290827,2.477480 C 2.298945,2.485598 2.307801,2.489657 2.317395,2.489657 M 2.670532,2.789657 C 2.667580,2.789657 2.664812,2.788550 2.662229,2.786336 C 2.659646,2.784122 2.658355,2.780801 2.658355,2.776373 C 2.652451,2.707738 2.649499,2.658660 2.649499,2.629140 C 2.649499,2.601834 2.652451,2.581170 2.658355,2.567147 C 2.664997,2.549435 2.668318,2.521760 2.668318,2.484122 C 2.668318,2.476742 2.669240,2.467332 2.671085,2.455893 C 2.672930,2.444454 2.673853,2.434306 2.673853,2.425450 C 2.673853,2.411428 2.670163,2.404417 2.662783,2.404417 C 2.650975,2.404417 2.645070,2.397775 2.645070,2.384491 C 2.649499,2.323974 2.659462,2.264564 2.674960,2.206262 L 2.686030,2.164195 C 2.688244,2.153863 2.689351,2.146483 2.689351,2.142055 C 2.687875,2.139103 2.686399,2.136520 2.684923,2.134306 C 2.683447,2.132092 2.682709,2.129140 2.682709,2.125450 C 2.682709,2.121760 2.683262,2.117332 2.684369,2.112166 C 2.685476,2.107000 2.686030,2.100358 2.686030,2.092240 C 2.686030,2.081908 2.683816,2.069731 2.679388,2.055708 C 2.674960,2.041686 2.672746,2.029509 2.672746,2.019177 C 2.672746,2.009583 2.673484,1.999620 2.674960,1.989288 C 2.677912,1.976004 2.679388,1.951280 2.679388,1.915118 C 2.679388,1.885598 2.693041,1.870838 2.720347,1.870838 C 2.740273,1.870838 2.753927,1.870100 2.761307,1.868623 C 2.768687,1.867147 2.777543,1.866040 2.787875,1.865302 C 2.796731,1.865302 2.805772,1.866225 2.814997,1.868070 C 2.824222,1.869915 2.834000,1.870838 2.844332,1.870838 C 2.849499,1.870838 2.852082,1.873790 2.852082,1.879694 C 2.852820,1.879694 2.856325,1.888181 2.862598,1.905155 C 2.868871,1.922129 2.873853,1.939103 2.877543,1.956077 C 2.879757,1.969362 2.883262,1.983384 2.888059,1.998144 C 2.892856,2.012904 2.896731,2.028771 2.899683,2.045745 C 2.901897,2.065671 2.906694,2.084491 2.914074,2.102203 C 2.917026,2.109583 2.918871,2.117147 2.919609,2.124897 C 2.920347,2.132646 2.922008,2.139657 2.924591,2.145930 C 2.927174,2.152203 2.932524,2.155708 2.940642,2.156446 C 2.942856,2.156446 2.944517,2.154970 2.945624,2.152018 C 2.946731,2.149066 2.948022,2.145007 2.949499,2.139841 C 2.952451,2.127295 2.962414,2.094823 2.979388,2.042424 C 2.980864,2.037996 2.981602,2.033937 2.981602,2.030247 C 2.981602,2.024343 2.980495,2.019362 2.978281,2.015302 C 2.976067,2.011243 2.974960,2.007000 2.974960,2.002572 C 2.974960,1.996668 2.977912,1.989288 2.983816,1.980432 C 2.994886,1.959029 3.002266,1.936889 3.005956,1.914011 C 3.006694,1.908107 3.007801,1.902572 3.009277,1.897406 C 3.018871,1.870100 3.035845,1.856446 3.060200,1.856446 C 3.068318,1.857184 3.076251,1.858107 3.084000,1.859214 C 3.091749,1.860321 3.102635,1.860874 3.116657,1.860874 C 3.129203,1.860874 3.139166,1.860136 3.146546,1.858660 C 3.164259,1.858660 3.176620,1.867332 3.183631,1.884675 C 3.190642,1.902018 3.194148,1.921391 3.194148,1.942793 C 3.194148,1.959767 3.193225,1.977111 3.191380,1.994823 C 3.189535,2.012535 3.188613,2.029509 3.188613,2.045745 C 3.188613,2.061981 3.190089,2.076004 3.193041,2.087812 C 3.193041,2.092240 3.192303,2.096299 3.190827,2.099989 C 3.189351,2.103679 3.188613,2.107738 3.188613,2.112166 C 3.191565,2.135782 3.193041,2.164564 3.193041,2.198513 C 3.195255,2.231723 3.198945,2.260136 3.204111,2.283753 C 3.208539,2.302203 3.210753,2.317701 3.210753,2.330247 C 3.210753,2.340579 3.208908,2.348697 3.205218,2.354601 C 3.201528,2.360505 3.199683,2.366409 3.199683,2.372314 L 3.200790,2.386705 C 3.203742,2.395561 3.205218,2.415487 3.205218,2.446483 C 3.205218,2.470838 3.206510,2.489841 3.209093,2.503494 C 3.211676,2.517147 3.214997,2.539841 3.219056,2.571576 C 3.223115,2.603310 3.225144,2.630985 3.225144,2.654601 C 3.225144,2.664933 3.224406,2.673421 3.222930,2.680063 C 3.219978,2.695561 3.217949,2.707922 3.216842,2.717147 C 3.215735,2.726373 3.214443,2.732830 3.212967,2.736520 C 3.211491,2.740210 3.208539,2.742055 3.204111,2.742055 C 3.199683,2.742055 3.196731,2.740948 3.195255,2.738734 C 3.193779,2.736520 3.193041,2.732461 3.193041,2.726557 L 3.194148,2.708845 C 3.194148,2.662350 3.183447,2.639103 3.162045,2.639103 L 3.142118,2.640210 C 3.128096,2.640210 3.116842,2.639841 3.108355,2.639103 C 3.099868,2.638365 3.093041,2.637996 3.087875,2.637996 C 3.079019,2.637996 3.072377,2.639472 3.067949,2.642424 C 3.063521,2.645376 3.060200,2.650911 3.057986,2.659029 C 3.049868,2.686336 3.045808,2.714011 3.045808,2.742055 C 3.045808,2.755339 3.045439,2.762719 3.044701,2.764195 L 3.044701,2.766409 C 3.043963,2.767885 3.043410,2.769731 3.043041,2.771945 C 3.042672,2.774159 3.041934,2.776557 3.040827,2.779140 C 3.039720,2.781723 3.036952,2.783015 3.032524,2.783015 C 3.025144,2.782277 3.020716,2.777295 3.019240,2.768070 C 3.017764,2.758845 3.017026,2.745745 3.017026,2.728771 L 3.017026,2.717701 C 3.017026,2.697037 3.015550,2.672314 3.012598,2.643531 L 3.012598,2.631354 C 3.012598,2.615118 3.015919,2.599251 3.022561,2.583753 C 3.024775,2.579325 3.025882,2.576373 3.025882,2.574897 C 3.021454,2.561612 3.018871,2.541317 3.018133,2.514011 C 3.017395,2.486705 3.017026,2.457922 3.017026,2.427664 C 3.017026,2.349435 3.013705,2.307000 3.007063,2.300358 C 2.996731,2.315856 2.987321,2.332461 2.978834,2.350173 C 2.970347,2.367885 2.963152,2.381354 2.957248,2.390579 C 2.951344,2.399804 2.946915,2.410690 2.943963,2.423236 C 2.941749,2.429878 2.938059,2.433199 2.932893,2.433199 C 2.924775,2.433199 2.919240,2.425819 2.916288,2.411059 C 2.916288,2.401465 2.912229,2.387074 2.904111,2.367885 C 2.887875,2.329509 2.876805,2.309583 2.870901,2.308107 C 2.864997,2.325819 2.862045,2.339841 2.862045,2.350173 L 2.863152,2.362350 C 2.863152,2.384491 2.860938,2.408107 2.856510,2.433199 C 2.854296,2.446483 2.853004,2.459952 2.852635,2.473605 C 2.852266,2.487258 2.850606,2.501649 2.847653,2.516778 C 2.844701,2.531908 2.842118,2.547406 2.839904,2.563273 C 2.837690,2.579140 2.834738,2.594823 2.831048,2.610321 C 2.826620,2.633937 2.823853,2.652572 2.822746,2.666225 C 2.821639,2.679878 2.819978,2.689841 2.817764,2.696114 C 2.815550,2.702387 2.811122,2.705893 2.804480,2.706631 C 2.800790,2.706631 2.798207,2.705708 2.796731,2.703863 C 2.795255,2.702018 2.794148,2.700173 2.793410,2.698328 C 2.792672,2.696483 2.791934,2.695192 2.791196,2.694454 C 2.788982,2.689288 2.787875,2.683015 2.787875,2.675635 L 2.787875,2.671207 C 2.787875,2.664564 2.786030,2.657922 2.782340,2.651280 C 2.778650,2.644638 2.764997,2.640210 2.741380,2.637996 C 2.712598,2.637996 2.696362,2.653494 2.692672,2.684491 C 2.690458,2.698513 2.689351,2.712904 2.689351,2.727664 C 2.689351,2.760874 2.686030,2.780432 2.679388,2.786336 C 2.676436,2.788550 2.673484,2.789657 2.670532,2.789657 M 3.662414,2.830616 C 3.655034,2.830616 3.650606,2.826557 3.649130,2.818439 L 3.646915,2.790764 L 3.648022,2.774159 C 3.648022,2.760136 3.639535,2.728402 3.622561,2.678956 C 3.620347,2.673790 3.619240,2.668254 3.619240,2.662350 C 3.617764,2.650542 3.612598,2.644638 3.603742,2.644638 L 3.576067,2.646852 C 3.555403,2.646852 3.533078,2.643900 3.509093,2.637996 C 3.485107,2.632092 3.469425,2.624712 3.462045,2.615856 C 3.436214,2.632830 3.418871,2.641317 3.410015,2.641317 C 3.405587,2.641317 3.401528,2.640210 3.397838,2.637996 C 3.394148,2.635782 3.386768,2.634675 3.375698,2.634675 C 3.365366,2.634675 3.357801,2.636151 3.353004,2.639103 C 3.348207,2.642055 3.345808,2.648328 3.345808,2.657922 L 3.346915,2.676742 C 3.346915,2.687074 3.345439,2.697406 3.342487,2.707738 C 3.341749,2.712166 3.341011,2.716409 3.340273,2.720469 C 3.339535,2.724528 3.337875,2.727480 3.335292,2.729325 C 3.332709,2.731170 3.329572,2.732092 3.325882,2.732092 C 3.320716,2.732092 3.316288,2.728402 3.312598,2.721022 C 3.308170,2.700358 3.305218,2.684491 3.303742,2.673421 C 3.302266,2.662350 3.300421,2.652941 3.298207,2.645192 C 3.295993,2.637443 3.291934,2.628587 3.286030,2.618623 C 3.280126,2.608660 3.277174,2.598144 3.277174,2.587074 C 3.277174,2.577480 3.278281,2.567885 3.280495,2.558291 C 3.284185,2.540579 3.286952,2.521760 3.288797,2.501834 C 3.290642,2.481908 3.291749,2.463457 3.292118,2.446483 C 3.292487,2.429509 3.293410,2.417701 3.294886,2.411059 L 3.295993,2.401096 C 3.295993,2.395192 3.295255,2.388919 3.293779,2.382277 C 3.292303,2.375635 3.291565,2.368254 3.291565,2.360136 C 3.291565,2.357922 3.290089,2.355708 3.287137,2.353494 C 3.275329,2.346114 3.269425,2.337996 3.269425,2.329140 C 3.269425,2.323236 3.270901,2.315671 3.273853,2.306446 C 3.276805,2.297221 3.279757,2.284491 3.282709,2.268254 C 3.285661,2.252018 3.287137,2.230616 3.287137,2.204048 C 3.287137,2.190026 3.286214,2.174528 3.284369,2.157553 C 3.282524,2.140579 3.281602,2.120653 3.281602,2.097775 L 3.282709,2.036889 C 3.282709,2.019915 3.281417,2.006077 3.278834,1.995376 C 3.276251,1.984675 3.274960,1.975266 3.274960,1.967147 L 3.273853,1.927295 C 3.273853,1.910321 3.275882,1.895930 3.279941,1.884122 C 3.284000,1.872314 3.293041,1.866409 3.307063,1.866409 L 3.319240,1.867516 C 3.327358,1.868992 3.344701,1.869731 3.371270,1.869731 L 3.433262,1.869731 C 3.448761,1.870469 3.458724,1.876373 3.463152,1.887443 C 3.465366,1.892609 3.467949,1.895192 3.470901,1.895192 C 3.475329,1.895192 3.481233,1.891871 3.488613,1.885229 C 3.510015,1.870469 3.541749,1.863088 3.583816,1.863088 C 3.602266,1.863088 3.620347,1.866409 3.638059,1.873052 C 3.655772,1.879694 3.677174,1.891133 3.702266,1.907369 C 3.715550,1.915487 3.731048,1.932461 3.748761,1.958291 C 3.775329,1.996668 3.788613,2.040579 3.788613,2.090026 C 3.788613,2.126188 3.782709,2.157922 3.770901,2.185229 C 3.759093,2.212535 3.746546,2.230616 3.733262,2.239472 L 3.755403,2.262719 C 3.771639,2.278218 3.786399,2.295561 3.799683,2.314749 C 3.815181,2.339841 3.822930,2.375635 3.822930,2.422129 C 3.822930,2.454601 3.815550,2.488919 3.800790,2.525081 C 3.795624,2.539841 3.788797,2.552018 3.780310,2.561612 C 3.771823,2.571207 3.760200,2.582646 3.745439,2.595930 L 3.704480,2.634675 C 3.697838,2.642055 3.693041,2.654786 3.690089,2.672867 C 3.687137,2.690948 3.685661,2.708107 3.685661,2.724343 C 3.685661,2.732461 3.685845,2.738181 3.686214,2.741502 C 3.686583,2.744823 3.686952,2.747959 3.687321,2.750911 C 3.687690,2.753863 3.687875,2.758291 3.687875,2.764195 C 3.687875,2.772314 3.685845,2.780432 3.681786,2.788550 C 3.677727,2.796668 3.675329,2.806262 3.674591,2.817332 C 3.673115,2.826188 3.669056,2.830616 3.662414,2.830616 M 3.524037,2.489657 C 3.574960,2.489657 3.601159,2.466040 3.602635,2.418808 C 3.602635,2.403310 3.598207,2.391133 3.589351,2.382277 C 3.580495,2.373421 3.567580,2.366040 3.550606,2.360136 C 3.546177,2.358660 3.542118,2.357922 3.538428,2.357922 C 3.528096,2.357922 3.521639,2.363088 3.519056,2.373421 C 3.516473,2.383753 3.514997,2.395007 3.514628,2.407184 C 3.514259,2.419362 3.512967,2.430063 3.510753,2.439288 C 3.508539,2.448513 3.503004,2.453125 3.494148,2.453125 C 3.487506,2.453125 3.483078,2.451649 3.480864,2.448697 C 3.478650,2.445745 3.477543,2.441317 3.477543,2.435413 C 3.477543,2.431723 3.477912,2.426742 3.478650,2.420469 C 3.479388,2.414195 3.479757,2.407369 3.479757,2.399989 C 3.479757,2.390395 3.477543,2.380432 3.473115,2.370100 L 3.473115,2.373421 C 3.473115,2.382277 3.472561,2.389841 3.471454,2.396114 C 3.470347,2.402387 3.469794,2.410321 3.469794,2.419915 C 3.469794,2.428771 3.470532,2.437996 3.472008,2.447590 L 3.474222,2.461981 C 3.474222,2.464933 3.473668,2.467885 3.472561,2.470838 C 3.471454,2.473790 3.470901,2.476742 3.470901,2.479694 C 3.472377,2.485598 3.476805,2.488550 3.484185,2.488550 L 3.524037,2.489657 M 3.497469,2.169731 C 3.518133,2.168992 3.535476,2.160136 3.549499,2.143162 C 3.555403,2.135044 3.563152,2.128402 3.572746,2.123236 C 3.575698,2.121022 3.577174,2.117701 3.577174,2.113273 C 3.577174,2.105155 3.575144,2.096114 3.571085,2.086151 C 3.567026,2.076188 3.563521,2.068992 3.560569,2.064564 C 3.560569,2.080801 3.559277,2.092793 3.556694,2.100542 C 3.554111,2.108291 3.549130,2.112166 3.541749,2.112166 C 3.532155,2.112166 3.527358,2.104786 3.527358,2.090026 L 3.527358,2.082277 C 3.527358,2.061612 3.523853,2.047221 3.516842,2.039103 C 3.509831,2.030985 3.502266,2.026926 3.494148,2.026926 C 3.481602,2.026926 3.475329,2.034675 3.475329,2.050173 L 3.476436,2.067885 L 3.475329,2.094454 C 3.475329,2.102572 3.475882,2.110690 3.476989,2.118808 C 3.478096,2.126926 3.478650,2.136520 3.478650,2.147590 L 3.478650,2.160874 C 3.478650,2.168254 3.480864,2.171945 3.485292,2.171945 L 3.497469,2.169731 M 3.910384,2.816225 C 3.905956,2.816225 3.901713,2.812719 3.897653,2.805708 C 3.893594,2.798697 3.891565,2.790764 3.891565,2.781908 L 3.891565,2.767516 C 3.890827,2.760874 3.889351,2.747221 3.887137,2.726557 C 3.884923,2.705893 3.882524,2.684122 3.879941,2.661243 C 3.877358,2.638365 3.876067,2.622498 3.876067,2.613642 L 3.878281,2.569362 C 3.880495,2.531723 3.881602,2.493347 3.881602,2.454232 C 3.881602,2.435044 3.881233,2.421022 3.880495,2.412166 L 3.880495,2.405524 C 3.880495,2.400358 3.880864,2.395561 3.881602,2.391133 C 3.882340,2.386705 3.882709,2.381908 3.882709,2.376742 L 3.882709,2.352387 C 3.881233,2.325081 3.880495,2.290764 3.880495,2.249435 L 3.880495,2.144269 C 3.880495,2.136889 3.878281,2.131170 3.873853,2.127111 C 3.869425,2.123052 3.867211,2.116225 3.867211,2.106631 L 3.868318,2.088919 C 3.872746,2.055708 3.874960,2.029509 3.874960,2.010321 C 3.874960,1.988919 3.873853,1.973421 3.871639,1.963826 L 3.870532,1.945007 C 3.870532,1.938365 3.872746,1.929325 3.877174,1.917885 C 3.881602,1.906446 3.888059,1.896299 3.896546,1.887443 C 3.905034,1.878587 3.915550,1.874159 3.928096,1.874159 C 3.935476,1.874159 3.944517,1.875450 3.955218,1.878033 C 3.965919,1.880616 3.978281,1.881908 3.992303,1.881908 C 4.001897,1.881908 4.009093,1.881354 4.013890,1.880247 C 4.018687,1.879140 4.024406,1.878218 4.031048,1.877480 C 4.044332,1.877480 4.053927,1.883937 4.059831,1.896852 C 4.065735,1.909767 4.068687,1.923236 4.068687,1.937258 C 4.068687,1.944638 4.067764,1.951280 4.065919,1.957184 C 4.064074,1.963088 4.063152,1.976742 4.063152,1.998144 C 4.063152,2.023236 4.064074,2.053863 4.065919,2.090026 C 4.067764,2.126188 4.068687,2.157184 4.068687,2.183015 C 4.068687,2.202203 4.068133,2.215118 4.067026,2.221760 C 4.065919,2.228402 4.065366,2.234306 4.065366,2.239472 C 4.065366,2.257922 4.069056,2.276373 4.076436,2.294823 C 4.080864,2.311059 4.083447,2.327664 4.084185,2.344638 L 4.089720,2.388919 C 4.089720,2.399251 4.084554,2.405893 4.074222,2.408845 C 4.067580,2.411059 4.064259,2.414749 4.064259,2.419915 L 4.065366,2.437627 C 4.065366,2.449435 4.064628,2.460874 4.063152,2.471945 C 4.061676,2.483015 4.060938,2.494085 4.060938,2.505155 C 4.062414,2.530247 4.065735,2.550173 4.070901,2.564933 C 4.074591,2.576742 4.076436,2.587443 4.076436,2.597037 C 4.076436,2.606631 4.073115,2.615118 4.066473,2.622498 C 4.064997,2.624712 4.064259,2.627295 4.064259,2.630247 L 4.064259,2.647959 C 4.064259,2.678956 4.058355,2.716963 4.046546,2.761981 C 4.044332,2.768623 4.041011,2.771945 4.036583,2.771945 C 4.033631,2.771945 4.030126,2.769915 4.026067,2.765856 C 4.022008,2.761797 4.019240,2.753863 4.017764,2.742055 L 4.014443,2.709952 C 4.012229,2.682646 4.010200,2.668254 4.008355,2.666778 C 4.006510,2.665302 4.001897,2.664564 3.994517,2.664564 L 3.949130,2.666778 C 3.944701,2.666778 3.941749,2.668992 3.940273,2.673421 L 3.933631,2.696668 C 3.926251,2.713642 3.922561,2.730616 3.922561,2.747590 L 3.923668,2.759767 C 3.923668,2.780432 3.922746,2.795007 3.920901,2.803494 C 3.919056,2.811981 3.915550,2.816225 3.910384,2.816225 M 4.223668,2.923605 C 4.209646,2.923605 4.202635,2.909214 4.202635,2.880432 L 4.203742,2.862719 C 4.195624,2.822867 4.191565,2.790026 4.191565,2.764195 L 4.191565,2.757553 C 4.191565,2.739841 4.190273,2.724159 4.187690,2.710505 C 4.185107,2.696852 4.179019,2.684491 4.169425,2.673421 C 4.165735,2.670469 4.163336,2.667516 4.162229,2.664564 C 4.161122,2.661612 4.159646,2.657000 4.157801,2.650727 C 4.155956,2.644454 4.149499,2.626188 4.138428,2.595930 C 4.135476,2.590026 4.134000,2.583753 4.134000,2.577111 C 4.134000,2.573421 4.135107,2.569731 4.137321,2.566040 C 4.142487,2.557184 4.145993,2.547590 4.147838,2.537258 C 4.149683,2.526926 4.150975,2.513642 4.151713,2.497406 C 4.152451,2.475266 4.155403,2.444269 4.160569,2.404417 C 4.160569,2.391871 4.159831,2.382092 4.158355,2.375081 C 4.156879,2.368070 4.156141,2.361243 4.156141,2.354601 L 4.157248,2.300358 C 4.158724,2.269362 4.159831,2.237443 4.160569,2.204601 C 4.161307,2.171760 4.162045,2.149435 4.162783,2.137627 C 4.162783,2.128771 4.161860,2.120100 4.160015,2.111612 C 4.158170,2.103125 4.157248,2.094823 4.157248,2.086705 L 4.158355,2.080063 C 4.158355,2.075635 4.158724,2.072683 4.159462,2.071207 C 4.158724,2.067516 4.156141,2.065671 4.151713,2.065671 L 4.147284,2.065671 C 4.139904,2.065671 4.136214,2.059767 4.136214,2.047959 C 4.136214,2.035413 4.138797,2.014749 4.143963,1.985967 C 4.146915,1.971207 4.148391,1.960136 4.148391,1.952756 L 4.148391,1.947221 C 4.148391,1.937627 4.150052,1.928033 4.153373,1.918439 C 4.156694,1.908845 4.165366,1.900358 4.179388,1.892978 C 4.199314,1.884122 4.218871,1.879694 4.238059,1.879694 C 4.249868,1.879694 4.262783,1.880432 4.276805,1.881908 C 4.296731,1.884860 4.325144,1.886336 4.362045,1.886336 C 4.379757,1.886336 4.396546,1.885782 4.412414,1.884675 C 4.428281,1.883568 4.455034,1.883015 4.492672,1.883015 C 4.502266,1.883015 4.511307,1.881723 4.519794,1.879140 C 4.528281,1.876557 4.544332,1.875266 4.567949,1.875266 C 4.582709,1.875266 4.601528,1.876373 4.624406,1.878587 C 4.641380,1.881539 4.651528,1.890948 4.654849,1.906815 C 4.658170,1.922683 4.659831,1.939103 4.659831,1.956077 L 4.659831,1.970469 C 4.659831,1.984491 4.658355,1.997221 4.655403,2.008660 C 4.652451,2.020100 4.649130,2.032092 4.645439,2.044638 C 4.634369,2.071945 4.624775,2.115487 4.616657,2.175266 C 4.615919,2.183384 4.614074,2.191133 4.611122,2.198513 C 4.608170,2.205893 4.602266,2.209583 4.593410,2.209583 C 4.580864,2.208845 4.574591,2.195561 4.574591,2.169731 C 4.574591,2.157184 4.575329,2.144085 4.576805,2.130432 C 4.578281,2.116778 4.579019,2.105155 4.579019,2.095561 C 4.579019,2.077111 4.575698,2.067885 4.569056,2.067885 C 4.566104,2.068623 4.563336,2.069362 4.560753,2.070100 C 4.558170,2.070838 4.554296,2.071207 4.549130,2.071207 C 4.542487,2.071207 4.536030,2.070469 4.529757,2.068992 C 4.523484,2.067516 4.517395,2.066778 4.511491,2.066778 L 4.474960,2.066778 C 4.461676,2.066778 4.448761,2.068254 4.436214,2.071207 C 4.433262,2.071945 4.429941,2.072314 4.426251,2.072314 C 4.418871,2.072314 4.411122,2.071207 4.403004,2.068992 C 4.394886,2.066778 4.386768,2.065302 4.378650,2.064564 C 4.365366,2.064564 4.358724,2.070100 4.358724,2.081170 L 4.358724,2.094454 C 4.356510,2.101096 4.355403,2.108107 4.355403,2.115487 C 4.355403,2.125081 4.356325,2.134306 4.358170,2.143162 C 4.360015,2.152018 4.361307,2.160505 4.362045,2.168623 C 4.362783,2.173790 4.364259,2.176373 4.366473,2.176373 C 4.368687,2.176373 4.371270,2.175635 4.374222,2.174159 C 4.380126,2.171207 4.388613,2.169731 4.399683,2.169731 C 4.404111,2.169731 4.409093,2.170653 4.414628,2.172498 C 4.420163,2.174343 4.428465,2.175266 4.439535,2.175266 L 4.468318,2.174159 C 4.480864,2.174159 4.491934,2.176742 4.501528,2.181908 C 4.511122,2.187074 4.515919,2.198882 4.515919,2.217332 L 4.515919,2.260505 C 4.515919,2.283384 4.511491,2.304786 4.502635,2.324712 C 4.496731,2.342424 4.493779,2.361612 4.493779,2.382277 L 4.493779,2.391133 C 4.493779,2.397037 4.493225,2.402203 4.492118,2.406631 C 4.491011,2.411059 4.486768,2.413273 4.479388,2.413273 C 4.467580,2.413273 4.461676,2.405524 4.461676,2.390026 C 4.461676,2.383384 4.462229,2.377480 4.463336,2.372314 C 4.464443,2.367147 4.464997,2.361981 4.464997,2.356815 C 4.464997,2.352387 4.461676,2.350173 4.455034,2.350173 C 4.435845,2.350173 4.422377,2.351096 4.414628,2.352941 C 4.406879,2.354786 4.400790,2.356631 4.396362,2.358476 C 4.391934,2.360321 4.387506,2.361243 4.383078,2.361243 C 4.379388,2.361243 4.376067,2.360690 4.373115,2.359583 C 4.370163,2.358476 4.366842,2.357553 4.363152,2.356815 C 4.359462,2.356815 4.357617,2.358660 4.357617,2.362350 L 4.358724,2.371207 C 4.360938,2.375635 4.362045,2.381539 4.362045,2.388919 C 4.362045,2.397037 4.361491,2.404601 4.360384,2.411612 C 4.359277,2.418623 4.358724,2.425081 4.358724,2.430985 C 4.358724,2.442055 4.362783,2.451096 4.370901,2.458107 C 4.379019,2.465118 4.397838,2.468623 4.427358,2.468623 C 4.437690,2.468623 4.447284,2.468254 4.456141,2.467516 C 4.464997,2.466778 4.476067,2.466040 4.489351,2.465302 C 4.507063,2.465302 4.529572,2.466963 4.556879,2.470284 C 4.584185,2.473605 4.603742,2.478587 4.615550,2.485229 C 4.631048,2.494085 4.642118,2.506446 4.648761,2.522314 C 4.655403,2.538181 4.658724,2.562719 4.658724,2.595930 C 4.658724,2.632830 4.655403,2.684860 4.648761,2.752018 C 4.646546,2.768992 4.642118,2.781354 4.635476,2.789103 C 4.628834,2.796852 4.621823,2.800727 4.614443,2.800727 C 4.607801,2.800727 4.602082,2.798144 4.597284,2.792978 C 4.592487,2.787812 4.590089,2.780801 4.590089,2.771945 C 4.590089,2.766778 4.591011,2.760874 4.592856,2.754232 C 4.594701,2.747590 4.595624,2.740948 4.595624,2.734306 C 4.595624,2.720284 4.593041,2.706631 4.587875,2.693347 C 4.586399,2.687443 4.585107,2.682092 4.584000,2.677295 C 4.582893,2.672498 4.580864,2.669546 4.577912,2.668439 C 4.574960,2.667332 4.570716,2.666778 4.565181,2.666778 C 4.559646,2.666778 4.554665,2.666409 4.550237,2.665671 C 4.535476,2.664195 4.520716,2.663457 4.505956,2.663457 C 4.497100,2.663457 4.490273,2.663826 4.485476,2.664564 C 4.480679,2.665302 4.475329,2.665671 4.469425,2.665671 C 4.463521,2.665671 4.457432,2.665118 4.451159,2.664011 C 4.444886,2.662904 4.438059,2.662350 4.430679,2.662350 C 4.409277,2.662350 4.390089,2.660505 4.373115,2.656815 C 4.359093,2.653863 4.346177,2.652387 4.334369,2.652387 C 4.320347,2.652387 4.307986,2.656631 4.297284,2.665118 C 4.286583,2.673605 4.277358,2.689657 4.269609,2.713273 C 4.261860,2.736889 4.255403,2.771207 4.250237,2.816225 L 4.249130,2.846114 L 4.249130,2.853863 C 4.249130,2.868623 4.247284,2.883384 4.243594,2.898144 C 4.236952,2.915118 4.230310,2.923605 4.223668,2.923605"/><path fill="currentColor" fill-rule="evenodd" d="M 4.723717,1.875000 L 5.051080,1.875000 C 5.139612,1.875000 5.207684,1.881949 5.255295,1.895846 C 5.302907,1.909744 5.338680,1.929818 5.362615,1.956069 C 5.386549,1.982319 5.402763,2.014103 5.411256,2.051421 C 5.419749,2.088738 5.423995,2.146515 5.423995,2.224753 L 5.423995,2.333616 C 5.423995,2.413398 5.415760,2.471562 5.399289,2.508107 C 5.382818,2.544652 5.352578,2.572704 5.308569,2.592264 C 5.264560,2.611823 5.207040,2.621603 5.136009,2.621603 L 5.048764,2.621603 L 5.048764,3.125000 L 4.723717,3.125000 L 4.723717,1.875000 M 5.048764,2.088867 L 5.048764,2.406964 C 5.058029,2.407479 5.066007,2.407736 5.072698,2.407736 C 5.102552,2.407736 5.123269,2.400401 5.134851,2.385732 C 5.146432,2.371062 5.152223,2.340565 5.152223,2.294240 L 5.152223,2.191553 C 5.152223,2.148832 5.145531,2.121037 5.132148,2.108169 C 5.118766,2.095301 5.090971,2.088867 5.048764,2.088867 M 5.805436,1.875000 L 5.805436,2.874846 L 6.003089,2.874846 L 6.003089,3.125000 L 5.480390,3.125000 L 5.480390,1.875000 L 5.805436,1.875000 M 6.782136,2.606933 C 6.782136,2.732525 6.779176,2.821443 6.773257,2.873687 C 6.767338,2.925932 6.748808,2.973672 6.717667,3.016909 C 6.686526,3.060145 6.644448,3.093345 6.591432,3.116507 C 6.538415,3.139670 6.476649,3.151251 6.406132,3.151251 C 6.339218,3.151251 6.279125,3.140313 6.225851,3.118437 C 6.172577,3.096562 6.129727,3.063748 6.097300,3.019997 C 6.064872,2.976246 6.045570,2.928634 6.039393,2.877162 C 6.033217,2.825690 6.030128,2.735614 6.030128,2.606933 L 6.030128,2.393067 C 6.030128,2.267475 6.033088,2.178557 6.039007,2.126313 C 6.044927,2.074068 6.063457,2.026328 6.094597,1.983091 C 6.125738,1.939855 6.167816,1.906655 6.220833,1.883493 C 6.273849,1.860330 6.335615,1.848749 6.406132,1.848749 C 6.473046,1.848749 6.533140,1.859687 6.586413,1.881563 C 6.639687,1.903438 6.682537,1.936252 6.714965,1.980003 C 6.747392,2.023754 6.766694,2.071366 6.772871,2.122838 C 6.779048,2.174310 6.782136,2.264386 6.782136,2.393067 L 6.782136,2.606933 M 6.457090,2.196186 C 6.457090,2.138022 6.453873,2.100834 6.447439,2.084620 C 6.441005,2.068406 6.427750,2.060300 6.407676,2.060300 C 6.390691,2.060300 6.377694,2.066862 6.368686,2.079988 C 6.359679,2.093113 6.355175,2.131846 6.355175,2.196186 L 6.355175,2.779880 C 6.355175,2.852455 6.358134,2.897236 6.364054,2.914222 C 6.369973,2.931208 6.383742,2.939700 6.405360,2.939700 C 6.427493,2.939700 6.441648,2.929921 6.447825,2.910361 C 6.454001,2.890802 6.457090,2.844220 6.457090,2.770615 L 6.457090,2.196186 M 7.521051,1.875000 L 7.521051,2.125154 L 7.328030,2.125154 L 7.328030,3.125000 L 7.002984,3.125000 L 7.002984,2.125154 L 6.810736,2.125154 L 6.810736,1.875000 L 7.521051,1.875000"/></svg>'
ZOMBIE_SVG = '<svg class="zombie" viewBox="0 0 16 18" shape-rendering="crispEdges" aria-hidden="true"><g class="fa"><rect x="4" y="0" width="5" height="1" fill="#3b2f27"/><rect x="3" y="1" width="8" height="1" fill="#3b2f27"/><rect x="3" y="2" width="2" height="1" fill="#3b2f27"/><rect x="5" y="2" width="6" height="1" fill="#86c35f"/><rect x="11" y="2" width="1" height="1" fill="#5c9440"/><rect x="3" y="3" width="1" height="1" fill="#3b2f27"/><rect x="4" y="3" width="3" height="1" fill="#86c35f"/><rect x="7" y="3" width="1" height="1" fill="#f2f2e8"/><rect x="8" y="3" width="1" height="1" fill="#141816"/><rect x="9" y="3" width="2" height="1" fill="#86c35f"/><rect x="11" y="3" width="1" height="1" fill="#5c9440"/><rect x="3" y="4" width="1" height="1" fill="#5c9440"/><rect x="4" y="4" width="7" height="1" fill="#86c35f"/><rect x="11" y="4" width="1" height="1" fill="#5c9440"/><rect x="3" y="5" width="1" height="1" fill="#5c9440"/><rect x="4" y="5" width="4" height="1" fill="#86c35f"/><rect x="8" y="5" width="2" height="1" fill="#a8352c"/><rect x="10" y="5" width="2" height="1" fill="#86c35f"/><rect x="4" y="6" width="1" height="1" fill="#5c9440"/><rect x="5" y="6" width="2" height="1" fill="#86c35f"/><rect x="7" y="6" width="2" height="1" fill="#a8352c"/><rect x="9" y="6" width="2" height="1" fill="#86c35f"/><rect x="5" y="7" width="1" height="1" fill="#5c9440"/><rect x="6" y="7" width="3" height="1" fill="#86c35f"/><rect x="4" y="8" width="4" height="1" fill="#6f86a6"/><rect x="8" y="8" width="7" height="1" fill="#86c35f"/><rect x="3" y="9" width="1" height="1" fill="#6f86a6"/><rect x="4" y="9" width="1" height="1" fill="#4f6380"/><rect x="5" y="9" width="3" height="1" fill="#6f86a6"/><rect x="8" y="9" width="1" height="1" fill="#5c9440"/><rect x="9" y="9" width="5" height="1" fill="#86c35f"/><rect x="14" y="9" width="1" height="1" fill="#5c9440"/><rect x="3" y="10" width="1" height="1" fill="#6f86a6"/><rect x="4" y="10" width="1" height="1" fill="#4f6380"/><rect x="5" y="10" width="4" height="1" fill="#6f86a6"/><rect x="3" y="11" width="3" height="1" fill="#6f86a6"/><rect x="6" y="11" width="1" height="1" fill="#4f6380"/><rect x="7" y="11" width="2" height="1" fill="#6f86a6"/><rect x="3" y="12" width="1" height="1" fill="#6f86a6"/><rect x="4" y="12" width="1" height="1" fill="#4f6380"/><rect x="5" y="12" width="2" height="1" fill="#6f86a6"/><rect x="7" y="12" width="1" height="1" fill="#4f6380"/><rect x="8" y="12" width="1" height="1" fill="#6f86a6"/><rect x="3" y="13" width="6" height="1" fill="#5a4636"/><rect x="3" y="14" width="2" height="1" fill="#5a4636"/><rect x="7" y="14" width="3" height="1" fill="#5a4636"/><rect x="2" y="15" width="2" height="1" fill="#5a4636"/><rect x="4" y="15" width="1" height="1" fill="#3d2f24"/><rect x="8" y="15" width="2" height="1" fill="#5a4636"/><rect x="2" y="16" width="1" height="1" fill="#5a4636"/><rect x="3" y="16" width="1" height="1" fill="#3d2f24"/><rect x="9" y="16" width="2" height="1" fill="#5a4636"/><rect x="1" y="17" width="3" height="1" fill="#22262a"/><rect x="9" y="17" width="4" height="1" fill="#22262a"/></g><g class="fb"><rect x="4" y="0" width="5" height="1" fill="#3b2f27"/><rect x="3" y="1" width="8" height="1" fill="#3b2f27"/><rect x="3" y="2" width="2" height="1" fill="#3b2f27"/><rect x="5" y="2" width="6" height="1" fill="#86c35f"/><rect x="11" y="2" width="1" height="1" fill="#5c9440"/><rect x="3" y="3" width="1" height="1" fill="#3b2f27"/><rect x="4" y="3" width="3" height="1" fill="#86c35f"/><rect x="7" y="3" width="1" height="1" fill="#f2f2e8"/><rect x="8" y="3" width="1" height="1" fill="#141816"/><rect x="9" y="3" width="2" height="1" fill="#86c35f"/><rect x="11" y="3" width="1" height="1" fill="#5c9440"/><rect x="3" y="4" width="1" height="1" fill="#5c9440"/><rect x="4" y="4" width="7" height="1" fill="#86c35f"/><rect x="11" y="4" width="1" height="1" fill="#5c9440"/><rect x="3" y="5" width="1" height="1" fill="#5c9440"/><rect x="4" y="5" width="4" height="1" fill="#86c35f"/><rect x="8" y="5" width="2" height="1" fill="#a8352c"/><rect x="10" y="5" width="2" height="1" fill="#86c35f"/><rect x="4" y="6" width="1" height="1" fill="#5c9440"/><rect x="5" y="6" width="2" height="1" fill="#86c35f"/><rect x="7" y="6" width="2" height="1" fill="#a8352c"/><rect x="9" y="6" width="2" height="1" fill="#86c35f"/><rect x="5" y="7" width="1" height="1" fill="#5c9440"/><rect x="6" y="7" width="3" height="1" fill="#86c35f"/><rect x="4" y="8" width="4" height="1" fill="#6f86a6"/><rect x="3" y="9" width="1" height="1" fill="#6f86a6"/><rect x="4" y="9" width="1" height="1" fill="#4f6380"/><rect x="5" y="9" width="4" height="1" fill="#6f86a6"/><rect x="9" y="9" width="7" height="1" fill="#86c35f"/><rect x="3" y="10" width="1" height="1" fill="#6f86a6"/><rect x="4" y="10" width="1" height="1" fill="#4f6380"/><rect x="5" y="10" width="3" height="1" fill="#6f86a6"/><rect x="8" y="10" width="1" height="1" fill="#5c9440"/><rect x="9" y="10" width="5" height="1" fill="#86c35f"/><rect x="14" y="10" width="1" height="1" fill="#5c9440"/><rect x="3" y="11" width="3" height="1" fill="#6f86a6"/><rect x="6" y="11" width="1" height="1" fill="#4f6380"/><rect x="7" y="11" width="2" height="1" fill="#6f86a6"/><rect x="3" y="12" width="1" height="1" fill="#6f86a6"/><rect x="4" y="12" width="1" height="1" fill="#4f6380"/><rect x="5" y="12" width="2" height="1" fill="#6f86a6"/><rect x="7" y="12" width="1" height="1" fill="#4f6380"/><rect x="8" y="12" width="1" height="1" fill="#6f86a6"/><rect x="3" y="13" width="6" height="1" fill="#5a4636"/><rect x="4" y="14" width="2" height="1" fill="#5a4636"/><rect x="7" y="14" width="2" height="1" fill="#5a4636"/><rect x="4" y="15" width="2" height="1" fill="#5a4636"/><rect x="8" y="15" width="2" height="1" fill="#5a4636"/><rect x="4" y="16" width="1" height="1" fill="#3d2f24"/><rect x="5" y="16" width="1" height="1" fill="#5a4636"/><rect x="8" y="16" width="1" height="1" fill="#5a4636"/><rect x="9" y="16" width="1" height="1" fill="#3d2f24"/><rect x="3" y="17" width="3" height="1" fill="#22262a"/><rect x="9" y="17" width="4" height="1" fill="#22262a"/></g></svg>'
MASCOT_CSS = """
.logo{height:24px;width:auto;display:block}
.zombie{height:30px;width:auto;display:block}
.zombie .fa{animation:zfa .9s steps(1) infinite}
.zombie .fb{visibility:hidden;animation:zfb .9s steps(1) infinite}
@keyframes zfa{0%{visibility:visible}50%{visibility:hidden}}
@keyframes zfb{0%{visibility:hidden}50%{visibility:visible}}
.brand{display:flex;align-items:flex-end;min-width:0}
.brand .zombie{margin-left:7px;margin-bottom:-3px}
@media (prefers-reduced-motion: reduce){.zombie .fa,.zombie .fb{animation:none}}
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#15181b">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg">
<title>zombiePlot</title>
<style>
:root{--bg:#15181b;--panel:#1e2327;--line:#30373d;--text:#e9edf0;--dim:#97a2ab;--go:#86c35f;--go-text:#10200a;
--ok:#4fbf7b;--warn:#f2a33a;--bad:#ef5d52;--vinyl:#262c31}
@media (prefers-color-scheme:light){:root{--bg:#f3f4f2;--panel:#fff;--line:#d5d9dc;--text:#181c1f;--dim:#5d6870;
--go:#4f8f33;--go-text:#fff;--ok:#1f8a4c;--warn:#a85f00;--bad:#c5362c;--vinyl:#e8ebec}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body{max-width:640px;margin:0 auto;padding:0 14px calc(20px + env(safe-area-inset-bottom));-webkit-tap-highlight-color:transparent}
header{display:flex;align-items:center;gap:10px;padding:14px 0 12px;position:sticky;top:0;background:var(--bg);z-index:2}
header h1{font-size:19px;margin:0;flex:1;font-weight:650;letter-spacing:.01em}
header .brand{flex:1}
@media (max-width:369px){header .brand .zombie{display:none}}
/*MASCOT*/
button,.btn{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;
min-height:50px;padding:0 16px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px}
button:disabled{opacity:.45}
button:focus-visible,.btn:focus-within,input:focus-visible,select:focus-visible{outline:2px solid var(--go);outline-offset:2px}
.small{min-height:40px;padding:0 12px;font-size:15px}
#pill{font-size:14px;padding:0 12px;min-height:34px;border-radius:17px;white-space:nowrap}
#pill i{width:9px;height:9px;border-radius:50%;background:var(--dim);display:block}
#pill.ok i{background:var(--ok)}#pill.warn i{background:var(--warn)}#pill.bad i{background:var(--bad)}
.note{color:var(--dim);font-size:14px;margin:10px 2px}
.msg{border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;padding:10px 12px;margin:10px 0;font-size:15px;background:var(--panel)}
.msg.bad{border-left-color:var(--bad)}.msg.ok{border-left-color:var(--ok)}
ul{list-style:none;margin:12px 0 0;padding:0}
li button{width:100%;justify-content:space-between;text-align:left;margin-bottom:8px;padding:10px 14px;min-height:58px}
li .n{font-weight:600;overflow-wrap:anywhere}li .s{color:var(--dim);font-size:13px;white-space:nowrap;margin-left:12px;text-align:right}
li .f{color:var(--dim);font-size:13px;font-weight:400;display:block}
.wide{width:100%}
#stage{background:var(--vinyl);border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;height:42vh;min-height:220px;position:relative}
#stage svg{position:absolute;inset:0;width:100%;height:100%;color:var(--text)}
#stage .edge{fill:none;stroke:var(--dim);stroke-width:1;vector-effect:non-scaling-stroke}
#stage .limit{stroke:var(--bad);stroke-width:1.5;stroke-dasharray:6 5;vector-effect:non-scaling-stroke}
#stage .org{fill:var(--go)}
#fname{font-weight:650;font-size:17px;overflow-wrap:anywhere;margin:2px 2px 8px}
#dims{font-size:15px;margin:0 2px 10px}#dims b{font-weight:650}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
.step{display:grid;grid-template-columns:56px 1fr 56px;gap:0;align-items:center;margin:10px 0;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.step button{border:0;background:none;font-size:24px}
.step div{text-align:center}.step small{display:block;color:var(--dim);font-size:12px}.step output{font-size:20px;font-weight:650}
button[aria-pressed=true]{border-color:var(--go);color:var(--go)}
#cut{background:var(--go);color:var(--go-text);border-color:var(--go);font-weight:700;font-size:19px;min-height:62px;letter-spacing:.02em}
#stop{border-color:var(--bad);color:var(--bad);font-weight:650;min-height:62px}
.cutrow{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-top:14px}
#prog{height:10px;border-radius:5px;background:var(--panel);border:1px solid var(--line);overflow:hidden;margin:14px 0 6px}
#bar{height:100%;width:0;background:var(--go);transition:width .3s}
label.f{display:block;margin:14px 0 0;font-size:14px;color:var(--dim)}
label.f input,label.f select{display:block;width:100%;margin-top:5px;min-height:48px;font:inherit;color:var(--text);
background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:0 12px}
label.c{display:flex;align-items:center;gap:12px;min-height:48px;margin-top:6px}
label.c input{width:22px;height:22px;accent-color:var(--go)}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:26px 2px 0;font-weight:650}
[hidden]{display:none!important}

.vbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:0 0 10px;background:var(--panel)}
.vbar .vtxt{flex-basis:100%;font-size:14px}.vbar .vtxt b{font-size:16px}
.vbar button{flex:1;min-height:40px;padding:0 8px;font-size:14px}
#stage .used{fill:var(--dim);fill-opacity:.22;stroke:var(--dim);stroke-width:1;stroke-dasharray:4 3;vector-effect:non-scaling-stroke}
#stage .jobbox{fill:none;stroke:var(--go);stroke-width:1.5;stroke-dasharray:6 4;vector-effect:non-scaling-stroke}
#stage.clash .jobbox{stroke:var(--bad)}
#stage .lbl{fill:var(--dim);font-family:system-ui,sans-serif}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:0;margin:10px 0;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.seg button{border:0;border-radius:0;min-height:46px;font-size:15px}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--go);color:var(--go-text)}
.seglbl{font-size:13px;color:var(--dim);margin:12px 2px -4px}
</style>
</head>
<body>
<header>
  <button id="back" class="small" hidden>&lsaquo; Files</button>
  <div class="brand" id="brand"><!--LOGO--><!--ZOMBIE--></div>
  <h1 id="title" hidden></h1>
  <button id="pill"><i></i><span>Checking</span></button>
  <button id="gear" class="small">Settings</button>
</header>

<div id="pillmsg" class="msg bad" hidden></div>

<div id="vbar" class="vbar">
  <div class="vtxt" id="vtxt">Vinyl</div>
  <button id="vwidth">Width</button>
  <button id="vundo" hidden>Undo last</button>
  <button id="vnew">New vinyl</button>
</div>

<main id="v-login" hidden>
  <h2>Locked</h2>
  <p class="note">Enter the PIN for this plotter.</p>
  <label class="f">PIN<input id="pin" type="password" autocomplete="current-password"></label>
  <div id="loginmsg" class="msg bad" hidden></div>
  <button id="unlock" class="wide" style="margin-top:12px;border-color:var(--go);color:var(--go);font-weight:650">Unlock</button>
</main>

<main id="v-files">
  <button id="upnote" class="msg ok wide" style="justify-content:flex-start;text-align:left" hidden></button>
  <label class="btn wide"><input id="up" type="file" accept=".svg,image/svg+xml" hidden>Send a file from this device</label>
  <div id="libmsg" class="msg bad" hidden></div>
  <ul id="list"></ul>
  <p id="empty" class="note" hidden></p>
  <button id="refresh" class="small wide">Refresh list</button>
</main>

<main id="v-job" hidden>
  <div id="fname"></div>
  <div id="stage"></div>
  <div id="dims"></div>
  <div id="warns"></div>
  <div class="row">
    <button id="rot">Rotate</button>
    <button id="mir" aria-pressed="false">Mirror</button>
  </div>
  <div class="step">
    <button id="minus" aria-label="Fewer copies">&minus;</button>
    <div><output id="cnt">1</output><small id="cntl">copy</small></div>
    <button id="plus" aria-label="More copies">+</button>
  </div>
  <div id="layrow" hidden>
    <div class="seglbl">Copies</div>
    <div class="seg"><button id="lay-across" aria-pressed="true">Side by side</button><button id="lay-stacked" aria-pressed="false">Stacked</button></div>
  </div>
  <div id="plcrow" hidden>
    <div class="seglbl">Where on the vinyl</div>
    <div class="seg"><button id="plc-auto" aria-pressed="true">Next free spot</button><button id="plc-origin" aria-pressed="false">At the start point</button></div>
  </div>
  <div class="seglbl">Start jobs from</div>
  <div class="seg"><button id="crn-origin" aria-pressed="true">Left edge</button><button id="crn-far" aria-pressed="false">Right edge</button></div>
  <div id="prog" hidden><div id="bar"></div></div>
  <div id="jobmsg" class="note"></div>
  <div class="cutrow">
    <button id="cut">CUT</button>
    <button id="stop">Stop</button>
  </div>
  <button id="del" class="small wide" style="margin-top:22px;border-color:var(--bad);color:var(--bad)">Delete this file</button>
</main>

<main id="v-set" hidden>
  <h2>Updates</h2>
  <p id="up-info" class="note">Checking the installed version.</p>
  <div id="up-msg" class="msg" hidden></div>
  <div class="row">
    <button id="up-check">Check for updates</button>
    <button id="up-install" style="border-color:var(--go);color:var(--go);font-weight:650" hidden>Install</button>
  </div>
  <label class="c"><input type="checkbox" id="s-auto">Install updates automatically (overnight, never during a cut)</label>
  <button id="up-back" class="small wide" hidden>Go back to the previous version</button>

  <h2>Access</h2>
  <p id="pin-info" class="note"></p>
  <label class="f">New PIN (4 or more characters)<input id="s-pin" type="password" autocomplete="new-password"></label>
  <div class="row">
    <button id="pin-set">Set PIN</button>
    <button id="pin-clear">Remove PIN</button>
  </div>

  <h2>Vinyl</h2>
  <label class="f"><span id="l-vw">Usable vinyl width</span><input id="s-vw" inputmode="decimal"></label>
  <p class="note">How far across the plotter can cut on the vinyl loaded now. Change it when you load a different width.</p>
  <label class="f">Start jobs from<select id="s-corner"><option value="origin">Left edge (the plotter's start point)</option><option value="far">Right edge</option></select></label>
  <label class="f"><span id="l-gap">Gap between copies and jobs</span><input id="s-gap" inputmode="decimal"></label>
  <label class="f">When the job ends<select id="s-end"><option value="origin">Return to the start point</option><option value="next">Move to the next free spot</option><option value="advance">Feed the vinyl past the job</option></select></label>
  <label class="f">Overcut on closed shapes (mm)<input id="s-over" inputmode="decimal"></label>
  <label class="f">Show sizes in<select id="s-units"><option value="in">inches</option><option value="mm">millimetres</option></select></label>

  <h2>Plotter</h2>
  <label class="f"><span id="l-width">Widest the plotter can cut</span><input id="s-width" inputmode="decimal"></label>
  <p class="note">Load scrap vinyl and cut the test square: the zombiePlot logo, a 2 inch square with "zP" inside. Standing in front of the plotter the P should read normally. If it is backwards, upside down or lying on its side, change these and cut it again.</p>
  <label class="c"><input type="checkbox" id="s-swap">Swap the two axes</label>
  <label class="c"><input type="checkbox" id="s-mc">Mirror left to right</label>
  <label class="c"><input type="checkbox" id="s-mf">Mirror front to back</label>
  <button id="test" class="wide" style="margin-top:10px">Cut test square</button>

  <h2>Car Thing</h2>
  <label class="f">Screen goes dark when idle for<select id="s-tsleep"><option value="0">Never</option><option value="1">1 minute</option><option value="2">2 minutes</option><option value="5">5 minutes</option><option value="10">10 minutes</option><option value="30">30 minutes</option></select></label>
  <p class="note">Any dial turn or button press wakes it, and that first touch only wakes it. It also wakes when a cut starts or finishes.</p>

  <h2>Connection</h2>
  <label class="f">Cable type<select id="s-link"><option value="auto">Find it automatically</option><option value="parallel">Parallel (USB-to-parallel cable)</option><option value="serial">Serial (USB-to-serial cable)</option></select></label>
  <label class="f">Flow control, serial only (plotter menu: H-WIRE = Hardware)<select id="s-flow"><option value="hardware">Hardware</option><option value="xonxoff">XON/XOFF</option><option value="none">None</option></select></label>
  <label class="f">Baud rate, serial only<select id="s-baud"><option>2400</option><option>4800</option><option>9600</option><option>19200</option></select></label>
  <label class="f">Port ("auto" finds the cable)<input id="s-port" autocapitalize="off" autocorrect="off" spellcheck="false"></label>
  <label class="f">Plotter units per mm (Roland CAMM-1 and HP-GL: 40)<input id="s-upm" inputmode="decimal"></label>
  <label class="f">Pixels per inch, for files with no real size<input id="s-ppi" inputmode="decimal"></label>
  <p id="info" class="note"></p>
  <div id="setmsg" class="msg bad" hidden></div>
  <button id="save" class="wide" style="margin-top:8px;border-color:var(--go);color:var(--go);font-weight:650">Save settings</button>
</main>

<script>
"use strict";
const $ = id => document.getElementById(id);
const S = {view:"files", id:null, rot:"auto", mirror:false, copies:1, layout:"across", place:"auto",
           units:"in", status:null, armed:0, pv:null, settings:null, vjobs:-1};

async function api(path, opts){
  let r;
  try { r = await fetch(path, opts); }
  catch(e){ throw new Error("Cannot reach the zombiePlot box. Check that it has power and Wi-Fi."); }
  let j = null;
  try { j = await r.json(); } catch(e){}
  if(r.status === 401 && j && j.auth){ showLogin(); throw new Error(j.error); }
  if(!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("Error " + r.status));
  return j;
}
const post = (path, obj) => api(path, {method:"POST", headers:{"Content-Type":"application/json", "X-ZombiePlot":"1"}, body:JSON.stringify(obj||{})});

function showLogin(){
  if(S.view === "login") return;
  S.view = "login";
  for(const v of ["files","job","set"]) $("v-"+v).hidden = true;
  $("v-login").hidden = false; $("vbar").hidden = true; $("back").hidden = true; $("gear").hidden = true; $("pill").hidden = true;
  $("brand").hidden = false; $("title").hidden = true;
  setTimeout(() => $("pin").focus(), 50);
}

async function unlock(){
  const box = $("loginmsg"); box.hidden = true;
  try { await post("/api/login", {pin: $("pin").value}); }
  catch(e){ box.textContent = e.message; box.hidden = false; return; }
  $("pin").value = ""; $("v-login").hidden = true; S.view = "";
  show("files"); try { await loadSettings(); } catch(e){} loadFiles(); poll();
}
const len = mm => S.units === "mm" ? mm.toFixed(1) + " mm" : (mm/25.4).toFixed(2) + " in";
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const unit = () => S.units === "mm" ? 1 : 25.4;

function show(view){
  S.view = view;
  for(const v of ["files","job","set"]) $("v-"+v).hidden = v !== view;
  $("v-login").hidden = true; $("pill").hidden = false;
  $("back").hidden = view === "files";
  $("gear").hidden = view === "set";
  $("vbar").hidden = view === "set";
  $("brand").hidden = view !== "files"; $("title").hidden = view === "files";
  $("title").textContent = view === "set" ? "Settings" : "";
  window.scrollTo(0,0);
}

function ago(t){
  const s = Date.now()/1000 - t;
  if(s < 90) return "just now";
  if(s < 3600) return Math.round(s/60) + " min ago";
  if(s < 86400) return Math.round(s/3600) + " hr ago";
  if(s < 86400*14) return Math.round(s/86400) + " days ago";
  return new Date(t*1000).toLocaleDateString();
}

async function loadFiles(){
  $("refresh").disabled = true;
  try{
    const r = await api("/api/files");
    $("libmsg").hidden = r.library_ok;
    $("libmsg").textContent = r.library_message + " Files sent from this device still work.";
    const ul = $("list"); ul.textContent = "";
    for(const f of r.files){
      const li = document.createElement("li"), b = document.createElement("button");
      b.innerHTML = '<span class="n">' + esc(f.name.replace(/\.svg$/i,"")) + (f.folder ? '<span class="f">' + esc(f.folder) + '</span>' : '') +
        '</span><span class="s">' + ago(f.mtime) + '</span>';
      b.onclick = () => openFile(f.id);
      li.appendChild(b); ul.appendChild(li);
    }
    $("empty").hidden = r.files.length > 0;
    $("empty").textContent = "No SVG files yet. Save one into " + r.library + " or send one from this device.";
  }catch(e){
    $("libmsg").hidden = false; $("libmsg").textContent = e.message;
  }
  $("refresh").disabled = false;
}

function openFile(id){
  S.id = id; S.rot = "auto"; S.mirror = false; S.copies = 1; S.layout = "across"; S.place = "auto"; S.pv = null;
  $("fname").textContent = ""; $("stage").textContent = ""; $("dims").textContent = "Loading"; $("warns").textContent = "";
  $("jobmsg").textContent = "";
  show("job"); refreshPreview();
}

let pvSeq = 0;
async function refreshPreview(){
  if(!S.id) return;
  const seq = ++pvSeq;
  disarm();
  $("mir").setAttribute("aria-pressed", S.mirror);
  $("cnt").textContent = S.copies; $("cntl").textContent = S.copies === 1 ? "copy" : "copies";
  $("lay-across").setAttribute("aria-pressed", S.layout === "across");
  $("lay-stacked").setAttribute("aria-pressed", S.layout === "stacked");
  $("plc-auto").setAttribute("aria-pressed", S.place === "auto");
  $("plc-origin").setAttribute("aria-pressed", S.place === "origin");
  $("layrow").hidden = S.copies < 2;
  try{
    const p = await api("/api/preview?id=" + encodeURIComponent(S.id) + "&rot=" + S.rot + "&mirror=" + (S.mirror?1:0) +
                        "&copies=" + S.copies + "&layout=" + S.layout + "&place=" + S.place);
    if(seq !== pvSeq) return;
    S.pv = p; S.rot = p.rot;
    $("crn-origin").setAttribute("aria-pressed", !p.far);
    $("crn-far").setAttribute("aria-pressed", !!p.far);
    $("fname").textContent = p.name.replace(/\.svg$/i,"");
    $("plcrow").hidden = p.used.length === 0;
    draw(p);
    let t = "<b>" + len(p.w) + " wide &times; " + len(p.h) + " long</b>" + (p.copies > 1 ? " each" : "");
    if(p.copies > 1){
      const how = p.layout === "stacked" ? "stacked" : p.cols + " across" + (p.rows > 1 ? ", " + p.rows + " rows" : "");
      t += "<br>All " + p.copies + ": <b>" + len(p.job_w) + " wide &times; " + len(p.job_h) + " long</b> (" + how + ")";
    }
    const along = p.base[1], across = p.far ? p.vinyl_width - p.base[0] - p.job_w : p.base[0];
    const at = (along < 0.05 && across < 0.05) ? (p.far ? "At the right-hand corner" : "At the start point")
             : "Placed " + len(across) + " across" + (p.far ? " from the right" : "") + ", " + len(along) + " along";
    t += "<br>" + at + " &middot; " + p.shapes + " shape" + (p.shapes === 1 ? "" : "s");
    $("dims").innerHTML = t;
    const notes = p.warnings.map(w => '<div class="msg' + (p.fits && !(p.clash && w.startsWith("This spot")) ? "" : " bad") + '">' + esc(w) + "</div>");
    if(p.note) notes.push('<div class="msg ok">' + esc(p.note) + "</div>");
    $("warns").innerHTML = notes.join("");
  }catch(e){
    if(seq !== pvSeq) return;
    S.pv = null; $("stage").textContent = ""; $("dims").textContent = "";
    $("warns").innerHTML = '<div class="msg bad">' + esc(e.message) + "</div>";
  }
  paint();
}

function draw(p){
  const VW = p.vinyl_width, bx = p.base[0], by = p.base[1];
  const minX = Math.min(0, bx), maxX = Math.max(VW, bx + p.job_w);
  let H = by + p.job_h;
  for(const u of p.used) H = Math.max(H, u.y + u.h);
  const W = maxX - minX, m = Math.max(W, H) * 0.05 + 1, fs = (W + 2*m) * 0.035;
  const top = H + m, bottom = -m - fs * 1.6;
  let used = "";
  for(const u of p.used) used += '<rect class="used" x="' + u.x.toFixed(2) + '" y="' + u.y.toFixed(2) + '" width="' + u.w.toFixed(2) + '" height="' + u.h.toFixed(2) + '"/>';
  let uses = "";
  for(const o of p.offsets) uses += '<use href="#one" x="' + (bx + o[0]).toFixed(2) + '" y="' + (by + o[1]).toFixed(2) + '"/>';
  const r = Math.max(W, H) * 0.012;
  $("stage").className = p.clash ? "clash" : "";
  $("stage").innerHTML = '<svg viewBox="' + (minX - m) + " " + (-m) + " " + (W + 2*m) + " " + (top - bottom) + '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Preview of the vinyl">' +
    '<g transform="translate(0,' + H + ') scale(1,-1)"><defs><path id="one" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" vector-effect="non-scaling-stroke" d="' + p.d + '"/></defs>' +
    '<path class="edge" d="M0 ' + top + 'V0H' + VW + 'V' + top + '"/>' + used +
    '<rect class="jobbox" x="' + bx + '" y="' + by + '" width="' + p.job_w + '" height="' + p.job_h + '"/>' +
    '<g>' + uses + '</g><circle class="org" r="' + r + '"/></g>' +
    '<text class="lbl" x="0" y="' + (H + m + fs * 0.9) + '" font-size="' + fs + '">Left</text>' +
    '<text class="lbl" x="' + VW + '" y="' + (H + m + fs * 0.9) + '" font-size="' + fs + '" text-anchor="end">Right</text></svg>';
}

function disarm(){ S.armed = 0; }

function paintUpNote(){
  const n = $("upnote"), v = S.status && S.status.update;
  n.hidden = !v;
  if(v) n.textContent = "Version " + v + " is available. Tap to open Settings and install it.";
}

function paintVinyl(){
  const v = S.status && S.status.vinyl;
  if(!v){ $("vtxt").textContent = "Vinyl"; return; }
  S.units = v.units || S.units;
  let t = "<b>" + len(v.width_mm) + "</b> vinyl &middot; ";
  t += v.jobs ? len(v.used_mm) + " used (" + v.jobs + " job" + (v.jobs === 1 ? "" : "s") + ")" : "nothing cut yet";
  $("vtxt").innerHTML = t;
  $("vundo").hidden = !v.jobs;
}

function paint(){
  const st = S.status, job = st && st.job, sending = job && (job.state === "sending" || job.state === "stopping");
  const pill = $("pill"); let cls = "", txt = "Checking";
  if(!st){ cls = "bad"; txt = "Offline"; }
  else if(sending){ cls = "ok"; txt = job.state === "stopping" ? "Stopping" : "Sending " + Math.floor(100 * job.sent / Math.max(1, job.total)) + "%"; }
  else if(st.port.state === "missing"){ cls = "bad"; txt = "No cable"; }
  else if(st.port.state === "notready"){ cls = "warn"; txt = "Not ready"; }
  else { cls = "ok"; txt = "Ready"; }
  if(st && !sending && st.port.link && st.port.state !== "missing") txt += " \u00b7 " + (st.port.link === "serial" ? "SER" : "PAR");
  pill.className = cls; pill.lastChild.textContent = txt;
  paintVinyl();
  paintUpNote();
  const cut = $("cut");
  cut.disabled = !!sending || !S.pv || !S.pv.fits || !st;
  const armed = S.armed && Date.now() < S.armed;
  cut.textContent = sending ? "SENDING" : armed ? (S.pv && S.pv.clash ? "CUTS OVER USED VINYL - TAP AGAIN" : "TAP AGAIN TO CUT") : "CUT";
  $("prog").hidden = !sending;
  if(sending) $("bar").style.width = (100 * job.sent / Math.max(1, job.total)).toFixed(1) + "%";
  let m = "";
  if(job){
    if(job.state === "sending") m = job.waiting ? "Waiting for the plotter to take more data." : "Sending " + job.name + " to the plotter.";
    else if(job.state === "stopping") m = "Stopping.";
    else if(job.state === "done") m = "Sent. The plotter keeps cutting until it finishes what it was sent.";
    else if(job.state === "stopped" || job.state === "error") m = job.message;
  }
  if(S.view === "job") $("jobmsg").textContent = m;
  for(const id of ["rot","mir","minus","plus","test","save","vnew","vundo","vwidth","lay-across","lay-stacked","plc-auto","plc-origin","crn-origin","crn-far","del"]) $(id).disabled = !!sending;
  if(sending && S.view === "set"){ $("up-install").disabled = true; $("up-back").disabled = true; }
}

function pillDetail(){
  const st = S.status; let m = "";
  if(!st) m = "Cannot reach the zombiePlot box. Check that it has power and is on Wi-Fi.";
  else if(st.port.state === "missing") m = st.port.message;
  else if(st.port.state === "notready") m = st.port.message;
  else m = "Plotter connected on " + st.port.device + " (" + st.port.link + ").";
  const box = $("pillmsg"); box.textContent = m; box.className = "msg" + (st && st.port.state !== "missing" && st.port.state !== "notready" ? " ok" : " bad");
  box.hidden = false; window.scrollTo(0,0); setTimeout(() => { box.hidden = true; }, 9000);
}

async function poll(){
  if(S.view === "login") return;
  try { S.status = await api("/api/status"); } catch(e){ S.status = null; }
  if(S.armed && Date.now() > S.armed) S.armed = 0;
  const jobs = S.status && S.status.vinyl ? S.status.vinyl.jobs : -1;
  if(jobs !== S.vjobs){ const first = S.vjobs === -1; S.vjobs = jobs; if(!first && S.view === "job") refreshPreview(); }
  paint();
}

async function doCut(){
  if(!S.pv) return;
  if(!(S.armed && Date.now() < S.armed)){ S.armed = Date.now() + 4000; paint(); setTimeout(paint, 4050); return; }
  disarm();
  try{
    await post("/api/cut", {id:S.id, rot:S.rot, mirror:S.mirror, copies:S.copies, layout:S.layout, place:S.place});
    $("jobmsg").textContent = "";
    S.place = "auto";
  }catch(e){ $("warns").innerHTML = '<div class="msg bad">' + esc(e.message) + "</div>"; }
  await poll();
  refreshPreview();
}

async function loadSettings(){
  const r = await api("/api/settings"), s = r.settings;
  S.units = s.units; S.settings = s;
  const u = unit(), d = s.units === "mm" ? 1 : 3;
  $("l-width").textContent = "Widest the plotter can cut (" + s.units + ")";
  $("l-gap").textContent = "Gap between copies and jobs (" + s.units + ")";
  $("l-vw").textContent = "Usable vinyl width (" + s.units + ")";
  $("s-width").value = +(s.cut_width_mm / u).toFixed(d); $("s-gap").value = +(s.copy_gap_mm / u).toFixed(d);
  $("s-vw").value = +(s.vinyl_width_mm / u).toFixed(d); $("s-corner").value = s.start_corner;
  $("s-tsleep").value = String(s.thing_sleep_min);
  $("s-units").value = s.units; $("s-end").value = s.end_action; $("s-over").value = s.overcut_mm;
  $("s-swap").checked = s.swap_axes; $("s-mc").checked = s.mirror_carriage; $("s-mf").checked = s.mirror_feed;
  $("s-link").value = s.link; $("s-flow").value = s.flow; $("s-baud").value = s.baud; $("s-port").value = s.port;
  $("s-ppi").value = s.px_per_inch; $("s-upm").value = s.steps_per_mm;
  $("pin-info").textContent = r.pin_set ? "A PIN is set. Each phone or computer asks for it once."
    : "No PIN. Anyone on your network can use this page. Set a PIN if others share your network.";
  $("pin-clear").hidden = !r.pin_set;
  $("info").textContent = "Files folder: " + r.library + " · Cable: " + (r.device || "not found") + " · Version " + r.version;
}

async function saveSettings(){
  const u = S.settings.units === "mm" ? 1 : 25.4;
  const body = {
    cut_width_mm: parseFloat($("s-width").value) * u, copy_gap_mm: parseFloat($("s-gap").value) * u,
    vinyl_width_mm: parseFloat($("s-vw").value) * u, start_corner: $("s-corner").value, thing_sleep_min: parseInt($("s-tsleep").value, 10),
    units: $("s-units").value, end_action: $("s-end").value, overcut_mm: parseFloat($("s-over").value),
    swap_axes: $("s-swap").checked, mirror_carriage: $("s-mc").checked, mirror_feed: $("s-mf").checked,
    link: $("s-link").value, flow: $("s-flow").value, baud: parseInt($("s-baud").value, 10),
    port: $("s-port").value.trim() || "auto", px_per_inch: parseFloat($("s-ppi").value),
    steps_per_mm: parseFloat($("s-upm").value)
  };
  for(const k in body) if(typeof body[k] === "number" && isNaN(body[k])) throw new Error("One of the number fields is empty or not a number.");
  await post("/api/settings", body);
  await loadSettings();
}

function paintUpdates(){
  const u = S.update;
  if(!u) return;
  let t = "Installed: version " + u.installed + ".";
  if(u.latest) t += u.available ? " Version " + u.latest + " is available." : " This is the newest version.";
  if(u.checked) t += " Checked " + ago(u.checked) + ".";
  $("up-info").textContent = t;
  $("up-install").hidden = !u.available;
  $("up-install").textContent = "Install version " + u.latest;
  $("up-back").hidden = !u.previous;
  $("up-back").textContent = "Go back to version " + u.previous;
  $("s-auto").checked = !!u.auto;
  const box = $("up-msg"), busy = S.updating || u.pending || u.state === "requested" || u.state === "installing";
  if(busy){ box.className = "msg"; box.textContent = (u.message || "Updating.") + " The page reloads by itself when it is done."; }
  else if(u.state === "failed"){ box.className = "msg bad"; box.textContent = u.message; }
  else if(u.state === "ok" || u.state === "skipped"){ box.className = "msg ok"; box.textContent = u.message; }
  else box.className = "";
  box.hidden = !box.className;
  for(const id of ["up-check","up-install","up-back"]) $(id).disabled = !!busy;
}

async function loadUpdates(){
  try { S.update = await api("/api/update"); } catch(e){ return; }
  paintUpdates();
}

async function watchUpdate(from){
  S.updating = true; paintUpdates();
  const start = Date.now(), end = start + 45 * 60 * 1000;
  while(Date.now() < end){
    await new Promise(r => setTimeout(r, 2500));
    let u = null;
    try { u = await api("/api/update"); } catch(e){ continue; }      // the box restarts while installing
    S.update = u;
    if(u.installed !== from){ location.reload(); return; }
    if(u.state === "failed" || u.state === "current"){ S.updating = false; paintUpdates(); return; }
    if(u.pending && Date.now() > start + 90000){
      u.message = "The update helper has not picked up the request. Log in to the Pi and run the one-line install from the guide once to set it up.";
    }
    paintUpdates();
  }
  S.updating = false; paintUpdates();
}

function setMsg(text, ok){ const b = $("setmsg"); b.textContent = text; b.className = "msg " + (ok ? "ok" : "bad"); b.hidden = !text; }

$("back").onclick = () => { show("files"); loadFiles(); };
$("gear").onclick = async () => { setMsg(""); try { await loadSettings(); show("set"); loadUpdates(); } catch(e){ alert(e.message); } };
$("upnote").onclick = () => $("gear").onclick();
$("unlock").onclick = unlock;
$("pin").onkeydown = ev => { if(ev.key === "Enter") unlock(); };
$("pin-set").onclick = async () => {
  const v = $("s-pin").value;
  if(v.length < 4){ setMsg("Use at least 4 characters for the PIN.", false); return; }
  try { await post("/api/pin", {pin: v}); $("s-pin").value = ""; await loadSettings(); setMsg("PIN set. Other phones and computers will be asked for it.", true); }
  catch(e){ setMsg(e.message, false); }
};
$("pin-clear").onclick = async () => {
  if(!confirm("Remove the PIN? Anyone on your network will be able to use this page.")) return;
  try { await post("/api/pin", {pin: ""}); await loadSettings(); setMsg("PIN removed.", true); } catch(e){ setMsg(e.message, false); }
};
$("up-check").onclick = async () => {
  $("up-check").disabled = true; $("up-info").textContent = "Checking GitHub.";
  try { S.update = await post("/api/update/check"); } catch(e){ await loadUpdates(); $("up-msg").hidden = false; $("up-msg").className = "msg bad"; $("up-msg").textContent = e.message; $("up-check").disabled = false; return; }
  paintUpdates(); poll();
};
$("up-install").onclick = async () => {
  if(!confirm("Install version " + S.update.latest + "? The plotter page restarts during the update. Settings and files are kept.")) return;
  const from = S.update.installed;
  try { S.update = await post("/api/update/install"); watchUpdate(from); } catch(e){ alert(e.message); }
};
$("up-back").onclick = async () => {
  if(!confirm("Go back to version " + S.update.previous + "?")) return;
  const from = S.update.installed;
  try { S.update = await post("/api/update/rollback"); watchUpdate(from); } catch(e){ alert(e.message); }
};
$("s-auto").onchange = async () => {
  try { await post("/api/settings", {auto_update: $("s-auto").checked}); await loadUpdates(); } catch(e){ alert(e.message); }
};
$("pill").onclick = pillDetail;
$("refresh").onclick = loadFiles;
$("rot").onclick = () => { S.rot = ((S.rot === "auto" ? 0 : S.rot) + 90) % 360; refreshPreview(); };
$("mir").onclick = () => { S.mirror = !S.mirror; refreshPreview(); };
$("minus").onclick = () => { if(S.copies > 1){ S.copies--; refreshPreview(); } };
$("plus").onclick = () => { if(S.copies < 500){ S.copies++; refreshPreview(); } };
$("lay-across").onclick = () => { S.layout = "across"; refreshPreview(); };
$("lay-stacked").onclick = () => { S.layout = "stacked"; refreshPreview(); };
$("plc-auto").onclick = () => { S.place = "auto"; refreshPreview(); };
async function setCorner(c){
  try { await post("/api/settings", {start_corner: c}); if(S.settings) S.settings.start_corner = c; refreshPreview(); }
  catch(e){ $("warns").innerHTML = '<div class="msg bad">' + esc(e.message) + "</div>"; }
}
$("crn-origin").onclick = () => setCorner("origin");
$("del").onclick = async () => {
  const name = S.pv ? S.pv.name : "this file";
  if(!confirm('Delete "' + name + '"? It is removed from the box and can\'t be undone.')) return;
  try {
    await post("/api/delete", {id: S.id});
    S.id = null; S.pv = null; show("files"); await loadFiles();
    $("libmsg").hidden = false; $("libmsg").className = "msg ok"; $("libmsg").textContent = "Deleted " + name + ".";
    setTimeout(() => { $("libmsg").hidden = true; $("libmsg").className = "msg bad"; }, 4000);
  } catch(e){ $("warns").innerHTML = '<div class="msg bad">' + esc(e.message) + "</div>"; }
};
$("crn-far").onclick = () => setCorner("far");
$("plc-origin").onclick = () => { S.place = "origin"; refreshPreview(); };
$("cut").onclick = doCut;
$("stop").onclick = async () => { disarm(); try { await post("/api/stop"); } catch(e){ $("jobmsg").textContent = e.message; } poll(); };
$("save").onclick = async () => { try { await saveSettings(); setMsg("Saved.", true); } catch(e){ setMsg(e.message, false); } };
$("test").onclick = async () => {
  try { await saveSettings(); await post("/api/cut", {id:"test", rot:0, mirror:false, copies:1}); setMsg("Test square sent to the plotter.", true); }
  catch(e){ setMsg(e.message, false); }
  poll();
};
$("vwidth").onclick = async () => {
  const v = S.status && S.status.vinyl, u = unit();
  const cur = v ? (v.width_mm / u).toFixed(S.units === "mm" ? 0 : 2) : "";
  const a = prompt("Usable vinyl width in " + (S.units === "mm" ? "mm" : "inches") + " (how far across the plotter can cut on this vinyl):", cur);
  if(a === null) return;
  const n = parseFloat(a);
  if(!(n > 0)){ alert("That is not a number."); return; }
  try { await post("/api/settings", {vinyl_width_mm: n * u}); await poll(); if(S.view === "job") refreshPreview(); }
  catch(e){ alert(e.message); }
};
$("vnew").onclick = async () => {
  if(!confirm("Start fresh vinyl? This forgets where earlier jobs were cut. Do this after loading new vinyl or setting a new start point on the plotter.")) return;
  try { await post("/api/session/new"); await poll(); if(S.view === "job"){ S.place = "auto"; refreshPreview(); } } catch(e){ alert(e.message); }
};
$("vundo").onclick = async () => {
  if(!confirm("Forget the last job's area? Only do this if it did not actually cut.")) return;
  try { await post("/api/session/undo"); await poll(); if(S.view === "job") refreshPreview(); } catch(e){ alert(e.message); }
};
$("up").onchange = async ev => {
  const f = ev.target.files[0]; ev.target.value = "";
  if(!f) return;
  try{
    const r = await api("/api/upload?name=" + encodeURIComponent(f.name), {method:"POST", headers:{"X-ZombiePlot":"1"}, body:f});
    openFile(r.id);
  }catch(e){ $("libmsg").hidden = false; $("libmsg").textContent = e.message; }
};

(async function(){
  try { await loadSettings(); } catch(e){}
  loadFiles(); poll();
  setInterval(() => { if(!document.hidden) poll(); }, 1000);
  document.addEventListener("visibilitychange", () => { if(!document.hidden){ poll(); if(S.view === "files") loadFiles(); } });
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Car Thing remote (Spotify Car Thing plugged into this box's USB port)
# ----------------------------------------------------------------------------
# The Car Thing's screen is a Chromium web view that loads a page from its own flash. Flashed with
# the Thing Labs image, it also has adb and a USB network link (RNDIS): the Car Thing is
# 192.168.7.2 and this box takes 192.168.7.1 on that cable (install.sh sets that up). A small
# launcher page, put on the Car Thing once with adb, opens /thing from this box over that link, so
# it gets the same protections as the phone page (Host check, X-ZombiePlot header, PIN) and the Car
# Thing never needs Wi-Fi.
#
# Putting the launcher on: --car-thing-install, from any computer that can run adb (Windows too).
# On a Pi that can run adb (Pi 2 and newer), --car-thing also does it on its own, and adds an
# "adb reverse" route to http://localhost:8742 as a second way in. The original Model B+ and Zero
# have an ARMv6 processor that the packaged adb doesn't run on, so they use the USB link only.
#
# Car Thing input as the web view sees it: dial turn = wheel event, dial press = Enter,
# back button = Escape, the four top buttons = 1 to 4, the top-right button = m.

CAR_THING_PORT = 8742                      # adb reverse route on the Car Thing
CAR_THING_LINK_HOST = "192.168.7.1"        # this box on the USB network link
CAR_THING_LINK_IP = "192.168.7.2"          # the Car Thing on that link (Thing Labs image)
CAR_THING_MAC = "a0:b1:c2:d3:e4:01"        # the link's address as this box sees it
CAR_THING_WEBAPP = "/usr/share/qt-superbird-app/webapp"
CAR_THING_BACKUP = CAR_THING_WEBAPP + ".before-zombieplot"


class CarThingError(Exception):
    pass


def car_thing_launcher():
    """The page put on the Car Thing's flash. Tries each way of reaching this box until one
    answers, then opens /thing there."""
    bases = ["http://%s" % CAR_THING_LINK_HOST, "http://%s:8080" % CAR_THING_LINK_HOST,
             "http://localhost:%d" % CAR_THING_PORT]
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>zombiePlot</title></head>"
            "<body style=\"margin:0;background:#15181b;color:#97a2ab;font:28px sans-serif;display:flex;"
            "align-items:center;justify-content:center;height:100vh\"><div>Waiting for zombiePlot...</div>"
            "<script>var bases=%s,n=0;"
            "function tryit(){var b=bases[n++%%bases.length];"
            "if(!window.fetch){setTimeout(function(){location.replace(bases[0]+'/thing');},3000);return;}"
            "fetch(b+'/api/status',{mode:'no-cors',cache:'no-store'})"
            ".then(function(){location.replace(b+'/thing');},function(){setTimeout(tryit,1000);});}"
            "tryit();</script></body></html>" % json.dumps(bases))


def _adb(adb, *args, timeout=60):
    try:
        r = subprocess.run([adb] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CarThingError("adb %s timed out" % args[0])
    except OSError as e:
        raise CarThingError("cannot run adb: %s" % e)
    out = (r.stdout or "").strip()
    if r.returncode < 0:
        out = ("adb crashed (signal %d). This computer's processor may not run this adb build; "
               "the original Pi Model B+ and Zero can't." % -r.returncode + (" " + out if out else ""))
    return r.returncode, out


def _adb_ok(adb, *args, timeout=60):
    code, out = _adb(adb, *args, timeout=timeout)
    if code:
        raise CarThingError("adb %s failed: %s" % (" ".join(args)[:80], _tail(out, 300)))
    return out


def adb_works(adb):
    try:
        return _adb(adb, "version", timeout=20)[0] == 0
    except CarThingError:
        return False


def car_thing_reverse(adb, http_port):
    _adb_ok(adb, "reverse", "tcp:%d" % CAR_THING_PORT, "tcp:%d" % http_port)


def car_thing_install_page(adb):
    """Puts the launcher on the Car Thing if it isn't there yet. True when it changed the screen."""
    page = car_thing_launcher()
    mark = hashlib.sha256(page.encode("utf-8")).hexdigest()[:16]
    _, cur = _adb(adb, "shell", "cat %s/.zombieplot 2>/dev/null" % CAR_THING_WEBAPP)
    if cur.strip() == mark:
        return False
    w, b = CAR_THING_WEBAPP, CAR_THING_BACKUP
    _adb_ok(adb, "shell", "mount -o remount,rw / && { [ -e %s ] || [ ! -d %s ] || cp -a %s %s; } "
                          "&& rm -rf %s && mkdir -p %s && echo ok" % (b, w, w, b, w, w))
    fd, tmp = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(page)
        _adb_ok(adb, "push", tmp, w + "/index.html")
    finally:
        os.remove(tmp)
    _adb(adb, "shell", "echo %s > %s/.zombieplot; chmod 755 %s; chmod 644 %s/index.html %s/.zombieplot; sync; "
                       "mount -o remount,ro / 2>/dev/null; supervisorctl restart chromium >/dev/null 2>&1 || reboot"
                       % (mark, w, w, w, w))
    return True


def car_thing_restore(adb):
    """Puts back whatever the Car Thing's screen showed before zombiePlot."""
    w, b = CAR_THING_WEBAPP, CAR_THING_BACKUP
    out = _adb_ok(adb, "shell", "[ -d %s ] && mount -o remount,rw / && rm -rf %s && mv %s %s && sync "
                                "&& echo restored || echo nobackup" % (b, w, b, w))
    if "restored" not in out:
        raise CarThingError("There is no earlier screen saved on this Car Thing.")
    _adb(adb, "shell", "mount -o remount,ro / 2>/dev/null; supervisorctl restart chromium >/dev/null 2>&1 || reboot")


def car_thing_once(adb, restore=False):
    """--car-thing-install / --car-thing-restore: wait for the Car Thing, do it, finish."""
    adb = adb or shutil.which("adb")
    if not adb:
        raise SystemExit("adb was not found. Install Android platform-tools, or give its path with --adb.")
    if not adb_works(adb):
        raise SystemExit("adb does not run on this computer (%s). Use another computer for this step." % adb)
    print("Waiting for the Car Thing (plug it into this computer's USB port)...")
    sys.stdout.flush()
    code, out = _adb(adb, "wait-for-usb-device", timeout=None)
    if code:
        raise SystemExit("adb: " + _tail(out, 300))
    try:
        if restore:
            car_thing_restore(adb)
            print("Done. The Car Thing shows its earlier screen again.")
        elif car_thing_install_page(adb):
            print("Done. The Car Thing's screen now opens zombiePlot. Plug it into the zombiePlot box.")
        else:
            print("The Car Thing already has the zombiePlot start page. Plug it into the zombiePlot box.")
    except CarThingError as e:
        raise SystemExit(str(e))


def run_car_thing(http_port):
    """--car-thing: runs as root next to the web page on a Pi that can run adb
    (zombieplot-carthing.service). Keeps the launcher installed and the adb route up."""
    adb = shutil.which("adb")
    if not adb:
        raise SystemExit("adb is not installed. Run: sudo apt-get install -y adb")
    if not adb_works(adb):
        raise SystemExit("adb does not run on this Pi's processor. The Car Thing still works over the "
                         "USB network link; put its start page on once from a computer (see README).")

    def log(msg):
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    log("zombiePlot %s: Car Thing remote, forwarding its port %d to web port %d"
        % (VERSION, CAR_THING_PORT, http_port))
    while True:
        code, out = _adb(adb, "wait-for-usb-device", timeout=None)
        if code:
            log("adb: " + _tail(out, 200))
            time.sleep(10)
            continue
        try:
            car_thing_reverse(adb, http_port)
            if car_thing_install_page(adb):
                log("Car Thing connected; its screen now opens zombiePlot.")
            else:
                log("Car Thing connected.")
        except CarThingError as e:
            log("Car Thing: %s" % e)
            time.sleep(10)
            continue
        while True:
            time.sleep(5)
            try:
                code, state = _adb(adb, "get-state", timeout=15)
                if code or state != "device":
                    log("Car Thing unplugged.")
                    break
                _, rev = _adb(adb, "reverse", "--list", timeout=15)
                if "tcp:%d" % CAR_THING_PORT not in rev:
                    car_thing_reverse(adb, http_port)
            except CarThingError as e:
                log("Car Thing: %s" % e)
                break


THING = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>zombiePlot</title>
<style>
/* Sized for the Car Thing's 800 x 480 screen and its old Chromium: no flex gap, no newer syntax.
   The dial overhangs the right edge of the screen: about the right 50 px, top 320 px. Nothing to
   read or press goes there (measured from a photo of a real Car Thing). */
:root{--bg:#15181b;--panel:#1e2327;--line:#30373d;--text:#e9edf0;--dim:#97a2ab;--go:#86c35f;--go-text:#10200a;
--ok:#4fbf7b;--warn:#f2a33a;--bad:#ef5d52;--vinyl:#262c31}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--text);
font:20px/1.3 system-ui,"Segoe UI",sans-serif;-webkit-user-select:none;user-select:none}
::-webkit-scrollbar{display:none}
button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:0}
button:disabled{opacity:.4}
[hidden]{display:none!important}
#keys{height:42px;display:flex;align-items:stretch;border-bottom:1px solid var(--line);padding-right:64px}
#keys .k{flex:1;display:flex;align-items:center;padding:0 8px;font-size:17px;color:var(--dim);white-space:nowrap;overflow:hidden}
#keys .k i{font-style:normal;display:inline-block;min-width:22px;height:22px;line-height:20px;text-align:center;border:1px solid var(--line);
border-radius:11px;font-size:13px;margin-right:6px;color:var(--dim)}
#keys .k b{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis}
#keys .k.on b{color:var(--go)}
#keys .k.armed{background:var(--go)}#keys .k.armed b,#keys .k.armed i{color:var(--go-text);border-color:var(--go-text)}
#keys .k.empty{visibility:hidden}
#keys .k.m{flex:0 0 110px;justify-content:flex-end}
#keys .k.m b{color:var(--bad)}
#bar{height:36px;display:flex;align-items:center;padding:0 76px 0 12px;font-size:17px;color:var(--dim)}
#pill{display:inline-flex;align-items:center;font-weight:650;color:var(--text);margin-right:14px}
#pill:before{content:"";width:12px;height:12px;border-radius:6px;margin-right:7px;background:var(--dim)}
#pill.ok:before{background:var(--ok)}#pill.warn:before{background:var(--warn)}#pill.bad:before{background:var(--bad)}
/*MASCOT*/
#bar .brand{margin-right:14px}#bar .logo{height:19px}#bar .zombie{height:25px;margin-bottom:-2px}
#vtxt{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#vtxt b{color:var(--text)}
#prog{height:4px;background:var(--line)}#bar2{height:4px;width:0;background:var(--ok)}
main{position:absolute;top:82px;left:0;right:0;bottom:0}
section{position:absolute;top:0;left:0;right:0;bottom:0}
#list{list-style:none;margin:0;padding:4px 74px 10px 10px;height:100%;overflow-y:auto}
#list li{display:flex;align-items:center;height:62px;padding:0 14px;margin-bottom:6px;border-radius:10px;
background:var(--panel);border:2px solid transparent}
#list li.sel{border-color:var(--go)}
#list .n{flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:22px}
#list .f{color:var(--dim);font-size:16px;margin-left:10px}
#list .s{color:var(--dim);font-size:16px;margin-left:12px;white-space:nowrap}
.hint{color:var(--dim);font-size:16px}
#empty{padding:30px 24px;color:var(--dim)}
#stage{position:absolute;left:10px;top:4px;bottom:10px;width:50%;background:var(--vinyl);border:1px solid var(--line);border-radius:10px}
#stage svg{position:absolute;left:0;top:0;width:100%;height:100%;color:var(--text)}
#stage .edge{fill:none;stroke:var(--dim);stroke-width:1;vector-effect:non-scaling-stroke}
#stage .org{fill:var(--go)}
#stage .used{fill:var(--dim);fill-opacity:.22;stroke:var(--dim);stroke-width:1;stroke-dasharray:4 3;vector-effect:non-scaling-stroke}
#stage .jobbox{fill:none;stroke:var(--go);stroke-width:1.5;stroke-dasharray:6 4;vector-effect:non-scaling-stroke}
#stage.clash .jobbox{stroke:var(--bad)}
#side{position:absolute;left:calc(50% + 22px);right:10px;top:4px;bottom:10px;display:flex;flex-direction:column}
#fname,#dims,#cnt,#warns,#jobhint{margin-right:54px}
#fname{font-size:22px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#dims{font-size:17px;color:var(--dim);margin-top:2px}
#dims b{color:var(--text);font-weight:600}
#cnt{display:flex;align-items:center;margin-top:8px}
#cnt button{width:56px;height:48px;font-size:28px;line-height:1}
#cnt span{flex:1;text-align:center;font-size:22px}
#cnt span b{font-size:30px}
#warns{flex:1;overflow:hidden;font-size:16px;margin-top:6px}
.msg{padding:6px 10px;border-radius:8px;background:var(--panel);border-left:4px solid var(--dim);margin-bottom:5px}
.msg.bad{border-left-color:var(--bad)}.msg.ok{border-left-color:var(--ok)}
#cut{height:76px;font-size:26px;font-weight:750;letter-spacing:.04em;background:var(--go);color:var(--go-text);border:0}
#cut.armed{background:var(--bad);color:#fff}
#stop{height:76px;font-size:26px;font-weight:750;background:var(--bad);color:#fff;border:0}
#v-login{display:flex;align-items:center;justify-content:center;padding-right:64px}
#v-login .l{width:300px;margin-right:40px}
#dots{font-size:44px;letter-spacing:.2em;min-height:60px;margin-top:10px}
#pad{width:330px;display:flex;flex-wrap:wrap}
#pad button{width:100px;height:72px;margin:0 10px 10px 0;font-size:28px}
#toast{position:absolute;left:20px;right:84px;bottom:16px;padding:14px 18px;border-radius:10px;background:var(--panel);
border-left:5px solid var(--bad);font-size:19px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
#toast.ok{border-left-color:var(--ok)}
#sleep{position:absolute;left:0;top:0;right:0;bottom:0;background:#000;z-index:50}
</style>
</head>
<body>
<div id="keys">
  <div class="k" id="k1"><i>1</i><b></b></div>
  <div class="k" id="k2"><i>2</i><b></b></div>
  <div class="k" id="k3"><i>3</i><b></b></div>
  <div class="k" id="k4"><i>4</i><b></b></div>
  <div class="k m" id="km"><b>STOP</b><i style="margin:0 0 0 6px">M</i></div>
</div>
<div id="bar"><div class="brand"><!--LOGO--><!--ZOMBIE--></div><span id="pill" class="">Checking</span><span id="vtxt"></span></div>
<div id="prog"><div id="bar2"></div></div>
<main>
  <section id="v-files">
    <ul id="list"></ul>
    <div id="empty" hidden></div>
  </section>
  <section id="v-job" hidden>
    <div id="stage"></div>
    <div id="side">
      <div id="fname"></div>
      <div id="dims"></div>
      <div id="cnt"><button id="minus" aria-label="Fewer copies">&minus;</button><span><b id="cntn">1</b> <span id="cntl">copy</span></span><button id="plus" aria-label="More copies">+</button></div>
      <div id="warns"></div>
      <div class="hint" id="jobhint">Dial: copies &middot; Press dial: cut &middot; Back: files</div>
      <button id="cut" style="margin-top:6px">CUT</button>
      <button id="stop" style="margin-top:6px" hidden>STOP</button>
    </div>
  </section>
  <section id="v-login" hidden>
    <div class="l"><div style="font-size:26px;font-weight:650">Enter the PIN</div>
      <div class="hint">Set in zombiePlot's Settings on your phone.</div><div id="dots"></div></div>
    <div id="pad"></div>
  </section>
</main>
<div id="toast" hidden></div>
<div id="sleep" hidden></div>
<script>
"use strict";
var $ = function(id){ return document.getElementById(id); };
var S = {view:"files", files:[], sel:0, id:null, rot:"auto", mirror:false, copies:1, layout:"across", place:"auto",
         units:"in", status:null, pv:null, arm:null, vjobs:-1, pin:""};

async function api(path, opts){
  var r;
  try { r = await fetch(path, opts); }
  catch(e){ throw new Error("Cannot reach zombiePlot."); }
  var j = null;
  try { j = await r.json(); } catch(e){}
  if(r.status === 401 && j && j.auth){ showLogin(); throw new Error(j.error); }
  if(!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ("Error " + r.status));
  return j;
}
function post(path, obj){
  return api(path, {method:"POST", headers:{"Content-Type":"application/json", "X-ZombiePlot":"1"}, body:JSON.stringify(obj || {})});
}
var esc = function(s){ return String(s).replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); };
var len = function(mm){ return S.units === "mm" ? mm.toFixed(1) + " mm" : (mm/25.4).toFixed(2) + " in"; };
var num = function(mm){ return S.units === "mm" ? mm.toFixed(0) : (mm/25.4).toFixed(2); };
var bare = function(n){ return n.replace(/\.svg$/i, ""); };

var toastTimer = 0;
function toast(msg, good){
  var t = $("toast"); t.textContent = msg; t.className = good ? "ok" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(function(){ t.hidden = true; }, 6000);
}

/* Press twice within 4 seconds for anything that cuts or forgets vinyl. */
function armed(what){ return !!(S.arm && S.arm.what === what && Date.now() < S.arm.until); }
function twice(what, fn){
  if(!armed(what)){ S.arm = {what:what, until:Date.now() + 4000}; paint(); setTimeout(paint, 4050); return; }
  S.arm = null; paint(); fn();
}

function show(view){
  S.view = view;
  var vs = ["files", "job", "login"];
  for(var i = 0; i < vs.length; i++) $("v-" + vs[i]).hidden = vs[i] !== view;
  paint();
}

/* ---------- files ---------- */
function ago(t){
  var s = Date.now()/1000 - t;
  if(s < 90) return "just now";
  if(s < 3600) return Math.round(s/60) + " min";
  if(s < 86400) return Math.round(s/3600) + " hr";
  if(s < 86400*14) return Math.round(s/86400) + " days";
  return new Date(t*1000).toLocaleDateString();
}
async function loadFiles(){
  var keep = S.files[S.sel] ? S.files[S.sel].id : null;
  try{
    var r = await api("/api/files");
    S.files = r.files;
    S.sel = 0;
    for(var i = 0; i < S.files.length; i++) if(S.files[i].id === keep) S.sel = i;
    var ul = $("list"); ul.textContent = "";
    S.files.forEach(function(f, i){
      var li = document.createElement("li");
      li.innerHTML = '<span class="n">' + esc(bare(f.name)) + (f.folder ? '<span class="f">' + esc(f.folder) + '</span>' : '') +
                     '</span><span class="s">' + ago(f.mtime) + '</span>';
      li.onclick = function(){ S.sel = i; openFile(); };
      ul.appendChild(li);
    });
    $("empty").hidden = S.files.length > 0;
    $("empty").textContent = r.library_ok ? "No SVG files yet. Send one from your phone, or save one into " + r.library + "."
                                          : r.library_message;
    if(!r.library_ok && S.files.length) toast(r.library_message);
    paintSel();
  }catch(e){ if(S.view !== "login") toast(e.message); }
}
function paintSel(){
  var lis = $("list").children;
  for(var i = 0; i < lis.length; i++) lis[i].className = i === S.sel ? "sel" : "";
  if(lis[S.sel]) lis[S.sel].scrollIntoView({block:"nearest"});
}
function openFile(){
  var f = S.files[S.sel];
  if(!f) return;
  S.id = f.id; S.rot = "auto"; S.mirror = false; S.copies = 1; S.layout = "across"; S.place = "auto"; S.pv = null; S.arm = null;
  $("fname").textContent = bare(f.name); $("stage").textContent = ""; $("dims").textContent = "Loading"; $("warns").textContent = "";
  show("job"); refreshPreview();
}

/* ---------- job ---------- */
var pvSeq = 0, pvTimer = 0;
function later(){ clearTimeout(pvTimer); pvTimer = setTimeout(refreshPreview, 250); }
async function refreshPreview(){
  if(!S.id) return;
  var seq = ++pvSeq;
  if(armed("cut")) S.arm = null;
  paint();
  try{
    var p = await api("/api/preview?id=" + encodeURIComponent(S.id) + "&rot=" + S.rot + "&mirror=" + (S.mirror ? 1 : 0) +
                      "&copies=" + S.copies + "&layout=" + S.layout + "&place=" + S.place);
    if(seq !== pvSeq) return;
    S.pv = p; S.rot = p.rot;
    if(!p.used.length) S.place = "auto";
    $("fname").textContent = bare(p.name);
    draw(p);
    var u = S.units === "mm" ? " mm" : " in";
    var t = "<b>" + num(p.w) + " &times; " + num(p.h) + u + "</b> " + (p.copies > 1 ? "each" : "wide &times; long");
    if(p.copies > 1){
      var how = p.layout === "stacked" ? "stacked" : p.cols + " across" + (p.rows > 1 ? ", " + p.rows + " rows" : "");
      t += "<br>All: <b>" + num(p.job_w) + " &times; " + num(p.job_h) + u + "</b> (" + how + ")";
    }
    var along = p.base[1], across = p.far ? p.vinyl_width - p.base[0] - p.job_w : p.base[0];
    t += "<br>" + ((along < 0.05 && across < 0.05) ? (p.far ? "At the right-hand corner" : "At the start point")
                 : len(across) + " across" + (p.far ? " from the right" : "") + ", " + len(along) + " along");
    $("dims").innerHTML = t;
    var notes = p.warnings.map(function(w){ return '<div class="msg' + (p.fits && !(p.clash && w.indexOf("This spot") === 0) ? "" : " bad") + '">' + esc(w) + "</div>"; });
    if(p.note) notes.push('<div class="msg ok">' + esc(p.note) + "</div>");
    $("warns").innerHTML = notes.join("");
  }catch(e){
    if(seq !== pvSeq) return;
    S.pv = null; $("stage").textContent = ""; $("dims").textContent = "";
    $("warns").innerHTML = '<div class="msg bad">' + esc(e.message) + "</div>";
  }
  paint();
}
function draw(p){
  var VW = p.vinyl_width, bx = p.base[0], by = p.base[1];
  var minX = Math.min(0, bx), maxX = Math.max(VW, bx + p.job_w), H = by + p.job_h, i;
  for(i = 0; i < p.used.length; i++) H = Math.max(H, p.used[i].y + p.used[i].h);
  var W = maxX - minX, m = Math.max(W, H) * 0.05 + 1, top = H + m;
  var used = "", uses = "";
  for(i = 0; i < p.used.length; i++){
    var q = p.used[i];
    used += '<rect class="used" x="' + q.x.toFixed(2) + '" y="' + q.y.toFixed(2) + '" width="' + q.w.toFixed(2) + '" height="' + q.h.toFixed(2) + '"/>';
  }
  for(i = 0; i < p.offsets.length; i++)
    uses += '<use href="#one" xlink:href="#one" x="' + (bx + p.offsets[i][0]).toFixed(2) + '" y="' + (by + p.offsets[i][1]).toFixed(2) + '"/>';
  $("stage").className = p.clash ? "clash" : "";
  $("stage").innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="' + (minX - m) + " " + (-m) + " " + (W + 2*m) + " " + (top + m) +
    '" preserveAspectRatio="xMidYMid meet"><g transform="translate(0,' + H + ') scale(1,-1)"><defs><path id="one" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" vector-effect="non-scaling-stroke" d="' + p.d + '"/></defs>' +
    '<path class="edge" d="M0 ' + top + 'V0H' + VW + 'V' + top + '"/>' + used +
    '<rect class="jobbox" x="' + bx + '" y="' + by + '" width="' + p.job_w + '" height="' + p.job_h + '"/>' +
    uses + '<circle class="org" r="' + (Math.max(W, H) * 0.015) + '"/></g></svg>';
}
function sending(){ var j = S.status && S.status.job; return !!(j && (j.state === "sending" || j.state === "stopping")); }
function setCopies(n){
  n = Math.max(1, Math.min(500, n));
  if(n === S.copies || sending()) return;
  S.copies = n; paint(); later();
}
async function doCut(){
  try{
    await post("/api/cut", {id:S.id, rot:S.rot, mirror:S.mirror, copies:S.copies, layout:S.layout, place:S.place});
    S.place = "auto";
    toast("Sending to the plotter.", true);
  }catch(e){ toast(e.message); }
  await poll();
  refreshPreview();
}
async function stop(){
  S.arm = null;
  try { await post("/api/stop"); toast("Stopping.", true); } catch(e){ toast(e.message); }
  poll();
}

/* ---------- the four top buttons ---------- */
function keyMap(){
  var v = S.status && S.status.vinyl, busy = sending();
  if(S.view === "files") return [
    {label:"Refresh", run:loadFiles},
    {label:armed("vnew") ? "Press again" : "New vinyl", armed:armed("vnew"), off:busy,
     run:function(){ twice("vnew", async function(){ try { await post("/api/session/new"); toast("New vinyl: earlier cuts forgotten.", true); } catch(e){ toast(e.message); } poll(); }); }},
    {label:armed("vundo") ? "Press again" : "Undo last", armed:armed("vundo"), off:busy || !(v && v.jobs),
     run:function(){ twice("vundo", async function(){ try { await post("/api/session/undo"); toast("Last job's area forgotten.", true); } catch(e){ toast(e.message); } poll(); }); }},
    {label:(v && v.start_corner === "far" ? "Right edge" : "Left edge"), on:!!(v && v.start_corner === "far"), off:busy || !v,
     run:async function(){ try { await post("/api/settings", {start_corner: v.start_corner === "far" ? "origin" : "far"}); } catch(e){ toast(e.message); } poll(); }}];
  if(S.view === "job") return [
    {label:"Rotate " + (S.rot === "auto" ? 0 : S.rot) + "°", off:busy,
     run:function(){ S.rot = ((S.rot === "auto" ? 0 : S.rot) + 90) % 360; refreshPreview(); }},
    {label:"Mirror " + (S.mirror ? "on" : "off"), on:S.mirror, off:busy,
     run:function(){ S.mirror = !S.mirror; refreshPreview(); }},
    {label:S.layout === "stacked" ? "Stacked" : "Side by side", off:busy || S.copies < 2,
     run:function(){ S.layout = S.layout === "stacked" ? "across" : "stacked"; refreshPreview(); }},
    {label:S.place === "origin" ? "Start point" : "Next free", on:S.place === "origin", off:busy || !(S.pv && S.pv.used.length),
     run:function(){ S.place = S.place === "origin" ? "auto" : "origin"; refreshPreview(); }}];
  return [null, null, null, null];
}
function press(i){
  var k = keyMap()[i];
  if(k && !k.off) k.run();
}

/* ---------- status ---------- */
function paint(){
  var st = S.status, job = st && st.job, busy = sending();
  var pill = $("pill"), cls = "", txt = "Checking";
  if(!st){ cls = "bad"; txt = "Offline"; }
  else if(busy){ cls = "ok"; txt = job.state === "stopping" ? "Stopping" : "Sending " + Math.floor(100 * job.sent / Math.max(1, job.total)) + "%"; }
  else if(st.port.state === "missing"){ cls = "bad"; txt = "No cable"; }
  else if(st.port.state === "notready"){ cls = "warn"; txt = "Not ready"; }
  else { cls = "ok"; txt = "Ready"; }
  if(st && !busy && st.port.link && st.port.state !== "missing") txt += " \u00b7 " + (st.port.link === "serial" ? "SER" : "PAR");
  pill.className = cls; pill.textContent = txt;
  var v = st && st.vinyl;
  if(v){
    S.units = v.units || S.units;
    $("vtxt").innerHTML = "<b>" + len(v.width_mm) + "</b> vinyl &middot; " +
      (v.jobs ? len(v.used_mm) + " used (" + v.jobs + " job" + (v.jobs === 1 ? "" : "s") + ")" : "nothing cut yet");
  } else $("vtxt").textContent = "";
  $("bar2").style.width = busy ? (100 * job.sent / Math.max(1, job.total)).toFixed(1) + "%" : "0";

  var keys = keyMap();
  for(var i = 0; i < 4; i++){
    var el = $("k" + (i + 1)), k = keys[i];
    el.className = "k" + (!k ? " empty" : "") + (k && k.on ? " on" : "") + (k && k.armed ? " armed" : "");
    el.style.opacity = k && k.off ? ".35" : "";
    el.lastChild.textContent = k ? k.label : "";
  }
  $("km").className = "k m" + (busy ? "" : " empty");

  if(S.view === "job"){
    $("cntn").textContent = S.copies; $("cntl").textContent = S.copies === 1 ? "copy" : "copies";
    $("minus").disabled = busy || S.copies <= 1; $("plus").disabled = busy || S.copies >= 500;
    var cut = $("cut"), a = armed("cut");
    cut.hidden = busy; $("stop").hidden = !busy;
    cut.disabled = !S.pv || !S.pv.fits || !st;
    cut.className = a ? "armed" : "";
    cut.textContent = a ? (S.pv && S.pv.clash ? "OVER USED VINYL - AGAIN" : "PRESS AGAIN TO CUT") : "CUT";
  }
}
async function poll(){
  if(S.view === "login") return;
  try { S.status = await api("/api/status"); } catch(e){ S.status = null; }
  var jobs = S.status && S.status.vinyl ? S.status.vinyl.jobs : -1;
  if(jobs !== S.vjobs){ var first = S.vjobs === -1; S.vjobs = jobs; if(!first && S.view === "job") refreshPreview(); }
  paint();
  sleepCheck();
}

/* ---------- screen sleep: black after the idle time set in Settings (the backlight stays on) ---------- */
var lastInput = Date.now(), asleep = false, lastJob = null;
function wake(){
  lastInput = Date.now();
  if(!asleep) return false;
  asleep = false; $("sleep").hidden = true;
  return true;
}
function sleepCheck(){
  var st = S.status, j = st && st.job, js = j ? j.state + "|" + j.name + "|" + j.total : "";
  if(lastJob !== null && js !== lastJob) wake();          /* a cut started or finished: show it */
  lastJob = js;
  var mins = st ? +st.thing_sleep_min : 0;
  if(!asleep && mins > 0 && !sending() && S.view !== "login" && Date.now() - lastInput > mins * 60000){
    asleep = true; $("sleep").hidden = false;
  }
}
["keydown", "click", "touchstart", "mousedown"].forEach(function(t){
  window.addEventListener(t, function(e){ if(wake()){ e.preventDefault(); e.stopImmediatePropagation(); } }, true);
});
window.addEventListener("wheel", function(e){ if(wake()){ e.preventDefault(); e.stopImmediatePropagation(); } }, {capture:true, passive:false});

/* ---------- PIN ---------- */
function showLogin(){
  if(S.view === "login") return;
  S.pin = ""; $("dots").textContent = ""; S.status = null;
  show("login");
}
function pinKey(k){
  if(k === "del") S.pin = S.pin.slice(0, -1);
  else if(k === "ok"){ unlock(); return; }
  else if(S.pin.length < 64) S.pin += k;
  $("dots").textContent = new Array(S.pin.length + 1).join("•");
}
async function unlock(){
  try { await post("/api/login", {pin:S.pin}); }
  catch(e){ S.pin = ""; $("dots").textContent = ""; toast(e.message); return; }
  S.pin = ""; S.view = ""; show("files"); loadFiles(); poll();
}
["1","2","3","4","5","6","7","8","9","del","0","ok"].forEach(function(k){
  var b = document.createElement("button");
  b.textContent = k === "del" ? "⌫" : k === "ok" ? "OK" : k;
  if(k === "ok"){ b.style.background = "var(--go)"; b.style.color = "var(--go-text)"; b.style.border = "0"; }
  b.onclick = function(){ pinKey(k); };
  $("pad").appendChild(b);
});

/* ---------- Car Thing controls (and a keyboard, for trying it on a computer) ---------- */
function turn(d){
  if(S.view === "files" && S.files.length){ S.sel = Math.max(0, Math.min(S.files.length - 1, S.sel + d)); paintSel(); }
  else if(S.view === "job") setCopies(S.copies + d);
}
function enter(){
  if(S.view === "files") openFile();
  else if(S.view === "job" && !sending() && S.pv && S.pv.fits) twice("cut", doCut);
  else if(S.view === "login") unlock();
}
function back(){
  if(S.view === "job"){ S.arm = null; S.id = null; S.pv = null; show("files"); loadFiles(); }
  else if(S.view === "login") pinKey("del");
}
document.addEventListener("keydown", function(e){
  var k = e.key || "", c = e.code || "";
  if(S.view === "login" && /^[0-9]$/.test(k)){ pinKey(k); e.preventDefault(); return; }
  var n = /^Digit[1-4]$/.test(c) ? +c.slice(5) : /^[1-4]$/.test(k) ? +k : 0;
  if(n){ press(n - 1); }
  else if(c === "KeyM" || k === "m" || k === "M"){ if(sending()) stop(); }
  else if(k === "Escape" || k === "Backspace" || k === "BrowserBack") back();
  else if(k === "Enter" || k === " ") enter();
  else if(k === "ArrowDown" || k === "ArrowRight") turn(1);
  else if(k === "ArrowUp" || k === "ArrowLeft") turn(-1);
  else return;
  e.preventDefault();
});
var wheelAt = 0;
document.addEventListener("wheel", function(e){
  e.preventDefault();
  var d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
  var now = Date.now();
  if(!d || now - wheelAt < 40) return;      /* a mouse or trackpad sends bursts; the dial sends one per click */
  wheelAt = now;
  turn(d > 0 ? 1 : -1);
}, {passive:false});
for(var i = 1; i <= 4; i++) (function(n){ $("k" + n).onclick = function(){ press(n - 1); }; })(i);
$("km").onclick = function(){ if(sending()) stop(); };
$("minus").onclick = function(){ setCopies(S.copies - 1); };
$("plus").onclick = function(){ setCopies(S.copies + 1); };
$("cut").onclick = function(){ if(S.pv && S.pv.fits) twice("cut", doCut); };
$("stop").onclick = stop;

loadFiles(); poll();
setInterval(poll, 1000);
setInterval(function(){ if(S.view === "files") loadFiles(); }, 30000);
</script>
</body>
</html>
"""


PAGE, THING = [x.replace("/*MASCOT*/", MASCOT_CSS).replace("<!--LOGO-->", LOGO_SVG).replace("<!--ZOMBIE-->", ZOMBIE_SVG)
                for x in (PAGE, THING)]


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="zombiePlot: phone control for Roland CAMM-1 vinyl cutters")
    ap.add_argument("--port", type=int, default=int(env("ZOMBIEPLOT_HTTP_PORT", env("PLOTTER_HTTP_PORT", "8080"))),
                    help="web port (default 8080)")
    ap.add_argument("--data", default=env("ZOMBIEPLOT_DATA", "/var/lib/zombieplot"),
                    help="folder for settings and files sent from the phone")
    ap.add_argument("--library", default=env("ZOMBIEPLOT_LIBRARY", env("PLOTTER_LIBRARY", "")),
                    help="folder of SVG files (for example the mounted server share)")
    ap.add_argument("--selftest", action="store_true", help="check this copy works, print its version")
    ap.add_argument("--updater", action="store_true", help="act on an update request (run as root)")
    ap.add_argument("--auto", action="store_true", help="with --updater: nightly automatic update")
    ap.add_argument("--clear-pin", action="store_true", help="remove the access PIN (run as the zombieplot user)")
    ap.add_argument("--car-thing", action="store_true",
                    help="run the Car Thing remote: forwards a USB-connected Car Thing to the web page (run as root)")
    ap.add_argument("--car-thing-install", action="store_true",
                    help="put the zombiePlot start page on a Car Thing plugged into this computer (needs adb)")
    ap.add_argument("--car-thing-restore", action="store_true",
                    help="put back the Car Thing's earlier screen (needs adb, Car Thing plugged in)")
    ap.add_argument("--adb", default="", help="path to adb, for --car-thing-install / --car-thing-restore")
    args = ap.parse_args()
    if args.car_thing_install or args.car_thing_restore:
        car_thing_once(args.adb, restore=args.car_thing_restore)
        return
    if args.car_thing:
        run_car_thing(args.port)
        return
    if args.clear_pin:
        Config(os.path.join(args.data, "config.json")).update({"pin_hash": ""})
        try:
            os.remove(os.path.join(args.data, "sessions.json"))
        except OSError:
            pass
        print("PIN removed. Restart with: sudo systemctl restart zombieplot")
        return
    if args.selftest:
        selftest()
        return
    if args.updater:
        sys.exit(run_updater(args.data, args.auto))
    os.makedirs(args.data, exist_ok=True)
    Handler.app = App(args.data, args.library)
    srv = ThreadingHTTPServer(("", args.port), Handler)
    srv.daemon_threads = True
    sys.stderr.write("zombiePlot %s: web page on port %d, files in %s\n"
                     % (VERSION, args.port, Handler.app.library))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
