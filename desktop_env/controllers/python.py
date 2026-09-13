import json
import base64
import logging
import os
import random
from typing import Any, Dict, Optional
import time
import traceback
import requests

from desktop_env.actions import KEYBOARD_KEYS

logger = logging.getLogger("desktopenv.pycontroller")


class SequenceExecutionError(RuntimeError):
    """A failed sequence may have executed a prefix; never replay it automatically."""
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details

PYAUTOGUI_INPUT_PATCH = """
import platform as _osworld_platform
import pyautogui as _osworld_pyautogui

_osworld_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:"<>?'
_osworld_linux_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:">?'
_osworld_pyautogui.isShiftCharacter = (
    lambda character: character.isupper()
    or character in (
        _osworld_linux_shift_chars
        if _osworld_platform.system() == "Linux"
        else _osworld_shift_chars
    )
)
"""

PYAUTOGUI_PKGS_PREFIX = (
    "import pyautogui; import time; import platform; "
    "pyautogui.FAILSAFE = False; "
    "_osworld_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\"<>?'; "
    "_osworld_linux_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\">?'; "
    "pyautogui.isShiftCharacter = lambda character: character.isupper() or "
    "character in (_osworld_linux_shift_chars if platform.system() == 'Linux' else _osworld_shift_chars); "
    "{command}"
)


def _inject_pyautogui_input_patch(script: str) -> str:
    if "pyautogui" not in script:
        return script

    lines = script.splitlines(keepends=True)
    insert_at = 0

    if lines and lines[0].startswith("#!"):
        insert_at = 1

    if insert_at < len(lines) and "coding" in lines[insert_at] and lines[insert_at].lstrip().startswith("#"):
        insert_at += 1

    if insert_at < len(lines):
        stripped = lines[insert_at].lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                insert_at += 1
            else:
                insert_at += 1
                while insert_at < len(lines):
                    if quote in lines[insert_at]:
                        insert_at += 1
                        break
                    insert_at += 1

    while insert_at < len(lines) and lines[insert_at].lstrip().startswith("from __future__ import "):
        insert_at += 1

    return "".join(lines[:insert_at]) + PYAUTOGUI_INPUT_PATCH + "\n" + "".join(lines[insert_at:])


class PythonController:
    def __init__(self, vm_ip: str,
                 server_port: int,
                 pkgs_prefix: str = PYAUTOGUI_PKGS_PREFIX):
        self.vm_ip = vm_ip
        self.http_server = f"http://{vm_ip}:{server_port}"
        self.pkgs_prefix = pkgs_prefix  # fixme: this is a hacky way to execute python commands. fix it and combine it with installation of packages
        self.retry_times = 3
        self.retry_interval = 5

    @staticmethod
    def _is_valid_image_response(content_type: str, data: Optional[bytes]) -> bool:
        """Quick validation for PNG/JPEG payload using magic bytes; Content-Type is advisory.
        Returns True only when bytes look like a real PNG or JPEG.
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        # PNG magic
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        # JPEG magic
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return True
        # If server explicitly marks as image, accept as a weak fallback (some environments strip magic)
        if content_type and ("image/png" in content_type or "image/jpeg" in content_type or "image/jpg" in content_type):
            return True
        return False

    def get_screenshot(self) -> Optional[bytes]:
        """
        Gets a screenshot from the server. With the cursor. None -> no screenshot or unexpected error.
        """

        if getattr(self, "realtime_session", None):
            data = self._realtime_request("GET", "/observation")
            self.last_observation_time = data["task_time_s"]
            self.last_capture_interval = (data["capture_started_s"], data["capture_finished_s"])
            return base64.b64decode(data["image"], validate=True)

        for attempt_idx in range(self.retry_times):
            try:
                response = requests.get(self.http_server + "/screenshot", timeout=10)
                if response.status_code == 200:
                    content_type = response.headers.get("Content-Type", "")
                    content = response.content
                    if self._is_valid_image_response(content_type, content):
                        logger.info("Got screenshot successfully")
                        return content
                    else:
                        logger.error("Invalid screenshot payload (attempt %d/%d).", attempt_idx + 1, self.retry_times)
                        logger.info("Retrying to get screenshot.")
                else:
                    logger.error("Failed to get screenshot. Status code: %d", response.status_code)
                    logger.info("Retrying to get screenshot.")
            except Exception as e:
                logger.error("An error occurred while trying to get the screenshot: %s", e)
                logger.info("Retrying to get screenshot.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get screenshot.")
        return None

    def get_accessibility_tree(self) -> Optional[str]:
        """
        Gets the accessibility tree from the server. None -> no accessibility tree or unexpected error.
        """

        for _ in range(self.retry_times):
            try:
                response: requests.Response = requests.get(self.http_server + "/accessibility")
                if response.status_code == 200:
                    logger.info("Got accessibility tree successfully")
                    return response.json()["AT"]
                else:
                    logger.error("Failed to get accessibility tree. Status code: %d", response.status_code)
                    logger.info("Retrying to get accessibility tree.")
            except Exception as e:
                logger.error("An error occurred while trying to get the accessibility tree: %s", e)
                logger.info("Retrying to get accessibility tree.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get accessibility tree.")
        return None

    def get_terminal_output(self) -> Optional[str]:
        """
        Gets the terminal output from the server. None -> no terminal output or unexpected error.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.get(self.http_server + "/terminal")
                if response.status_code == 200:
                    logger.info("Got terminal output successfully")
                    return response.json()["output"]
                else:
                    logger.error("Failed to get terminal output. Status code: %d", response.status_code)
                    logger.info("Retrying to get terminal output.")
            except Exception as e:
                logger.error("An error occurred while trying to get the terminal output: %s", e)
                logger.info("Retrying to get terminal output.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get terminal output.")
        return None

    def get_file(self, file_path: str) -> Optional[bytes]:
        """
        Gets a file from the server.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/file", data={"file_path": file_path})
                if response.status_code == 200:
                    logger.info("File downloaded successfully")
                    return response.content
                else:
                    logger.error("Failed to get file. Status code: %d", response.status_code)
                    logger.info("Retrying to get file.")
            except Exception as e:
                logger.error("An error occurred while trying to get the file: %s", e)
                logger.info("Retrying to get file.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get file.")
        return None

    def execute_python_command(self, command: str) -> None:
        """
        Executes a python command on the server.
        It can be used to execute the pyautogui commands, or... any other python command. who knows?
        """
        # command_list = ["python", "-c", self.pkgs_prefix.format(command=command)]
        command_list = ["python", "-c", self.pkgs_prefix.format(command=command)]
        payload = json.dumps({"command": command_list, "shell": False})

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/execute", headers={'Content-Type': 'application/json'},
                                         data=payload, timeout=120)
                if response.status_code == 200:
                    logger.info("Command executed successfully: %s", response.text)
                    return response.json()
                else:
                    logger.error("Failed to execute command. Status code: %d", response.status_code)
                    logger.info("Retrying to execute command.")
            except requests.exceptions.ReadTimeout:
                break
            except Exception as e:
                logger.error("An error occurred while trying to execute the command: %s", e)
                logger.info("Retrying to execute command.")
            time.sleep(self.retry_interval)

        logger.error("Failed to execute command.")
        return None
    
    def run_python_script(self, script: str) -> Optional[Dict[str, Any]]:
        """
        Executes a python script on the server.
        """
        script = _inject_pyautogui_input_patch(script)
        payload = json.dumps({"code": script})

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/run_python", headers={'Content-Type': 'application/json'},
                                         data=payload, timeout=90)
                if response.status_code == 200:
                    return response.json()
                else:
                    return {"status": "error", "message": "Failed to execute command.", "output": None, "error": response.json()["error"]}
            except requests.exceptions.ReadTimeout:
                break
            except Exception:
                logger.error("An error occurred while trying to execute the command: %s", traceback.format_exc())
                logger.info("Retrying to execute command.")
            time.sleep(self.retry_interval)

        logger.error("Failed to execute command.")
        return {"status": "error", "message": "Failed to execute command.", "output": "", "error": "Retry limit reached."}
    
    def run_bash_script(self, script: str, timeout: int = 30, working_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Executes a bash script on the server.
        
        :param script: The bash script content (can be multi-line)
        :param timeout: Execution timeout in seconds (default: 30)
        :param working_dir: Working directory for script execution (optional)
        :return: Dictionary with status, output, error, and returncode, or None if failed
        """
        payload = json.dumps({
            "script": script,
            "timeout": timeout,
            "working_dir": working_dir
        })

        for _ in range(self.retry_times):
            try:
                response = requests.post(
                    self.http_server + "/run_bash_script", 
                    headers={'Content-Type': 'application/json'},
                    data=payload, 
                    timeout=timeout + 100  # Add buffer to HTTP timeout
                )
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Bash script executed successfully with return code: %d", result.get("returncode", -1))
                    return result
                else:
                    logger.error("Failed to execute bash script. Status code: %d, response: %s", 
                                response.status_code, response.text)
                    logger.info("Retrying to execute bash script.")
            except requests.exceptions.ReadTimeout:
                logger.error("Bash script execution timed out")
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Script execution timed out after {timeout} seconds",
                    "returncode": -1
                }
            except Exception as e:
                logger.error("An error occurred while trying to execute the bash script: %s", e)
                logger.info("Retrying to execute bash script.")
            time.sleep(self.retry_interval)

        logger.error("Failed to execute bash script after %d retries.", self.retry_times)
        return {
            "status": "error",
            "output": "",
            "error": f"Failed to execute bash script after {self.retry_times} retries",
            "returncode": -1
        }

    def execute_action(self, action):
        """
        Executes an action on the server computer.
        """
        # Handle string actions
        if action in ['WAIT', 'FAIL', 'DONE']:
            return
        
        # Handle dictionary actions
        if type(action) == dict and action.get('action_type') in ['WAIT', 'FAIL', 'DONE']:
            return

        action_type = action["action_type"]
        parameters = action["parameters"] if "parameters" in action else {param: action[param] for param in action if param != 'action_type'}
        move_mode = random.choice(
            ["pyautogui.easeInQuad", "pyautogui.easeOutQuad", "pyautogui.easeInOutQuad", "pyautogui.easeInBounce",
             "pyautogui.easeInElastic"])
        duration = random.uniform(0.5, 1)

        if action_type == "MOVE_TO":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.moveTo()")
            elif "x" in parameters and "y" in parameters:
                x = parameters["x"]
                y = parameters["y"]
                self.execute_python_command(f"pyautogui.moveTo({x}, {y}, {duration}, {move_mode})")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "CLICK":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.click()")
            elif set(parameters) == {"num_clicks"}:
                self.execute_python_command(f"pyautogui.click(clicks={parameters['num_clicks']})")
            elif "button" in parameters and "x" in parameters and "y" in parameters:
                button = parameters["button"]
                x = parameters["x"]
                y = parameters["y"]
                if "num_clicks" in parameters:
                    num_clicks = parameters["num_clicks"]
                    self.execute_python_command(
                        f"pyautogui.click(button='{button}', x={x}, y={y}, clicks={num_clicks})")
                else:
                    self.execute_python_command(f"pyautogui.click(button='{button}', x={x}, y={y})")
            elif "button" in parameters and "x" not in parameters and "y" not in parameters:
                button = parameters["button"]
                if "num_clicks" in parameters:
                    num_clicks = parameters["num_clicks"]
                    self.execute_python_command(f"pyautogui.click(button='{button}', clicks={num_clicks})")
                else:
                    self.execute_python_command(f"pyautogui.click(button='{button}')")
            elif "button" not in parameters and "x" in parameters and "y" in parameters:
                x = parameters["x"]
                y = parameters["y"]
                if "num_clicks" in parameters:
                    num_clicks = parameters["num_clicks"]
                    self.execute_python_command(f"pyautogui.click(x={x}, y={y}, clicks={num_clicks})")
                else:
                    self.execute_python_command(f"pyautogui.click(x={x}, y={y})")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "MOUSE_DOWN":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.mouseDown()")
            elif "button" in parameters:
                button = parameters["button"]
                self.execute_python_command(f"pyautogui.mouseDown(button='{button}')")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "MOUSE_UP":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.mouseUp()")
            elif "button" in parameters:
                button = parameters["button"]
                self.execute_python_command(f"pyautogui.mouseUp(button='{button}')")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "RIGHT_CLICK":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.rightClick()")
            elif "x" in parameters and "y" in parameters:
                x = parameters["x"]
                y = parameters["y"]
                self.execute_python_command(f"pyautogui.rightClick(x={x}, y={y})")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "DOUBLE_CLICK":
            if parameters == {} or None:
                self.execute_python_command("pyautogui.doubleClick()")
            elif "x" in parameters and "y" in parameters:
                x = parameters["x"]
                y = parameters["y"]
                self.execute_python_command(f"pyautogui.doubleClick(x={x}, y={y})")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "DRAG_TO":
            if "x" in parameters and "y" in parameters:
                x = parameters["x"]
                y = parameters["y"]
                self.execute_python_command(
                    f"pyautogui.dragTo({x}, {y}, duration=1.0, button='left', mouseDownUp=True)")

        elif action_type == "SCROLL":
            # todo: check if it is related to the operating system, as https://github.com/TheDuckAI/DuckTrack/blob/main/ducktrack/playback.py pointed out
            if "dx" in parameters and "dy" in parameters:
                dx = parameters["dx"]
                dy = parameters["dy"]
                self.execute_python_command(f"pyautogui.hscroll({dx})")
                self.execute_python_command(f"pyautogui.vscroll({dy})")
            elif "dx" in parameters and "dy" not in parameters:
                dx = parameters["dx"]
                self.execute_python_command(f"pyautogui.hscroll({dx})")
            elif "dx" not in parameters and "dy" in parameters:
                dy = parameters["dy"]
                self.execute_python_command(f"pyautogui.vscroll({dy})")
            else:
                raise Exception(f"Unknown parameters: {parameters}")

        elif action_type == "TYPING":
            if "text" not in parameters:
                raise Exception(f"Unknown parameters: {parameters}")
            # deal with special ' and \ characters
            # text = parameters["text"].replace("\\", "\\\\").replace("'", "\\'")
            # self.execute_python_command(f"pyautogui.typewrite('{text}')")
            text = parameters["text"]
            self.execute_python_command("pyautogui.typewrite({:})".format(repr(text)))

        elif action_type == "PRESS":
            if "key" not in parameters:
                raise Exception(f"Unknown parameters: {parameters}")
            key = parameters["key"]
            if key.lower() not in KEYBOARD_KEYS:
                raise Exception(f"Key must be one of {KEYBOARD_KEYS}")
            self.execute_python_command(f"pyautogui.press({key!r})")

        elif action_type == "KEY_DOWN":
            if "key" not in parameters:
                raise Exception(f"Unknown parameters: {parameters}")
            key = parameters["key"]
            if key.lower() not in KEYBOARD_KEYS:
                raise Exception(f"Key must be one of {KEYBOARD_KEYS}")
            self.execute_python_command(f"pyautogui.keyDown({key!r})")

        elif action_type == "KEY_UP":
            if "key" not in parameters:
                raise Exception(f"Unknown parameters: {parameters}")
            key = parameters["key"]
            if key.lower() not in KEYBOARD_KEYS:
                raise Exception(f"Key must be one of {KEYBOARD_KEYS}")
            self.execute_python_command(f"pyautogui.keyUp({key!r})")

        elif action_type == "HOTKEY":
            if "keys" not in parameters:
                raise Exception(f"Unknown parameters: {parameters}")
            keys = parameters["keys"]
            if not isinstance(keys, list):
                raise Exception("Keys must be a list of keys")
            for key in keys:
                if key.lower() not in KEYBOARD_KEYS:
                    raise Exception(f"Key must be one of {KEYBOARD_KEYS}")

            self.execute_python_command(f"pyautogui.hotkey(*{keys!r})")

        elif action_type in ['WAIT', 'FAIL', 'DONE']:
            pass

        else:
            raise Exception(f"Unknown action type: {action_type}")

    def _realtime_request(self, method, path, payload=None, timeout=60):
        payload = dict(payload or {})
        if getattr(self, "realtime_session", None):
            payload["session_id"] = self.realtime_session
        response = requests.request(method, self.http_server + "/realtime" + path,
                                    **({"params": payload} if method == "GET" else {"json": payload}),
                                    timeout=timeout)
        # Never automatically replay a sequence after a transport error: it may
        # already have executed on the VM.
        if not response.ok:
            if path == "/sequence":
                try:
                    details = response.json()
                except ValueError:
                    details = {"status": "unknown", "actions": []}
                raise SequenceExecutionError(f"VM sequence failed ({response.status_code})", details)
            raise RuntimeError(f"Realtime endpoint {path} failed ({response.status_code}): {response.text[:500]}")
        return response.json()

    def start_realtime_recording(self, fragment_ms=100):
        response = requests.get(self.http_server + "/realtime/capabilities", timeout=10)
        if response.status_code != 200:
            raise RuntimeError("VM server needs the realtime extension; run scripts/python/install_realtime_server.py first")
        self.realtime_session = None
        data = self._realtime_request("POST", "/start", {"fragment_ms": fragment_ms})
        self.realtime_session = data["session_id"]
        return data

    def get_frames(self, times_s):
        return self._realtime_request("POST", "/frames", {"times_s": times_s}, timeout=180)

    def execute_sequence(self, actions, pause=0):
        from mm_agents.realtime_protocol import validate_action
        # Reuse the official action compiler, including random MOVE_TO duration.
        collector = object.__new__(PythonController)
        groups = []
        for action in actions:
            validate_action(action)
            commands = []
            collector.execute_python_command = lambda code: commands.append(PYAUTOGUI_PKGS_PREFIX.format(command=code))
            collector.execute_action(action)
            groups.append({"action": action, "commands": commands})
        return self._realtime_request("POST", "/sequence", {"groups": groups, "pause": pause}, timeout=300)

    def end_realtime_recording(self, directory):
        self._realtime_request("POST", "/stop")
        os.makedirs(directory, exist_ok=True)
        for name in ("recording.mp4", "recording_ffmpeg.log", "recording_index.json"):
            with requests.get(self.http_server + "/realtime/artifact/" + name,
                              params={"session_id": self.realtime_session}, stream=True, timeout=120) as response:
                response.raise_for_status()
                temporary = os.path.join(directory, name + ".tmp")
                with open(temporary, "wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
                os.replace(temporary, os.path.join(directory, name))
        self.realtime_session = None

    # Record video
    def start_recording(self):
        """
        Starts recording the screen.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/start_recording")
                if response.status_code == 200:
                    logger.info("Recording started successfully")
                    return
                else:
                    logger.error("Failed to start recording. Status code: %d", response.status_code)
                    logger.info("Retrying to start recording.")
            except Exception as e:
                logger.error("An error occurred while trying to start recording: %s", e)
                logger.info("Retrying to start recording.")
            time.sleep(self.retry_interval)

        logger.error("Failed to start recording.")

    def end_recording(self, dest: str):
        """
        Ends recording the screen.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/end_recording")
                if response.status_code == 200:
                    logger.info("Recording stopped successfully")
                    with open(dest, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    # Preserve the FFmpeg progress and diagnostics next to the
                    # downloaded MP4. Older VM images do not expose this file,
                    # so a missing log must not turn a valid recording into an
                    # evaluation failure.
                    try:
                        log_response = requests.post(
                            self.http_server + "/file",
                            data={"file_path": "/tmp/recording_ffmpeg.log"},
                            timeout=30,
                        )
                        if log_response.status_code == 200:
                            log_dest = os.path.join(
                                os.path.dirname(dest), "recording_ffmpeg.log"
                            )
                            with open(log_dest, "wb") as log_file:
                                log_file.write(log_response.content)
                            logger.info("Recording diagnostics saved to %s", log_dest)
                        else:
                            logger.warning(
                                "FFmpeg recording log was unavailable. Status code: %d",
                                log_response.status_code,
                            )
                    except Exception as log_error:
                        logger.warning(
                            "Could not download FFmpeg recording log: %s", log_error
                        )
                    return
                else:
                    logger.error("Failed to stop recording. Status code: %d", response.status_code)
                    logger.info("Retrying to stop recording.")
            except Exception as e:
                logger.error("An error occurred while trying to stop recording: %s", e)
                logger.info("Retrying to stop recording.")
            time.sleep(self.retry_interval)

        logger.error("Failed to stop recording.")

    # Additional info
    def get_vm_platform(self):
        """
        Gets the size of the vm screen.
        """
        return self.execute_python_command("import platform; print(platform.system())")['output'].strip()
    
    def get_vm_machine(self):
        """
        Gets the machine of the vm.
        """
        return self.execute_python_command("import platform; print(platform.machine())")['output'].strip()


    def get_vm_screen_size(self):
        """
        Gets the size of the vm screen.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/screen_size")
                if response.status_code == 200:
                    logger.info("Got screen size successfully")
                    return response.json()
                else:
                    logger.error("Failed to get screen size. Status code: %d", response.status_code)
                    logger.info("Retrying to get screen size.")
            except Exception as e:
                logger.error("An error occurred while trying to get the screen size: %s", e)
                logger.info("Retrying to get screen size.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get screen size.")
        return None

    def get_vm_window_size(self, app_class_name: str):
        """
        Gets the size of the vm app window.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/window_size", data={"app_class_name": app_class_name})
                if response.status_code == 200:
                    logger.info("Got window size successfully")
                    return response.json()
                else:
                    logger.error("Failed to get window size. Status code: %d", response.status_code)
                    logger.info("Retrying to get window size.")
            except Exception as e:
                logger.error("An error occurred while trying to get the window size: %s", e)
                logger.info("Retrying to get window size.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get window size.")
        return None

    def get_vm_wallpaper(self):
        """
        Gets the wallpaper of the vm.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/wallpaper")
                if response.status_code == 200:
                    logger.info("Got wallpaper successfully")
                    return response.content
                else:
                    logger.error("Failed to get wallpaper. Status code: %d", response.status_code)
                    logger.info("Retrying to get wallpaper.")
            except Exception as e:
                logger.error("An error occurred while trying to get the wallpaper: %s", e)
                logger.info("Retrying to get wallpaper.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get wallpaper.")
        return None

    def get_vm_desktop_path(self) -> Optional[str]:
        """
        Gets the desktop path of the vm.
        """

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/desktop_path")
                if response.status_code == 200:
                    logger.info("Got desktop path successfully")
                    return response.json()["desktop_path"]
                else:
                    logger.error("Failed to get desktop path. Status code: %d", response.status_code)
                    logger.info("Retrying to get desktop path.")
            except Exception as e:
                logger.error("An error occurred while trying to get the desktop path: %s", e)
                logger.info("Retrying to get desktop path.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get desktop path.")
        return None

    def get_vm_directory_tree(self, path) -> Optional[Dict[str, Any]]:
        """
        Gets the directory tree of the vm.
        """
        payload = json.dumps({"path": path})

        for _ in range(self.retry_times):
            try:
                response = requests.post(self.http_server + "/list_directory", headers={'Content-Type': 'application/json'}, data=payload)
                if response.status_code == 200:
                    logger.info("Got directory tree successfully")
                    return response.json()["directory_tree"]
                else:
                    logger.error("Failed to get directory tree. Status code: %d", response.status_code)
                    logger.info("Retrying to get directory tree.")
            except Exception as e:
                logger.error("An error occurred while trying to get directory tree: %s", e)
                logger.info("Retrying to get directory tree.")
            time.sleep(self.retry_interval)

        logger.error("Failed to get directory tree.")
        return None
