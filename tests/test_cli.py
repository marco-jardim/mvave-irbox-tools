"""CLI behavior against the in-memory fake device (conftest). No device I/O."""

from contextlib import contextmanager

import numpy as np
import pytest

import irbox_tool
from conftest import make_block
from irbox.ir_convert import convert, export_wav
from irbox.model import decode_s24le


@pytest.fixture
def cli(hw, box_factory, monkeypatch, tmp_path):
    @contextmanager
    def fake_open(args, trace=None):
        if args.transport != "midi":
            raise irbox_tool.UsageError("midi only")
        yield box_factory(progress=print)

    monkeypatch.setattr(irbox_tool, "open_device", fake_open)
    monkeypatch.chdir(tmp_path)
    return hw


@pytest.fixture
def no_device(monkeypatch, tmp_path):
    def refuse(*_a, **_k):
        raise AssertionError("device must not be opened")

    monkeypatch.setattr(irbox_tool, "open_device", refuse)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def ir_wav(tmp_path):
    rng = np.random.default_rng(1)
    n = 3000
    x = rng.standard_normal(n) * np.exp(-np.arange(n) / 300.0) * 0.2
    samples = [int(v) for v in np.clip(np.rint(x * (1 << 23)), -(1 << 23), (1 << 23) - 1)]
    p = tmp_path / "in.wav"
    export_wav(samples, p)
    return p


def parse(*argv):
    return irbox_tool.build_parser().parse_args(list(argv))


def test_slot_numbers_are_one_based():
    assert parse("select", "1").slot == 0
    assert parse("select", "32").slot == 31
    assert parse("ir-export", "--slot", "5", "o.wav").slot == 4
    assert parse("rename", "--slot", "7", "Name").slot == 6
    assert parse("ir-upload", "a.wav", "--slot", "2").slot == 1
    assert parse("restore", "d", "--slot", "1", "32", "--slot", "3").slot == [0, 31, 2]
    assert irbox_tool.slot_arg("12") == 11
    assert irbox_tool.display_slot(11) == 12


@pytest.mark.parametrize(
    "argv",
    [
        ["select", "0"],
        ["select", "33"],
        ["select", "x"],
        ["volume", "128"],
        ["volume", "-1"],
        ["eq-band", "peak1", "--gain", "12.5"],
        ["eq-band", "peak1", "--q", "0.05"],
        ["eq-band", "peak1", "--freq", "19"],
        ["eq-band", "peak9"],
        ["eq-band", "peak1", "--enable", "--disable"],
        ["save", "--name", "x" * 17],
        ["rename", "--slot", "1", "caf\u00e9"],
        ["ir-load", "a.wav", "--fade", "3000"],
        ["--chunk", "1001", "info"],
    ],
)
def test_invalid_arguments_rejected(argv):
    with pytest.raises(SystemExit) as e:
        parse(*argv)
    assert e.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["save"],
        ["save", "--name", "New"],
        ["rename", "--slot", "3", "New"],
        ["save", "--no-backup"],
    ],
)
def test_flash_commands_refuse_without_yes(no_device, capsys, argv):
    assert irbox_tool.main(argv) == 2
    out, err = capsys.readouterr()
    assert "plan:" in out and "--yes" in err


def test_upload_refuses_without_yes_but_shows_conversion(no_device, ir_wav, capsys):
    assert irbox_tool.main(["ir-upload", str(ir_wav), "--slot", "3"]) == 2
    out, err = capsys.readouterr()
    assert "44100 Hz" in out and "46.4 ms" in out and "slot 3" in out
    assert "--yes" in err


def test_restore_refuses_without_yes(cli, capsys, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "slot_04.bin").write_bytes(make_block(4))
    assert irbox_tool.main(["restore", str(bk)]) == 2
    out, _ = capsys.readouterr()
    assert "slot 5:" in out
    assert cli.requests == []


def test_select_and_working_copy_commands(cli, capsys):
    assert irbox_tool.main(["select", "5"]) == 0
    assert cli.active == 4
    assert "active slot: 5 (Factory 5)" in capsys.readouterr().out
    assert irbox_tool.main(["volume", "20"]) == 0
    assert cli.working[0x2B] == 20
    assert irbox_tool.main(["cab", "off"]) == 0
    assert irbox_tool.main(["eq", "on"]) == 0
    assert (cli.working[0x11], cli.working[0x12]) == (0, 1)
    assert irbox_tool.main(["eq-band", "peak2", "--gain", "-12", "--q", "0.5", "--freq", "1000", "--enable"]) == 0
    eq = cli.working[0x1C00:0x1C28]
    assert (eq[3], eq[10], eq[15], bytes(eq[24:26])) == (1, 5, 0x88, b"\xe8\x03")
    assert ("refresh", 2) in cli.ops
    assert not any(op[0] == "save" for op in cli.ops)
    assert bytes(cli.flash[4]) == make_block(4)


def test_eq_band_rejects_param_for_band(cli, capsys):
    assert irbox_tool.main(["eq-band", "hpf", "--gain", "3"]) == 2
    assert irbox_tool.main(["eq-band", "lowshelf", "--q", "1"]) == 2
    assert cli.requests == []


def test_eq_band_show(cli, capsys):
    assert irbox_tool.main(["eq-band", "peak3"]) == 0
    out = capsys.readouterr().out
    assert "peak3" in out and "1000 Hz" in out and "Q 0.5" in out
    assert not any(op[0] in ("write", "refresh") for op in cli.ops)


def test_info_is_one_based(cli, capsys):
    assert irbox_tool.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "active slot: 23" in out
    assert "*23: Factory 23" in out
    assert " 1: Factory 1" in out and "32: Factory 32" in out


def test_ir_load_preview_not_saved(cli, ir_wav, capsys):
    assert irbox_tool.main(["ir-load", str(ir_wav)]) == 0
    expected = convert(ir_wav).samples
    assert decode_s24le(bytes(cli.working[44:6188])) == expected
    assert ("refresh", 1) in cli.ops
    assert not any(op[0] == "save" for op in cli.ops)
    assert "truncated: yes" in capsys.readouterr().out


def test_save_with_yes(cli, capsys):
    irbox_tool.main(["volume", "33"])
    assert irbox_tool.main(["save", "--name", "My Tone", "--yes", "--no-backup"]) == 0
    assert bytes(cli.flash[22][:8]) == b"My Tone\x00"
    assert cli.flash[22][0x2B] == 33


def test_save_auto_backup_keeps_unsaved_changes(cli, tmp_path):
    irbox_tool.main(["volume", "33"])
    assert irbox_tool.main(["save", "--yes"]) == 0
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    bk = backups[0]
    assert len(list(bk.glob("slot_*.bin"))) == 32
    assert (bk / "manifest.json").exists()
    assert (bk / "slot_22.bin").read_bytes()[0x2B] == 70  # flash state before the save
    assert cli.flash[22][0x2B] == 33  # unsaved change survived the backup and was saved


def test_ir_upload_with_backup_and_verify(cli, ir_wav, tmp_path):
    argv = ["ir-upload", str(ir_wav), "--slot", "3", "--name", "Uploaded", "--yes"]
    assert irbox_tool.main(argv) == 0
    expected = convert(ir_wav).samples
    flash = bytes(cli.flash[2])
    assert decode_s24le(flash[44:6188]) == expected
    assert flash[:9] == b"Uploaded\x00" and flash[17] == 1
    assert cli.active == 2
    assert len(list((tmp_path / "backups").glob("*/slot_*.bin"))) == 32
    assert all(raw[2] != 0x21 for raw in cli.requests)


def test_rename_and_restore_roundtrip(cli, tmp_path):
    assert irbox_tool.main(["backup", "bk"]) == 0
    assert irbox_tool.main(["rename", "--slot", "4", "Renamed", "--yes", "--no-backup"]) == 0
    assert bytes(cli.flash[3][:8]) == b"Renamed\x00"
    assert cli.active == 22
    assert irbox_tool.main(["restore", "bk", "--slot", "4", "--yes", "--no-backup"]) == 0
    assert bytes(cli.flash[3]) == make_block(3)
    assert cli.active == 22


def test_ir_export(cli, tmp_path):
    out = tmp_path / "x.wav"
    assert irbox_tool.main(["ir-export", "--slot", "2", str(out)]) == 0
    assert convert(out, normalize="none").samples == decode_s24le(make_block(1)[44:6188])
    assert cli.active == 22


def test_ble_transport_rejected_for_editor_commands(cli, capsys):
    assert irbox_tool.main(["--transport", "ble", "select", "3"]) == 2
    assert "midi" in capsys.readouterr().err


def test_device_error_exit_code(cli, capsys):
    cli.ignore_select = True
    assert irbox_tool.main(["select", "3"]) == 1
    assert "error:" in capsys.readouterr().err
