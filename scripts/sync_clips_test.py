"""Unit tests for sync_clips."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from scripts import sync_clips as sc


# --- date_folder ----------------------------------------------------------


@pytest.mark.parametrize(
    "name,mtime,expected",
    [
        # Legacy auto-name: MMDD in filename, year from mtime.
        ("1003_05061251_C011.braw", "Tue, 06 May 2026 12:51:39", "2026-05-06"),
        ("1001_04221308_C001.braw", "Fri, 22 Apr 2026 13:08:00", "2026-04-22"),
        # YYMMDD-suffix names (camera-generated and custom alike).
        ("M20250923_4544_260210_0.braw", None, "2026-02-10"),
        ("M20250923_4544_260210_12.braw", None, "2026-02-10"),
        ("testVoorGomer_260506_1.braw", None, "2026-05-06"),
        ("testGlossBlackmagic_260506_0.braw", None, "2026-05-06"),
        # Custom-named clip falls back to mtime entirely.
        ("MyScene_Take1.braw", "Wed, 06 May 2026 12:52:17", "2026-05-06"),
        # Custom name with January date — verify zero-padding.
        ("foo.braw", "Sat, 03 Jan 2026 09:00:00", "2026-01-03"),
    ],
)
def test_date_folder(name, mtime, expected):
    assert sc.date_folder(name, mtime) == expected


def test_date_folder_custom_name_without_mtime_raises():
    with pytest.raises(ValueError):
        sc.date_folder("MyScene_Take1.braw", None)


def test_date_folder_legacy_without_mtime_raises():
    with pytest.raises(ValueError):
        sc.date_folder("1003_05061251_C011.braw", None)


# --- file-type filter -----------------------------------------------------


@pytest.mark.parametrize(
    "name,allowed",
    [
        ("1003_05061251_C011.braw", True),
        ("MyScene.BRAW", True),
        ("clip.mov", False),
        ("clip.mp4", False),
        ("SignSegmentation_full.zip", False),
        ("Magician Launcher.exe", False),
        ("RootCA.crt", False),
        ("noext", False),
    ],
)
def test_is_allowed_clip(name, allowed):
    assert sc.is_allowed_clip(name) is allowed


# --- download idempotency -------------------------------------------------


def test_download_clip_skips_when_size_matches(tmp_path):
    dest = tmp_path / "0506" / "clip.braw"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 10)

    client = mock.Mock()
    wrote = sc.download_clip(client, "usb/T9/clip.braw", 10, dest, dry_run=False)

    assert wrote is False
    assert client.download.call_count == 0


def test_download_clip_redownloads_when_size_differs(tmp_path):
    dest = tmp_path / "0506" / "clip.braw"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")

    new_payload = b"y" * 10
    fake_response = mock.MagicMock()
    fake_response.__enter__.return_value = fake_response
    fake_response.headers = {"Content-Length": str(len(new_payload))}
    fake_response.iter_content.return_value = iter([new_payload])
    fake_response.raise_for_status.return_value = None

    client = mock.Mock()
    client.download.return_value = fake_response

    wrote = sc.download_clip(client, "usb/T9/clip.braw", 10, dest, dry_run=False)

    assert wrote is True
    assert dest.read_bytes() == new_payload


def test_download_clip_cleans_up_tmp_on_failure(tmp_path):
    dest = tmp_path / "0506" / "clip.braw"
    dest.parent.mkdir(parents=True)

    fake_response = mock.MagicMock()
    fake_response.__enter__.return_value = fake_response
    fake_response.headers = {"Content-Length": "10"}

    def boom(_chunk_size):
        yield b"abc"
        raise IOError("network died")

    fake_response.iter_content.side_effect = boom
    fake_response.raise_for_status.return_value = None

    client = mock.Mock()
    client.download.return_value = fake_response

    with pytest.raises(IOError):
        sc.download_clip(client, "usb/T9/clip.braw", 10, dest, dry_run=False)

    assert not dest.exists()
    assert not (dest.parent / f".tmp.{dest.name}").exists()


def test_download_clip_dry_run_no_writes(tmp_path):
    dest = tmp_path / "0506" / "clip.braw"
    client = mock.Mock()

    wrote = sc.download_clip(client, "usb/T9/clip.braw", 10, dest, dry_run=True)
    assert wrote is False
    assert client.download.call_count == 0
    assert not dest.exists()


# --- iter_clips recursion -------------------------------------------------


def test_iter_clips_recurses_into_directories():
    listings = {
        "usb/T9": [
            {"name": "flat.braw", "type": "file", "size": 1, "mtime": "x"},
            {"name": "sub", "type": "directory"},
        ],
        "usb/T9/sub": [
            {"name": "nested.braw", "type": "file", "size": 2, "mtime": "y"},
        ],
    }
    client = mock.Mock()
    client.list_mount.side_effect = lambda p: listings[p]

    out = list(sc.iter_clips(client, "usb/T9"))
    paths = sorted(p for p, _ in out)
    assert paths == ["usb/T9/flat.braw", "usb/T9/sub/nested.braw"]


# --- url encoding ---------------------------------------------------------


def test_list_mount_encodes_path_segments():
    client = sc.BmcamClient("http://localhost:8000")
    with mock.patch.object(client.session, "get") as get:
        get.return_value = mock.Mock(json=lambda: [], raise_for_status=lambda: None)
        client.list_mount("usb/My Drive")
    assert get.call_args.args[0] == "http://localhost:8000/api/mounts/usb/My%20Drive"


def test_download_encodes_path_segments():
    client = sc.BmcamClient("http://localhost:8000")
    with mock.patch.object(client.session, "get") as get:
        client.download("usb/T9/some name.braw")
    assert get.call_args.args[0] == "http://localhost:8000/api/download/usb/T9/some%20name.braw"


def test_delete_uses_mounts_endpoint_and_encodes_path():
    client = sc.BmcamClient("http://localhost:8000")
    with mock.patch.object(client.session, "delete") as delete:
        delete.return_value = mock.Mock(status_code=204)
        client.delete("usb/T9/some name.braw")
    assert delete.call_args.args[0] == "http://localhost:8000/api/mounts/usb/T9/some%20name.braw"


# --- delete_from_camera ---------------------------------------------------


def test_delete_from_camera_treats_204_as_success():
    client = mock.Mock()
    client.delete.return_value = mock.Mock(status_code=204)
    assert sc.delete_from_camera(client, "usb/T9/x.braw", dry_run=False) is True


def test_delete_from_camera_treats_404_as_idempotent_success():
    client = mock.Mock()
    client.delete.return_value = mock.Mock(status_code=404, text="not found")
    assert sc.delete_from_camera(client, "usb/T9/x.braw", dry_run=False) is True


def test_delete_from_camera_returns_false_on_500():
    client = mock.Mock()
    client.delete.return_value = mock.Mock(status_code=500, text="boom")
    assert sc.delete_from_camera(client, "usb/T9/x.braw", dry_run=False) is False


def test_delete_from_camera_dry_run_no_call():
    client = mock.Mock()
    assert sc.delete_from_camera(client, "usb/T9/x.braw", dry_run=True) is True
    assert client.delete.call_count == 0


# --- transcode + verify ---------------------------------------------------


def test_transcode_braw_skips_when_output_already_valid(tmp_path):
    src = tmp_path / "src.braw"
    src.write_bytes(b"placeholder")
    dst = tmp_path / "dst.mp4"
    dst.write_bytes(b"existing-output")

    with mock.patch("scripts.sync_clips._ffprobe_ok", return_value=True), \
         mock.patch("scripts.sync_clips.subprocess.run") as run:
        wrote = sc.transcode_braw(src, dst, Path("/usr/bin/true"), "10M",
                                   dry_run=False)
    assert wrote is False
    assert run.call_count == 0


def test_transcode_braw_dry_run_no_subprocess(tmp_path):
    src = tmp_path / "src.braw"
    src.write_bytes(b"x")
    dst = tmp_path / "dst.mp4"

    with mock.patch("scripts.sync_clips.subprocess.run") as run:
        wrote = sc.transcode_braw(src, dst, Path("/usr/bin/true"), "10M",
                                   dry_run=True)
    assert wrote is False
    assert run.call_count == 0
    assert not dst.exists()


def test_transcode_braw_invokes_binary_and_renames_atomically(tmp_path):
    src = tmp_path / "src.braw"
    src.write_bytes(b"x" * 1024)
    dst = tmp_path / "dst.mp4"
    transcoder = tmp_path / "fake_braw2hevc"
    transcoder.write_text("#!/bin/sh\necho fake\n")
    transcoder.chmod(0o755)

    def fake_run(cmd, *args, **kwargs):
        # Simulate transcoder writing to the .tmp.* path passed as argv[2].
        Path(cmd[2]).write_bytes(b"y" * 256)
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("scripts.sync_clips.subprocess.run", side_effect=fake_run):
        wrote = sc.transcode_braw(src, dst, transcoder, "10M", dry_run=False)
    assert wrote is True
    assert dst.exists() and dst.stat().st_size == 256
    # Source mtime preserved.
    assert int(dst.stat().st_mtime) == int(src.stat().st_mtime)


def test_transcode_braw_raises_on_nonzero_exit(tmp_path):
    src = tmp_path / "src.braw"
    src.write_bytes(b"x")
    dst = tmp_path / "dst.mp4"
    transcoder = tmp_path / "fake_braw2hevc"
    transcoder.write_text("#!/bin/sh\nexit 1\n")
    transcoder.chmod(0o755)

    with mock.patch("scripts.sync_clips.subprocess.run",
                    return_value=mock.Mock(returncode=8, stdout="", stderr="")):
        import subprocess as _sp
        with pytest.raises(_sp.CalledProcessError):
            sc.transcode_braw(src, dst, transcoder, "10M", dry_run=False)
    assert not dst.exists()


def test_verify_remote_passes_when_size_matches_and_ffprobe_ok(tmp_path):
    local = tmp_path / "local.mp4"
    remote = tmp_path / "remote.mp4"
    local.write_bytes(b"a" * 100)
    remote.write_bytes(b"b" * 100)
    with mock.patch("scripts.sync_clips._ffprobe_ok", return_value=True):
        assert sc.verify_remote(local, remote) is True


def test_verify_remote_fails_on_size_mismatch(tmp_path):
    local = tmp_path / "local.mp4"
    remote = tmp_path / "remote.mp4"
    local.write_bytes(b"a" * 100)
    remote.write_bytes(b"b" * 99)
    with mock.patch("scripts.sync_clips._ffprobe_ok", return_value=True):
        assert sc.verify_remote(local, remote) is False


def test_verify_remote_fails_when_ffprobe_rejects(tmp_path):
    local = tmp_path / "local.mp4"
    remote = tmp_path / "remote.mp4"
    local.write_bytes(b"a" * 100)
    remote.write_bytes(b"a" * 100)
    with mock.patch("scripts.sync_clips._ffprobe_ok", return_value=False):
        assert sc.verify_remote(local, remote) is False


def test_verify_remote_fails_when_remote_missing(tmp_path):
    local = tmp_path / "local.mp4"
    remote = tmp_path / "remote.mp4"
    local.write_bytes(b"a")
    assert sc.verify_remote(local, remote) is False


# --- verify_upstream_durable ---------------------------------------------


def test_verify_upstream_durable_returns_true_when_no_remote_configured(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"abc")
    remote = tmp_path / "out" / "x.mp4"
    assert sc.verify_upstream_durable(local, remote, None, tmp_path) is True


def test_verify_upstream_durable_passes_when_size_matches(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a" * 100)
    remote = tmp_path / "AIHR" / "blackmagic_files" / "2026-05-06" / "x.mp4"
    fake_proc = mock.Mock(
        returncode=0, stdout='{"count":1,"bytes":100}', stderr=""
    )
    with mock.patch("scripts.sync_clips.shutil.which", return_value="/x/rclone"), \
         mock.patch("scripts.sync_clips.subprocess.run", return_value=fake_proc):
        assert sc.verify_upstream_durable(
            local, remote, "signcollect:", tmp_path, max_wait_seconds=2.0
        ) is True


def test_verify_upstream_durable_polls_then_succeeds(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a" * 100)
    remote = tmp_path / "AIHR" / "x.mp4"
    responses = [
        mock.Mock(returncode=0, stdout='{"count":0,"bytes":0}', stderr=""),
        mock.Mock(returncode=0, stdout='{"count":1,"bytes":100}', stderr=""),
    ]
    with mock.patch("scripts.sync_clips.shutil.which", return_value="/x/rclone"), \
         mock.patch("scripts.sync_clips.subprocess.run", side_effect=responses), \
         mock.patch("scripts.sync_clips.time.sleep") as sleep_mock:
        assert sc.verify_upstream_durable(
            local, remote, "signcollect:", tmp_path, max_wait_seconds=10.0
        ) is True
    assert sleep_mock.called


def test_verify_upstream_durable_times_out_on_size_mismatch(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a" * 100)
    remote = tmp_path / "AIHR" / "x.mp4"
    fake_proc = mock.Mock(
        returncode=0, stdout='{"count":0,"bytes":0}', stderr=""
    )
    # Patch monotonic so the loop exits quickly: first call sets `start`,
    # second call already past the deadline.
    times = iter([0.0, 100.0, 200.0])
    with mock.patch("scripts.sync_clips.shutil.which", return_value="/x/rclone"), \
         mock.patch("scripts.sync_clips.subprocess.run", return_value=fake_proc), \
         mock.patch("scripts.sync_clips.time.monotonic", side_effect=lambda: next(times)):
        assert sc.verify_upstream_durable(
            local, remote, "signcollect:", tmp_path, max_wait_seconds=5.0
        ) is False


def test_verify_upstream_durable_path_outside_root_returns_false(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a")
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    remote = tmp_path / "AIHR" / "x.mp4"  # not under other_root
    assert sc.verify_upstream_durable(
        local, remote, "signcollect:", other_root
    ) is False


# --- upload ---------------------------------------------------------------


def test_upload_clip_dry_run_no_subprocess(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a")
    remote = tmp_path / "out" / "x.mp4"
    with mock.patch("scripts.sync_clips.subprocess.run") as run:
        assert sc.upload_clip(local, remote, dry_run=True) is True
    assert run.call_count == 0


def test_upload_clip_invokes_rsync(tmp_path):
    local = tmp_path / "x.mp4"; local.write_bytes(b"a")
    remote = tmp_path / "out" / "x.mp4"
    with mock.patch("scripts.sync_clips.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        assert sc.upload_clip(local, remote, dry_run=False) is True
    cmd = run.call_args.args[0]
    assert cmd[0] == "rsync"
    assert cmd[-2] == str(local)
    assert cmd[-1] == str(remote)
