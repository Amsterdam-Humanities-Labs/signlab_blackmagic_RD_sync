"""Periodic sync of Blackmagic camera clips to the SignCollect research drive.

Per-clip pipeline:

    1. Download .braw from the bmcam server into staging.
    2. Transcode .braw -> .mp4 (HEVC) via the local `braw2hevc` binary.
    3. Upload the .mp4 to <root>/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/
       blackmagic_files/<YYYY-MM-DD>/<name>.mp4 via rsync.
    4. Verify the remote .mp4 is intact (size match + ffprobe parses).
    5. DELETE the source .braw on the camera via /api/mounts/{path}.
    6. Drop local .braw to save disk; keep the .mp4 cache.

Resumable: each step is idempotent and detected via filesystem state, so a
killed/restarted cycle picks up where it left off.

See docs/specs/2026-05-06-sync-videos-to-research-drive.md for the original
spec; the per-file workflow above supersedes the bulk download/rsync model
described there.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import quote

import requests

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

log = logging.getLogger("bmcam_sync")

_DATE_RE_LEGACY = re.compile(
    r"^\d{4}_(\d{2})(\d{2})\d{4}_C\d{3}\.[A-Za-z0-9]+$"
)
# Matches any name ending in `_YYMMDD_<takeIndex>.<ext>`. Examples:
#   M20250923_4544_260210_0.braw
#   testVoorGomer_260506_1.braw
#   testGlossBlackmagic_260506_0.braw
_DATE_RE_YYMMDD = re.compile(
    r".+_(\d{2})(\d{2})(\d{2})_\d+\.[A-Za-z0-9]+$"
)
ALLOWED_EXTENSIONS = {".braw"}
DOWNLOAD_CHUNK_BYTES = 64 * 1024
HTTP_CONNECT_READ_TIMEOUT = 30
DEST_SUBPATH = ("AIHR-FGW-TEST-SIGNLAB (Projectfolder)", "blackmagic_files")
OUTPUT_EXT = ".mp4"
DEFAULT_BITRATE = "50M"
SHUTDOWN = False
PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _install_signal_handlers() -> None:
    def _handle(signum, _frame):
        global SHUTDOWN
        log.warning("received signal %s; will exit after current clip", signum)
        SHUTDOWN = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def _load_pyproject_tool_config(
    pyproject_path: Path | None = None,
) -> dict[str, object]:
    if pyproject_path is None:
        pyproject_path = PYPROJECT_PATH

    if tomllib is None:
        log.warning(
            "could not read %s: install tomli or use Python 3.11+",
            pyproject_path,
        )
        return {}
    if not pyproject_path.exists():
        return {}

    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        log.warning("could not read %s: %s", pyproject_path, e)
        return {}

    tool_config = data.get("tool", {}).get("bmcam-sync", {})
    if isinstance(tool_config, dict):
        return tool_config
    log.warning("[tool.bmcam-sync] in %s must be a TOML table", pyproject_path)
    return {}


def _tool_config_str(config: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass
class Config:
    bmcam_url: str
    api_key: str | None
    signcollect_root: Path
    staging_dir: Path
    interval_seconds: int
    log_file: Path
    dry_run: bool
    verbose: bool
    transcoder: Path
    bitrate: str
    delete_source: bool
    # If set (e.g. "signcollect:"), the script verifies upstream durability
    # via `rclone size` before deleting from the camera. This protects
    # against rclone-mount VFS caching that returns rsync success before
    # the file actually reaches the backend.
    rclone_remote: str | None


def date_folder(name: str, mtime_http: str | None) -> str:
    """Return the `YYYY-MM-DD` folder for a clip.

    Recognized patterns:
      - Names ending in `_YYMMDD_<takeIndex>.<ext>`, e.g.
        `testVoorGomer_260506_1.braw` or `M20250923_4544_260210_0.braw`.
        The YYMMDD segment is the recording date.
      - `1003_05061251_C011.braw` — legacy auto-name; MMDD in filename, year
        from API mtime since the filename only carries month+day.
      - Anything else: full date from API mtime.
    """
    m = _DATE_RE_YYMMDD.match(name)
    if m:
        yy, mm, dd = m.groups()
        return f"20{yy}-{mm}-{dd}"

    m = _DATE_RE_LEGACY.match(name)
    if m:
        if not mtime_http:
            raise ValueError(f"cannot determine year for {name!r}: no mtime")
        mm, dd = m.groups()
        dt = parsedate_to_datetime(mtime_http)
        return f"{dt.year:04d}-{mm}-{dd}"

    if not mtime_http:
        raise ValueError(f"cannot determine date folder for {name!r}: no mtime")
    dt = parsedate_to_datetime(mtime_http)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def is_allowed_clip(name: str) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in ALLOWED_EXTENSIONS


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


class BmcamClient:
    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _encode_path(p: str) -> str:
        return "/".join(quote(seg, safe="") for seg in p.split("/"))

    def health(self) -> dict:
        r = self.session.get(self._url("/api/health"), timeout=HTTP_CONNECT_READ_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def mounts(self) -> list[dict]:
        r = self.session.get(self._url("/api/mounts"), timeout=HTTP_CONNECT_READ_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def list_mount(self, mount_path: str) -> list[dict]:
        r = self.session.get(
            self._url(f"/api/mounts/{self._encode_path(mount_path)}"),
            timeout=HTTP_CONNECT_READ_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def download(self, full_path: str) -> requests.Response:
        return self.session.get(
            self._url(f"/api/download/{self._encode_path(full_path)}"),
            stream=True,
            timeout=HTTP_CONNECT_READ_TIMEOUT,
        )

    def delete(self, full_path: str) -> requests.Response:
        return self.session.delete(
            self._url(f"/api/mounts/{self._encode_path(full_path)}"),
            timeout=HTTP_CONNECT_READ_TIMEOUT,
        )


def iter_clips(client: BmcamClient, mount_path: str) -> Iterator[tuple[str, dict]]:
    """Yield `(full_path, listing_entry)` for every file under `mount_path`,
    recursing into subdirectories. Caller filters by extension.
    """
    stack = [mount_path]
    while stack:
        current = stack.pop()
        try:
            entries = client.list_mount(current)
        except requests.RequestException as e:
            log.warning("failed to list %s: %s", current, e)
            continue
        for entry in entries:
            name = entry.get("name")
            etype = entry.get("type")
            if not name:
                continue
            child_path = f"{current}/{name}"
            if etype == "directory":
                stack.append(child_path)
            elif etype == "file":
                yield child_path, entry


def download_clip(
    client: BmcamClient,
    full_path: str,
    expected_size: int,
    dest: Path,
    dry_run: bool,
) -> bool:
    """Stream `full_path` into `dest` atomically. Returns True if a new
    file was written, False if skipped (already present at the right size).
    Raises on download error.
    """
    if dest.exists() and dest.stat().st_size == expected_size:
        log.debug("skip download %s: already present", dest)
        return False

    if dry_run:
        log.info("DRY-RUN would download %s (%s)", full_path, _format_size(expected_size))
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp.{dest.name}"
    started = time.monotonic()
    written = 0
    try:
        with client.download(full_path) as r:
            r.raise_for_status()
            content_length = r.headers.get("Content-Length")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
        if content_length is not None and int(content_length) != written:
            raise IOError(
                f"short read for {full_path}: got {written} bytes, "
                f"Content-Length said {content_length}"
            )
        if written != expected_size:
            log.warning(
                "size mismatch for %s: listing said %d, downloaded %d",
                full_path, expected_size, written,
            )
        os.replace(tmp, dest)
        elapsed = time.monotonic() - started
        rate = written / elapsed if elapsed > 0 else 0
        log.info("    downloaded %s in %.1fs (%s/s)",
                 _format_size(written), elapsed, _format_size(int(rate)))
        return True
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def transcode_braw(src: Path, dst: Path, transcoder: Path, bitrate: str,
                   dry_run: bool) -> bool:
    """Transcode `src` (.braw) to `dst` (.mp4) via the external transcoder
    binary. Returns True if a new file was written, False if skipped.
    Raises CalledProcessError on transcode failure.
    """
    if dst.exists() and dst.stat().st_size > 0 and _ffprobe_ok(dst):
        log.debug("skip transcode %s: already valid", dst)
        return False

    if dry_run:
        log.info("DRY-RUN would transcode %s -> %s", src.name, dst.name)
        return False

    if dst.exists():
        # Stale/incomplete output — drop and redo.
        try:
            dst.unlink()
        except OSError:
            pass

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".tmp.{dst.name}"
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    started = time.monotonic()
    cmd = [str(transcoder), str(src), str(tmp), bitrate]
    log.debug("transcode: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        # Only the lines that look like actual errors are surfaced; the
        # transcoder also writes \r-style progress to stderr ("frame X/Y"),
        # which would otherwise drown the real error message.
        error_keywords = ("failed", "error", "Error")
        for line in proc.stderr.splitlines():
            if any(k in line for k in error_keywords):
                log.error("    %s", line.strip())
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    for line in proc.stderr.splitlines():
        if line.strip():
            log.debug("    %s", line)

    if not tmp.exists() or tmp.stat().st_size == 0:
        raise IOError(f"transcoder produced no output for {src}")

    # Preserve source mtime on the local transcode for traceability.
    try:
        src_stat = src.stat()
        os.utime(tmp, (src_stat.st_atime, src_stat.st_mtime))
    except OSError:
        pass

    os.replace(tmp, dst)
    elapsed = time.monotonic() - started
    log.info("    transcoded -> %s in %.1fs (%s)",
             dst.name, elapsed, _format_size(dst.stat().st_size))
    return True


def _ffprobe_ok(path: Path) -> bool:
    """Returns True if ffprobe can parse the file and reports a video stream
    with a positive frame count (or duration)."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=nb_frames,duration,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    streams = data.get("streams") or []
    if not streams:
        return False
    s = streams[0]
    nb = s.get("nb_frames")
    dur = s.get("duration")
    if nb and nb.isdigit() and int(nb) > 0:
        return True
    try:
        if dur and float(dur) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def upload_clip(local_mp4: Path, remote_mp4: Path, dry_run: bool) -> bool:
    """rsync local mp4 to remote mp4. Returns True if rsync ran and matched."""
    if dry_run:
        log.info("DRY-RUN would rsync %s -> %s", local_mp4, remote_mp4)
        return True

    remote_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync", "-t",
        "--partial",
        "--omit-dir-times", "--no-perms", "--no-group", "--no-owner",
        str(local_mp4), str(remote_mp4),
    ]
    log.debug("rsync: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        if line.strip():
            log.warning("rsync: %s", line)
    if proc.returncode != 0:
        log.error("rsync exited with code %d", proc.returncode)
        return False
    return True


def verify_remote_size(local: Path, remote: Path) -> bool:
    """Confirm the remote file exists and matches `local` in size."""
    if not remote.exists():
        log.error("verify: %s missing on destination", remote)
        return False
    local_size = local.stat().st_size
    remote_size = remote.stat().st_size
    if local_size != remote_size:
        log.error(
            "verify: size mismatch for %s — local=%d remote=%d",
            remote.name, local_size, remote_size,
        )
        return False
    return True


def verify_remote(local_mp4: Path, remote_mp4: Path) -> bool:
    """Verify a remote .mp4 matches the local one and is readable.

    Checks:
      * remote exists and is non-empty
      * size matches local
      * ffprobe can parse the remote file and finds a positive frame count
        or duration
    """
    if not verify_remote_size(local_mp4, remote_mp4):
        return False
    if not _ffprobe_ok(remote_mp4):
        log.error("verify: ffprobe failed for %s", remote_mp4)
        return False
    return True


def _durability_timeout(size_bytes: int) -> float:
    """Generous upload window scaling with file size.

    Floor of 60 seconds; otherwise allow ~1 MB/s upstream throughput plus a
    20 % cushion. A 1 GB file gets ~20 minutes, a 100 MB file gets ~2 minutes.
    """
    by_size = (size_bytes / (1024 * 1024)) * 1.2
    return max(60.0, by_size)


def verify_upstream_durable(
    local_mp4: Path,
    remote_mp4: Path,
    rclone_remote: str | None,
    signcollect_root: Path,
    max_wait_seconds: float | None = None,
) -> bool:
    """Confirm the file is durable on the rclone backend, not just in the
    VFS cache. Polls `rclone size` until upstream size matches local size or
    `max_wait_seconds` is exceeded.

    Returns True when:
      - `rclone_remote` is None (caller hasn't configured durability check), or
      - upstream size matches local size.
    """
    if not rclone_remote:
        # No upstream check configured — caller has accepted that risk.
        return True
    if shutil.which("rclone") is None:
        log.error("rclone not on PATH; cannot verify upstream durability")
        return False

    try:
        rel = remote_mp4.relative_to(signcollect_root)
    except ValueError:
        log.error(
            "verify_upstream: %s is not under %s",
            remote_mp4, signcollect_root,
        )
        return False

    target = f"{rclone_remote}{rel.as_posix()}"
    expected = local_mp4.stat().st_size
    if max_wait_seconds is None:
        max_wait_seconds = _durability_timeout(expected)
    deadline = time.monotonic() + max_wait_seconds
    last_seen: int | None = None

    while True:
        try:
            proc = subprocess.run(
                ["rclone", "size", target, "--json"],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.error("rclone size %s failed: %s", target, e)
            return False
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
                upstream_bytes = int(data.get("bytes", -1))
                last_seen = upstream_bytes
                if upstream_bytes == expected:
                    return True
            except (json.JSONDecodeError, ValueError):
                pass
        if time.monotonic() >= deadline:
            log.error(
                "verify_upstream: %s not durable after %.0fs "
                "(expected %d bytes, last seen %s)",
                target, max_wait_seconds, expected,
                "missing" if last_seen is None else str(last_seen),
            )
            return False
        time.sleep(2.0)


def delete_from_camera(client: BmcamClient, full_path: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("DRY-RUN would DELETE %s on camera", full_path)
        return True
    try:
        r = client.delete(full_path)
    except requests.RequestException as e:
        log.error("camera DELETE failed for %s: %s", full_path, e)
        return False
    if r.status_code in (200, 204):
        log.info("    camera-side deleted %s", full_path)
        return True
    if r.status_code == 404:
        # Already gone — treat as success for idempotency.
        log.info("    camera-side already absent %s", full_path)
        return True
    log.error("camera DELETE %s -> HTTP %d: %s",
              full_path, r.status_code, r.text[:200])
    return False


@dataclass
class CycleStats:
    seen: int = 0
    transcoded: int = 0
    archived_source: int = 0  # transcode failed; original .braw archived instead
    uploaded: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    failed_paths: list[str] = field(default_factory=list)


def _dest_root(signcollect_root: Path) -> Path:
    return signcollect_root.joinpath(*DEST_SUBPATH)


def process_clip(
    client: BmcamClient, cfg: Config, full_path: str, entry: dict,
    stats: CycleStats,
) -> None:
    name = entry["name"]
    size = entry["size"]
    mtime = entry.get("mtime")
    try:
        folder = date_folder(name, mtime)
    except ValueError as e:
        log.error("could not determine date folder for %s: %s", name, e)
        stats.failed += 1
        stats.failed_paths.append(full_path)
        return

    out_name = Path(name).stem + OUTPUT_EXT
    staging_braw = cfg.staging_dir / folder / name
    staging_mp4 = cfg.staging_dir / folder / out_name
    remote_mp4 = _dest_root(cfg.signcollect_root) / folder / out_name

    log.info("clip %s (%s, %s/)", name, _format_size(size), folder)

    # Resume short-circuits: if the destination already has a valid mp4 and
    # the source is still on the camera, skip straight to the camera delete.
    remote_already_ok = (
        remote_mp4.exists()
        and remote_mp4.stat().st_size > 0
        and _ffprobe_ok(remote_mp4)
    )
    if remote_already_ok:
        log.info("    remote .mp4 already valid; checking upstream durability")
        size_source = staging_mp4 if staging_mp4.exists() else remote_mp4
        if not verify_upstream_durable(
            size_source, remote_mp4, cfg.rclone_remote, cfg.signcollect_root
        ):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        if cfg.delete_source:
            if delete_from_camera(client, full_path, cfg.dry_run):
                stats.deleted += 1
            else:
                stats.failed += 1
                stats.failed_paths.append(full_path)
                return
        stats.skipped += 1
        return

    # Resume case 2: source .braw was archived to the destination by a
    # previous cycle (transcode failure fallback). If the listing-reported
    # size matches the remote, treat it as already archived.
    remote_braw = _dest_root(cfg.signcollect_root) / folder / name
    remote_braw_already_ok = (
        remote_braw.exists()
        and remote_braw.stat().st_size == size
    )
    if remote_braw_already_ok:
        log.info("    remote .braw already archived; checking upstream durability")
        size_source = staging_braw if staging_braw.exists() else remote_braw
        if not verify_upstream_durable(
            size_source, remote_braw, cfg.rclone_remote, cfg.signcollect_root
        ):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        if cfg.delete_source:
            if delete_from_camera(client, full_path, cfg.dry_run):
                stats.deleted += 1
            else:
                stats.failed += 1
                stats.failed_paths.append(full_path)
                return
        stats.skipped += 1
        return

    # 1. Download
    try:
        download_clip(client, full_path, size, staging_braw, cfg.dry_run)
    except Exception as e:
        log.exception("download failed for %s: %s", full_path, e)
        stats.failed += 1
        stats.failed_paths.append(full_path)
        return

    if cfg.dry_run:
        log.info("    DRY-RUN would transcode + upload + verify + delete")
        stats.skipped += 1
        return

    # 2. Transcode (best-effort).
    transcode_ok = False
    try:
        wrote = transcode_braw(
            staging_braw, staging_mp4, cfg.transcoder, cfg.bitrate, cfg.dry_run
        )
        if wrote:
            stats.transcoded += 1
        transcode_ok = True
    except Exception as e:
        log.warning(
            "transcode failed for %s (%s) — archiving source .braw instead",
            staging_braw.name, e,
        )

    if transcode_ok:
        # 3a. Upload .mp4
        log.info("    uploading -> %s", remote_mp4)
        if not upload_clip(staging_mp4, remote_mp4, cfg.dry_run):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        stats.uploaded += 1

        # 4a. Verify .mp4 (size + ffprobe)
        if not verify_remote(staging_mp4, remote_mp4):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return

        # 4b. Verify upstream durability
        if not verify_upstream_durable(
            staging_mp4, remote_mp4, cfg.rclone_remote, cfg.signcollect_root
        ):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        log.info("    verified on destination")
    else:
        # 3b/4b. Fallback: archive the source .braw at the destination,
        # verified by size + upstream durability (no ffprobe — it's not an
        # MP4 container).
        remote_braw = _dest_root(cfg.signcollect_root) / folder / name
        log.info("    uploading source .braw -> %s", remote_braw)
        if not upload_clip(staging_braw, remote_braw, cfg.dry_run):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        stats.uploaded += 1
        if not verify_remote_size(staging_braw, remote_braw):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        if not verify_upstream_durable(
            staging_braw, remote_braw, cfg.rclone_remote, cfg.signcollect_root
        ):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        stats.archived_source += 1
        log.info(
            "    source .braw archived to destination (transcode unavailable)"
        )

    # 5. Delete from camera (point of no return).
    if cfg.delete_source:
        if not delete_from_camera(client, full_path, cfg.dry_run):
            stats.failed += 1
            stats.failed_paths.append(full_path)
            return
        stats.deleted += 1

    # 6. Drop local .braw to save disk; keep the .mp4 cache when present.
    try:
        if staging_braw.exists():
            staging_braw.unlink()
            log.debug("    dropped local %s", staging_braw)
    except OSError as e:
        log.warning("failed to drop local %s: %s", staging_braw, e)


def preflight(cfg: Config, client: BmcamClient) -> bool:
    try:
        h = client.health()
        if h.get("status") != "ok":
            log.error("bmcam /api/health returned %r; skipping cycle", h)
            return False
    except requests.RequestException as e:
        log.error("bmcam server unreachable at %s: %s", cfg.bmcam_url, e)
        return False

    if not cfg.signcollect_root.exists():
        log.error(
            "SIGNCOLLECT_ROOT %s does not exist; is the research drive mounted?",
            cfg.signcollect_root,
        )
        return False
    if not cfg.signcollect_root.is_dir():
        log.error("SIGNCOLLECT_ROOT %s is not a directory", cfg.signcollect_root)
        return False
    if not cfg.dry_run and not os.access(cfg.signcollect_root, os.W_OK):
        log.error("SIGNCOLLECT_ROOT %s is not writable", cfg.signcollect_root)
        return False

    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.dry_run and not os.access(cfg.staging_dir, os.W_OK):
        log.error("staging dir %s is not writable", cfg.staging_dir)
        return False

    if not cfg.transcoder.exists() or not os.access(cfg.transcoder, os.X_OK):
        log.error(
            "transcoder %s missing or not executable. "
            "Run scripts/build_braw2hevc.sh.",
            cfg.transcoder,
        )
        return False

    for tool in ("rsync", "ffprobe", "ffmpeg"):
        if shutil.which(tool) is None:
            log.error("%s not found on PATH", tool)
            return False

    return True


def run_once(cfg: Config, client: BmcamClient) -> CycleStats:
    log.info("cycle start")
    stats = CycleStats()
    if not preflight(cfg, client):
        log.warning("pre-flight failed; skipping cycle")
        stats.failed += 1
        return stats

    try:
        mounts = client.mounts()
    except requests.RequestException as e:
        log.error("failed to list mounts: %s", e)
        stats.failed += 1
        return stats
    if not mounts:
        log.warning("no camera mounts found (USB disk inserted?); skipping cycle")
        return stats

    mount_path = mounts[0]["name"]
    log.info("using mount %s", mount_path)

    candidates = [
        (full_path, entry)
        for full_path, entry in iter_clips(client, mount_path)
        if is_allowed_clip(entry["name"])
    ]
    log.info("found %d clips on %s", len(candidates), mount_path)
    stats.seen = len(candidates)

    for full_path, entry in candidates:
        if SHUTDOWN:
            log.warning("shutdown requested; stopping mid-cycle")
            break
        process_clip(client, cfg, full_path, entry, stats)

    log.info(
        "cycle done — seen=%d transcoded=%d archived_source=%d uploaded=%d "
        "deleted=%d skipped=%d failed=%d",
        stats.seen, stats.transcoded, stats.archived_source, stats.uploaded,
        stats.deleted, stats.skipped, stats.failed,
    )
    if stats.failed_paths:
        log.warning("failed clips: %s", ", ".join(stats.failed_paths))
    return stats


def loop(cfg: Config, client: BmcamClient) -> None:
    while not SHUTDOWN:
        started = time.monotonic()
        try:
            run_once(cfg, client)
        except Exception:
            log.exception("sync cycle failed")
        if SHUTDOWN:
            break
        elapsed = time.monotonic() - started
        sleep_for = max(60, cfg.interval_seconds - elapsed)
        log.info("sleeping %.0f s until next cycle", sleep_for)
        end = time.monotonic() + sleep_for
        while not SHUTDOWN and time.monotonic() < end:
            time.sleep(min(5.0, end - time.monotonic()))


def _setup_logging(cfg: Config) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if cfg.verbose else logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if cfg.verbose else logging.INFO)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _build_config(argv: Sequence[str] | None = None) -> Config:
    tool_config = _load_pyproject_tool_config()
    default_staging_dir = (
        os.environ.get("STAGING_DIR")
        or _tool_config_str(tool_config, "staging-dir", "staging_dir")
        or str(Path.home() / "bmcam_sync_staging")
    )

    p = argparse.ArgumentParser(
        description="Periodic sync + H.265 transcode of Blackmagic clips to "
                    "the SignCollect research drive."
    )
    p.add_argument("--bmcam-url", default=os.environ.get("BMCAM_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("BMCAM_API_KEY") or None)
    p.add_argument("--signcollect-root",
                   default=os.environ.get("SIGNCOLLECT_ROOT"),
                   help="Research-drive mount root (required)")
    p.add_argument("--staging-dir",
                   default=default_staging_dir)
    p.add_argument("--interval-seconds", type=int,
                   default=int(os.environ.get("SYNC_INTERVAL_SECONDS", str(12 * 3600))))
    p.add_argument("--log-file",
                   default=os.environ.get("SYNC_LOG_FILE")
                   or str(Path.home() / "bmcam_sync.log"))
    p.add_argument("--dry-run", action="store_true",
                   default=os.environ.get("SYNC_DRY_RUN", "0") == "1")
    p.add_argument("--once", action="store_true",
                   help="Run a single cycle and exit")
    p.add_argument("--verbose", action="store_true")

    default_transcoder = (
        Path(__file__).resolve().parent.parent / "build" / "braw2hevc"
    )
    if os.name == "nt":
        default_transcoder = default_transcoder.with_suffix(".exe")
    p.add_argument("--transcoder",
                   default=os.environ.get("BRAW_TRANSCODER")
                   or str(default_transcoder),
                   help="Path to the braw2hevc binary")
    p.add_argument("--bitrate",
                   default=os.environ.get("TRANSCODE_BITRATE", DEFAULT_BITRATE),
                   help="HEVC bitrate target (e.g. 50M)")
    p.add_argument("--no-delete-source", action="store_true",
                   default=os.environ.get("NO_DELETE_SOURCE", "0") == "1",
                   help="Skip the camera-side DELETE step")
    p.add_argument("--rclone-remote",
                   default=os.environ.get("RCLONE_REMOTE") or None,
                   help="rclone remote prefix matching SIGNCOLLECT_ROOT "
                        "(e.g. 'signcollect:'). When set, the script uses "
                        "`rclone size` to confirm upstream durability before "
                        "deleting from the camera.")
    args = p.parse_args(argv)

    if not args.signcollect_root:
        p.error(
            "SIGNCOLLECT_ROOT is required (env or --signcollect-root). "
            "Point this at the mounted research drive."
        )

    cfg = Config(
        bmcam_url=args.bmcam_url,
        api_key=args.api_key,
        signcollect_root=Path(args.signcollect_root).expanduser(),
        staging_dir=Path(args.staging_dir).expanduser(),
        interval_seconds=args.interval_seconds,
        log_file=Path(args.log_file).expanduser(),
        dry_run=args.dry_run,
        verbose=args.verbose,
        transcoder=Path(args.transcoder).expanduser(),
        bitrate=args.bitrate,
        delete_source=not args.no_delete_source,
        rclone_remote=args.rclone_remote,
    )
    cfg._once = args.once  # type: ignore[attr-defined]
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    cfg = _build_config(argv)
    _setup_logging(cfg)
    _install_signal_handlers()
    log.info(
        "starting bmcam-sync: bmcam=%s root=%s staging=%s interval=%ds "
        "bitrate=%s delete_source=%s rclone_remote=%s dry_run=%s",
        cfg.bmcam_url, cfg.signcollect_root, cfg.staging_dir,
        cfg.interval_seconds, cfg.bitrate, cfg.delete_source,
        cfg.rclone_remote or "<unset>", cfg.dry_run,
    )

    client = BmcamClient(cfg.bmcam_url, cfg.api_key)

    try:
        client.health()
    except requests.RequestException as e:
        log.error("bmcam server unreachable at startup: %s", e)
        return 2
    if not cfg.signcollect_root.exists():
        log.error(
            "SIGNCOLLECT_ROOT %s does not exist at startup; is the research "
            "drive mounted?",
            cfg.signcollect_root,
        )
        return 2

    if getattr(cfg, "_once", False):
        run_once(cfg, client)
        return 0

    loop(cfg, client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
