"""Optional real-time endpoints for the existing OSWorld VM server (Linux/X11)."""
import base64
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

try:  # VM server is usually launched as /home/user/server/main.py.
    from .fmp4 import FragmentIndex
except ImportError:
    from fmp4 import FragmentIndex


class LiveRecording:
    def __init__(self, directory="/tmp/osworld-realtime", ffmpeg="ffmpeg"):
        self.directory = Path(directory)
        self.ffmpeg = ffmpeg
        self.process = None
        self.origin_wall = None
        self.index = None
        self.session_id = None
        self.log = None

    def start(self, fps=30, fragment_ms=100, width=1920, height=1080):
        if self.process and self.process.poll() is None:
            raise RuntimeError("A recording is already active")
        if fps != 30 or (width, height) != (1920, 1080):
            raise ValueError("This experiment uses 30 FPS at 1920x1080 for every group")
        if not isinstance(fragment_ms, int) or not 1 <= fragment_ms <= 1000:
            raise ValueError("Invalid fragment length")
        self.session_id = uuid.uuid4().hex
        self.path = self.directory / self.session_id
        self.path.mkdir(parents=True)
        self.log_path = self.path / "recording_ffmpeg.log"
        self.video_path = self.path / "recording.mp4"
        self.index_path = self.path / "recording_index.json"
        self.origin_wall = None
        self.index = FragmentIndex(self.video_path, ffmpeg=self.ffmpeg)
        self.log = self.log_path.open("w")
        # showinfo precedes setpts: log actual X11 capture PTS, normalize only the
        # encoded timeline. VFR prevents fabricated duplicate frames during stalls.
        command = [
            self.ffmpeg,
            "-y",
            "-nostdin",
            "-loglevel",
            "info",
            "-copyts",
            "-use_wallclock_as_timestamps",
            "1",
            "-f",
            "x11grab",
            "-draw_mouse",
            "1",
            "-framerate",
            str(fps),
            "-video_size",
            f"{width}x{height}",
            "-i",
            os.getenv("DISPLAY", ":0.0"),
            "-vf",
            "showinfo,setpts=PTS-STARTPTS",
            "-vsync",
            "0",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-g",
            str(fps),
            "-keyint_min",
            str(fps),
            "-sc_threshold",
            "0",
            "-enc_time_base",
            "1:1000000",
            "-video_track_timescale",
            "1000000",
            "-movflags",
            "+empty_moov+default_base_moof+frag_keyframe",
            "-frag_duration",
            str(fragment_ms * 1000),
            "-flush_packets",
            "1",
            str(self.video_path),
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=self.log
        )
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "FFmpeg exited during startup; see recording_ffmpeg.log"
                    )
                self.index.refresh()
                log = self.log_path.read_text(errors="replace")
                tb = re.search(r"config in time_base:\s*(\d+)/(\d+)", log)
                pts = re.search(r"n:\s*0\s+pts:\s*(-?\d+)", log)
                if tb and pts and self.index.available_until_s is not None:
                    self.origin_wall = int(pts[1]) * int(tb[1]) / int(tb[2])
                    if abs(time.time() - self.origin_wall) > 30:
                        raise RuntimeError(
                            "Capture timestamps do not match the VM wall clock"
                        )
                    return self.status()
                time.sleep(0.03)
            raise RuntimeError("Timed out waiting for the first complete fragment")
        except Exception:
            self.stop()
            raise

    def elapsed(self):
        if self.origin_wall is None:
            raise RuntimeError("Recording has not started")
        # X11 input PTS are wall-clock timestamps. Use the same source for
        # observations and queries: VM clock correction can make an independently
        # advancing monotonic clock disagree with capture PTS during a long run.
        # Execution durations are measured separately with monotonic().
        return time.time() - self.origin_wall

    def status(self):
        if self.index:
            self.index.refresh()
        return {
            "session_id": self.session_id,
            "task_time_s": self.elapsed(),
            "available_until_s": self.index.available_until_s,
            "recording": bool(self.process and self.process.poll() is None),
            "fps": 30,
            "width": 1920,
            "height": 1080,
            "clock_origin_wall_s": self.origin_wall,
        }

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
            self.log = None
        if self.index:
            self.index.save(self.index_path)


def register_realtime(app, capture, pyautogui):
    bp = Blueprint("realtime", __name__, url_prefix="/realtime")
    recorder = LiveRecording()
    action_lock = threading.Lock()
    held_keys, held_buttons = set(), set()

    def check_session():
        data = request.get_json(silent=True) or request.args
        if not recorder.session_id or data.get("session_id") != recorder.session_id:
            raise ValueError("Recording session mismatch")
        return data

    def release_inputs():
        for key in list(held_keys):
            pyautogui.keyUp(key)
        for button in list(held_buttons):
            pyautogui.mouseUp(button=button)
        held_keys.clear()
        held_buttons.clear()

    @bp.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(status="error", message=str(exc)), 400

    @bp.errorhandler(RuntimeError)
    def failed_request(exc):
        return jsonify(status="error", message=str(exc)), 409

    @bp.get("/capabilities")
    def capabilities():
        return jsonify(version=1, fmp4=True, sequence=True)

    @bp.post("/start")
    def start():
        with action_lock:
            release_inputs()
            data = request.get_json(silent=True) or {}
            if tuple(pyautogui.size()) != (1920, 1080):
                raise ValueError(
                    "Set the VM screen to 1920x1080 before starting the experiment"
                )
            return jsonify(recorder.start(fragment_ms=data.get("fragment_ms", 100)))

    @bp.get("/status")
    def status():
        check_session()
        return jsonify(recorder.status())

    @bp.get("/observation")
    def observation():
        check_session()
        with action_lock:
            started = recorder.elapsed()
            response = app.make_response(capture())
            response.direct_passthrough = False
            image = response.get_data()
            response.close()
            ended = recorder.elapsed()
        return jsonify(
            task_time_s=(started + ended) / 2,
            capture_started_s=started,
            capture_finished_s=ended,
            image=base64.b64encode(image).decode("ascii"),
        )

    @bp.post("/frames")
    def frames():
        data = check_session()
        result = recorder.index.get_frames(data.get("times_s"))
        result["task_time_s"] = recorder.elapsed()
        return jsonify(result)

    @bp.post("/sequence")
    def sequence():
        data = check_session()
        groups = data.get("groups")
        pause = data.get("pause", 0)
        if not isinstance(groups, list) or not 1 <= len(groups) <= 16:
            raise ValueError("Sequence must contain 1-16 actions")
        if (
            isinstance(pause, bool)
            or not isinstance(pause, (int, float))
            or not math.isfinite(pause)
            or pause < 0
        ):
            raise ValueError("Invalid pause")
        # Validate the entire envelope before executing anything.
        for i, group in enumerate(groups):
            if not isinstance(group.get("commands"), list) or not all(
                isinstance(c, str) for c in group["commands"]
            ):
                raise ValueError("Commands must be strings")
            kind = group["action"]["action_type"]
            if kind in {"DONE", "FAIL"} and i != len(groups) - 1:
                raise ValueError("Terminal action must be last")
        results, done, info = [], False, {}
        with action_lock:
            # Legacy actions run in fresh PyAutoGUI subprocesses, whose PAUSE is
            # 0.1. Do not inherit main.py's in-process PAUSE=0 for the sequence group.
            original_pause, original_failsafe = pyautogui.PAUSE, pyautogui.FAILSAFE
            pyautogui.PAUSE, pyautogui.FAILSAFE = 0.1, False
            namespace = {"pyautogui": pyautogui, "time": time}
            try:
                for group in groups:
                    action = group["action"]
                    kind = action["action_type"]
                    params = action.get("parameters", {})
                    started = recorder.elapsed()
                    started_monotonic = time.monotonic()
                    if kind == "WAIT":
                        time.sleep(pause)
                    elif kind in {"DONE", "FAIL"}:
                        done, info = True, {kind.lower(): True}
                    else:
                        for command in group["commands"]:
                            exec(command, namespace)
                        if kind == "KEY_DOWN":
                            held_keys.add(params["key"])
                        if kind == "KEY_UP":
                            held_keys.discard(params["key"])
                        if kind == "MOUSE_DOWN":
                            held_buttons.add(params.get("button", "left"))
                        if kind == "MOUSE_UP":
                            held_buttons.discard(params.get("button", "left"))
                    results.append(
                        {
                            "action": action,
                            "started_s": started,
                            "finished_s": recorder.elapsed(),
                            "duration_s": time.monotonic() - started_monotonic,
                        }
                    )
                    if done:
                        release_inputs()
                        break
            except Exception as exc:
                release_inputs()
                results.append(
                    {
                        "action": action,
                        "started_s": started,
                        "finished_s": recorder.elapsed(),
                        "status": "error",
                    }
                )
                return (
                    jsonify(
                        status="error", message=type(exc).__name__, actions=results
                    ),
                    500,
                )
            finally:
                pyautogui.PAUSE, pyautogui.FAILSAFE = original_pause, original_failsafe
        return jsonify(status="ok", actions=results, done=done, info=info)

    @bp.post("/stop")
    def stop():
        check_session()
        with action_lock:
            release_inputs()
            recorder.stop()
        return jsonify(status="ok", session_id=recorder.session_id)

    @bp.get("/artifact/<name>")
    def artifact(name):
        check_session()
        if name not in {
            "recording.mp4",
            "recording_ffmpeg.log",
            "recording_index.json",
        }:
            raise ValueError("Unknown artifact")
        return send_file(recorder.path / name, as_attachment=True)

    app.register_blueprint(bp)
    app.extensions["osworld_realtime"] = recorder
