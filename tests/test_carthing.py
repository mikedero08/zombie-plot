"""Car Thing remote: the adb bridge, against a fake adb that records what it was asked to do."""
import json, os, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if sys.platform == "win32":                 # zombieplot imports these for the cable code only
    import types
    for _m in ("fcntl", "termios"):
        _mod = types.ModuleType(_m)
        _mod.__getattr__ = lambda attr: 0
        sys.modules.setdefault(_m, _mod)
import zombieplot as P

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)[:400]) if detail and not cond else ""))
    if not cond:
        fails.append(name)

tmp = tempfile.mkdtemp(prefix="carthing")
STATE = os.path.join(tmp, "state.json")
FAKE = os.path.join(tmp, "fake_adb.py")
open(FAKE, "w").write(textwrap.dedent(r'''
    import json, os, re, sys
    path = os.environ["FAKE_ADB_STATE"]
    st = json.load(open(path))
    a = sys.argv[1:]
    st["calls"].append(a)
    out, code = "", 0
    if os.environ.get("FAKE_ADB_FAIL") == a[0]:
        out, code = "error: no devices/emulators found", 1
    elif a[0] == "reverse" and a[1] == "--list":
        out = "\n".join("UsbFfs %s %s" % tuple(r) for r in st["reverse"])
    elif a[0] == "reverse":
        st["reverse"] = [a[1:3]]
    elif a[0] == "push":
        st["files"][a[2]] = open(a[1], encoding="utf-8").read()
    elif a[0] == "get-state":
        out = "device"
    elif a[0] == "shell":
        cmd = a[1]
        m = re.match(r"cat (\S+)", cmd)
        if m:
            out = st["files"].get(m.group(1), "")
        elif "mkdir -p" in cmd:
            st["backup"] = True
            out = "ok"
        elif "&& mv " in cmd:
            out = "restored" if st.get("backup") else "nobackup"
            st["backup"] = False
        m = re.match(r"echo (\w+) > (\S+);", cmd)
        if m:
            st["files"][m.group(2)] = m.group(1)
    json.dump(st, open(path, "w"))
    print(out)
    sys.exit(code)
'''))
if sys.platform == "win32":
    ADB = os.path.join(tmp, "adb.cmd")
    open(ADB, "w").write('@"%s" "%s" %%*\n' % (sys.executable, FAKE))
else:
    ADB = os.path.join(tmp, "adb")
    open(ADB, "w").write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
    os.chmod(ADB, 0o755)
os.environ["FAKE_ADB_STATE"] = STATE

def reset(**extra):
    st = {"calls": [], "reverse": [], "files": {}}
    st.update(extra)
    json.dump(st, open(STATE, "w"))
def state():
    return json.load(open(STATE))
def shells(st):
    return [c[1] for c in st["calls"] if c[0] == "shell"]

page = P.car_thing_launcher()
check("launcher opens /thing on the forwarded port", "http://localhost:%d" % P.CAR_THING_PORT in page and "/thing" in page)
check("launcher waits for the box before leaving", "setTimeout(tryit" in page and "location.replace" in page)

reset()
P.car_thing_reverse(ADB, 80)
check("reverse forwards the Car Thing port to the web port", state()["reverse"] == [["tcp:%d" % P.CAR_THING_PORT, "tcp:80"]], state())

reset()
changed = P.car_thing_install_page(ADB)
st = state()
idx = P.CAR_THING_WEBAPP + "/index.html"
check("first connect installs the launcher", changed and st["files"].get(idx) == page, st["files"].keys())
sh = " | ".join(shells(st))
check("root made writable, old screen backed up once", "mount -o remount,rw /" in sh and P.CAR_THING_BACKUP in sh and "[ -e %s ]" % P.CAR_THING_BACKUP in sh, sh)
check("screen restarted and root made read-only again", "supervisorctl restart chromium" in sh and "remount,ro" in sh, sh)
check("marker written", len(st["files"].get(P.CAR_THING_WEBAPP + "/.zombieplot", "")) == 16, st["files"])

n_push = sum(1 for c in st["calls"] if c[0] == "push")
changed = P.car_thing_install_page(ADB)
st = state()
check("second connect leaves the Car Thing alone", not changed and sum(1 for c in st["calls"] if c[0] == "push") == n_push
      and not any("remount,rw" in s for s in shells(st)[-1:]), st["calls"][-2:])

reset(backup=True)
P.car_thing_restore(ADB)
check("restore puts the earlier screen back", any("&& mv %s %s" % (P.CAR_THING_BACKUP, P.CAR_THING_WEBAPP) in s for s in shells(state())), shells(state()))
try:
    P.car_thing_restore(ADB)
    check("restore with nothing saved is refused", False)
except P.CarThingError as e:
    check("restore with nothing saved is refused", "no earlier screen" in str(e), e)

reset()
os.environ["FAKE_ADB_FAIL"] = "reverse"
try:
    P.car_thing_reverse(ADB, 80)
    check("adb failure reported", False)
except P.CarThingError as e:
    check("adb failure reported", "no devices" in str(e), e)
del os.environ["FAKE_ADB_FAIL"]

r = subprocess.run([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--help"], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, universal_newlines=True) if sys.platform != "win32" else None
if r is not None:
    check("--car-thing listed in --help", "--car-thing" in r.stdout and "--car-thing-restore" in r.stdout, r.stdout[-400:])

print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL CAR THING TESTS PASSED")
sys.exit(1 if fails else 0)
