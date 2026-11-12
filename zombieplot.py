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
import fcntl
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
import termios
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.4.1"
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


TEST_SIZE = 50.8   # 2 inch square with a letter F in it


def test_design():
    s = TEST_SIZE
    polys = [
        ([(0.0, 0.0), (s, 0.0), (s, s), (0.0, s)], True),
        ([(35.0, 40.0), (15.0, 40.0), (15.0, 10.0)], False),
        ([(15.0, 26.0), (29.0, 26.0)], False),
    ]
    return polys, s, s


# ----------------------------------------------------------------------------
# Plotter connection: USB-to-parallel (usblp, /dev/usb/lp*) or USB-to-serial
# ----------------------------------------------------------------------------

class PortError(Exception):
    pass


class Cancelled(Exception):
    pass


BAUDS = {2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200}
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
                          "units": self.cfg.get("units")}}


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
            public = path in ("/", "/index.html", "/thing", "/manifest.json", "/icon.svg", "/api/login")
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
                if path == "/icon.svg":
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
        'fill="#15181b"/><path d="M22 30h52M22 30v40h52V30M34 30v-8h28v8" fill="none" stroke="#f2a33a" '
        'stroke-width="5" stroke-linejoin="round"/><path d="M34 58c8-14 20 6 28-8" fill="none" '
        'stroke="#e9edf0" stroke-width="5" stroke-linecap="round"/></svg>')

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
:root{--bg:#15181b;--panel:#1e2327;--line:#30373d;--text:#e9edf0;--dim:#97a2ab;--go:#f2a33a;--go-text:#1a1305;
--ok:#4fbf7b;--warn:#f2a33a;--bad:#ef5d52;--vinyl:#262c31}
@media (prefers-color-scheme:light){:root{--bg:#f3f4f2;--panel:#fff;--line:#d5d9dc;--text:#181c1f;--dim:#5d6870;
--go:#c9720a;--go-text:#fff;--ok:#1f8a4c;--warn:#a85f00;--bad:#c5362c;--vinyl:#e8ebec}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body{max-width:640px;margin:0 auto;padding:0 14px calc(20px + env(safe-area-inset-bottom));-webkit-tap-highlight-color:transparent}
header{display:flex;align-items:center;gap:10px;padding:14px 0 12px;position:sticky;top:0;background:var(--bg);z-index:2}
header h1{font-size:19px;margin:0;flex:1;font-weight:650;letter-spacing:.01em}
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
  <h1 id="title">zombiePlot</h1>
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
  <div id="prog" hidden><div id="bar"></div></div>
  <div id="jobmsg" class="note"></div>
  <div class="cutrow">
    <button id="cut">CUT</button>
    <button id="stop">Stop</button>
  </div>
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
  <p class="note">Load scrap vinyl and cut the test square. Standing in front of the plotter it should be a 2 inch square with a normal, upright F in it. If the F is mirrored or lying on its side, change these and cut it again.</p>
  <label class="c"><input type="checkbox" id="s-swap">Swap the two axes</label>
  <label class="c"><input type="checkbox" id="s-mc">Mirror left to right</label>
  <label class="c"><input type="checkbox" id="s-mf">Mirror front to back</label>
  <button id="test" class="wide" style="margin-top:10px">Cut test square</button>

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
  $("title").textContent = "zombiePlot";
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
  $("title").textContent = view === "set" ? "Settings" : view === "job" ? "" : "zombiePlot";
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
  for(const id of ["rot","mir","minus","plus","test","save","vnew","vundo","vwidth","lay-across","lay-stacked","plc-auto","plc-origin"]) $(id).disabled = !!sending;
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
    vinyl_width_mm: parseFloat($("s-vw").value) * u, start_corner: $("s-corner").value,
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
# The Car Thing's screen is a Chromium web view that loads a page from its own flash. With adb
# turned on (flashed once from a PC), --car-thing puts a small launcher page there and runs
# "adb reverse", so the Car Thing reaches this box's web server at http://localhost:8742 over USB.
# The launcher opens /thing, which is served from here like the phone page, so it gets the same
# protections (Host check, X-ZombiePlot header, PIN). The Car Thing never needs Wi-Fi.
#
# Car Thing input as the web view sees it: dial turn = wheel event, dial press = Enter,
# back button = Escape, the four top buttons = 1 to 4, the top-right button = m.

CAR_THING_PORT = 8742
CAR_THING_WEBAPP = "/usr/share/qt-superbird-app/webapp"
CAR_THING_BACKUP = CAR_THING_WEBAPP + ".before-zombieplot"


class CarThingError(Exception):
    pass


def car_thing_launcher():
    """The page put on the Car Thing's flash. Waits until this box answers, then opens /thing."""
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>zombiePlot</title></head>"
            "<body style=\"margin:0;background:#15181b;color:#97a2ab;font:28px sans-serif;display:flex;"
            "align-items:center;justify-content:center;height:100vh\"><div>Waiting for zombiePlot...</div>"
            "<script>var base='http://localhost:%d';function go(){location.replace(base+'/thing');}"
            "function tryit(){if(!window.fetch){setTimeout(go,3000);return;}"
            "fetch(base+'/api/status',{mode:'no-cors',cache:'no-store'}).then(go,function(){setTimeout(tryit,2000);});}"
            "tryit();</script></body></html>" % CAR_THING_PORT)


def _adb(adb, *args, timeout=60):
    try:
        r = subprocess.run([adb] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CarThingError("adb %s timed out" % args[0])
    return r.returncode, (r.stdout or "").strip()


def _adb_ok(adb, *args, timeout=60):
    code, out = _adb(adb, *args, timeout=timeout)
    if code:
        raise CarThingError("adb %s failed: %s" % (" ".join(args)[:80], _tail(out, 300)))
    return out


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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        _adb_ok(adb, "push", tmp, w + "/index.html")
    finally:
        os.remove(tmp)
    _adb(adb, "shell", "echo %s > %s/.zombieplot; sync; mount -o remount,ro / 2>/dev/null; "
                       "supervisorctl restart chromium >/dev/null 2>&1 || reboot" % (mark, w))
    return True


def car_thing_restore(adb):
    """Puts back whatever the Car Thing's screen showed before zombiePlot."""
    w, b = CAR_THING_WEBAPP, CAR_THING_BACKUP
    out = _adb_ok(adb, "shell", "[ -d %s ] && mount -o remount,rw / && rm -rf %s && mv %s %s && sync "
                                "&& echo restored || echo nobackup" % (b, w, b, w))
    if "restored" not in out:
        raise CarThingError("There is no earlier screen saved on this Car Thing.")
    _adb(adb, "shell", "mount -o remount,ro / 2>/dev/null; supervisorctl restart chromium >/dev/null 2>&1 || reboot")


def run_car_thing(http_port, restore=False):
    """Runs as root next to the web page (zombieplot-carthing.service). Handles plug and unplug."""
    adb = shutil.which("adb")
    if not adb:
        raise SystemExit("adb is not installed. Run: sudo apt-get install -y adb")

    def log(msg):
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    if restore:
        log("Waiting for the Car Thing to be plugged in...")
        _adb(adb, "wait-for-usb-device", timeout=None)
        try:
            car_thing_restore(adb)
        except CarThingError as e:
            raise SystemExit(str(e))
        log("Done. The Car Thing shows its earlier screen again.")
        return
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
/* Sized for the Car Thing's 800 x 480 screen and its old Chromium: no flex gap, no newer syntax. */
:root{--bg:#15181b;--panel:#1e2327;--line:#30373d;--text:#e9edf0;--dim:#97a2ab;--go:#f2a33a;--go-text:#1a1305;
--ok:#4fbf7b;--warn:#f2a33a;--bad:#ef5d52;--vinyl:#262c31}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--text);
font:20px/1.3 system-ui,"Segoe UI",sans-serif;-webkit-user-select:none;user-select:none}
::-webkit-scrollbar{display:none}
button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:0}
button:disabled{opacity:.4}
[hidden]{display:none!important}
#keys{height:42px;display:flex;align-items:stretch;border-bottom:1px solid var(--line)}
#keys .k{flex:1;display:flex;align-items:center;padding:0 8px;font-size:17px;color:var(--dim);white-space:nowrap;overflow:hidden}
#keys .k i{font-style:normal;display:inline-block;min-width:22px;height:22px;line-height:20px;text-align:center;border:1px solid var(--line);
border-radius:11px;font-size:13px;margin-right:6px;color:var(--dim)}
#keys .k b{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis}
#keys .k.on b{color:var(--go)}
#keys .k.armed{background:var(--go)}#keys .k.armed b,#keys .k.armed i{color:var(--go-text);border-color:var(--go-text)}
#keys .k.empty{visibility:hidden}
#keys .k.m{flex:0 0 110px;justify-content:flex-end}
#keys .k.m b{color:var(--bad)}
#bar{height:36px;display:flex;align-items:center;padding:0 12px;font-size:17px;color:var(--dim)}
#pill{display:inline-flex;align-items:center;font-weight:650;color:var(--text);margin-right:14px}
#pill:before{content:"";width:12px;height:12px;border-radius:6px;margin-right:7px;background:var(--dim)}
#pill.ok:before{background:var(--ok)}#pill.warn:before{background:var(--warn)}#pill.bad:before{background:var(--bad)}
#vtxt{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#vtxt b{color:var(--text)}
#prog{height:4px;background:var(--line)}#bar2{height:4px;width:0;background:var(--ok)}
main{position:absolute;top:82px;left:0;right:0;bottom:0}
section{position:absolute;top:0;left:0;right:0;bottom:0}
#list{list-style:none;margin:0;padding:4px 10px 10px;height:100%;overflow-y:auto}
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
#v-login{display:flex;align-items:center;justify-content:center}
#v-login .l{width:300px;margin-right:40px}
#dots{font-size:44px;letter-spacing:.2em;min-height:60px;margin-top:10px}
#pad{width:330px;display:flex;flex-wrap:wrap}
#pad button{width:100px;height:72px;margin:0 10px 10px 0;font-size:28px}
#toast{position:absolute;left:20px;right:20px;bottom:16px;padding:14px 18px;border-radius:10px;background:var(--panel);
border-left:5px solid var(--bad);font-size:19px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
#toast.ok{border-left-color:var(--ok)}
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
<div id="bar"><span id="pill" class="">Checking</span><span id="vtxt"></span></div>
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
    null];
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
}

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
    ap.add_argument("--car-thing-restore", action="store_true",
                    help="put back the Car Thing's earlier screen (run as root, Car Thing plugged in)")
    args = ap.parse_args()
    if args.car_thing or args.car_thing_restore:
        run_car_thing(args.port, restore=args.car_thing_restore)
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
