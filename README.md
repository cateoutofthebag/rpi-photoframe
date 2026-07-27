# rpi-photoframe

A digital picture frame for a **Raspberry Pi Zero 2 W** driving a portable HDMI
monitor. Photos are added remotely from any browser on your network, and the
clock and weather can be overlaid on the photo — always, on a schedule, or on
demand.

```
  ┌───────────────────────────────┐
  │                               │
  │        [ your photo ]         │
  │                               │
  │  ┌──────────────┐             │
  │  │ 14:32        │             │
  │  │ Sunday 26 July             │
  │  │ 20°C  Mainly clear 26°/19° │
  │  └──────────────┘             │
  └───────────────────────────────┘
```

## What it does

- **Slideshow** — shuffled or in order, adjustable interval, crossfade between
  photos, and three ways to handle photos that don't match the screen shape:
  blurred edges (default), letterboxed, or filled-and-cropped.
- **Remote uploads** — a phone-friendly web page for drag-and-drop uploads,
  with a thumbnail grid for deleting photos you're bored of.
- **Network sources** — pull photos automatically from an SMB share, a
  PhotoPrism album, a public iCloud shared album, or a watched folder. Add one
  upstream and it appears; delete one upstream and it goes.
- **Web mirror** — a live copy of the screen at `/mirror`, for checking the
  frame from your desk or driving a second display.
- **Overlay** — clock, date, and current weather, in any corner. Show it
  always, on a daily schedule, or press *Show info 10 min* when you actually
  want to know the time.
- **Weather** from [Open-Meteo](https://open-meteo.com) — no account, no API
  key. Set the location from the settings page (or let the browser fill it in).
- **Quiet hours** — blank the panel overnight, optionally powering the HDMI
  output down entirely.
- **Live controls** — next, previous, pause, straight from the web page.

Everything runs in **one process**: the display loop on the main thread, the
web server and weather poller on background threads. On a 512 MB board that
matters, and it means no database or IPC — the threads share the same objects.

## Hardware

- Raspberry Pi Zero 2 W (a Pi 3/4/5 works too, and will feel snappier)
- Portable HDMI monitor, plus a mini-HDMI-to-HDMI cable for the Zero
- A picture frame with the depth to swallow the monitor and the Pi
- A decent 5 V supply — undervoltage on a Zero 2 W shows up as random freezes

## Install (Raspberry Pi OS **Lite**, Bookworm)

Use Lite. There's no need for a desktop: the frame renders straight to the
screen through KMS/DRM, which leaves far more of the 512 MB for photos.

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pygame python3-pil python3-numpy libopenjp2-7
```

`python3-pygame` from apt is strongly preferred — building pygame with pip on a
Zero 2 W takes the better part of an hour.

```bash
git clone https://github.com/cateoutofthebag/rpi-photoframe.git
cd rpi-photoframe
python3 -m venv --system-site-packages .venv   # --system-site-packages picks up apt's pygame
.venv/bin/pip install Flask requests
.venv/bin/python run.py
```

Open the URL it prints (`http://<pi-address>:8080`) and start uploading.

### iPhone photos (HEIC)

HEIC uploads are rejected unless `pillow-heif` is installed. It has no
prebuilt ARM wheel, so it compiles from source:

```bash
sudo apt install -y libheif-dev && .venv/bin/pip install pillow-heif
```

If you'd rather not, set your iPhone to *Settings → Camera → Formats → Most
Compatible* and it'll send JPEGs.

## Try it in Docker first

Handy before the Pi exists, or for developing on a laptop. There's no
framebuffer in a container, so it runs the display loop against SDL's dummy
driver — the slideshow, transitions, overlay, quiet hours and source syncing
all behave exactly as they will on the Pi, and you watch it at `/mirror`
instead of on an HDMI panel.

```bash
docker compose up -d --build
```

Then open `http://localhost:8080/` to add photos and `http://localhost:8080/mirror`
to watch the frame. Set `PHOTOFRAME_URL` in `docker-compose.yml` if you're
reaching it from another machine.

Anything you drop in `./photos` can be imported by adding a *Watched folder*
source pointing at `/photos` — the quickest way to exercise the sync machinery
without a NAS.

Photos and settings live in a named volume, so `docker compose down` keeps
them; `docker compose down -v` starts over.

The image is Python 3.11 on Debian Bookworm, matching Raspberry Pi OS, with
DejaVu fonts so the overlay renders exactly as it will on the frame. Every
dependency has a prebuilt wheel on both amd64 and arm64, so no compiler is
needed and the build takes about a minute.

This is a functional stand-in, not a performance one: a container on a desktop
tells you nothing about how a Zero 2 W will cope.

## Run it as a service

```bash
sudo cp systemd/photoframe.service /etc/systemd/system/
sudoedit /etc/systemd/system/photoframe.service   # check User= and the paths
sudo systemctl enable --now photoframe
journalctl -u photoframe -f
```

The unit adds the service user to `video`, `render` and `input`, which KMSDRM
needs. If the screen stays black, that's the first thing to check.

## Network sources

Add these under *Network sources* on the web page. Each is checked on its own
interval, photos are copied to the frame, and the copy is kept in step with
the source — remove a photo upstream and it disappears here on the next check
(switchable per source). Photos you uploaded by hand are never touched, even
if a source also happens to supply the same picture.

Sources are polled one at a time, with a pause between downloads, so a first
sync of several hundred photos doesn't starve the slideshow.

### SMB / network share

Speaks SMB2/3 directly through `smbprotocol`, so there's no mount to set up,
no `fstab` entry, and no boot hanging because the NAS is off.

```bash
.venv/bin/pip install smbprotocol
```

Give it a server, a share, and optionally a folder within the share; it
recurses up to six levels deep. Leave the username blank for a guest share.

### PhotoPrism album

Point it at your server and name an album — by title (`Holidays`) or by UID.
An **app password** is the easiest credential: in PhotoPrism, *Settings →
Account → Apps and Devices*. A username and password works too.

Photos come through PhotoPrism's thumbnailer at your chosen size rather than
as originals, so they arrive already scaled and correctly rotated — much
kinder to the Pi and to your network than pulling 12 MP JPEGs. `fit_1920` suits
a 1080p panel. Pick `original` only if you want the untouched file.

### iCloud shared album

Needs a **public** album link: in Photos, share an album, then turn on *Public
Website* and copy the `https://www.icloud.com/sharedalbum/#B0...` URL. Anyone
who can add photos to that album can then add photos to your frame, which is a
rather good way to let family contribute.

This uses the same unofficial JSON service the iCloud share page itself uses.
It needs no account and has been stable for years, but Apple doesn't document
or promise it. Private iCloud libraries aren't supported and won't be — that
needs a full two-factor session, which a picture frame has no business holding.

### Watched folder

Any directory on the Pi: a USB stick, an OS-level mount, or a folder kept in
step by rclone or Syncthing.

### A note on Google Photos

There's no Google Photos source, because there can't usefully be one any more.
Google removed the `photoslibrary.readonly` scope on 31 March 2025; third-party
apps can now only read media they uploaded themselves, and the replacement
Picker API needs a human to hand-pick items in a browser every time. Neither is
any use to a frame that's meant to run unattended for months.

The workable route is a watched folder fed by something else — a Google Takeout
export, or a sync tool you already trust — pointed at the *Watched folder*
source above.

## Web mirror

`http://<pi-address>:8080/mirror` shows exactly what's on the panel, overlay
and all, with the same prev/pause/next controls. It's genuinely the frame's
own output rather than a reconstruction: the display loop hands over the
composed screen and the server encodes it as a JPEG.

It's built to be left up on a spare monitor — the chrome fades out after a few
seconds of no input, and there's a fullscreen button and a fit/fill toggle.

The mirror costs nothing when nobody's watching. Frames are only captured while
a viewer has asked for one in the last 30 seconds, and the page long-polls, so
it updates the instant the picture changes rather than re-fetching on a timer.

Testing off the Pi is the other use:

```bash
.venv/bin/python run.py --headless
```

That renders to an off-screen buffer, so the whole slideshow — transitions,
overlay, quiet hours — runs and can be watched at `/mirror` on any machine,
with no display attached and no Pi involved.

Turn the mirror off, or cap its width to save bandwidth, under *Settings → Web
mirror*.

## Settings

All of these are on the web page, and take effect immediately — no restart.
They're stored in `data/config.json`.

| Setting | Default | Notes |
| --- | --- | --- |
| Seconds per photo | 90 | |
| Crossfade | 0.8 s | 0 disables it; the Zero 2 W manages ~30 fps at 1080p |
| Fill mode | blurred edges | or `contain` (black bars) / `cover` (crops) |
| Shuffle | on | reshuffles each time it's been all the way round |
| Overlay | on a schedule, 07:00–22:30 | or always / never |
| Overlay corner, size | bottom-left, 1.0 | |
| Weather location | London | the *Use this device's location* button fills it in |
| Rotation | 0° | for a frame hung in portrait |
| Quiet hours | off | blanks the screen, and cuts HDMI power if enabled |
| Web mirror | on, quality 80 | max width 0 sends at the screen's own size |

A window whose start and end are equal is treated as *never*.

### Password

The web page is open to anyone on your network by default. To require a
password (any username):

```bash
PHOTOFRAME_PASSWORD=somethingsecret .venv/bin/python run.py
```

Don't expose the frame to the internet — it's plain HTTP basic auth, fine for a
home LAN and nothing more.

Source credentials are stored in `data/config.json`, which is written
owner-only (`0600`). They're never sent to the browser: the settings page shows
a placeholder, and saving the page back unchanged keeps what's already stored.

## How photos are stored

Under `data/` (git-ignored):

```
data/
├── config.json    settings and source credentials (chmod 600)
├── index.json     the photo index, including where each photo came from
├── sync.json      what each source has already contributed
├── originals/     photos, downscaled to 3200 px max
├── thumbs/        480 px, for the web grid
└── cache/         photos composed at exactly screen size
```

Uploads are downscaled and re-encoded on arrival, and each photo is composed
to an exact screen-sized JPEG the first time it's shown — so the display loop
never decodes a 12 MP image or scales anything. A background warmer builds the
rest of the cache at low priority, then deletes renders left over from an
earlier screen size or fill mode.

Uploading the same photo twice is a no-op: photos are keyed by content hash.

## HTTP API

Handy for scripting — a cron job that posts the photo of the day, say.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | current photo, paused state, weather |
| `GET` | `/api/photos` | list photos |
| `POST` | `/api/photos` | upload (multipart, field `photos`, repeatable) |
| `DELETE` | `/api/photos/<id>` | delete one |
| `GET` | `/api/config` | current settings |
| `PUT` | `/api/config` | partial settings update (deep-merged, validated) |
| `POST` | `/api/control` | `{"action": "next\|previous\|pause\|resume\|toggle_pause\|reload\|info_burst"}` |
| `GET` | `/api/weather` | last weather reading |
| `GET` | `/api/sources` | configured sources with sync status |
| `POST` | `/api/sources/<id>/test` | check a source's settings |
| `POST` | `/api/sources/<id>/sync` | sync one source now |
| `POST` | `/api/sync` | sync everything now |
| `GET` | `/api/frame.jpg` | current screen; `?since=<n>` long-polls for the next change |

```bash
curl -F "photos=@beach.jpg" http://frame.local:8080/api/photos
curl -X PUT http://frame.local:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"slideshow": {"interval_seconds": 30}}'
```

A bad file in a multi-file upload is reported in `errors` without failing the
rest of the batch.

## Keyboard

If you plug a keyboard in: `→`/`space` next, `←` previous, `p` pause, `i` show
the overlay for 10 minutes, `q`/`esc` quit.

## Developing off the Pi

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python run.py --windowed          # slideshow in a window
.venv/bin/python run.py --headless          # no window; watch it at /mirror
.venv/bin/python run.py --no-display        # web UI only, no pygame needed
.venv/bin/python run.py --no-sync           # leave network sources alone
.venv/bin/python -m pytest tests/ -q
```

On Python 3.13+ there may be no `pygame` wheel yet; `pip install pygame-ce`
is a drop-in replacement that imports as `pygame`.

## Troubleshooting

**Black screen, service running.** Almost always permissions: the user needs
`video` and `render`. Check `journalctl -u photoframe` for the line reporting
the driver — it should say `kmsdrm`.

**It picked the wrong resolution.** Force it in `/boot/firmware/cmdline.txt`
with `video=HDMI-A-1:1920x1080@60`, or set `hdmi_group`/`hdmi_mode` in
`config.txt`. Portable monitors often report odd EDID modes.

**Screen blanks by itself.** Console blanking is separate from quiet hours:
`consoleblank=0` on the kernel command line turns it off.

**First run through the photos is sluggish.** That's the render cache being
built. It settles after one pass — or seed it by leaving the frame running for
a few minutes after a bulk upload.

**Weather says "offline".** The frame keeps showing the last reading it got and
retries with a backoff; check DNS and the clock (`timedatectl`) — a wrong clock
breaks TLS.

**A source won't connect.** Press *Test* next to it — the message is whatever
the server actually said. A source that fails is recorded and retried on its
normal schedule; it never blocks the slideshow or the other sources.

**I deleted a synced photo and want it back.** Deleting one in the web grid is
permanent for that source — the frame remembers it has already seen it and
won't fetch it again. Remove the source and add it back to start over.

**The mirror says "frame not running".** The web server is up but the display
loop isn't — you're running with `--no-display`. Use `--headless` instead.
