import base64
import hashlib
import shutil
import subprocess
import time

import pytest

from desktop_env.server.fmp4 import FragmentIndex


@pytest.fixture
def ffmpeg():
    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pytest.skip("Install FFmpeg or imageio-ffmpeg for live recording tests")
    return executable


def command(ffmpeg, path, live=False):
    return (
        [ffmpeg, "-v", "error"]
        + (["-re"] if live else [])
        + [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "30",
            "-sc_threshold",
            "0",
            "-movflags",
            "+empty_moov+default_base_moof+frag_keyframe",
            "-frag_duration",
            "100000",
            "-flush_packets",
            "1",
            str(path),
        ]
    )


def test_live_queries_equal_final_recording(ffmpeg, tmp_path):
    path = tmp_path / "live.mp4"
    process = subprocess.Popen(command(ffmpeg, path, live=True), stderr=subprocess.PIPE)
    index = FragmentIndex(path, ffmpeg=ffmpeg)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            index.refresh()
            if index.available_until_s is not None and index.available_until_s >= 1.2:
                break
            assert process.poll() is None, process.stderr.read().decode()
            time.sleep(0.03)
        assert process.poll() is None
        assert index.available_until_s >= 1.2
        times = [1.13, 0.03, 0.72, 0.2]
        during = index.get_frames(times)
        assert all(f["status"] == "ok" for f in during["frames"])
        assert index.get_frames([99])["frames"][0]["status"] == "not_ready"
        process.wait(timeout=10)
        final = FragmentIndex(path, ffmpeg=ffmpeg).get_frames(times)
        for a, b in zip(during["frames"], final["frames"]):
            assert a["actual_time_s"] == b["actual_time_s"]
            assert a["image"] == b["image"]
            n = round(a["actual_time_s"] * 30)
            # Independent full-file decode verifies selection, not only equality
            # between two invocations of our own reader.
            reference = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-threads",
                    "1",
                    "-i",
                    str(path),
                    "-vf",
                    f"select=eq(n\\,{n})",
                    "-frames:v",
                    "1",
                    "-threads",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
            ).stdout
            assert (
                hashlib.sha256(reference).digest()
                == hashlib.sha256(base64.b64decode(a["image"]["data"])).digest()
            )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_partial_last_fragment_is_not_published_then_can_be_completed(ffmpeg, tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(command(ffmpeg, source), check=True)
    full = FragmentIndex(source, ffmpeg=ffmpeg)
    full.refresh()
    last = full.fragments[-1]
    data = source.read_bytes()
    target = tmp_path / "growing.mp4"
    target.write_bytes(data[: last.end - 9])
    partial = FragmentIndex(target, ffmpeg=ffmpeg)
    partial.refresh()
    assert len(partial.fragments) == len(full.fragments) - 1
    assert partial.get_frames([last.times[0]])["frames"][0]["status"] == "not_ready"
    with target.open("ab") as f:
        f.write(data[last.end - 9 :])
    partial.refresh()
    assert partial.frame_times == full.frame_times
    assert partial.get_frames([last.times[0]])["frames"][0]["status"] == "ok"


def test_irregular_capture_times_are_not_rounded_to_thirtieths(ffmpeg, tmp_path):
    target = tmp_path / "irregular.mp4"
    args = command(ffmpeg, target)
    args[-1:-1] = [
        "-vf",
        "settb=1/1000000,setpts=N*33333+floor(N/2)*1700",
        "-vsync",
        "0",
        "-enc_time_base",
        "1:1000000",
        "-video_track_timescale",
        "1000000",
    ]
    subprocess.run(args, check=True)
    index = FragmentIndex(target, ffmpeg=ffmpeg)
    index.refresh()
    assert index.frame_times[:4] == pytest.approx(
        [0, 0.033333, 0.068366, 0.101699], abs=1e-6
    )
    assert index.get_frames([index.frame_times[-1]])["frames"][0]["status"] == "ok"
