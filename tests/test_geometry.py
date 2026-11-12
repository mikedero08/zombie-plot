import os
import math, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)

def svg(body, attrs='width="100mm" height="100mm" viewBox="0 0 100 100"'):
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" %s>%s</svg>' % (attrs, body)).encode()

def near(a, b, tol=1e-6):
    return abs(a - b) <= tol

CFG = dict(P.DEFAULTS)
CFG["overcut_mm"] = 0.0

# 1. real-world units: 4in x 2in rectangle drawn in a 400x200 viewBox
polys, w, h, warn = P.parse_svg(svg('<rect x="0" y="0" width="400" height="200"/>', 'width="4in" height="2in" viewBox="0 0 400 200"'))
check("inch units size", near(w, 101.6) and near(h, 50.8), (w, h))
check("inch units no warning", warn == [], warn)
cols, rows, offs, jw, jh = P.grid(w, h, 1, 584, 5)
hp = P.build_hpgl(polys, offs, jw, jh, CFG).decode()
check("rect hpgl exact", hp == "IN;PA;PU0,2032;PD4064,2032;PD4064,0;PD0,0;PD0,2032;PU0,0;", hp)

# 2. mm units, circle: every point on the radius, bbox right
polys, w, h, warn = P.parse_svg(svg('<circle cx="50" cy="50" r="25"/>'))
check("circle bbox", near(w, 50, 0.01) and near(h, 50, 0.01), (w, h))
pts, closed = polys[0]
dev = max(abs(math.hypot(x - 25, y - 25) - 25) for x, y in pts)
check("circle points on radius (<0.0005mm)", dev < 0.0005 and closed, dev)
seq = pts + [pts[0]]
sag = max(25 - math.hypot((a[0] + b[0]) / 2 - 25, (a[1] + b[1]) / 2 - 25) for a, b in zip(seq, seq[1:]))
check("circle chord error within tolerance", sag <= P.FLATTEN_TOL_MM + 0.005, sag)
check("circle point count sane", 20 < len(pts) < 400, len(pts))

# 3. no units -> px at 96/in, with a warning
polys, w, h, warn = P.parse_svg(svg('<rect width="96" height="192"/>', ''))
check("unitless 96px = 1in", near(w, 25.4) and near(h, 50.8), (w, h))
check("unitless warns", len(warn) == 1)
polys, w, h, warn = P.parse_svg(svg('<rect width="72" height="72"/>', ''), 72.0)
check("px_per_inch setting", near(w, 25.4), w)

# 4. y flip: the SVG top becomes high y. L-shape: vertical stroke on left, foot at bottom going right.
polys, w, h, _ = P.parse_svg(svg('<path d="M10 10 V60 H40"/>'))
pts = polys[0][0]
check("y flip", pts[0] == (0.0, 50.0) and near(pts[1][1], 0) and near(pts[2][0], 30) and near(pts[2][1], 0), pts)

# 5. arcs: all four flag combinations stay on a radius-45 circle through both endpoints
def centres(p, q, r):
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    dx, dy = q[0] - p[0], q[1] - p[1]
    d = math.hypot(dx, dy)
    hgt = math.sqrt(r * r - d * d / 4)
    return [(mx - dy / d * hgt, my + dx / d * hgt), (mx + dy / d * hgt, my - dx / d * hgt)]
ok = True
lengths = []
for fa in (0, 1):
    for fs in (0, 1):
        out = []
        pb = P.PathBuilder(P.IDENT, out)
        P.parse_path("M10 80 A45 45 0 %d %d 60 120" % (fa, fs), pb)
        pts = out[0][0]
        cs = centres((10, 80), (60, 120), 45)
        d = min(max(abs(math.hypot(x - c[0], y - c[1]) - 45) for x, y in pts) for c in cs)
        ok = ok and d < 0.0005 and near(pts[-1][0], 60) and near(pts[-1][1], 120)
        lengths.append(round(P.cut_length([(pts, False)]), 1))
check("arc flags geometry", ok)
check("arc large/small lengths differ correctly", lengths[0] == lengths[1] and lengths[2] == lengths[3] and lengths[2] > lengths[0], lengths)
total = lengths[0] + lengths[2]
check("arc small+large = full circle", abs(total - 2 * math.pi * 45) < 0.3, total)

# 6. compact arc flags and odd number packing
def flat(d):
    out = []
    P.parse_path(d, P.PathBuilder(P.IDENT, out))
    return out
a = flat("M0 0a10 10 0 0110 10")
b = flat("M0 0 a 10 10 0 0 1 10 10")
check("compact arc flags", a == b and len(a) == 1 and len(a[0][0]) > 3)
a = flat("M.5.5l1-1.5.5 2e0-1E-1,3")
check("packed numbers", [tuple(round(v, 6) for v in p) for p in a[0][0]] == [(0.5, 0.5), (1.5, -1.0), (2.0, 1.0), (1.9, 4.0)], a)

# 7. cubic flattening accuracy vs dense sampling
out = flat("M0 0 C 0 100 150 100 150 0")
pts = out[0][0]
def bez(t):
    u = 1 - t
    return (3 * u * t * t * 150 + t ** 3 * 150, 3 * u * u * t * 100 + 3 * u * t * t * 100)
def dist_to_poly(p):
    best = 1e9
    for a, b in zip(pts, pts[1:]):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L = vx * vx + vy * vy
        t = max(0, min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L))
        best = min(best, math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy))
    return best
err = max(dist_to_poly(bez(i / 5000.0)) for i in range(5001))
check("cubic flatten error <= tolerance", err <= P.FLATTEN_TOL_MM, err)

# 8. smooth and relative commands equal their explicit forms
a = flat("M10 10 C 20 0 30 0 40 10 S 60 20 70 10")
b = flat("M10 10 C 20 0 30 0 40 10 C 50 20 60 20 70 10")
check("S command", a == b)
a = flat("M10 10 Q 25 0 40 10 T 70 10")
b = flat("M10 10 Q 25 0 40 10 Q 55 20 70 10")
check("T command", a == b)
a = flat("m10 10 h20 v20 h-20 z m5 5 l10 0 0 10 z")
check("relative + z then m", len(a) == 2 and a[0][1] and a[1][1] and a[1][0][0] == (15.0, 15.0) and a[0][0] == [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)], a)
a = flat("M0 0 L10 0 L10 10 Z L-5 -5")
check("draw after Z restarts at sub-path start", len(a) == 2 and a[1][0] == [(0.0, 0.0), (-5.0, -5.0)], a)
a = flat("M0 0 L10 0 L10 10 X 5 5 L 20 20")
check("bad command keeps what parsed", len(a) == 1 and len(a[0][0]) == 3, a)

# 9. transforms: nested groups, rotate about a point, matrix
polys, w, h, _ = P.parse_svg(svg('<g transform="translate(20,20) rotate(90)"><rect width="30" height="10"/></g>'))
check("rotate 90 swaps size", near(w, 10, 1e-9) and near(h, 30, 1e-9), (w, h))
polys, w, h, _ = P.parse_svg(svg('<g transform="scale(2)"><g transform="translate(5 5)"><rect width="10" height="20" transform="matrix(1,0,0,1,3,3)"/></g></g>'))
check("nested scale", near(w, 20) and near(h, 40), (w, h))
m = P.parse_transform("rotate(90, 10, 10)")
check("rotate about point", all(near(a, b, 1e-9) for a, b in zip(P.mat_apply(m, 20, 10), (10, 20))), P.mat_apply(m, 20, 10))
m = P.parse_transform("translate(10,0) scale(2)")
check("transform order", P.mat_apply(m, 1, 1) == (12.0, 2.0), P.mat_apply(m, 1, 1))

# 10. other shapes, use, hidden, defs, text
body = ('<defs><path id="tri" d="M0 0 L10 0 L5 8 Z"/></defs>'
        '<use xlink:href="#tri" x="0" y="0"/><use href="#tri" x="20" y="0"/>'
        '<g style="display:none"><rect width="500" height="500"/></g>'
        '<rect display="none" width="900" height="900"/>'
        '<text x="0" y="0">hello</text>'
        '<polygon points="40,0 50,0 50,10 40,10"/><polyline points="60,0 70,10 80,0"/>'
        '<line x1="0" y1="20" x2="80" y2="20"/><ellipse cx="40" cy="40" rx="20" ry="5"/>'
        '<rect x="0" y="50" width="20" height="10" rx="3"/>')
polys, w, h, warn = P.parse_svg(svg(body))
check("shape count (defs/hidden skipped, use expanded)", len(polys) == 7, len(polys))
check("mixed bbox", near(w, 80, 0.01) and near(h, 60, 0.01), (w, h))
check("text warning", any("text" in x for x in warn), warn)
closed = [c for _, c in polys]
check("closed flags", closed == [True, True, True, False, False, True, True], closed)
rr = polys[-1][0]
check("rounded rect has arcs", len(rr) > 12)

# 11. orient
L = [([(0.0, 50.0), (0.0, 0.0), (30.0, 0.0)], False)]
r, w, h = P.orient(L, 90, False)
check("rotate 90 ccw", r[0][0] == [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0)] and (w, h) == (50.0, 30.0), r)
r, w, h = P.orient(L, 0, True)
check("mirror", r[0][0] == [(30.0, 50.0), (30.0, 0.0), (0.0, 0.0)], r)
r, w, h = P.orient(L, 180, False)
check("rotate 180", r[0][0] == [(30.0, 0.0), (30.0, 50.0), (0.0, 50.0)], r)
r, w, h = P.orient(L, 270, False)
check("rotate 270", r[0][0] == [(50.0, 30.0), (0.0, 30.0), (0.0, 0.0)], r)

# 12. copies grid
cols, rows, offs, jw, jh = P.grid(100, 40, 7, 584, 5)
check("grid 5 across", (cols, rows) == (5, 2) and offs[5] == (0, 45) and offs[4] == (420, 0) and near(jw, 520) and near(jh, 85), (cols, rows, offs, jw, jh))
cols, rows, offs, jw, jh = P.grid(600, 40, 3, 584, 5)
check("grid too wide stacks", (cols, rows) == (1, 3) and jw == 600, (cols, rows, jw))
cols, rows, offs, jw, jh = P.grid(100, 40, 2, 584, 5)
check("grid fewer copies than columns", (cols, rows) == (2, 1) and near(jw, 205), (cols, jw))

# 13. machine orientation flags
sq = [([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)], False)]
def hp(**kw):
    c = dict(CFG); c.update(kw)
    return P.build_hpgl(sq, [(0, 0)], 10, 5, c).decode()
check("plain", hp() == "IN;PA;PU0,0;PD400,0;PD400,200;PU0,0;", hp())
check("swap axes", hp(swap_axes=True) == "IN;PA;PU0,0;PD0,400;PD200,400;PU0,0;", hp(swap_axes=True))
check("mirror carriage", hp(mirror_carriage=True) == "IN;PA;PU400,0;PD0,0;PD0,200;PU0,0;", hp(mirror_carriage=True))
check("mirror feed", hp(mirror_feed=True) == "IN;PA;PU0,200;PD400,200;PD400,0;PU0,0;", hp(mirror_feed=True))
check("advance", hp(end_action="advance").endswith("PU0,1200;"), hp(end_action="advance"))
check("advance swapped", hp(end_action="advance", swap_axes=True).endswith("PU1200,0;"))

# 14. overcut and closing
ring = P.close_ring([(0, 0), (10, 0), (10, 10), (0, 10)], 0)
check("close ring", ring == [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
ring = P.close_ring([(0, 0), (10, 0), (10, 10), (0, 10)], 4)
check("overcut part segment", ring[-1] == (4.0, 0.0), ring)
ring = P.close_ring([(0, 0), (10, 0), (10, 10), (0, 10)], 13)
check("overcut past a corner", ring[-2:] == [(10, 0), (10.0, 3.0)], ring)

# 15. ordering keeps every shape and moves up the feed
import random
random.seed(4)
many = []
for i in range(300):
    x, y = random.uniform(0, 500), random.uniform(0, 900)
    many.append(([(x, y), (x + 5, y), (x + 5, y + 5), (x, y + 5)], True))
o = P.order_polys(many)
check("order keeps all shapes", len(o) == 300 and sorted(map(id, [p for p, _ in o])) == sorted(map(id, [p for p, _ in many])))
back = max(max(0, o[i][0][0][1] - o[i + 1][0][0][1]) for i in range(len(o) - 1))
check("order: feed never backs up more than a band", back <= 55.01, back)
def travel(seq):
    pos = (0, 0); t = 0
    for pts, _ in seq:
        t += math.hypot(pts[0][0] - pos[0], pts[0][1] - pos[1]); pos = pts[0]
    return t
check("order shortens travel", travel(o) < travel(many) * 0.35, (travel(o), travel(many)))
openp = [([(100.0, 0.0), (0.0, 0.0)], False), ([(100.0, 10.0), (200.0, 10.0)], False), ([(300.0, 20.0), (200.0, 20.0)], False)]
o = P.order_polys(openp)
check("open paths reversed to nearest end", o[0][0][0] == (0.0, 0.0) and o[1][0][0] == (100.0, 10.0) and o[2][0][0] == (200.0, 20.0), o)

# 16. test pattern and errors
tp, w, h = P.test_design()
check("test design size", (w, h) == (50.8, 50.8) and len(tp) == 3)
for bad in (b"not xml", b"<html></html>", svg("")):
    try:
        P.parse_svg(bad); check("bad svg rejected", False, bad)
    except P.SvgError:
        check("bad svg rejected", True)

# 17. preview path string
d = P.preview_path([([(0, 0), (0.001, 0), (10, 0), (10, 10)], True)], 0.5)
check("preview path", d == "M0.00 0.00L10.00 0.00L10.00 10.00Z", d)

print("\n%d failed" % len(fails) if fails else "\nALL GEOMETRY TESTS PASSED")
sys.exit(1 if fails else 0)
