"""Incremental, bounded reads of completed video-only fMP4 fragments.

The writer uses default-base-is-moof, no B frames, and fragments at keyframes.
No decoder ever receives a live file tail. A query copies just the initialization
segment and completed fragments from the preceding keyframe through its target.
"""
import base64
import bisect
import json
import math
import os
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError("Truncated extended box header")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        if size < header or start + size > end:
            raise ValueError("Incomplete or unsupported MP4 box")
        yield kind, data[start + header : start + size]
        start += size
    if start != end:
        raise ValueError("Trailing bytes inside an MP4 box")


def child(data, name):
    return next(value for kind, value in boxes(data) if kind == name)


@dataclass(frozen=True)
class Fragment:
    start: int
    end: int
    times: tuple
    keyframe: bool


class FragmentIndex:
    def __init__(self, path, *, ffmpeg="ffmpeg"):
        self.path = Path(path)
        self.ffmpeg = ffmpeg
        self.offset = 0
        self.init = b""
        self.pending = None
        self.timescale = None
        self.defaults = (0, 0, 0)
        self.fragments = []
        self.frame_times = []
        self.frame_locations = []
        self.origin_pts = None
        self.lock = threading.RLock()

    def _init_movie(self, data):
        trak = child(data, b"trak")
        mdia = child(trak, b"mdia")
        mdhd = child(mdia, b"mdhd")
        self.timescale = struct.unpack_from(">I", mdhd, 20 if mdhd[0] == 1 else 12)[0]
        trex = child(child(data, b"mvex"), b"trex")
        self.defaults = struct.unpack_from(">III", trex, 12)

    def _samples(self, data):
        traf = child(data, b"traf")
        tfhd = child(traf, b"tfhd")
        flags = int.from_bytes(tfhd[1:4], "big")
        cursor = 8
        if flags & 1:
            raise ValueError(
                "Writer must use default_base_moof, not absolute base offsets"
            )
        if flags & 2:
            cursor += 4
        duration, size, default_flags = self.defaults
        if flags & 8:
            duration = struct.unpack_from(">I", tfhd, cursor)[0]
            cursor += 4
        if flags & 16:
            size = struct.unpack_from(">I", tfhd, cursor)[0]
            cursor += 4
        if flags & 32:
            default_flags = struct.unpack_from(">I", tfhd, cursor)[0]
        tfdt = child(traf, b"tfdt")
        dts = struct.unpack_from(">Q" if tfdt[0] == 1 else ">I", tfdt, 4)[0]
        times, sync = [], []
        for kind, trun in boxes(traf):
            if kind != b"trun":
                continue
            flags = int.from_bytes(trun[1:4], "big")
            count = struct.unpack_from(">I", trun, 4)[0]
            cursor = 8 + (4 if flags & 1 else 0)
            first_flags = None
            if flags & 4:
                first_flags = struct.unpack_from(">I", trun, cursor)[0]
                cursor += 4
            for i in range(count):
                sample_duration, sample_flags, cts = duration, default_flags, 0
                if flags & 256:
                    sample_duration = struct.unpack_from(">I", trun, cursor)[0]
                    cursor += 4
                if flags & 512:
                    cursor += 4
                if flags & 1024:
                    sample_flags = struct.unpack_from(">I", trun, cursor)[0]
                    cursor += 4
                elif i == 0 and first_flags is not None:
                    sample_flags = first_flags
                if flags & 2048:
                    cts = struct.unpack_from(
                        ">i" if trun[0] == 1 else ">I", trun, cursor
                    )[0]
                    cursor += 4
                # A finalized VFR file may give its last frame duration zero.
                # Its start timestamp and encoded image are still valid.
                times.append((dts + cts) / self.timescale)
                sync.append(not bool(sample_flags & 0x10000))
                dts += sample_duration
        if not times:
            raise ValueError("Empty video fragment")
        return tuple(times), sync[0]

    def refresh(self):
        with self.lock:
            if not self.path.exists():
                return
            size = self.path.stat().st_size
            if size < self.offset:
                raise RuntimeError(
                    "Recording was replaced; create a new index for the new task"
                )
            with self.path.open("rb") as f:
                while self.offset + 8 <= size:
                    f.seek(self.offset)
                    length, kind = struct.unpack(">I4s", f.read(8))
                    header = 8
                    if length == 1:
                        if self.offset + 16 > size:
                            break
                        length = struct.unpack(">Q", f.read(8))[0]
                        header = 16
                    if length == 0:  # unbounded live tail is never safe to read
                        break
                    if length < header:
                        raise ValueError("Invalid top-level box size")
                    end = self.offset + length
                    if end > size:
                        break
                    if kind in {b"ftyp", b"moov"}:
                        f.seek(self.offset)
                        complete = f.read(length)
                        self.init += complete
                        if kind == b"moov":
                            self._init_movie(complete[header:])
                    elif kind == b"moof":
                        self.pending = (self.offset, f.read(length - header))
                    elif kind == b"mdat" and self.pending is not None:
                        start, moof = self.pending
                        pts, keyframe = self._samples(moof)
                        if self.origin_pts is None:
                            self.origin_pts = pts[0]
                        times = tuple(t - self.origin_pts for t in pts)
                        if self.frame_times and times[0] < self.frame_times[-1]:
                            raise ValueError(
                                "Expected monotonic video timestamps (B frames disabled)"
                            )
                        index = len(self.fragments)
                        self.fragments.append(Fragment(start, end, times, keyframe))
                        self.frame_times.extend(times)
                        self.frame_locations.extend(
                            (index, i) for i in range(len(times))
                        )
                        self.pending = None
                    self.offset = end

    @property
    def available_until_s(self):
        return self.frame_times[-1] if self.frame_times else None

    def get_frames(self, times_s):
        if (
            not isinstance(times_s, list)
            or not 1 <= len(times_s) <= 8
            or any(
                isinstance(t, bool)
                or not isinstance(t, (float, int))
                or not math.isfinite(t)
                or t < 0
                for t in times_s
            )
        ):
            raise ValueError("times_s must contain 1-8 finite nonnegative numbers")
        # Freeze the set of complete fragments for this request.
        with self.lock:
            self.refresh()
            frames, available = [], self.available_until_s
            for requested in times_s:
                result = {"requested_time_s": requested, "available_until_s": available}
                if available is None or requested > available:
                    frames.append({**result, "status": "not_ready"})
                    continue
                upper = bisect.bisect_left(self.frame_times, requested)
                candidates = [max(0, upper - 1), min(upper, len(self.frame_times) - 1)]
                selected = min(
                    candidates,
                    key=lambda i: (
                        abs(self.frame_times[i] - requested),
                        self.frame_times[i],
                    ),
                )
                if (
                    abs(
                        abs(self.frame_times[candidates[0]] - requested)
                        - abs(self.frame_times[candidates[1]] - requested)
                    )
                    < 1e-9
                ):
                    selected = candidates[0]
                target_fragment, local_index = self.frame_locations[selected]
                begin = target_fragment
                while begin > 0 and not self.fragments[begin].keyframe:
                    begin -= 1
                if not self.fragments[begin].keyframe:
                    raise ValueError("No preceding keyframe fragment")
                decode_index = (
                    sum(len(f.times) for f in self.fragments[begin:target_fragment])
                    + local_index
                )
                with self.path.open("rb") as source:
                    source.seek(self.fragments[begin].start)
                    data = self.init + source.read(
                        self.fragments[target_fragment].end
                        - self.fragments[begin].start
                    )
                try:
                    decoded = subprocess.run(
                        [
                            self.ffmpeg,
                            "-v",
                            "error",
                            "-threads",
                            "1",
                            "-i",
                            "pipe:0",
                            "-vf",
                            f"select=eq(n\\,{decode_index})",
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
                        input=data,
                        capture_output=True,
                        timeout=20,
                        check=True,
                    )
                    if not decoded.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise RuntimeError("Decoder did not produce a PNG")
                    result.update(
                        status="ok",
                        actual_time_s=self.frame_times[selected],
                        image={
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(decoded.stdout).decode("ascii"),
                        },
                    )
                except (subprocess.SubprocessError, RuntimeError) as exc:
                    result.update(
                        status="error",
                        message=type(exc).__name__ + ": frame decoding failed",
                    )
                frames.append(result)
            return {"frames": frames, "available_until_s": available}

    def save(self, path):
        with self.lock:
            self.refresh()
            Path(path).write_text(
                json.dumps(
                    {
                        "timescale": self.timescale,
                        "frame_times_s": self.frame_times,
                        "fragments": [
                            {
                                "start_byte": f.start,
                                "end_byte": f.end,
                                "first_time_s": f.times[0],
                                "last_time_s": f.times[-1],
                                "keyframe": f.keyframe,
                            }
                            for f in self.fragments
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
