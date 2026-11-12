# zombiePlot

Bring an old Roland CAMM-1 vinyl cutter back to life: control it from your phone, tablet or computer over Wi-Fi.

A small Raspberry Pi sits next to the cutter and stays switched on. You open a web page on your phone, pick an SVG file, and tap CUT. There's no software to install on the phone, no USB cable to a computer, and nothing to set up again after a power cut.

**Phone, tablet or computer** → Wi-Fi → **Raspberry Pi** → USB cable → **vinyl cutter**

**What the page does**

- Lists your SVG files, from a network folder or sent straight from the phone.
- Shows a preview of the cut on the vinyl, with its size.
- Rotate, mirror, and copies laid out side by side or stacked.
- Remembers where you have already cut on the current vinyl, puts the next job in the next free spot, and warns you before you cut over used vinyl.
- Can start jobs from either edge of the vinyl.
- Cut and Stop buttons, plus a test square for checking orientation.
- Updates itself from this repository, from the Settings page or automatically overnight.

**Files in this repository**

| File | What it is |
|---|---|
| `zombieplot.py` | The program. It needs only Python, which Raspberry Pi OS already has. |
| `install.sh` | Sets it up on the Pi and makes it start on every boot. |
| `get.sh` | The one-line installer: downloads the newest release and runs `install.sh`. |
| `latest.json` | Says which release is the newest. The updater reads it. |
| `tests/` | Automated tests. |

---

## Will it work with my cutter?

| | |
|---|---|
| **Tested** | Roland CAMM-1 PNC-1210, connected with a Prolific PL2305 USB-to-parallel cable (sold as "USB to IEEE-1284" or "USB to Centronics" printer cables). |
| **Should work** | Other Roland CAMM-1 cutters of the same era that take CAMM-GL III / HP-GL commands, such as the PNC-900, 910, 950, 960, 1000, 1100, 1200, 1410, 1600, 1850 and 1860. Connect with a USB-to-parallel cable or a USB-to-serial cable. |
| **Probably works** | Other cutters and pen plotters that accept HP-GL (the `PU`/`PD` commands) over a parallel or serial port. If yours uses a step other than 0.025 mm, change "Plotter units per mm" in Settings. |
| **Will not work** | Cricut, Silhouette, Brother ScanNCut and other cutters with closed protocols. Also cutters that only work through their own Windows driver. |

The cable has to appear on the Pi as a printer port (`/dev/usb/lp0`) or a serial port (`/dev/ttyUSB0`). Both kinds of cable described above do.

---

## What you need

- **A Raspberry Pi.** Any model works, including the original 2014 Model B+. Older models without built-in Wi-Fi need a USB Wi-Fi adapter or a network cable.
- **A microSD card**, 8 GB or larger, and a computer that can write to it.
- **The Pi's power supply.**
- **The USB cable** that connects to your cutter.
- **A Windows or Mac computer** on the same network, for the one-time setup.

---

## Step 1: Make the SD card

1. Install **Raspberry Pi Imager** from https://www.raspberrypi.com/software/ and open it.
2. **Choose Device:** your Pi model.
3. **Choose OS:** **Raspberry Pi OS (other)**, then **Raspberry Pi OS Lite (32-bit)**.
4. **Choose Storage:** the SD card.
5. When Imager offers to customise settings, say yes and set all of these:
   - **Hostname:** `zombieplot`
   - **User name and password:** your choice. These instructions use the user name `pi`; if you pick another, use yours wherever you see `pi`.
   - **Wireless LAN:** your Wi-Fi name, password and country. Skip this if you use a network cable.
   - **Services:** turn on **SSH** with **password authentication**. Without this you can't log in from your computer.
6. Write the card, put it in the Pi, connect the cutter's USB cable, and power the Pi on.

The first start takes 3 minutes on a recent Pi and up to 10 minutes on an original Model B+.

## Step 2: Find the Pi on your network

On your computer, open **PowerShell** (Windows: Start menu, type `powershell`) or **Terminal** (Mac), and run:

```
ping zombieplot.local
```

If it replies, you can use `zombieplot.local` as the Pi's address. If it doesn't, look in your router's list of connected devices for one named `zombieplot` and use its IP address (for example `192.168.1.57`) everywhere these steps say `zombieplot.local`.

While you're in the router, give the Pi a fixed IP address (often called a DHCP reservation) so the address never changes.

## Step 3: Log in to the Pi

```
ssh pi@zombieplot.local
```

The first time, it asks "Are you sure you want to continue connecting?" Type `yes`, then the Pi's password. Your prompt changes to something like `pi@zombieplot:~ $`.

## Step 4: Install

At the Pi's prompt, run this one line. It downloads the newest release from GitHub and installs it:

```
curl -fsSL https://raw.githubusercontent.com/mikedero08/zombie-plot/main/get.sh | sudo bash
```

It asks one question: the network folder that holds your SVG files (see "Network folder" below). Press Enter to skip it for now. At the end it prints the page address and whether it found the cutter's cable:

```
Web page:        running, version 1.2
Plotter cable:   found, plotter is signalling ready (/dev/usb/lp0, parallel)
```

Type `exit` to log out. You won't need to log in again for normal use; updates are installed from the Settings page.

**If the Pi has no internet access:** on your computer, download this repository as a zip (green **Code** button, **Download ZIP**), unzip it, open PowerShell or Terminal in that folder, and copy the two files across. The colon at the end is required:

```
scp zombieplot.py install.sh pi@zombieplot.local:
```

Then log in as in Step 3 and run `sudo bash install.sh`.

## Step 5: Open it on your phone

On your phone, connected to the same Wi-Fi, open `http://zombieplot.local`, or `http://` followed by the Pi's IP address.

To get an app icon:
- **Android Chrome:** menu (three dots), **Add to Home screen**.
- **iPhone Safari:** Share button, **Add to Home Screen**.

## Step 6: Cut the test square

1. Load scrap vinyl and set the start point on the cutter as you normally do.
2. Open **Settings**, tap **Cut test square**. It cuts a 2 inch square with a letter F inside.
3. Stand in front of the cutter and check the F:
   - **Reads normally:** you're done.
   - **Lying on its side:** tick **Swap the two axes** and cut again.
   - **Backwards:** tick **Mirror left to right** and cut again.
   - **Upside down:** tick **Mirror front to back** and cut again.
4. Measure the square. It should be 2 inches.

---

## Using it

### The vinyl bar

The bar at the top shows the width of the vinyl loaded now and how much of it you've used.

- **Width:** type the usable width of the vinyl, meaning how far across the cutter can cut on it. The cutter measures the vinyl when you load it, but it can't report that back over the cable, so the page needs you to enter it. Change it when you load a different width.
- **New vinyl:** tap this after loading fresh vinyl or setting a new start point on the cutter. It forgets the areas already cut.
- **Undo last:** forgets the last job's area, if that job didn't actually cut.

### Cutting a file

1. Tap a file. The preview shows the whole vinyl width, the start point (orange dot), areas already cut (grey), and the new job.
2. Under the preview: the size of one copy, and the total size when there's more than one.
3. **Rotate** turns it a quarter turn per tap. **Mirror** flips it, for decals applied inside glass.
4. **- / +** sets the number of copies. **Side by side** fills across the vinyl first. **Stacked** puts them one after another along the vinyl.
5. **Where on the vinyl:**
   - **Next free spot** (normal) places the job clear of everything already cut.
   - **At the start point** cuts at the start point, and warns you if that area was already used.
6. Tap **CUT**, then tap it again within 4 seconds. The second tap stops a stray touch from wasting vinyl.
7. **Stop** stops sending and tells the cutter to drop the rest of the job.

### Where the page gets its positions

All positions are measured from the cutter's start point (origin). The page moves each job by changing its coordinates; it never moves the cutter's start point. If you press the cutter's origin button, reload vinyl, or the vinyl slips, the remembered areas no longer line up with the vinyl. Tap **New vinyl** after any of those.

---

## Settings

| Setting | What it does | Starts at |
|---|---|---|
| Usable vinyl width | Same as Width on the vinyl bar. | Plotter width |
| Start jobs from | The start point side of the vinyl, or the far side. With the far side, jobs sit against the opposite edge. Uses the vinyl width, so keep that accurate. | Start point side |
| Gap between copies and jobs | Space left between copies and between jobs. | 5 mm |
| When the job ends | Return to the start point; move to the next free spot so you can see where the next job goes; or feed the vinyl past the job so you can cut it off. | Return |
| Overcut on closed shapes | Cuts slightly past the start of each closed shape so the corner weeds cleanly. 0 turns it off. | 1 mm |
| Show sizes in | Inches or millimetres. | inches |
| Widest the plotter can cut | The machine's limit. The vinyl width can't be set wider than this. PNC-1210: 584 mm (23 in). | 584 mm |
| Swap / Mirror | Orientation, set once with the test square. | off |
| Cable type | Parallel or serial; automatic finds it. | automatic |
| Flow control, Baud rate | Serial cables only; must match the cutter's serial settings. | Hardware, 9600 |
| Port | `auto` finds the cable. | auto |
| Plotter units per mm | 40 for Roland CAMM-1 and standard HP-GL (0.025 mm steps). | 40 |
| Pixels per inch | Only for SVG files that contain no inch or mm size. The page warns when a file is like that. | 96 |

Force and speed are not sent. Set them on the cutter as usual.

---

## Network folder (optional)

The Pi can read SVG files from a shared folder on your network (SMB / Windows file sharing) instead of you sending them from the phone. Read-only: the Pi can't change or delete anything there.

You need:
- **The share path**, written `//server/share`, for example `//192.168.1.10/plots`. If Windows opens it as `\\192.168.1.10\plots`, it's the same thing with the slashes flipped.
- **A user name and password** that can read it.

To add or change it, log in to the Pi and run the installer again (it keeps your settings):
```
ssh pi@zombieplot.local
```
```
sudo bash /opt/zombieplot/install.sh
```

Sub-folders are listed too, up to three levels deep, newest files first. Save your SVGs into that folder from your design program, and they show up in the list.

---

## Updates

Open **Settings**. The **Updates** section shows the installed version.

- **Check for updates** asks GitHub for the newest release. If there is one, an **Install version …** button appears, and the files screen shows a notice too.
- **Install** downloads the new version, checks that it runs, installs it, and restarts. The page reloads by itself when it's done, a few minutes on an older Pi. Settings, files and the vinyl record are kept.
- **Install updates automatically** does the same overnight, between 3:00 and 4:30. It never updates while a job is being sent.
- **Go back to version …** puts the previous version back if a new one misbehaves.

Only published releases are offered, never half-finished changes. If a download is damaged or fails its self-test, it isn't installed and the old version keeps running.

The updater needs the Pi to reach `raw.githubusercontent.com`. To update by hand instead, log in and run the one-line installer from Step 4 again.

---

## Troubleshooting

**`ssh` says "Connection refused".** SSH isn't turned on. Either redo the card with SSH ticked in Imager, or put the SD card in your computer, open the drive named **bootfs**, and create an empty file named `ssh` with no extension. In PowerShell, replace `E:` with that drive's letter:
```
New-Item -Path E:\ssh -ItemType File
```
Put the card back in the Pi and power it on.

**`ssh` says "REMOTE HOST IDENTIFICATION HAS CHANGED".** Another device used that address before. Clear the old entry, then try again:
```
ssh-keygen -R zombieplot.local
```

**The one-line install says it can't read the release information.** The Pi can't reach GitHub. Check it's on Wi-Fi with internet access, or use the no-internet route in Step 4.

**The page doesn't load.** Check the Pi has power and has had a few minutes to start. Try the IP address instead of `zombieplot.local`. To see the program's own messages, log in and run:
```
sudo journalctl -u zombieplot -n 50
```

**Status says "No cable".** Check the cable is plugged into the Pi. To see what the Pi detects:
```
ls -l /dev/usb/ /dev/serial/by-id/
```
`lp0` means a parallel cable was found, and anything under `by-id` means a serial cable was found.

**"Cannot open /dev/usb/lp0: Permission denied".** Run the one-line installer from Step 4 again.

**Status says "Not ready" while the cutter is on with vinyl loaded.** Some cables report status wrongly. If cutting still works, ignore it.

**Cut comes out the wrong size.** Compare the size on the page with your design program. If they differ, the SVG has no real units in it; try 72 or 90 for Pixels per inch in Settings.

**Parts of the drawing are missing.** The page lists what it skipped. Convert text to curves before exporting, and remove pictures (bitmaps), which can't be cut.

**Stop says the buffer is full.** Over a parallel cable the cutter only takes the stop command when its buffer has room. Switch the cutter off to stop a large job.

**The file list says it can't read the folder.** The server or share is offline. Files sent from the phone still work.

**An update failed.** Settings shows the reason, and the old version keeps running. Logs from the updater:
```
sudo journalctl -u zombieplot-update -n 50
```

**Remove everything.** Log in and run:
```
sudo bash /opt/zombieplot/install.sh --uninstall
```

---

## What has been tested

- **On real hardware:** a Raspberry Pi Model B+ (2014) with a USB Wi-Fi adapter, a Roland CAMM-1 PNC-1210, and a Prolific PL2305 USB-to-parallel cable. The test square and real cuts came out in the correct orientation with no settings changed.
- **With simulated cables:**
  - Reading SVG files: lines, curves, arcs, shapes, transforms, hidden layers and units.
  - The exact commands sent to the cutter.
  - Copies, placement on the vinyl, the far-side option, the warning for used vinyl.
  - Stop, a cable unplugged and plugged back in, and restarts.
  - The page in a phone-sized browser.
- **Not tested:**
  - Other cutter models.
  - Serial cables on real hardware.
  - Whether the Stop command (`ESC .K`) works over a parallel cable on every model.

---

## For developers

Run the tests (Python 3.9+, standard library only; `test_ui.py` also needs Playwright with Chromium):

```
for t in geometry placement updater server parallel; do python3 tests/test_$t.py; done
```

To publish a release:

1. Set `VERSION` in `zombieplot.py` (for example `"1.3"`) and commit.
2. In a second commit, set `latest.json` to `{"version": "1.3", "ref": "<full hash of the first commit>", "notes": "..."}`.
3. Push both commits together.

Pis read `latest.json` from `main` and download `zombieplot.py` and `install.sh` from the commit it names. A commit can't be changed after the fact, so a release can't shift under anyone. The version in `latest.json` must match `VERSION` in that commit's file, or the updater refuses it. A tag name works in place of `ref` too (`"tag": "v1.3"`).
