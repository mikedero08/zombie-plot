import json, os, sys, tempfile, math
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P
fails = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + ((" :: " + str(d)[:300]) if d and not c else ""))
    if not c: fails.append(n)

# overlaps / find_spot
check("overlap basic", P.overlaps((0,0,10,10), (5,5,10,10)))
check("touching is not overlap", not P.overlaps((0,0,10,10), (10,0,10,10)))
check("gap counts as overlap", P.overlaps((0,0,10,10), (12,0,10,10), 5) and not P.overlaps((0,0,10,10), (15,0,10,10), 5))
check("empty vinyl -> origin", P.find_spot([], 50, 20, 300, 5) == (0.0, 0.0))
used = [(0,0,100,50)]
check("fills across first", P.find_spot(used, 100, 50, 300, 5) == (105, 0.0), P.find_spot(used, 100, 50, 300, 5))
check("goes up a row when no room across", P.find_spot(used, 250, 50, 300, 5) == (0.0, 55), P.find_spot(used, 250, 50, 300, 5))
used = [(0,0,100,50),(105,0,100,80)]
check("lowest spot wins", P.find_spot(used, 80, 30, 300, 5) == (210, 0.0), P.find_spot(used, 80, 30, 300, 5))
check("fits in gap above short job", P.find_spot(used, 90, 20, 205, 5) == (0.0, 55), P.find_spot(used, 90, 20, 205, 5))
# random: result never overlaps and stays in width
import random
random.seed(7)
ok = True
used = []
for i in range(120):
    w, h = random.uniform(10, 200), random.uniform(10, 120)
    x, y = P.find_spot(used, w, h, 584, 5)
    if x + w > 584 + 1e-6 or any(P.overlaps((x, y, w, h), r, 5) for r in used): ok = False
    used.append((x, y, w, h))
check("120 random jobs: no overlaps, all inside width", ok)
area = sum(r[2]*r[3] for r in used); length = max(r[1]+r[3] for r in used)
check("packing reasonably tight (>55% of vinyl used)", area / (584 * length) > 0.55, area / (584 * length))
check("mirror_x", P.mirror_x((10, 5, 30, 20), 100) == (60, 5, 30, 20))

# grid layout
c, r, offs, jw, jh = P.grid(100, 40, 4, 584, 5, "stacked")
check("stacked layout", (c, r) == (1, 4) and offs[3] == (0, 135) and jw == 100 and jh == 175, (c, r, offs, jw, jh))
c, r, offs, jw, jh = P.grid(100, 40, 4, 584, 5, "across")
check("across layout", (c, r) == (4, 1) and jw == 415 and jh == 40, (c, r, jw, jh))

# hpgl placement and units
sq = [([(0.0, 0.0), (10.0, 0.0)], False)]
cfg = dict(P.DEFAULTS); cfg["overcut_mm"] = 0
hp = P.build_hpgl(sq, [(0, 0)], 10, 5, cfg, base=(100, 50)).decode()
check("base offset", hp == "IN;PA;PU4000,2000;PD4400,2000;PU0,0;", hp)
c2 = dict(cfg); c2["swap_axes"] = True
hp = P.build_hpgl(sq, [(0, 0)], 10, 5, c2, base=(100, 50)).decode()
check("base offset with swapped axes", hp == "IN;PA;PU2000,4000;PD2000,4400;PU0,0;", hp)
hp = P.build_hpgl(sq, [(0, 0)], 10, 5, cfg, base=(100, 50), park=(0, 60)).decode()
check("park", hp.endswith("PU0,2400;"), hp)
c3 = dict(cfg); c3["end_action"] = "advance"
hp = P.build_hpgl(sq, [(0, 0)], 10, 5, c3, base=(0, 50)).decode()
check("advance measured from the job's end", hp.endswith("PU0,3200;"), hp)
c4 = dict(cfg); c4["steps_per_mm"] = 100
hp = P.build_hpgl(sq, [(0, 0)], 10, 5, c4).decode()
check("units per mm setting", hp == "IN;PA;PU0,0;PD1000,0;PU0,0;", hp)

# App.plan end to end
tmp = tempfile.mkdtemp(); lib = os.path.join(tmp, "lib"); os.makedirs(lib)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4in" height="2in" viewBox="0 0 400 200"><rect width="400" height="200"/></svg>'
open(os.path.join(lib, "a.svg"), "wb").write(SVG)
app = P.App(os.path.join(tmp, "data"), lib)
app.cfg.update({"vinyl_width_mm": 300, "copy_gap_mm": 5, "overcut_mm": 0})
p = app.plan({"id": "lib:a.svg", "copies": 1})
check("first job at origin", p["base"] == (0.0, 0.0) and not p["clash"])
app.session.add(p["rects"], p["name"])
p = app.plan({"id": "lib:a.svg", "copies": 1})
check("second job beside the first", abs(p["base"][0] - 106.6) < 1e-6 and p["base"][1] == 0.0, p["base"])
p2 = app.plan({"id": "lib:a.svg", "copies": 1, "place": "origin"})
check("at-origin over used area warns", p2["clash"] and "already cut" in p2["warnings"][0] and "(a)" in p2["warnings"][0], p2["warnings"])
app.session.add(p["rects"], p["name"])
p = app.plan({"id": "lib:a.svg", "copies": 1})
check("third job goes up a row (vinyl only 300 wide)", p["base"] == (0.0, 55.8) or (abs(p["base"][1] - 55.8) < 1e-6 and p["base"][0] == 0.0), p["base"])
p = app.plan({"id": "lib:a.svg", "copies": 3})
check("3 copies across do not fit 300 mm, so 2 across 2 rows", (p["cols"], p["rows"]) == (2, 2), (p["cols"], p["rows"]))
p = app.plan({"id": "lib:a.svg", "copies": 3, "layout": "stacked"})
check("stacked copies", (p["cols"], p["rows"]) == (1, 3) and abs(p["job_h"] - (3*50.8 + 10)) < 1e-6)
# far side
app.session.clear()
app.cfg.update({"start_corner": "far"})
p = app.plan({"id": "lib:a.svg", "copies": 1})
check("far side: first job against the far edge", abs(p["base"][0] - (300 - 101.6)) < 1e-6 and p["base"][1] == 0, p["base"])
app.session.add(p["rects"], p["name"])
p = app.plan({"id": "lib:a.svg", "copies": 1})
check("far side: second job next to it, toward the origin", abs(p["base"][0] - (300 - 101.6 - 5 - 101.6)) < 1e-6, p["base"])
check("far side rects stored from origin", abs(app.session.rects()[0][0] - 198.4) < 1e-6, app.session.rects())
hp = P.build_hpgl(p["polys"], p["offsets"], p["job_w"], p["job_h"], p["cfg"], base=p["base"]).decode()
check("far side hpgl starts at the offset", hp.startswith("IN;PA;PU%d," % round((300 - 208.2) * 40)), hp[:40])
# too wide for vinyl
app.cfg.update({"start_corner": "origin", "vinyl_width_mm": 80})
p = app.plan({"id": "lib:a.svg", "copies": 1, "rot": 0})
check("too wide for vinyl", not p["fits"] and "vinyl width" in p["warnings"][0], p["warnings"])
p = app.plan({"id": "lib:a.svg", "copies": 1, "rot": "auto"})
check("auto-rotate to vinyl width", p["rot"] == 90 and p["fits"])
# vinyl wider than plotter is capped
app.cfg.update({"vinyl_width_mm": 2000})
p = app.plan({"id": "lib:a.svg", "copies": 10})
check("vinyl width capped at cutting width", p["vinyl_w"] == 584.0 and p["cols"] == 5, (p["vinyl_w"], p["cols"]))
# persistence and undo
app.session.clear(); app.session.add([(0, 0, 10, 10)], "x"); app.session.add([(20, 0, 10, 10), (40, 0, 10, 10)], "y")
app2 = P.App(os.path.join(tmp, "data"), lib)
check("session survives restart", len(app2.session.rects()) == 3 and app2.session.snapshot()["jobs"] == 2)
app2.session.undo()
check("undo removes the whole last job", app2.session.rects() == [(0, 0, 10, 10)])
open(os.path.join(tmp, "data", "session.json"), "w").write("garbage")
check("corrupt session file ignored", P.App(os.path.join(tmp, "data"), lib).session.rects() == [])
# the 1.8 front left / front right choice is turned back into the switches
pth = os.path.join(tmp, "old.json")
for stored, want in (({"orientation": "right", "swap_axes": True}, (True, True, False)), ({"orientation": "left", "swap_axes": True}, (False, False, False)),
                     ({"orientation": "custom", "swap_axes": True}, (True, False, False)), ({"swap_axes": True, "mirror_feed": True}, (True, False, True))):
    json.dump(stored, open(pth, "w"))
    got = P.machine_flags(P.Config(pth).snapshot())
    check("1.8 setting %s read as switches %s" % (stored, want), got == want, got)
# preview tells the page which corner the start point is in
app.cfg.update({"swap_axes": True, "mirror_carriage": True, "mirror_feed": False, "vinyl_width_mm": 300, "start_corner": "origin"})
app.session.clear()
pv = app.preview({"id": "lib:a.svg", "copies": 2})
check("preview: start point front right", (pv["origin_side"], pv["origin_row"]) == ("right", "front") and len(pv["rects"]) == 2, (pv["origin_side"], pv["rects"]))
app.cfg.update({"swap_axes": False, "mirror_carriage": False})
# the setup test cut ignores the switches and the sheet memory, and is not remembered
app.cfg.update({"swap_axes": True, "mirror_carriage": True}); app.session.add([(0, 0, 100, 100)], "x")
ps = app.plan({"id": "test", "setup": True})
check("setup test: switches off, at the start point", ps["setup"] and P.machine_flags(ps["cfg"]) == (False, False, False) and ps["base"] == (0.0, 0.0) and not ps["clash"], ps["base"])
pn = app.plan({"id": "lib:a.svg", "setup": True})
check("setup flag only works for the test logo", not pn["setup"])
app.cfg.update({"swap_axes": False, "mirror_carriage": False}); app.session.clear()
check("status reports the start point side", app.status()["vinyl"]["origin_side"] == "left")
print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL PLACEMENT TESTS PASSED")
sys.exit(1 if fails else 0)
