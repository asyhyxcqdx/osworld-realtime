import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_env.evaluators.getters import file as file_getter


def _zip_bytes(name="document.xml", content=b"complete"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


class _SequenceController:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = 0

    def get_file(self, _path):
        self.calls += 1
        return next(self._responses)


class VMFileGetterTests(unittest.TestCase):
    def test_retries_truncated_pptx(self):
        complete = _zip_bytes()
        controller = _SequenceController([complete[:-10], complete])

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            file_getter.time, "sleep"
        ):
            cache_dir = Path(temp_dir)
            env = SimpleNamespace(controller=controller, cache_dir=str(cache_dir))
            result = file_getter.get_vm_file(
                env,
                {
                    "path": "/home/user/Desktop/result.pptx",
                    "dest": "result.pptx",
                },
            )

            self.assertEqual(controller.calls, 2)
            self.assertEqual(result, str(cache_dir / "result.pptx"))
            self.assertEqual((cache_dir / "result.pptx").read_bytes(), complete)

    def test_does_not_publish_truncated_pptx(self):
        complete = _zip_bytes()
        truncated = complete[:-10]
        controller = _SequenceController([truncated] * 3)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            file_getter.time, "sleep"
        ):
            cache_dir = Path(temp_dir)
            env = SimpleNamespace(controller=controller, cache_dir=str(cache_dir))
            existing = cache_dir / "result.pptx"
            existing.write_bytes(complete)
            result = file_getter.get_vm_file(
                env,
                {
                    "path": "/home/user/Desktop/result.pptx",
                    "dest": "result.pptx",
                },
            )

            self.assertEqual(controller.calls, 3)
            self.assertIsNone(result)
            self.assertEqual(existing.read_bytes(), complete)


if __name__ == "__main__":
    unittest.main()
