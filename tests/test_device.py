"""IRBox high-level operations against the in-memory fake device (conftest)."""

import hashlib
import json

import pytest

from conftest import make_block, make_box
from irbox.device import DeviceError, IRBox, check_write_target, load_backup
from irbox.model import decode_s24le
from irbox.protocol import CMD_READ, CMD_WRITE, ForbiddenOperation

NAMES = 0x80000000


def _per_slot_restore(s):
    return [
        ("select", s),
        ("write", 0, 24),
        ("write", 24, 6164),
        ("write", 0x1C00, 40),
        ("refresh", 1),
        ("refresh", 2),
        ("refresh", 3),
        ("write", 0, 24),
        ("save", s),
        ("select", (s + 1) % 32),
        ("select", s),
    ]


def test_write_chunking(box, hw):
    data = bytes(range(256)) * 24 + bytes(range(20))
    assert len(data) == 6164
    assert box.write(5, 0x18, data) == 36
    writes = hw.frames(CMD_WRITE)
    assert len(writes) == 36
    assert [addr for _, addr, _ in writes] == [0x18 + i * 173 for i in range(36)]
    assert all(t == 5 and len(p) <= 173 for t, _, p in writes)
    assert len(writes[-1][2]) == 6164 - 35 * 173
    assert b"".join(p for _, _, p in writes) == data
    assert bytes(hw.working[0x18 : 0x18 + 6164]) == data


def test_write_nack_raises(box, hw):
    hw.nack.add((5, 0x18 + 173))
    with pytest.raises(DeviceError, match="status 1"):
        box.write(5, 0x18, bytes(400))
    assert len(hw.frames(CMD_WRITE)) == 2


@pytest.mark.parametrize(
    "type_,addr,data",
    [
        (5, NAMES, b"x"),
        (0, 0, b"\x00"),
        (4, 0x20000000, b"\x00"),
        (4, 0xE0000003, b"\x04"),
        (5, 8190, b"abc"),
        (5, 0xF0000000, b"\x01"),
        (5, 0xF0000020, b"\x00"),
        (5, 0xA0000000, b"\x04"),
        (5, 0, b""),
    ],
)
def test_write_targets_refused(box, hw, type_, addr, data):
    with pytest.raises((ForbiddenOperation, ValueError)):
        box.write(type_, addr, data)
    assert hw.requests == []


def test_write_targets_allowed():
    check_write_target(4, 0xE0000000 + 31, bytes([31]))
    check_write_target(5, 0, bytes(8192))
    check_write_target(5, 0xA0000000, b"\x02")
    check_write_target(5, 0xF0000000 + 7, b"\x00")


def test_names_single_request(hw):
    m, _ = make_box()
    try:
        small = IRBox(m, read_chunk=128)  # even with small chunks: one 544-byte request
        names = small.names()
    finally:
        m.close()
    assert hw.frames(CMD_READ) == [(5, NAMES, (544).to_bytes(3, "little"))]
    assert names[0] == "Factory 1" and names[31] == "Factory 32"


def test_read_chunk_limit(hw):
    m, _ = make_box()
    try:
        with pytest.raises(ValueError):
            IRBox(m, read_chunk=1001)
    finally:
        m.close()


def test_active_slot_and_select(box, hw):
    assert box.active_slot() == 22
    box.select(4)
    assert hw.active == 4
    assert hw.high_level() == [("select", 4)]
    assert box.read_preset() == make_block(4)


def test_select_verifies(box, hw):
    hw.ignore_select = True
    with pytest.raises(DeviceError, match="reports slot 23"):
        box.select(4)


def test_implausible_active_slot(box, hw):
    hw.active = 40
    with pytest.raises(DeviceError, match="implausible"):
        box.active_slot()


def test_select_range(box, hw):
    for bad in (-1, 32, True):
        with pytest.raises(ValueError):
            box.select(bad)
    assert hw.requests == []


def test_volume_cab_eq_refresh(box, hw):
    box.set_volume(20)
    box.set_cab(False)
    box.set_eq(True)
    assert hw.high_level() == [
        ("write", 0x2B, 1),
        ("refresh", 3),
        ("write", 0x11, 1),
        ("refresh", 1),
        ("write", 0x12, 1),
        ("refresh", 1),
    ]
    assert (hw.working[0x2B], hw.working[0x11], hw.working[0x12]) == (20, 0, 1)
    with pytest.raises(ValueError):
        box.set_volume(128)


def test_set_eq_band(box, hw):
    box.set_eq_band("peak2", freq=1000, gain_db=-12.0, q=0.5, enable=True)
    eq = hw.working[0x1C00:0x1C28]
    assert eq[3] == 1  # enable index: hpf, lowshelf, peak1, peak2
    assert eq[10] == 5  # peakQ[1] @0x1C0A
    assert eq[15] == 0x88  # peakDb[1] @0x1C0F
    assert bytes(eq[24:26]) == b"\xe8\x03"  # peakHz[1] @0x1C18
    assert hw.high_level() == [("write", 0x1C00, 40), ("refresh", 2)]
    box.set_eq_band("hpf", freq=800, enable=True)
    assert hw.working[0x1C00] == 1 and bytes(hw.working[0x1C26:0x1C28]) == (800).to_bytes(2, "little")
    with pytest.raises(ValueError):
        box.set_eq_band("lpf", gain_db=3.0)
    with pytest.raises(ValueError):
        box.set_eq_band("peak1")


def test_load_ir(box, hw):
    hw.working[0x2B] = 55
    samples = list(range(-1024, 1024))
    block = box.load_ir(samples)
    assert hw.high_level() == [("write", 0x18, 6164), ("refresh", 1)]
    w = bytes(hw.working)
    assert w[24:28] == b"CAB\x00" and w[28:42] == bytes(14)
    assert (w[42], w[43]) == (2, 55)
    assert decode_s24le(w[44:6188]) == samples
    assert block == w[24:6188]
    assert bytes(hw.flash[22]) == make_block(22)  # nothing saved


def test_save_requires_active_slot(box, hw):
    with pytest.raises(DeviceError, match="not active"):
        box.save(3)
    assert hw.frames(CMD_WRITE) == []


def test_save_bad_name_before_io(box, hw):
    with pytest.raises(ValueError):
        box.save(22, "x" * 17)
    assert hw.requests == []


def test_save_with_name(box, hw):
    box.set_volume(33)
    hw.ops.clear()
    box.save(22, "New Name")
    assert hw.high_level() == [("write", 0, 24), ("save", 22), ("select", 23), ("select", 22)]
    assert bytes(hw.flash[22][:17]) == b"New Name".ljust(17, b"\x00")
    assert hw.flash[22][0x2B] == 33
    assert box.names()[22] == "New Name"


def test_backup_sequence_and_files(box, hw, tmp_path):
    out = tmp_path / "bk"
    res = box.backup(out)
    hl = hw.high_level()
    assert hl == [("select", i) for i in range(32)] + [("select", 22)]
    assert [op for op in hw.ops if op[0] == "read" and op[2] == NAMES] == [("read", 5, NAMES, 544)]
    assert hw.active == 22 and res.active_slot == 22
    for i in range(32):
        assert (out / f"slot_{i:02d}.bin").read_bytes() == make_block(i)
    assert (out / "slot_06.bin").read_bytes()[17] == 0xFF
    assert (out / "names.bin").read_bytes() == hw.name_table()
    assert (out / "working_copy.bin").read_bytes() == make_block(22)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["active_slot"] == 23 and manifest["active_index"] == 22
    assert manifest["device"] == "IR-BOX_010"
    assert manifest["slots"][0]["slot"] == 1 and manifest["slots"][0]["file"] == "slot_00.bin"
    assert manifest["slots"][5]["sha256"] == hashlib.sha256(make_block(5)).hexdigest()
    assert res.names[0] == "Factory 1"
    with pytest.raises(FileExistsError):
        box.backup(out)


def test_backup_keeps_unsaved_working_copy(box, hw, tmp_path):
    box.set_volume(10)
    box.backup(tmp_path / "bk")
    assert hw.active == 22
    assert hw.working[0x2B] == 10  # re-applied
    assert hw.flash[22][0x2B] == 70  # never saved
    assert not any(op[0] == "save" for op in hw.ops)
    assert (tmp_path / "bk" / "working_copy.bin").read_bytes()[0x2B] == 10
    assert (tmp_path / "bk" / "slot_22.bin").read_bytes()[0x2B] == 70


def test_restore_sequence(box, hw, tmp_path):
    bk = tmp_path / "bk"
    box.backup(bk)
    hw.flash[2][0:5] = b"XXXXX"
    hw.flash[5][100] ^= 0xFF
    hw.flash[5][0x1C05] = 1
    hw.ops.clear()
    assert box.restore(bk, [5, 2]) == [2, 5]
    assert hw.high_level() == _per_slot_restore(2) + _per_slot_restore(5) + [("select", 22)]
    assert bytes(hw.flash[2]) == make_block(2)
    assert bytes(hw.flash[5]) == make_block(5)
    assert hw.active == 22
    assert all(raw[2] != 0x21 for raw in hw.requests)


def test_restore_missing_and_corrupt(box, hw, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "slot_03.bin").write_bytes(make_block(3))
    with pytest.raises(ValueError, match="no data"):
        box.restore(bk, [4])
    bad = bytearray(make_block(3))
    bad[19:24] = b"xxxxx"
    (bk / "slot_03.bin").write_bytes(bytes(bad))
    with pytest.raises(ValueError, match="patch"):
        box.restore(bk)
    assert hw.frames(CMD_WRITE) == []


def test_load_backup_checks_manifest(box, hw, tmp_path):
    bk = tmp_path / "bk"
    box.backup(bk)
    assert sorted(load_backup(bk)) == list(range(32))
    data = bytearray((bk / "slot_07.bin").read_bytes())
    data[5000] ^= 1
    (bk / "slot_07.bin").write_bytes(bytes(data))
    with pytest.raises(ValueError, match="SHA-256"):
        load_backup(bk)


def test_upload_ir(box, hw):
    samples = [(k * 4099) % 16000000 - 8000000 for k in range(2048)]
    box.upload_ir(6, samples, "Uploaded")
    assert hw.high_level() == [
        ("select", 6),
        ("write", 0x18, 6164),
        ("refresh", 1),
        ("write", 0x11, 1),
        ("refresh", 1),
        ("write", 0, 24),
        ("save", 6),
        ("select", 7),
        ("select", 6),
    ]
    f = bytes(hw.flash[6])
    assert decode_s24le(f[44:6188]) == samples
    assert f[17] == 1  # cab on (was 0xFF factory quirk)
    assert f[:9] == b"Uploaded\x00"
    assert hw.active == 6


def test_upload_ir_to_active_slot_reloads_first(box, hw):
    box.set_volume(5)
    hw.ops.clear()
    box.upload_ir(22, [0] * 2048)
    assert hw.high_level()[:2] == [("select", 23), ("select", 22)]
    assert hw.flash[22][0x2B] == 70  # unsaved volume change discarded, not saved


def test_upload_ir_validates_before_io(box, hw):
    with pytest.raises(ValueError):
        box.upload_ir(3, [0] * 100)
    with pytest.raises(ValueError):
        box.upload_ir(3, [1 << 23] * 2048)
    assert hw.requests == []


def test_rename_other_slot_keeps_active_edits(box, hw):
    box.set_volume(10)
    box.rename(4, "Renamed")
    assert bytes(hw.flash[4][:8]) == b"Renamed\x00"
    assert hw.flash[4][44:6188] == make_block(4)[44:6188]
    assert hw.active == 22
    assert hw.working[0x2B] == 10
    assert hw.flash[22][0x2B] == 70


def test_rename_active_slot(box, hw):
    box.rename(22, "Active")
    assert bytes(hw.flash[22][:7]) == b"Active\x00"
    assert hw.active == 22


def test_read_slot(box, hw):
    box.set_volume(9)
    assert box.read_slot(22)[0x2B] == 9  # working copy of the active slot
    assert box.read_slot(3) == make_block(3)
    assert hw.active == 22
    assert hw.working[0x2B] == 9
