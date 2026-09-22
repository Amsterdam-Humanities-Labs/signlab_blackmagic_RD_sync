# signlab_blackmagic_RD_sync
Moves Blackmagic camera clips (`.braw`) to the SignCollect research drive as H.265 `.mp4`, then deletes them from the camera.

## What it does
Each cycle handles every `.braw` on the camera's USB disk through `bmcam serve`:
1. Download the clip to staging and transcode it with `braw2hevc` (Blackmagic RAW SDK -> ffmpeg; keeps the SMPTE start timecode). If the SDK cannot decode a clip, it archives the original `.braw` instead.
2. Upload to `<SIGNCOLLECT_ROOT>/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/blackmagic_files/<YYYY-MM-DD>/<name>.mp4`. The date comes from `_YYMMDD_` in the name, else from `<reel>_<MMDDHHMM>_C<NNN>` plus the mtime year, else from the mtime.
3. Check size and ffprobe. With `RCLONE_REMOTE` set, also check that the upload reached the remote (`rclone size`). Then `DELETE /api/mounts/usb/<volume>/<file>` on the camera.

Steps that finished are skipped on the next run. On an rclone mount without `RCLONE_REMOTE`, a clip can be deleted from the camera before the upload reaches the remote.

## Where it runs
- The Vicon PC (Windows) in the Visualisation Lab, next to `bmcam serve` on `localhost:8000`. Staging is on `E:\BlackmagicTemp` (the `pyproject.toml` default). `braw2hevc.exe` uses `libx265`.
- The macOS build (`build_braw2hevc.sh` with `clang++`, `hevc_videotoolbox`) works but is not deployed.

## Status
Experimental. Started by hand (`--once`, or a foreground loop every 12 h). No service unit, no pythonCron job, no heartbeat.

## How to run / deploy
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[test]'
./scripts/build_braw2hevc.sh                      # macOS; needs the BRAW SDK in /Applications
.venv/bin/pytest -q                               # unit tests only; no camera, drive or SDK
SIGNCOLLECT_ROOT=S:\ RCLONE_REMOTE=signcollect: .venv/bin/python -m scripts.sync_clips --once [--dry-run]
SIGNCOLLECT_ROOT=S:\ RCLONE_REMOTE=signcollect: .venv/bin/python -m scripts.sync_clips   # loop
```
SIGTERM or SIGINT stops after the current clip. `--help` lists every flag.
Research drive on Windows: WinFsp plus `rclone mount signcollect: S: --vfs-cache-mode=full --vfs-cache-max-size=10G`. A scheduled task starts it at logon, because `--daemon` does not work on Windows.

## Configuration
| Flag / env | Default | Purpose |
|---|---|---|
| `SIGNCOLLECT_ROOT` | required | research drive mount; the run aborts if it is missing |
| `RCLONE_REMOTE` | unset | remote for the upload check (defined in the user's `rclone.conf`, not in git) |
| `BMCAM_URL`, `BMCAM_API_KEY` | `http://localhost:8000`, unset | bmcam server |
| `STAGING_DIR` | `[tool.bmcam-sync].staging-dir` in `pyproject.toml` | local cache; machine-specific, so override it instead of editing the file |
| `TRANSCODE_BITRATE`, `SYNC_INTERVAL_SECONDS` | `50M`, `43200` | encoding, loop |
| `NO_DELETE_SOURCE=1`, `SYNC_DRY_RUN=1`, `SYNC_LOG_FILE`, `BRAW_TRANSCODER` | off, off, `~/bmcam_sync.log`, `./build/braw2hevc[.exe]` | safety, log (10 MB x 5), binary |

## Dependencies
- [signlab_blackmagic_control](https://github.com/Amsterdam-Humanities-Labs/signlab_blackmagic_control) (`bmcam serve`): `/api/health`, `/api/mounts[/{path}]` GET and DELETE, `/api/download/{path}`.
- Blackmagic RAW SDK, `ffmpeg`/`ffprobe` with an HEVC encoder, `rclone` >= 1.60, Python >= 3.10 with `requests`.
- Design spec: `docs/specs/2026-05-06-sync-videos-to-research-drive.md` in signlab_blackmagic_control.
- Stack overview: [signlab_signcollect-stack](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack).
