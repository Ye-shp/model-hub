"""Direct phone bridge input behavior, without connecting to an actual device."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("phone_bridge_client", Path(__file__).resolve().parents[1] / "console" / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class PhoneBridgeTests(unittest.TestCase):
    def setUp(self):
        self.phone = bridge.Phone("adb", "100.64.1.2:5555")
        self.phone.shell = MagicMock()

    def test_ascii_input_does_not_need_unicode_backend(self):
        with patch.object(self.phone, "automation") as backend:
            self.phone.text("hello world")
        backend.assert_not_called()
        self.phone.shell.assert_called_once_with("input", "text", "hello%sworld")

    def test_turkish_text_and_emoji_are_pasted_once_on_the_selected_phone(self):
        text = "Türkiye: çığ, şeker, öğle ☕"
        device = SimpleNamespace(clipboard=text, set_clipboard=MagicMock(),
                                 jsonrpc=SimpleNamespace(pasteClipboard=MagicMock()))
        module = SimpleNamespace(connect=MagicMock(return_value=device))
        with patch.dict(sys.modules, {"uiautomator2": module}):
            result = self.phone.text(text)
        module.connect.assert_called_once_with("100.64.1.2:5555")
        device.set_clipboard.assert_called_once_with(text, "Model Hub")
        device.jsonrpc.pasteClipboard.assert_called_once_with()
        self.assertEqual(result["input"], "unicode_clipboard")
        self.phone.shell.assert_not_called()

    def test_clipboard_mismatch_never_pastes(self):
        device = SimpleNamespace(clipboard="old", set_clipboard=MagicMock(),
                                 jsonrpc=SimpleNamespace(pasteClipboard=MagicMock()))
        with patch.object(self.phone, "automation", return_value=device), self.assertRaisesRegex(RuntimeError, "no paste"):
            self.phone.text("Türkiye")
        device.jsonrpc.pasteClipboard.assert_not_called()

    def test_uncertain_paste_is_not_retried(self):
        paste = MagicMock(side_effect=TimeoutError())
        device = SimpleNamespace(clipboard="Türkiye", set_clipboard=MagicMock(),
                                 jsonrpc=SimpleNamespace(pasteClipboard=paste))
        with patch.object(self.phone, "automation", return_value=device), self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.phone.text("Türkiye")
        paste.assert_called_once_with()
        self.phone.shell.assert_not_called()

    def test_missing_unicode_backend_reports_setup_command(self):
        with patch.dict(sys.modules, {"uiautomator2": None}), self.assertRaisesRegex(RuntimeError, "pip install"):
            self.phone.text("Türkiye")
        self.phone.shell.assert_not_called()

    def test_copied_link_is_returned_verbatim(self):
        url = "https://www.instagram.com/reel/example/"
        with patch.object(self.phone, "automation", return_value=SimpleNamespace(clipboard=url)):
            self.assertEqual(self.phone.clipboard(), {"text": url})

    def test_long_press_keeps_the_same_coordinate(self):
        self.phone.long_press(10, 20)
        self.phone.shell.assert_called_once_with("input", "swipe", "10", "20", "10", "20", "650")


if __name__ == "__main__":
    unittest.main()
