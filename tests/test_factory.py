"""Raw flash window, factory images, factory-list / factory-restore. Fake device only, no I/O."""

import json
from contextlib import contextmanager

import pytest

import irbox_tool
from conftest import RAW_FLASH, RAW_FLASH_LEN, make_block
from irbox.device import DeviceError, check_write_target, load_backup, load_factory_image
from irbox.protocol import CMD_ERASE, CMD_READ, CMD_WRITE

SHOWMAN = b" ShowmanD130s 1".ljust(17, b"\x00")  # leading space is real (BOR.bin slot 2)


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


def image_blocks(overrides=None):
    blocks = {i: make_block(i) for i in range(32)}
    blocks.update(overrides or {})
    return blocks


def write_image(path, overrides=None):
    blocks = image_blocks(overrides)
    path.write_bytes(b"".join(blocks[i] for i in range(32)))
    return path


@pytest.fixture
def image(tmp_path):
    """Factory image equal to the fake's initial flash, except slot 2 has a leading-space name."""
    blk = bytearray(make_block(1))
    blk[0:17] = SHOWMAN
    return write_image(tmp_path / "BOR.bin", {1: bytes(blk)})


def modify_device(hw):
    """Make slots 3, 10, 13 and 21 (display) differ from the image; slot 2 differs via the image."""
    hw.flash[2][0:3] = b"XYZ"  # name
    hw.flash[9][1000] ^= 0x01  # IR sample
    hw.flash[12][6500] ^= 0x01  # outside header/IR/EQ only
    hw.flash[20][0x2B] = 5  # volume


def assert_safe(hw):
    """No erase command, no write into the raw flash window, every write on the allowlist."""
    assert all(raw[2] != CMD_ERASE for raw in hw.requests)
    writes = hw.frames(CMD_WRITE)
    assert not [hex(addr) for _, addr, _ in writes if 0x70000000 <= addr <= 0x7FFFFFFF]
    for type_, addr, payload in writes:
        check_write_target(type_, addr, payload)


def raw_reads(ops):
    return [op for op in ops if op[0] == "read" and op[1] == 5 and RAW_FLASH <= op[2] < RAW_FLASH + RAW_FLASH_LEN]


def slot_line(out, n):
    lines = [line for line in out.splitlines() if line.startswith(f"  {n:2d}: ")]
    assert len(lines) == 1, (n, lines)
    return lines[0]


# -- device layer ---------------------------------------------------------------


def test_read_raw_flash_is_read_only(box, hw):
    box.set_volume(10)  # unsaved working-copy change
    hw.ops.clear()
    hw.requests.clear()
    blocks = box.read_raw_flash()
    assert blocks == {i: make_block(i) for i in range(32)}
    assert hw.active == 22
    assert hw.working[0x2B] == 10 and hw.flash[22][0x2B] == 70  # working copy untouched
    assert hw.frames(CMD_WRITE) == []
    assert not any(op[0] in ("select", "write", "refresh", "save") for op in hw.ops)
    reads = hw.frames(CMD_READ)
    assert len(reads) == 32 * 9  # 8 x 1000 + 192 bytes per slot
    for type_, addr, n in reads:
        length = int.from_bytes(n, "little")
        assert type_ == 5 and 1 <= length <= 1000
        assert RAW_FLASH <= addr and addr + length <= RAW_FLASH + RAW_FLASH_LEN
    assert [a for _, a, _ in reads[:9]] == [RAW_FLASH + k * 1000 for k in range(9)]
    assert reads[9][1] == RAW_FLASH + 8192


def test_read_raw_flash_subset_and_validation(box, hw):
    got = box.read_raw_flash([5, 2, 5])
    assert sorted(got) == [2, 5] and got[5] == make_block(5)
    assert hw.frames(CMD_READ)[0][1] == RAW_FLASH + 2 * 8192
    hw.requests.clear()
    for bad in ([32], [-1], [0, 40]):
        with pytest.raises(ValueError):
            box.read_raw_flash(bad)
    assert hw.requests == []


def test_raw_backup_equals_select_backup(box, hw, tmp_path):
    box.set_volume(10)
    sel = box.backup(tmp_path / "sel")
    hw.ops.clear()
    hw.requests.clear()
    raw = box.backup(tmp_path / "raw", raw=True)
    assert (sel.method, raw.method) == ("select", "raw-flash")
    assert hw.frames(CMD_WRITE) == []
    assert not any(op[0] in ("select", "write", "refresh", "save") for op in hw.ops)
    assert hw.active == 22 and hw.working[0x2B] == 10
    files = [f"slot_{i:02d}.bin" for i in range(32)] + ["names.bin", "working_copy.bin"]
    for name in files:
        assert (tmp_path / "raw" / name).read_bytes() == (tmp_path / "sel" / name).read_bytes(), name
    m_sel = json.loads((tmp_path / "sel" / "manifest.json").read_text(encoding="utf-8"))
    m_raw = json.loads((tmp_path / "raw" / "manifest.json").read_text(encoding="utf-8"))
    assert (m_sel["method"], m_raw["method"]) == ("select", "raw-flash")
    assert m_raw["slots"] == m_sel["slots"]
    assert m_raw["active_slot"] == m_sel["active_slot"] == 23
    assert load_backup(tmp_path / "raw") == load_backup(tmp_path / "sel")
    assert raw.names == sel.names
    with pytest.raises(FileExistsError):
        box.backup(tmp_path / "raw", raw=True)


def test_verify_raw_flash(box, hw):
    box.verify_raw_flash({3: make_block(3)})
    with pytest.raises(DeviceError, match="slot 4"):
        box.verify_raw_flash({3: make_block(4)})
    with pytest.raises(ValueError, match="no expected data"):
        box.verify_raw_flash({3: make_block(3)}, [3, 4])
    assert hw.frames(CMD_WRITE) == []


def test_restore_blocks_validates_before_io(box, hw):
    with pytest.raises(ValueError, match="patch"):
        box.restore_blocks({3: bytes(8192)})
    with pytest.raises(ValueError, match="no data"):
        box.restore_blocks({3: make_block(3)}, [4])
    with pytest.raises(ValueError):
        box.restore_blocks({3: make_block(3)}, [32])
    assert hw.requests == []


# -- factory image loader ---------------------------------------------------------


def test_load_factory_image(image):
    blocks = load_factory_image(image)
    assert sorted(blocks) == list(range(32))
    assert blocks[0] == make_block(0)
    assert blocks[1][:17] == SHOWMAN


@pytest.mark.parametrize("size", [0, 8192, RAW_FLASH_LEN - 1, RAW_FLASH_LEN + 1, 2 * RAW_FLASH_LEN])
def test_factory_image_bad_size(tmp_path, size):
    p = tmp_path / "BOR.bin"
    p.write_bytes(bytes(size))
    with pytest.raises(ValueError, match="262144"):
        load_factory_image(p)


@pytest.mark.parametrize(
    "index,offset,magic",
    [(5, 19, "patch"), (0, 23, "patch"), (31, 24, "CAB"), (17, 27, "CAB")],
)
def test_factory_image_bad_magic(tmp_path, index, offset, magic):
    blk = bytearray(make_block(index))
    blk[offset] ^= 0x20
    p = write_image(tmp_path / "BOR.bin", {index: bytes(blk)})
    with pytest.raises(ValueError, match=rf"slot {index + 1} .*{magic}"):
        load_factory_image(p)


def test_factory_image_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_factory_image(tmp_path / "nope.bin")
    with pytest.raises(FileNotFoundError):
        load_factory_image(tmp_path)  # a directory


# -- CLI: backup --raw --------------------------------------------------------------


def test_cli_backup_raw(cli, tmp_path, capsys):
    assert irbox_tool.main(["backup", "bk", "--raw"]) == 0
    assert "raw flash" in capsys.readouterr().out
    manifest = json.loads((tmp_path / "bk" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["method"] == "raw-flash"
    assert len(list((tmp_path / "bk").glob("slot_*.bin"))) == 32
    assert cli.frames(CMD_WRITE) == []
    assert cli.active == 22


def test_cli_backup_default_is_select(cli, tmp_path):
    assert irbox_tool.main(["backup", "bk"]) == 0
    manifest = json.loads((tmp_path / "bk" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["method"] == "select"
    assert ("select", 0) in cli.ops


# -- CLI: factory-list ----------------------------------------------------------------


def test_factory_list_offline(no_device, image, capsys):
    assert irbox_tool.main(["factory-list", str(image)]) == 0
    out = capsys.readouterr().out
    assert "sha256" in out
    assert "'Factory 1'" in slot_line(out, 1)
    assert "' ShowmanD130s 1'" in slot_line(out, 2)
    assert "'Factory 32'" in slot_line(out, 32)
    line7 = slot_line(out, 7)  # cab_on 0xFF counts as on
    assert "cab on" in line7 and "eq off" in line7 and "volume  70" in line7
    assert "IDENTICAL" not in out and "differs" not in out


def test_factory_list_bad_image_does_not_open_device(no_device, tmp_path, capsys):
    (tmp_path / "bad.bin").write_bytes(bytes(1000))
    assert irbox_tool.main(["factory-list", str(tmp_path / "bad.bin"), "--compare"]) == 1
    assert "262144" in capsys.readouterr().err


def test_factory_list_compare(cli, image, capsys):
    modify_device(cli)
    assert irbox_tool.main(["factory-list", str(image), "--compare"]) == 0
    out = capsys.readouterr().out
    assert slot_line(out, 1).endswith("IDENTICAL")
    assert slot_line(out, 2).endswith("differs: name")
    assert slot_line(out, 3).endswith("differs: name")
    assert slot_line(out, 10).endswith("differs: IR")
    assert "same preset" in slot_line(out, 13)
    assert slot_line(out, 21).endswith("differs: volume")
    assert slot_line(out, 32).endswith("IDENTICAL")
    assert "27 slot(s) IDENTICAL" in out
    assert "differ in 4 slot(s): [2, 3, 10, 21]" in out
    assert cli.frames(CMD_WRITE) == []
    assert cli.active == 22


# -- CLI: factory-restore ----------------------------------------------------------------


def test_factory_restore_parse():
    args = irbox_tool.build_parser().parse_args(["factory-restore", "img", "--slot", "1", "32", "--slot", "3"])
    assert args.slot == [0, 31, 2]
    assert not args.yes and not args.no_backup


def test_factory_restore_refuses_without_yes(cli, image, capsys):
    modify_device(cli)
    before = [bytes(b) for b in cli.flash]
    assert irbox_tool.main(["factory-restore", str(image)]) == 2
    out, err = capsys.readouterr()
    assert "plan:" in out and "slot 2:" in out and "slot 3:" in out and "--yes" in err
    assert "slot 13:" not in out
    assert cli.frames(CMD_WRITE) == []  # only read-only raw flash reads happened
    assert [bytes(b) for b in cli.flash] == before
    assert_safe(cli)


def test_factory_restore_explicit_slots_refuse_without_device(cli, image, capsys):
    assert irbox_tool.main(["factory-restore", str(image), "--slot", "4", "--no-backup"]) == 2
    out, err = capsys.readouterr()
    assert "slot 4:" in out and "--yes" in err
    assert cli.requests == []


def test_factory_restore_default_selects_differing_slots(cli, image, capsys):
    modify_device(cli)
    assert irbox_tool.main(["factory-restore", str(image), "--yes", "--no-backup"]) == 0
    out = capsys.readouterr().out
    assert "differ from the factory image: [2, 3, 10, 21]" in out
    assert "[13]" in out  # only other bytes differ: reported, skipped
    hl = cli.high_level()
    assert [op for op in hl if op[0] == "save"] == [("save", 1), ("save", 2), ("save", 9), ("save", 20)]
    assert {op[1] for op in hl if op[0] == "write"} <= {0, 24, 0x1C00}
    expected = image_blocks({1: load_factory_image(image)[1]})
    for i in (1, 2, 9, 20):
        assert bytes(cli.flash[i]) == expected[i], i
    assert bytes(cli.flash[1][:17]) == SHOWMAN
    assert cli.flash[12][6500] == make_block(12)[6500] ^ 0x01  # not restored
    assert cli.active == 22
    last_save = max(k for k, op in enumerate(cli.ops) if op[0] == "save")
    verified = {(op[2] - RAW_FLASH) // 8192 for op in raw_reads(cli.ops[last_save:])}
    assert verified == {1, 2, 9, 20}
    assert_safe(cli)


def test_factory_restore_explicit_slot_even_if_identical(cli, image):
    assert irbox_tool.main(["factory-restore", str(image), "--slot", "5", "--yes", "--no-backup"]) == 0
    assert [op for op in cli.high_level() if op[0] == "save"] == [("save", 4)]
    assert bytes(cli.flash[4]) == make_block(4)
    assert_safe(cli)


def test_factory_restore_nothing_to_do(cli, tmp_path, capsys):
    img = write_image(tmp_path / "BOR.bin")
    assert irbox_tool.main(["factory-restore", str(img), "--yes", "--no-backup"]) == 0
    assert "nothing to restore" in capsys.readouterr().out
    assert cli.frames(CMD_WRITE) == []


def test_factory_restore_with_auto_backup_is_safe(cli, image, tmp_path):
    modify_device(cli)
    assert irbox_tool.main(["factory-restore", str(image), "--yes"]) == 0
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "slot_02.bin").read_bytes()[0:3] == b"XYZ"  # state before the restore
    assert bytes(cli.flash[2]) == make_block(2)
    assert_safe(cli)


def test_factory_restore_bad_image(no_device, tmp_path, capsys):
    blk = bytearray(make_block(3))
    blk[20] = 0
    img = write_image(tmp_path / "BOR.bin", {3: bytes(blk)})
    assert irbox_tool.main(["factory-restore", str(img), "--yes"]) == 1
    assert "slot 4" in capsys.readouterr().err
