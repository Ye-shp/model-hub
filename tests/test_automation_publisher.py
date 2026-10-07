"""Offline native-phone regressions: no real device, install, or publication."""
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import automation_publisher as publisher


def hierarchy(package, nodes):
    root = ET.Element("hierarchy")
    for node in nodes:
        ET.SubElement(root, "node", {"package": package, "enabled": "true", **node})
    return ET.tostring(root, encoding="unicode")


class Phone:
    def __init__(self, *, platform="instagram", username="owner"):
        self.package = publisher._PACKAGES[platform]
        self.platform, self.username = platform, username
        self.screen, self.caption = "feed", ""
        self.events, self.adb = [], []
        self.submits = 0
        self.media_query = "unique"
        self.compose = True
        self.caption_accepts = True
        self.publish_disconnect = False
        self.screenshots_fail = False
        self.duplicate_submit = False
        self.start_error = False

    def connect(self):
        self.events.append("connect")
        return True

    def is_connected(self):
        return True

    def session(self):
        return self

    def app_start(self, package, **kw):
        self.package, self.screen = package, "feed"

    def app_current(self):
        return {"package": self.package}

    def dump_hierarchy(self):
        if self.screen == "feed":
            nodes = [{"text": "Profile", "bounds": "[0,0][20,20]"}]
        elif self.screen == "profile":
            nodes = [{"text": "Edit profile", "bounds": "[0,30][100,50]"},
                     {"text": self.username if self.platform == "instagram" else "@" + self.username,
                      "resource-id": self.package + ":id/action_bar_title", "bounds": "[0,60][100,80]"}]
        elif self.screen == "compose":
            nodes = [{"text": self.caption or "Write a caption", "class": "android.widget.EditText",
                      "resource-id": self.package + ":id/caption_input_text_view", "bounds": "[0,100][200,150]"},
                     {"text": "Share" if self.platform == "instagram" else "Post", "bounds": "[0,200][100,250]"}]
            if self.duplicate_submit:
                nodes.append({"text": "Share" if self.platform == "instagram" else "Post",
                              "bounds": "[120,200][220,250]"})
        else:
            nodes = [{"text": "Home", "bounds": "[0,0][20,20]"}]
        return hierarchy(self.package, nodes)

    def click(self, x, y):
        if self.screen == "feed":
            self.screen = "profile"
        elif self.screen == "compose" and y > 190:
            self.events.append("submit")
            self.submits += 1
            self.screen = "home"
            if self.publish_disconnect:
                raise RuntimeError("Phone vanished after accepting the tap")

    def send_keys(self, caption, clear=False):
        self.events.append("caption")
        if self.caption_accepts:
            self.caption = caption

    def push_file(self, path):
        self.filename = path.name
        self.transferred = path.read_bytes()
        self.events.append("push")
        return "/sdcard/DCIM/Camera/" + path.name

    def _adb(self, *args):
        self.adb.append(args)
        if args[1] == "content":
            # Ensure the SQL string retains its inner quotes after Android shell
            # parsing, rather than becoming an unquoted column expression.
            self.query = shlex.split(args[-1])[0]
            expected = f"_display_name='{self.filename}'"
            if self.query != expected:
                raise AssertionError("Unsafe or malformed media query")
            if self.media_query == "wrong":
                out = "Row: 0 _id=17, _display_name=another-video.mp4\n"
            elif self.media_query == "duplicate":
                out = f"Row: 0 _id=17, _display_name={self.filename}\nRow: 1 _id=18, _display_name={self.filename}\n"
            else:
                out = f"Row: 0 _id=17, _display_name={self.filename}\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[1] == "am":
            self.events.append("exact_uri")
            self.screen = "compose" if self.compose else "home"
            return subprocess.CompletedProcess(args, 0, "Error: target refused" if self.start_error else "Status: ok", "")
        if args[1] == "input":
            self.click(int(args[3]), int(args[4]))
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("Unexpected ADB operation")

    def screenshot(self, name):
        if self.screenshots_fail:
            raise RuntimeError("No screenshot")
        return Path("/controller/private/screenshots") / (name + ".png")


class NativePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "retained-video.mp4"
        self.source.write_bytes(b"retained approved original video")
        self.post = {"id": 41, "platform": "instagram", "kind": "reel", "caption": "Approved caption #test",
                     "media": [str(self.source)], "media_hashes": [hashlib.sha256(self.source.read_bytes()).hexdigest()]}
        self.account = {"platform": "instagram", "username": "owner"}
        self.settings = {"enabled": True, "dry_run": False, "transport": "direct_adb", "device_serial": "100.64.1.2:5555"}
        self.phone = Phone()
        self.device = patch.object(publisher, "_device", return_value=self.phone)
        self.device.start()
        self.markers = 0

    def tearDown(self):
        self.device.stop()
        self.temporary.cleanup()

    def on_submit(self):
        self.phone.events.append("durable_hold")
        self.markers += 1

    def publish(self):
        return publisher.publish(self.post, self.account, self.settings, self.on_submit, sleep=lambda _: None)

    def test_exact_approved_video_uri_and_caption_precede_one_submit(self):
        result = self.publish()
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(self.phone.transferred, self.source.read_bytes())
        self.assertEqual(self.phone.caption, self.post["caption"])
        intent = [args for args in self.phone.adb if args[1] == "am"][0]
        self.assertEqual(intent[intent.index("--eu") + 2], "content://media/external/video/media/17")
        self.assertEqual(intent[intent.index("-p") + 1], "com.instagram.android")
        self.assertEqual(self.phone.events[-2:], ["durable_hold", "submit"])
        self.assertEqual(self.phone.submits, 1)
        self.assertEqual(self.markers, 1)
        self.assertNotIn("id", result)
        self.assertNotIn("url", result)

    def test_tiktok_uses_exact_account_and_target_package(self):
        self.post.update(platform="tiktok", kind="video")
        self.account["platform"] = "tiktok"
        self.phone.platform = "tiktok"
        result = self.publish()
        self.assertTrue(result["submitted"])
        intent = [args for args in self.phone.adb if args[1] == "am"][0]
        self.assertEqual(intent[intent.index("-p") + 1], "com.zhiliaoapp.musically")

    def test_account_mismatch_prevents_transfer_and_submit(self):
        self.phone.username = "another_account"
        result = self.publish()
        self.assertFalse(result["ok"])
        self.assertNotIn("push", self.phone.events)
        self.assertEqual(self.markers, 0)
        self.assertEqual(self.phone.submits, 0)

    def test_changed_approved_file_rejected_before_phone_connection(self):
        self.source.write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "approved video changed"):
            self.publish()
        self.assertEqual(self.phone.events, [])

    def test_missing_or_implicit_live_enable_is_rejected(self):
        for settings in ({**self.settings, "enabled": False}, {k: v for k, v in self.settings.items() if k != "dry_run"},
                         {**self.settings, "dry_run": True}, {**self.settings, "transport": "bridge"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                publisher.publish(self.post, self.account, settings, self.on_submit, sleep=lambda _: None)
        self.assertEqual(self.phone.events, [])

    def test_image_and_multiple_videos_rejected_before_submit(self):
        for post in ({**self.post, "kind": "image"}, {**self.post, "kind": "story"},
                     {**self.post, "media": [str(self.source)] * 2, "media_hashes": self.post["media_hashes"] * 2}):
            with self.subTest(kind=post["kind"]), self.assertRaises(ValueError):
                publisher.publish(post, self.account, self.settings, self.on_submit, sleep=lambda _: None)
        self.assertEqual(self.phone.events, [])

    def test_wrong_or_ambiguous_media_row_is_manual_hold(self):
        for mode in ("wrong", "duplicate"):
            with self.subTest(mode=mode):
                self.phone.media_query = mode
                result = self.publish()
                self.assertEqual(result["status"], "awaiting_manual_publish")
                self.assertFalse(result["submitted"])
        self.assertEqual(self.markers, 0)
        self.assertEqual(self.phone.submits, 0)

    def test_home_without_composer_never_counts_as_publish(self):
        self.phone.compose = False
        result = self.publish()
        self.assertEqual(result["status"], "awaiting_manual_publish")
        self.assertEqual(self.markers, 0)
        self.assertEqual(self.phone.submits, 0)

    def test_caption_input_not_exact_never_submits(self):
        self.phone.caption_accepts = False
        result = self.publish()
        self.assertEqual(result["status"], "awaiting_manual_publish")
        self.assertFalse(result["submitted"])
        self.assertEqual(self.markers, 0)
        self.assertEqual(self.phone.submits, 0)

    def test_ambiguous_publish_button_is_manual_hold(self):
        self.phone.duplicate_submit = True
        result = self.publish()
        self.assertEqual(result["status"], "awaiting_manual_publish")
        self.assertEqual(self.markers, 0)

    def test_share_intent_rejected_does_not_submit(self):
        self.phone.start_error = True
        result = self.publish()
        self.assertEqual(result["status"], "awaiting_manual_publish")
        self.assertEqual(self.markers, 0)

    def test_lost_lease_callback_propagates_without_native_tap(self):
        def lost_lease():
            raise ValueError("Lease was replaced")
        with self.assertRaisesRegex(ValueError, "Lease was replaced"):
            publisher.publish(self.post, self.account, self.settings, lost_lease, sleep=lambda _: None)
        self.assertEqual(self.phone.submits, 0)

    def test_disconnect_after_submit_preserves_hold_without_retry(self):
        self.phone.publish_disconnect = True
        result = self.publish()
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(self.phone.submits, 1)
        self.assertEqual(self.markers, 1)

    def test_screenshot_failure_cannot_remove_submission_hold(self):
        self.phone.screenshots_fail = True
        result = self.publish()
        self.assertTrue(result["needs_confirmation"])
        self.assertIsNone(result["screenshot"])
        self.assertEqual(self.phone.submits, 1)

    def test_generic_bot_module_is_not_imported_or_replaced(self):
        sentinel = types.ModuleType("bot")
        with patch.dict(sys.modules, {"bot": sentinel}):
            controller = publisher._scaffold_device()
            self.assertIs(sys.modules["bot"], sentinel)
            self.assertTrue(controller.__module__.startswith(publisher._NAMESPACE + "."))


if __name__ == "__main__":
    unittest.main()
