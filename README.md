# signlab_blackmagic_RD_sync
Moves Blackmagic camera clips (`.braw`) to the SignCollect research drive as H.265 `.mp4`, then deletes them from the camera.

## What it does
Each cycle, per `.braw` on the camera's USB disk (via `bmcam serve`):
1. Download to staging, transcode with `braw2hevc` (Blackmagic RAW SDK -> ffmpeg; keeps the SMPTE start timecode). If the SDK cannot decode a clip, the original `.braw` is archived instead.
2. Upload to `<SIGNCOLLECT_ROOT>/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/blackmagic_files/<YYYY-MM-DD>/<name>.mp4`. Date comes from `_YYMMDD_` in the name, else `<reel>_<MMDDHHMM>_C<NNN>` + mtime year, else mtime.
3. Verify size + ffprobe and, with `RCLONE_REMOTE`, upstream durability (`rclone size`). Then `DELETE /api/mounts/usb/<volume>/<file>` on the camera.
Idempotent: finished steps are skipped on the next run. Without `RCLONE_REMOTE` on an rclone mount it can delete from the camera before the upload reaches the backend.

## Where it runs
- The Vicon PC (Windows) in the Visualisation Lab, next to `bmcam serve` on `localhost:8000`. Staging on `E:\BlackmagicTemp` (the `pyproject.toml` default), `braw2hevc.exe` with `libx265`.
- The macOS code (`build_braw2hevc.sh` with `clang++`, `hevc_videotoolbox`) works but is not deployed.

## Status
Experimental. Started by hand (`--once` or foreground daemon, 12 h loop); no service unit, no pythonCron job, no heartbeat.

## How to run / deploy
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[test]'
./scripts/build_braw2hevc.sh                      # macOS; needs the BRAW SDK in /Applications
.venv/bin/pytest -q                               # unit only, no camera / drive / SDK
SIGNCOLLECT_ROOT=S:\ RCLONE_REMOTE=signcollect: .venv/bin/python -m scripts.sync_clips --once [--dry-run]
SIGNCOLLECT_ROOT=S:\ RCLONE_REMOTE=signcollect: .venv/bin/python -m scripts.sync_clips   # daemon
```
SIGTERM/SIGINT stop after the current clip. `--help` lists every flag. Research drive on Windows: WinFsp + `rclone mount signcollect: S: --vfs-cache-mode=full --vfs-cache-max-size=10G`, started by a logon scheduled task (`--daemon` does not work on Windows).

## Configuration
| Flag / env | Default | Purpose |
|---|---|---|
| `SIGNCOLLECT_ROOT` | required | research drive mount; run aborts if missing |
| `RCLONE_REMOTE` | unset | remote (in the user's `rclone.conf`, not in git) for the durability check |
| `BMCAM_URL`, `BMCAM_API_KEY` | `http://localhost:8000`, unset | bmcam server |
| `STAGING_DIR` | `[tool.bmcam-sync].staging-dir` in `pyproject.toml` | local cache; machine-specific, override rather than edit |
| `TRANSCODE_BITRATE`, `SYNC_INTERVAL_SECONDS` | `50M`, `43200` | encoding, loop |
| `NO_DELETE_SOURCE=1`, `SYNC_DRY_RUN=1`, `SYNC_LOG_FILE`, `BRAW_TRANSCODER` | off, off, `~/bmcam_sync.log`, `./build/braw2hevc[.exe]` | safety, log (10 MB x 5), binary |

## Dependencies
- `signlab_blackmagic_control` (`bmcam serve`): `/api/health`, `/api/mounts[/{path}]` GET/DELETE, `/api/download/{path}`.
- Blackmagic RAW SDK, `ffmpeg`/`ffprobe` with an HEVC encoder, `rclone` >= 1.60, Python >= 3.10 + `requests`.
- Design spec: `docs/specs/2026-05-06-sync-videos-to-research-drive.md` in `signlab_blackmagic_control`.
- Stack overview: https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack
