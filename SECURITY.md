# Security

zombiePlot runs a small web page on a Raspberry Pi so phones and computers on the same network can drive a vinyl cutter. This page describes what it protects against and what it relies on you for.

## Intended setup

- **Home or shop network only.** Don't make it reachable from the internet. Never forward a port to it, and don't put it on a network where you don't trust the other people unless you set a PIN.
- **Plain HTTP.** Traffic on your network isn't encrypted. Nothing secret is sent except the PIN, once per device when you unlock.

## What it protects against

| Risk | Protection |
|---|---|
| Someone else on your network using the cutter | Optional PIN (Settings → Access). Stored only as a salted PBKDF2 hash. Each device unlocks once and gets a long-lived session cookie (`HttpOnly`, `SameSite=Strict`). After 5 wrong tries, logins are locked out for 30 s, rising to 15 minutes. |
| A website you visit making your browser send commands to the cutter (cross-site request forgery) | Every command must carry a custom header that browsers only allow the zombiePlot page itself to send. |
| A website pointing its own domain at your Pi to read from it (DNS rebinding) | Requests are only answered for IP addresses, local names (`.local`, `.lan`, `.home`, `.home.arpa`, `.internal`, names without dots), and names you list in `ZOMBIEPLOT_ALLOWED_HOSTS`. |
| Reading files outside the SVG folders | File requests are resolved and confined to the SVG folder and the upload folder. Uploaded file names are cleaned, and uploads must be valid SVG, up to 25 MB. |
| A fault in the web page leading to control of the Pi | The page runs as its own system user, `zombieplot`, with no login shell. It can use only the plotter cable (groups `lp`, `dialout`) and its own data folder. The root update helper never follows links in that folder and only installs official releases. |
| Tampered or broken updates | Updates come only from this repository over HTTPS, from the exact commit named in `latest.json`. Each one must pass a self-test and report the expected version before it's installed. The previous version is kept so you can go back. Automatic updates are off unless you switch them on. |

## What it relies on

- **The security of the GitHub account that owns this repository.** Anyone who can push here can publish an update, and the update helper runs as root. The owner should keep two-factor authentication on. Leave automatic updates off if you'd rather review each release before installing it.
- **The Pi itself.** Use a strong password, or SSH keys, for its login, and keep Raspberry Pi OS updated.

## Reporting a problem

Open an issue on this repository, or contact the owner privately if the problem shouldn't be public yet.
