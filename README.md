# blackmagic_RD_sync

Periodic, autonomous sync of Blackmagic camera clips from the camera's USB
disk → H.265 (`.mp4`) on the SignCollect research drive, with safe
camera-side cleanup.

## What it does

For every `.braw` clip on the camera's USB disk, on each cycle:

1. **Download** the clip through the bmcam server (`/api/download/...`) into
   a local staging directory.
2. **Transcode** `.braw` → H.265 (`.mp4`) using a small C++ tool linked
   against the Blackmagic RAW SDK, piping decoded RGBA frames into ffmpeg's
   `hevc_videotoolbox` encoder.
3. **Upload** the `.mp4` to the research drive at:
   ```
   <SIGNCOLLECT_ROOT>/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/blackmagic_files/<YYYY-MM-DD>/<name>.mp4
   ```
4. **Verify** the destination: size matches local + ffprobe parses + (if
   the destination is an rclone mount) the file is **durable upstream**, not
   just sitting in the VFS write-back cache.
5. **Delete** the source `.braw` from the camera via
   `DELETE /api/mounts/{path}`.
6. **Drop** the local `.braw` to save disk; keep the local `.mp4` cache so
   the next cycle can short-circuit on resume.

If the BRAW SDK can't decode a clip (corrupted/truncated recordings near
the end-of-stream are not unusual), the script falls back to **archiving
the original `.braw`** to the same destination folder, with the same
size+upstream verification before camera deletion.

The pipeline is idempotent and resume-aware: any step that already
completed (cached `.mp4`, `.mp4` already on destination, `.braw` already
archived) is skipped on the next run.

---

## Filesystem layout

```
<SIGNCOLLECT_ROOT>/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/blackmagic_files/
├── 2026-05-06/
│   ├── 1003_05060924_C001.mp4
│   ├── 1003_05060924_C002.mp4
│   └── ...
└── 2026-05-07/
    └── ...
```

Date-folder rules (in order):

1. Names matching `<anything>_YYMMDD_<takeIndex>.<ext>`
   (e.g. `testVoorGomer_260506_1.braw`, `M20250923_4544_260210_0.braw`):
   YYMMDD comes from the filename.
2. Legacy auto-names `<reel>_<MMDDHHMM>_C<NNN>.braw`
   (e.g. `1003_05061251_C011.braw`): MMDD from filename, year from the API
   mtime header.
3. Anything else: the full `YYYY-MM-DD` is taken from the API mtime.

---

## Requirements

- macOS or Linux (tested on macOS arm64).
- Python ≥ 3.10 with `requests`.
- `ffmpeg` and `ffprobe` on `PATH` (HEVC encoders required:
  `hevc_videotoolbox` on macOS, `libx265` elsewhere).
- `rclone` ≥ 1.60 on `PATH` if you're mounting the research drive via
  rclone (recommended).
- A running [bmcam](https://github.com/rem0g/blackmagic_API) server
  exposing `/api/health`, `/api/mounts`, `/api/mounts/{path}` (GET),
  `/api/mounts/{path}` (DELETE), and `/api/download/{path}`.
- The **Blackmagic RAW SDK** installed at
  `/Applications/Blackmagic RAW/Blackmagic RAW SDK` (macOS default).
  Download from
  [blackmagicdesign.com](https://www.blackmagicdesign.com/support/family/blackmagic-raw)
  if missing.

---

## Install

```bash
git clone https://github.com/rem0g/blackmagic_RD_sync.git
cd blackmagic_RD_sync

# Python deps
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'

# Build the BRAW transcoder (links against the BRAW SDK at runtime)
./scripts/build_braw2hevc.sh
```

Verify by running the unit tests:

```bash
.venv/bin/pytest -q
```

---

## Usage

### One-shot cycle

```bash
SIGNCOLLECT_ROOT=/Users/<you>/signcollect \
RCLONE_REMOTE=signcollect: \
.venv/bin/python -m scripts.sync_clips --once
```

### Dry-run (no downloads, no transcode, no uploads, no deletes)

```bash
SIGNCOLLECT_ROOT=/Users/<you>/signcollect \
.venv/bin/python -m scripts.sync_clips --once --dry-run
```

### Continuous daemon (12-hour interval, default)

```bash
SIGNCOLLECT_ROOT=/Users/<you>/signcollect \
RCLONE_REMOTE=signcollect: \
.venv/bin/python -m scripts.sync_clips
```

`SIGTERM`/`SIGINT` exits cleanly after the current clip.

### Useful flags

| Flag / env var | Default | Purpose |
|---|---|---|
| `--bmcam-url` / `BMCAM_URL` | `http://localhost:8000` | bmcam server base URL. |
| `--api-key` / `BMCAM_API_KEY` | unset | Sent as `X-API-Key` header if set. |
| `--signcollect-root` / `SIGNCOLLECT_ROOT` | required | Mount root of the research drive. |
| `--staging-dir` / `STAGING_DIR` | `~/bmcam_sync_staging` | Local cache. |
| `--rclone-remote` / `RCLONE_REMOTE` | unset | rclone remote prefix matching `SIGNCOLLECT_ROOT` (e.g. `signcollect:`). When set, the script checks upstream durability via `rclone size` before deleting from the camera. |
| `--bitrate` / `TRANSCODE_BITRATE` | `50M` | HEVC target bitrate. |
| `--interval-seconds` / `SYNC_INTERVAL_SECONDS` | `43200` (12 h) | Loop interval in daemon mode. |
| `--no-delete-source` / `NO_DELETE_SOURCE=1` | off | Skip the camera-side `DELETE` for safety while testing. |
| `--once` | — | Run a single cycle and exit. |
| `--dry-run` / `SYNC_DRY_RUN=1` | off | Plan a cycle without doing any work. |
| `--verbose` | off | Bump stdout to DEBUG. |
| `--log-file` / `SYNC_LOG_FILE` | `~/bmcam_sync.log` | Rotating log path (10 MB × 5). |
| `--transcoder` / `BRAW_TRANSCODER` | `./build/braw2hevc` | Override the transcoder binary path. |

### What the camera-side delete actually does

Once the upstream durability check passes (`rclone size` reports the file
on the backend at the expected size), the script issues:

```
DELETE /api/mounts/usb/<volume>/<filename>
```

Without `--rclone-remote`, the durability check is skipped — only the
local mount-side size+ffprobe checks are applied. Don't run with
`delete_source=True` and no `RCLONE_REMOTE` against an rclone mount, or
you risk deleting from the camera before the upload reaches the
backend.

---

## Mounting the SignCollect research drive

### macOS (rclone mount)

Install rclone (Homebrew):
```bash
brew install rclone macfuse
```

Configure the remote (one-time):
```bash
rclone config
# n) New remote
# name> signcollect
# type> webdav    (or whichever backend SignCollect provides)
# follow the prompts
```

Mount in the background:
```bash
mkdir -p ~/signcollect
rclone mount signcollect: ~/signcollect \
  --daemon \
  --vfs-cache-mode=full \
  --vfs-cache-max-size=10GiB \
  --vfs-refresh
```

The `--vfs-cache-mode=full` flag accelerates reads and lets `rsync` write
quickly into a local cache, which rclone then uploads to the backend
asynchronously. Because of this asynchrony, this script uses
`rclone size <remote>:<path>` to confirm the upload reached the backend
before deleting the source from the camera (see `RCLONE_REMOTE` above).

Unmount:
```bash
umount ~/signcollect
```

If `umount` says "Resource busy", quit any app that has a file open under
`~/signcollect`, or use `diskutil unmount force ~/signcollect`.

### Windows (rclone mount)

Install [WinFsp](https://winfsp.dev/) and rclone (`choco install rclone`
or download from [rclone.org](https://rclone.org/downloads/)).

Configure the remote (one-time):
```powershell
rclone config
# Same prompts as macOS
```

Mount as a drive letter (PowerShell as Administrator):
```powershell
rclone mount signcollect: S: --vfs-cache-mode=full --vfs-cache-max-size=10G
```

Or in the background using `--daemon` is **not** supported on Windows; use
`nssm install` or a scheduled task instead. A simple scheduled-task
recipe:

1. Save the mount command to `C:\rclone\mount-signcollect.bat`:
   ```bat
   "C:\Program Files\rclone\rclone.exe" mount signcollect: S: ^
     --vfs-cache-mode=full --vfs-cache-max-size=10G ^
     --log-file C:\rclone\rclone.log
   ```
2. Open Task Scheduler → Create Task → trigger "At log on" → action: run
   that batch file.
3. Set "Run only when user is logged on" so the drive letter is visible in
   Explorer.

When pointing this script at the Windows mount, `SIGNCOLLECT_ROOT=S:\`
and `RCLONE_REMOTE=signcollect:`.

---

## Layout

```
.
├── README.md
├── pyproject.toml
├── src/braw2hevc.cpp           # BRAW → HEVC C++ transcoder
├── scripts/
│   ├── build_braw2hevc.sh      # Compiles src/ -> build/braw2hevc
│   ├── sync_clips.py           # The sync daemon entrypoint
│   └── sync_clips_test.py      # pytest unit tests
└── build/                      # Compiled artefacts (gitignored)
```

---

## Troubleshooting

**`bmcam server unreachable at startup`** — start the bmcam server
(`bmcam serve`) on the host that's connected to the camera. Confirm it's
reachable: `curl http://localhost:8000/api/health`.

**`SIGNCOLLECT_ROOT … does not exist`** — the research drive isn't
mounted, or the path is wrong. Mount it (see above), confirm with
`ls "$SIGNCOLLECT_ROOT"`, then retry.

**`transcoder ... missing or not executable`** — run
`./scripts/build_braw2hevc.sh`. If it can't find the SDK, install the
Blackmagic RAW SDK at the canonical macOS path.

**`failed to load BRAW SDK framework`** — the SDK is installed but in a
non-standard path. Edit `src/braw2hevc.cpp` (constant `cfFrameworkPath`)
or symlink the framework directory.

**`frame N process failed (hr=0x8000ffff)`** — the BRAW SDK refused a
frame, almost always near end-of-clip. Cause: an interrupted recording
(USB pulled, power cut, force-stop). The script falls back to archiving
the original `.braw` so the source is preserved.

**`verify_upstream: ... not durable after Ns`** — the file made it into
the rclone VFS write-back cache but hasn't reached the backend within the
size-aware deadline. Check rclone logs (`--log-file` on the mount), the
backend's quota / health, and your network throughput. The clip stays on
the camera and the sync retries on the next cycle.

---

## Tests

```bash
.venv/bin/pytest -q
```

The suite is unit-only (no real camera, no real network drive, no real
BRAW SDK). End-to-end testing against the live camera is documented in
the spec checklist:

- `--dry-run --once` against the live camera prints the planned actions.
- `--once` against a tmpdir `SIGNCOLLECT_ROOT` produces correct
  `<YYYY-MM-DD>/<name>.mp4` layout.
- A second `--once` is a no-op (resume-short-circuit hits).
