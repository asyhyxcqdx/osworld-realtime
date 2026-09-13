import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_env.controllers.python import PythonController


def _response(status_code, content=b""):
    return SimpleNamespace(
        status_code=status_code,
        content=content,
        iter_content=lambda chunk_size: [content],
    )


class RecordingLogDownloadTests(unittest.TestCase):
    def _controller(self):
        controller = object.__new__(PythonController)
        controller.http_server = "http://desktop:5000"
        controller.retry_times = 1
        controller.retry_interval = 0
        return controller

    def test_downloads_ffmpeg_log_next_to_recording(self):
        responses = [
            _response(200, b"mp4-bytes"),
            _response(200, b"frame=300 time=00:00:10"),
        ]

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "desktop_env.controllers.python.requests.post",
            side_effect=responses,
        ) as post:
            dest = Path(temp_dir) / "recording.mp4"
            self._controller().end_recording(str(dest))

            self.assertEqual(dest.read_bytes(), b"mp4-bytes")
            self.assertEqual(
                (dest.parent / "recording_ffmpeg.log").read_bytes(),
                b"frame=300 time=00:00:10",
            )
            self.assertEqual(post.call_count, 2)
            self.assertEqual(
                post.call_args_list[1].kwargs["data"],
                {"file_path": "/tmp/recording_ffmpeg.log"},
            )

    def test_missing_ffmpeg_log_does_not_discard_valid_recording(self):
        responses = [_response(200, b"mp4-bytes"), _response(404)]

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "desktop_env.controllers.python.requests.post",
            side_effect=responses,
        ):
            dest = Path(temp_dir) / "recording.mp4"
            self._controller().end_recording(str(dest))

            self.assertEqual(dest.read_bytes(), b"mp4-bytes")
            self.assertFalse((dest.parent / "recording_ffmpeg.log").exists())


if __name__ == "__main__":
    unittest.main()
