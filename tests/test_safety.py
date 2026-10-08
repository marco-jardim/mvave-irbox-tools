import pytest

from irbox import ble
from irbox.protocol import build_read, build_write


def test_erase_frame_refused():
    frame = bytes.fromhex("005921010000" "00" "ff")
    with pytest.raises(ble.ForbiddenOperation):
        ble.guard_frame(frame)


def test_normal_frames_allowed():
    ble.guard_frame(build_read(4, 0x20000000, 1))
    ble.guard_frame(build_write(5, 0x11, b"\x01"))


@pytest.mark.parametrize("u", ["0000ae00-0000-1000-8000-00805f9b34fb", "0000AE01-0000-1000-8000-00805f9b34fb"])
def test_ota_uuid_refused(u):
    with pytest.raises(ble.ForbiddenOperation):
        ble.guard_uuid(u)


def test_midi_transport_shares_denylist():
    from irbox import midi

    assert midi.guard_frame is ble.guard_frame
    assert midi.ForbiddenOperation is ble.ForbiddenOperation
    assert 0x21 in midi.FORBIDDEN_COMMANDS


def test_cli_exposes_no_raw_or_erase_commands():
    import irbox_tool

    sub = irbox_tool.build_parser()._subparsers._group_actions[0]
    assert set(sub.choices) == {
        "scan", "midi-ports", "query", "info", "dump", "backup", "ir-export",
        "select", "volume", "cab", "eq", "eq-band", "ir-load",
        "save", "rename", "ir-upload", "restore",
    }


@pytest.mark.parametrize("command", ["save", "rename", "ir-upload", "restore"])
def test_flash_commands_have_yes_and_backup_flags(command):
    import irbox_tool

    sub = irbox_tool.build_parser()._subparsers._group_actions[0]
    flags = {s for a in sub.choices[command]._actions for s in a.option_strings}
    assert {"--yes", "--no-backup"} <= flags


def test_device_layer_refuses_erase_frames():
    from irbox.device import IRBox

    class Recorder:
        def __init__(self):
            self.sent = []

        def transact(self, raw, expect_cmd, timeout=None):
            ble.guard_frame(raw)
            self.sent.append(raw)
            raise AssertionError("not reached")

        def read(self, *a, **k):
            raise AssertionError("not reached")

        def query(self, timeout=None):
            raise AssertionError("not reached")

    rec = Recorder()
    box = IRBox(rec)
    for type_, addr in ((5, 0x80000000), (0, 0), (2, 0), (4, 0x20000000)):
        with pytest.raises(ble.ForbiddenOperation):
            box.write(type_, addr, b"\x00")
    assert rec.sent == []


def test_cli_defaults_to_midi_transport():
    import irbox_tool

    args = irbox_tool.build_parser().parse_args(["--chunk", "64", "info"])
    assert args.transport == "midi"
    assert args.chunk == 64
    with pytest.raises(SystemExit):
        irbox_tool.build_parser().parse_args(["--chunk", "0", "info"])
