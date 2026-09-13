import sys
import types
import unittest
from unittest.mock import patch

from mm_agents.uitars15_v1 import (
    parse_action_to_structure_output,
    parsing_response_to_pyautogui_code,
)


class FakeClipboardRoot:
    def __init__(self, events):
        self.events = events

    def withdraw(self):
        self.events.append(("withdraw",))

    def clipboard_clear(self):
        self.events.append(("clear",))

    def clipboard_append(self, text):
        self.events.append(("append", text))

    def update(self):
        self.events.append(("update",))

    def destroy(self):
        self.events.append(("destroy",))


class FakePrimaryHolder:
    def __init__(self, events):
        self.events = events

    def insert(self, index, text):
        self.events.append(("primary-insert", index, text))

    def tag_add(self, tag, start, end):
        self.events.append(("primary-select", tag, start, end))

    def selection_own(self, selection):
        self.events.append(("primary-own", selection))


def execute_generated_code(code):
    events = []
    ticks = iter((0.0, 0.1, 1.1))

    fake_pyautogui = types.ModuleType("pyautogui")
    fake_pyautogui.hotkey = lambda *keys: events.append(("hotkey", *keys))
    fake_pyautogui.press = lambda key: events.append(("press", key))
    fake_pyautogui.write = lambda text, interval=0: events.append(
        ("write", text, interval)
    )

    fake_time = types.ModuleType("time")
    fake_time.monotonic = lambda: next(ticks)
    fake_time.sleep = lambda seconds: events.append(("sleep", seconds))

    fake_tkinter = types.ModuleType("tkinter")

    def make_clipboard_root():
        events.append(("tk",))
        return FakeClipboardRoot(events)

    def make_primary_holder(root, exportselection):
        events.append(("primary-holder", exportselection))
        return FakePrimaryHolder(events)

    fake_tkinter.Tk = make_clipboard_root
    fake_tkinter.Text = make_primary_holder

    with patch.dict(
        sys.modules,
        {
            "pyautogui": fake_pyautogui,
            "time": fake_time,
            "tkinter": fake_tkinter,
        },
    ):
        exec(compile(code, "<generated-uitars-action>", "exec"), {})

    return events


class UITars15ClipboardTest(unittest.TestCase):
    def build_type_code(self, content, input_swap=True):
        return parsing_response_to_pyautogui_code(
            {
                "action_type": "type",
                "action_inputs": {"content": content},
            },
            image_height=1080,
            image_width=1920,
            input_swap=input_swap,
        )

    def parse_model_response(self, response):
        return parse_action_to_structure_output(
            response,
            factor=1000,
            origin_resized_height=1080,
            origin_resized_width=1920,
            model_type="qwen25vl",
        )

    def test_clipboard_preserves_quotes_symbols_unicode_and_newlines(self):
        text = (
            "ASCII O'Reilly 'quote' \\ backslash <>|:@\n"
            "Unicode \u4e2d\u6587 \u03a9\nsecond line"
        )
        code = self.build_type_code(text)

        self.assertNotIn("pyperclip", code)
        self.assertNotIn("Xlib", code)
        events = execute_generated_code(code)

        self.assertIn(("append", text), events)
        self.assertIn(("primary-insert", "1.0", text), events)
        self.assertIn(("primary-own", "PRIMARY"), events)
        self.assertIn(("hotkey", "shift", "insert"), events)
        self.assertLess(
            events.index(("hotkey", "shift", "insert")),
            events.index(("destroy",)),
        )

    def test_selection_owners_stay_alive_through_shift_insert(self):
        events = execute_generated_code(self.build_type_code("payload"))

        primary_owner = events.index(("primary-own", "PRIMARY"))
        clipboard_owner = events.index(("append", "payload"))
        paste = events.index(("hotkey", "shift", "insert"))
        destroy = events.index(("destroy",))
        self.assertLess(primary_owner, paste)
        self.assertLess(clipboard_owner, paste)
        self.assertLess(paste, destroy)
        self.assertIn(("sleep", 0.01), events[paste:destroy])

    def test_paste_shortcut_has_no_window_class_dependency(self):
        code = self.build_type_code("payload")

        self.assertNotIn("_NET_ACTIVE_WINDOW", code)
        self.assertNotIn("Xlib", code)
        self.assertIn("pyautogui.hotkey('shift', 'insert')", code)

    def test_full_response_preserves_newlines_blank_line_and_quotes(self):
        model_content = (
            "first O'Reilly 'quoted' \\\\ path\n\n"
            "Unicode 中文 Ω\nlast line"
        )
        expected = (
            "first O'Reilly 'quoted' \\ path\n\n"
            "Unicode 中文 Ω\nlast line"
        )
        response = (
            "Thought: enter the exact text\n"
            f"Action: type(content='{model_content}')"
        )

        parsed = self.parse_model_response(response)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["action_inputs"]["content"], expected)
        code = parsing_response_to_pyautogui_code(
            parsed[0], 1080, 1920, input_swap=True
        )
        self.assertIn(("append", expected), execute_generated_code(code))

    def test_full_response_literal_newline_marker_submits(self):
        response = "Thought: submit it\nAction: type(content='button\\n')"

        parsed = self.parse_model_response(response)

        self.assertEqual(parsed[0]["action_inputs"]["content"], "button\n")
        code = parsing_response_to_pyautogui_code(
            parsed[0], 1080, 1920, input_swap=True
        )
        events = execute_generated_code(code)
        self.assertIn(("append", "button"), events)
        self.assertIn(("press", "enter"), events)

    def test_structured_action_marker_ignores_action_text_in_thought_and_content(self):
        content = "keep Action: inline\nAction: plain content"
        response = (
            "Thought: the label says Action: and Next Action: in the UI\n"
            f"Action: type(content='{content}')"
        )

        parsed = self.parse_model_response(response)

        self.assertEqual(parsed[0]["action_inputs"]["content"], content)
        self.assertEqual(
            parsed[0]["thought"],
            "the label says Action: and Next Action: in the UI",
        )

    def test_type_followed_by_hotkey_parses_as_two_actions(self):
        response = (
            "Thought: enter the command and submit it\n"
            "Action: type(content='cd $sourceDir')\n\n"
            "hotkey(key='enter')"
        )

        parsed = self.parse_model_response(response)

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["action_type"], "type")
        self.assertEqual(parsed[0]["action_inputs"]["content"], "cd $sourceDir")
        self.assertEqual(parsed[1]["action_type"], "hotkey")
        self.assertEqual(parsed[1]["action_inputs"]["key"], "enter")
        events = execute_generated_code(
            parsing_response_to_pyautogui_code(
                parsed,
                image_height=1080,
                image_width=1920,
                input_swap=True,
            )
        )
        self.assertIn(("append", "cd $sourceDir"), events)
        self.assertIn(("hotkey", "enter"), events)

    def test_multiple_actions_accept_common_line_separators(self):
        for separator in ("\n", "\r\n\r\n", "\n \n"):
            with self.subTest(separator=repr(separator)):
                response = (
                    "Thought: enter the command and submit it\n"
                    "Action: type(content='pwd')"
                    f"{separator}hotkey(key='enter')"
                )

                parsed = self.parse_model_response(response)

                self.assertEqual(
                    [item["action_type"] for item in parsed],
                    ["type", "hotkey"],
                )

    def test_action_like_line_inside_type_content_is_not_split(self):
        content = "first line\n\nhotkey(key='enter')\nlast line"
        response = (
            "Thought: enter the text exactly\n"
            f"Action: type(content='{content}')"
        )

        parsed = self.parse_model_response(response)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["action_type"], "type")
        self.assertEqual(parsed[0]["action_inputs"]["content"], content)

    def test_structured_action_marker_inside_type_content_is_not_selected(self):
        content = "first line\nAction: hotkey(key='enter')\nlast line"
        response = (
            "Thought: enter the text exactly\n"
            f"Action: type(content='{content}')"
        )

        parsed = self.parse_model_response(response)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["action_type"], "type")
        self.assertEqual(parsed[0]["action_inputs"]["content"], content)

    def test_trailing_newline_pastes_text_then_presses_enter(self):
        code = self.build_type_code("button\n")
        events = execute_generated_code(code)

        self.assertIn(("append", "button"), events)
        self.assertGreater(
            events.index(("press", "enter")),
            events.index(("destroy",)),
        )

    def test_literal_newline_marker_does_not_strip_preceding_n(self):
        code = self.build_type_code(r"button\n")
        events = execute_generated_code(code)

        self.assertIn(("append", "button"), events)
        self.assertIn(("press", "enter"), events)

    def test_non_clipboard_mode_uses_repr_safe_text(self):
        text = "it's \\ safe"
        code = self.build_type_code(text, input_swap=False)
        events = execute_generated_code(code)

        self.assertIn(("write", text, 0.1), events)


if __name__ == "__main__":
    unittest.main()
