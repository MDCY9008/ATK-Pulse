"""Exercise Holtek 8K Nano routing and decoding without querying real hardware."""

import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

from atk_query import TargetQuery
from protocols import BindingStore, HidTransport, get_codec


def collection(pid, **overrides):
    return dict(dict(path=f"fixture:{pid:04x}".encode(), vendor_id=0x373B, product_id=pid,
                     usage_page=0xFF10, usage=1, interface_number=2,
                     input_report_length=65, feature_report_length=9, output_report_length=0),
                **overrides)


def reply(battery=0xA9, link=1):
    return [0x82] + [0] * 7 + [battery, link] + [0] * 54


class HoltekNano8kTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.bindings = BindingStore(Path(directory) / "bindings.json")
        self.device = Mock()
        self.device.send_feature_report.return_value = 9
        self.factory = Mock(return_value=self.device)

    def query(self, devices):
        transport = HidTransport(enumerate_fn=lambda: devices, device_factory=self.factory,
                                 write_delay_s=0)
        return TargetQuery(transport=transport, guard=lambda _: nullcontext(),
                           binding_store=self.bindings, legacy_history_probe=lambda: False)(
            "holtek-test", "auto", Event()
        )

    def test_wired_reads_firmware_percent(self):
        wired = collection(0x112D)
        self.device.read.side_effect = [[], [0x12] + [0] * 63, reply()]
        result = self.query([wired])
        self.assertEqual((result["outcome"], result["percent"]), ("success", 41))
        self.assertEqual((result["link_mode"], result["model_id"]), ("wired_usb", "atk_a9_pro"))
        self.device.open_path.assert_called_once_with(wired["path"])
        self.device.send_feature_report.assert_called_once_with([0, 0x82] + [0] * 7)
        self.device.write.assert_not_called()
        self.device.close.assert_called_once()

    def test_wireless_only_queries_after_binding(self):
        wireless = collection(0x1178)
        unbound = self.query([wireless])
        self.assertEqual(unbound["outcome"], "model_confirmation_required")
        self.factory.assert_not_called()
        self.bindings.set_binding(device_uid=unbound["device_uid"], vid_pid=unbound["vid_pid"],
                                  selected_model_id="atk_a9_pro",
                                  identity_basis=unbound["device_identity_basis"])
        self.device.read.side_effect = [[], reply()]
        bound = self.query([wireless])
        self.assertEqual((bound["outcome"], bound["binding_state"], bound["percent"]),
                         ("success", "bound", 41))
        self.device.open_path.assert_called_once_with(wireless["path"])
        self.device.send_feature_report.assert_called_once()

    def test_wired_is_not_blocked_by_unbound_wireless(self):
        wired, wireless = collection(0x112D), collection(0x1178)
        self.device.read.side_effect = [[], reply()]
        result = self.query([wireless, wired])
        self.assertEqual((result["vid_pid"], result["outcome"], result["percent"]),
                         ("373B:112D", "success", 41))
        self.device.open_path.assert_called_once_with(wired["path"])
        self.device.send_feature_report.assert_called_once()

    def test_wrong_interface_never_opens_device(self):
        for overrides in ({"usage_page": 0xFF11}, {"interface_number": 1},
                          {"input_report_length": 64}, {"feature_report_length": 8}):
            with self.subTest(overrides=overrides):
                result = self.query([collection(0x112D, **overrides)])
                self.assertEqual(result["outcome"], "interface_not_found")
                self.factory.assert_not_called()

    def test_sleep_reply_does_not_report_battery(self):
        self.device.read.side_effect = [[], reply(link=0)]
        result = self.query([collection(0x112D)])
        self.assertEqual(result["outcome"], "device_asleep")
        self.assertFalse(result["connected"])
        self.assertIsNone(result["percent"])

    def test_short_reply_and_out_of_range_percent_are_rejected(self):
        codec = get_codec("holtek_nano8k")
        short = reply()[:10]
        self.assertFalse(codec.make_response_matcher()(short))
        self.assertFalse(codec.parse_response(short).packet_valid)
        self.assertFalse(codec.parse_response(reply(battery=101)).percent_valid)
