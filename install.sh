#!/usr/bin/env bash
# zombiePlot installer for Raspberry Pi OS, Debian or Ubuntu.
# https://github.com/mikedero08/zombie-plot
#
#   Install or reconfigure:  sudo bash install.sh
#   Update quietly:          sudo bash install.sh --upgrade   (used by the built-in updater)
#   Remove:                  sudo bash install.sh --uninstall
#
# Safe to run again: settings, files and the vinyl record are kept.
# An older "plotter" install (the name before zombiePlot) is moved over automatically.

set -euo pipefail

APP=zombieplot
PROG=/opt/$APP
DATA=/var/lib/$APP
ENVFILE=/etc/default/$APP
UNIT=/etc/systemd/system/$APP.service
MARK="# zombieplot-share"
OLD_MARK="# plotter-share"
MNT=/mnt/zombieplot
CRED=/etc/zombieplot-share.cred

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run this with:  sudo bash install.sh"
command -v systemctl >/dev/null || fail "This installer needs systemd (Raspberry Pi OS, Debian or Ubuntu)."

MODE="${1:-}"

drop_share_lines() {
    # Removes our network-folder line from /etc/fstab (and the one from the old "plotter" name).
    local m changed=0
    for m in "$MARK" "$OLD_MARK"; do
        if grep -qx "$m" /etc/fstab 2>/dev/null; then
            sed -i "/^$m\$/,+1d" /etc/fstab
            changed=1
        fi
    done
    if [ "$changed" = 1 ]; then
        systemctl stop mnt-zombieplot.automount mnt-zombieplot.mount mnt-plots.automount mnt-plots.mount 2>/dev/null || true
        systemctl daemon-reload
    fi
}

remove_units() {
    local u
    for u in $APP-carthing.service $APP-autoupdate.timer $APP-update.path $APP.service $APP-update.service $APP-autoupdate.service plotter.service; do
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
}

if [ "$MODE" = "--uninstall" ]; then
    say "Removing zombiePlot"
    remove_units
    rm -f "$ENVFILE" /etc/default/plotter /etc/modules-load.d/$APP.conf /etc/modules-load.d/plotter.conf \
          /etc/NetworkManager/conf.d/$APP-wifi-powersave.conf /etc/NetworkManager/conf.d/plotter-wifi-powersave.conf
    rm -rf "$PROG" /opt/plotter
    drop_share_lines
    rm -f "$CRED" /etc/plotter-share.cred
    echo "Removed. Settings and files sent from the phone are still in $DATA (delete that folder if you want them gone)."
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/$APP.py" ] || fail "$APP.py must be in the same folder as install.sh."

# ---------------------------------------------------------------- 1. program
say "1/8  Installing the program"
command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 3.9 or newer is required. This system has $(python3 --version 2>&1)."
NEWVER="$(python3 "$HERE/$APP.py" --selftest)" || fail "$APP.py failed its self-test. Download it again."

# Carry over an install made under the old name.
OLD_LIB=""
if [ -f /etc/systemd/system/plotter.service ] || [ -d /opt/plotter ]; then
    echo "Moving the earlier \"plotter\" install over to zombiePlot."
    systemctl disable --now plotter.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/plotter.service
    [ -f /etc/default/plotter ] && OLD_LIB="$(sed -n 's/^PLOTTER_LIBRARY=//p' /etc/default/plotter | tail -n 1)"
    # A network folder set up under the old name mounted as user "plotter"; point it at the new user.
    if grep -qx "$OLD_MARK" /etc/fstab 2>/dev/null; then
        sed -i "/^$OLD_MARK\$/{n;s/uid=plotter,gid=plotter/uid=$APP,gid=$APP/}" /etc/fstab
    fi
    if [ -d /var/lib/plotter ] && [ ! -d "$DATA" ]; then
        mv /var/lib/plotter "$DATA"
    fi
    rm -rf /opt/plotter /etc/default/plotter /etc/modules-load.d/plotter.conf \
           /etc/NetworkManager/conf.d/plotter-wifi-powersave.conf
    systemctl daemon-reload
    id plotter >/dev/null 2>&1 && userdel plotter 2>/dev/null || true
fi

id $APP >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin $APP
usermod -aG dialout,lp $APP     # lp: parallel cable (/dev/usb/lp0), dialout: serial cable
install -d -m 755 "$DATA"
install -d -m 755 "$DATA/uploads" "$DATA/files"
chown -R -h $APP:$APP "$DATA"

install -d -m 755 "$PROG" "$PROG/previous"
if [ -f "$PROG/$APP.py" ] && ! cmp -s "$PROG/$APP.py" "$HERE/$APP.py"; then
    cp -p "$PROG/$APP.py" "$PROG/previous/$APP.py"
    [ -f "$PROG/install.sh" ] && cp -p "$PROG/install.sh" "$PROG/previous/install.sh"
fi
if [ "$HERE" != "$PROG" ]; then         # run again from /opt/zombieplot: files are already in place
    install -m 644 "$HERE/$APP.py" "$PROG/$APP.py"
    install -m 644 "$HERE/install.sh" "$PROG/install.sh"
fi
echo "Version $NEWVER is in $PROG, settings in $DATA."

# ---------------------------------------------------------------- 2. cable drivers
say "2/8  Setting up the plotter cable drivers"
echo usblp > /etc/modules-load.d/$APP.conf
modprobe usblp 2>/dev/null && echo "USB-to-parallel driver (usblp) loaded and set to load on every boot." \
    || echo "WARNING: the usblp driver did not load. A USB-to-parallel cable will not work until it does."
ls /dev/usb/lp* >/dev/null 2>&1 && echo "Found parallel cable: $(ls /dev/usb/lp* | head -n 1)"
if dpkg -s brltty >/dev/null 2>&1; then
    apt-get remove -y brltty >/dev/null && echo "Removed brltty (it takes over common USB serial chips)."
fi
if systemctl list-unit-files 2>/dev/null | grep -q '^ModemManager\.service'; then
    systemctl disable --now ModemManager.service >/dev/null 2>&1 || true
    systemctl mask ModemManager.service >/dev/null 2>&1 || true
    echo "Disabled ModemManager (it probes USB serial cables with modem commands)."
fi

# ---------------------------------------------------------------- 3. wifi
say "3/8  Turning off Wi-Fi power saving"
if [ -d /etc/NetworkManager/conf.d ]; then
    printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/$APP-wifi-powersave.conf
    systemctl reload NetworkManager 2>/dev/null || true
    echo "Done (keeps the box reachable when it has been idle)."
else
    echo "NetworkManager not found; skipped."
fi

# ---------------------------------------------------------------- 4. file folder
say "4/8  Where your SVG files live"
LIB="$DATA/files"
CURRENT=""
[ -f "$ENVFILE" ] && CURRENT="$(sed -n 's/^ZOMBIEPLOT_LIBRARY=//p' "$ENVFILE" | tail -n 1)"
[ -z "$CURRENT" ] && CURRENT="$OLD_LIB"
case "$CURRENT" in /var/lib/plotter*) CURRENT="$DATA/files" ;; esac

setup_share() {
    local share user pass opts line
    while true; do
        echo
        echo "Type the network folder that holds your SVG files, for example"
        echo "    //192.168.1.10/plots      (server address / share name)"
        echo "or just press Enter to skip and only send files from the phone or browser."
        read -r -p "Network folder: " share
        share="${share//\\//}"
        if [ -z "$share" ]; then
            drop_share_lines
            rm -f "$CRED" /etc/plotter-share.cred
            LIB="$DATA/files"
            echo "Skipped. Files will be kept on this box in $LIB."
            return
        fi
        case "$share" in //*/*) ;; *) echo "That needs to look like //server/share. Try again."; continue ;; esac
        read -r -p "User name for that folder (Enter if it needs none): " user
        pass=""
        if [ -n "$user" ]; then
            read -r -s -p "Password for $user: " pass; echo
        fi
        command -v mount.cifs >/dev/null || { echo "Installing cifs-utils..."; apt-get update >/dev/null && apt-get install -y cifs-utils >/dev/null; }
        ( umask 077; printf 'username=%s\npassword=%s\n' "${user:-guest}" "$pass" > "$CRED" )
        opts="credentials=$CRED,ro,uid=$APP,gid=$APP,iocharset=utf8"
        [ -n "$user" ] || opts="$opts,guest"
        drop_share_lines
        rm -f /etc/plotter-share.cred
        mkdir -p "$MNT"
        echo "Testing the connection..."
        if mount -t cifs "$share" "$MNT" -o "$opts"; then
            echo "Connected. Found $(find "$MNT" -maxdepth 3 -iname '*.svg' 2>/dev/null | wc -l) SVG file(s) there."
            umount "$MNT"
            line="${share// /\\040} $MNT cifs $opts,nofail,_netdev,x-systemd.automount,x-systemd.idle-timeout=300,x-systemd.mount-timeout=20 0 0"
            printf '%s\n%s\n' "$MARK" "$line" >> /etc/fstab
            systemctl daemon-reload
            systemctl restart mnt-zombieplot.automount
            LIB="$MNT"
            return
        fi
        echo
        echo "Could not connect (the reason is on the line above)."
        echo "Usual causes: wrong server address or share name, wrong user name or password,"
        echo "or the server is not reachable from this box."
        rm -f "$CRED"
    done
}

if [ "$MODE" = "--upgrade" ]; then
    LIB="${CURRENT:-$DATA/files}"
    echo "Keeping: $LIB"
elif [ -n "$CURRENT" ] && [ "$CURRENT" != "$DATA/files" ]; then
    read -r -p "Files are currently read from $CURRENT. Keep that? [Y/n] " keep
    case "$keep" in [nN]*) setup_share ;; *) LIB="$CURRENT" ;; esac
else
    setup_share
fi

# ---------------------------------------------------------------- 5. service
say "5/8  Setting it to start on every boot"
OLDPORT=""
ALLOWED=""
[ -f "$ENVFILE" ] && OLDPORT="$(sed -n 's/^ZOMBIEPLOT_HTTP_PORT=//p' "$ENVFILE" | tail -n 1)"
[ -f "$ENVFILE" ] && ALLOWED="$(sed -n 's/^ZOMBIEPLOT_ALLOWED_HOSTS=//p' "$ENVFILE" | tail -n 1)"
systemctl stop $APP.service 2>/dev/null || true
HTTP_PORT="${OLDPORT:-80}"
if [ -z "$OLDPORT" ] && command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -qE '[:.]80[[:space:]]'; then
    HTTP_PORT=8080
    echo "Something else on this machine already uses web port 80, so zombiePlot will use 8080."
fi
printf 'ZOMBIEPLOT_LIBRARY=%s\nZOMBIEPLOT_HTTP_PORT=%s\n' "$LIB" "$HTTP_PORT" > "$ENVFILE"
[ -z "$ALLOWED" ] || printf 'ZOMBIEPLOT_ALLOWED_HOSTS=%s\n' "$ALLOWED" >> "$ENVFILE"
cat > "$UNIT" <<EOF
[Unit]
Description=zombiePlot vinyl cutter controller
After=network.target

[Service]
User=$APP
Group=$APP
SupplementaryGroups=dialout lp
EnvironmentFile=-$ENVFILE
ExecStart=/usr/bin/python3 $PROG/$APP.py --data $DATA
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 6. updates
say "6/8  Setting up updates"
cat > /etc/systemd/system/$APP-update.service <<EOF
[Unit]
Description=zombiePlot update (requested from the Settings page)

[Service]
Type=oneshot
EnvironmentFile=-$ENVFILE
ExecStart=/usr/bin/python3 $PROG/$APP.py --updater --data $DATA
TimeoutStartSec=45min
EOF
cat > /etc/systemd/system/$APP-update.path <<EOF
[Unit]
Description=Watch for zombiePlot update requests

[Path]
PathExists=$DATA/update-request
Unit=$APP-update.service

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/$APP-autoupdate.service <<EOF
[Unit]
Description=zombiePlot nightly automatic update (only when switched on in Settings)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-$ENVFILE
ExecStart=/usr/bin/python3 $PROG/$APP.py --updater --auto --data $DATA
TimeoutStartSec=45min
EOF
cat > /etc/systemd/system/$APP-autoupdate.timer <<EOF
[Unit]
Description=zombiePlot nightly update check

[Timer]
OnCalendar=*-*-* 03:00
RandomizedDelaySec=90min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable $APP.service $APP-update.path $APP-autoupdate.timer >/dev/null 2>&1
systemctl restart $APP-update.path $APP-autoupdate.timer
echo "Updates can be installed from the Settings page. Automatic overnight updates are off until you switch them on there."
systemctl restart $APP.service

# ---------------------------------------------------------------- 7. car thing
say "7/8  Car Thing remote (optional)"
CT_UNIT=/etc/systemd/system/$APP-carthing.service
WANT_CT=0
if [ "$MODE" = "--upgrade" ]; then
    [ -f "$CT_UNIT" ] && WANT_CT=1
elif [ -f "$CT_UNIT" ]; then
    read -r -p "The Car Thing remote is set up. Keep it? [Y/n] " ans
    case "$ans" in [nN]*) ;; *) WANT_CT=1 ;; esac
else
    echo "A Spotify Car Thing plugged into this box's USB port can work as a remote:"
    echo "pick files with its dial, cut, stop. It must be flashed with adb turned on first (see README)."
    read -r -p "Set up the Car Thing remote? [y/N] " ans
    case "$ans" in [yY]*) WANT_CT=1 ;; esac
fi
if [ "$WANT_CT" = 1 ]; then
    if ! command -v adb >/dev/null; then
        echo "Installing adb..."
        { apt-get update >/dev/null && apt-get install -y adb >/dev/null; } \
            || apt-get install -y android-tools-adb >/dev/null \
            || fail "Could not install adb. Check this box has internet access, then run the installer again."
    fi
    cat > "$CT_UNIT" <<EOF
[Unit]
Description=zombiePlot Car Thing remote (USB)
After=$APP.service

[Service]
EnvironmentFile=-$ENVFILE
StateDirectory=$APP-carthing
Environment=HOME=/var/lib/$APP-carthing
ExecStart=/usr/bin/python3 $PROG/$APP.py --car-thing --data $DATA
ExecStopPost=-$(command -v adb) kill-server
Restart=always
RestartSec=5
PrivateTmp=yes
ProtectHome=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable $APP-carthing.service >/dev/null 2>&1
    systemctl restart $APP-carthing.service
    echo "Car Thing remote is on. Plug the Car Thing into this box; its screen switches to zombiePlot."
    echo "Messages: sudo journalctl -u $APP-carthing -n 30"
else
    if [ -f "$CT_UNIT" ]; then
        systemctl disable --now $APP-carthing.service >/dev/null 2>&1 || true
        rm -f "$CT_UNIT"
        systemctl daemon-reload
        echo "Car Thing remote removed."
    else
        echo "Skipped."
    fi
fi

# ---------------------------------------------------------------- 8. check
say "8/8  Checking"
sleep 4
if ! systemctl is-active --quiet $APP.service; then
    journalctl -u $APP.service -n 20 --no-pager || true
    fail "zombiePlot did not start. The lines above say why."
fi
python3 - "$HTTP_PORT" <<'EOF' || true
import json, sys, time, urllib.request
for attempt in range(10):
    try:
        s = json.load(urllib.request.urlopen("http://127.0.0.1:%s/api/status" % sys.argv[1], timeout=10))
        break
    except Exception as e:
        err = e
        time.sleep(2)
else:
    print("Could not reach the web page on this box: %s" % err)
    sys.exit(0)
p = s["port"]
text = {"ready": "found, plotter is signalling ready",
        "connected": "found",
        "notready": "found, but the plotter is not ready (switched off, no vinyl loaded, or in a menu)",
        "missing": "NOT FOUND - plug the plotter's USB cable into this box"}[p["state"]]
print("Web page:        running, version %s" % s.get("version"))
print("Plotter cable:   %s%s" % (text, (" (" + p["device"] + ", " + str(p.get("link")) + ")") if p.get("device") else ""))
EOF

SUFFIX=""
[ "$HTTP_PORT" = "80" ] || SUFFIX=":$HTTP_PORT"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. On your phone or computer, on the same network, open:"
echo "    http://$(hostname).local$SUFFIX"
[ -z "$IP" ] || echo "    http://$IP$SUFFIX      (use this one if the first does not load)"
echo
echo "It starts by itself every time this box is powered on."
