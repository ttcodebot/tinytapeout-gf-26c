import binascii
import json
import os
import re

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer


async def enable_design(dut, mux_addr):
    dut._log.info(f"enabling design at mux address {mux_addr}")

    # reset the controller
    dut.ctrl_sel_inc.value = 0
    dut.ctrl_sel_rst_n.value = 0
    dut.ctrl_ena.value = 0
    await ClockCycles(dut.clk, 5)
    dut.ctrl_sel_rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    # send the number of pulses required
    for c in range(mux_addr):
        dut.ctrl_sel_inc.value = 1
        await ClockCycles(dut.clk, 1)
        dut.ctrl_sel_inc.value = 0
        await ClockCycles(dut.clk, 1)

    dut.ctrl_ena.value = 1


@cocotb.test()
async def test_factory_test(dut):
    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz
    cocotb.start_soon(clock.start())

    dut.uio_in.value = 0
    dut.ui_in.value = 0
    # select test design
    dut.rst_n.value = 0
    await enable_design(dut, 1)

    # with bit 0 of ui_in set to 0, module will copy inputs to outputs
    dut.ui_in.value = 0b0
    await ClockCycles(dut.clk, 5)  # wait until the wait state config is read
    dut.rst_n.value = 1

    dut._log.info("test loopback")
    for i in range(256):
        dut.uio_in.value = i
        await ClockCycles(dut.clk, 1)
        assert dut.uo_out.value == i

    # with bit 0 of ui_in set to 1, module will enable bidirectional outputs and put a counter on both output and bidirectional output
    dut.ui_in.value = 0b1

    # reset it
    dut.rst_n.value = 0
    dut.uio_in.value = "z" * 8  # set uio pins to high-Z, so the DUT can drive them
    await Timer(10, unit="ns")  # Async reset pulse
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)  # Wait for counter to reset

    dut._log.info("test counter")
    for i in range(256):
        assert dut.uo_out.value == dut.uio_out.value
        assert dut.uo_out.value == i
        await ClockCycles(dut.clk, 1)  # Next count


@cocotb.test()
async def test_rom(dut):
    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz
    cocotb.start_soon(clock.start())

    dut.uio_in.value = 0
    dut.ui_in.value = 0
    # select ROM design
    dut.rst_n.value = 0
    await enable_design(dut, 0)

    await ClockCycles(dut.clk, 1)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)

    dut._log.info("Read ROM, addressing with ui_in (rst_n=1)")
    rom_data = bytearray(256)
    for byte_idx in range(len(rom_data)):
        dut.ui_in.value = byte_idx
        await ClockCycles(dut.clk, 1)
        rom_data[byte_idx] = dut.uo_out.value

    dut._log.info(f"Read ROM, addressing sequentially (rst_n=0)")
    await Timer(10, unit="ns")  # Async reset pulse
    dut.rst_n.value = 0
    rom_data_seq = bytearray(256)
    for byte_idx in range(len(rom_data_seq)):
        await ClockCycles(dut.clk, 1)
        rom_data_seq[byte_idx] = dut.uo_out.value

    text = rom_data[32:128].rstrip(b"\0").decode("ascii")
    items = {}
    for line in text.split("\n"):
        if len(line) == 0:
            break
        key, value = line.split("=", 2)
        items[key] = value

    dut._log.info(
        f"ROM start bytes: {binascii.hexlify(rom_data[:32], ' ').decode('ascii')}"
    )
    dut._log.info(f"ROM text data: {items}")

    assert "shuttle" in items
    assert "repo" in items
    assert "commit" in items

    assert rom_data == rom_data_seq, "ROM contents differ when addressing sequentially"

    assert items["repo"] == os.environ.get("EXPECTED_REPO")
    assert re.match("^[0-9a-f]{8}$", items["commit"]) != None

    magic = rom_data[248:252]
    assert magic == b"TT\xfa\xbb"

    crc32 = int.from_bytes(rom_data[252:256], "little")
    assert crc32 == binascii.crc32(rom_data[0:252])


def load_micro_tile_groups():
    """
    Returns the micro tile groups on this chip as a list of (mux address, group macro, micro tiles),
    where each micro tile is (select value, macro, reference vectors).
    Only the groups that are in shuttle_index.json (i.e. on the chip being simulated) are returned.
    """
    root = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../../..")
    groups_file = os.path.join(root, "projects/groups.json")
    index_file = os.path.join(root, "shuttle_index.json")
    if not os.path.exists(groups_file) or not os.path.exists(index_file):
        return []
    with open(groups_file) as f:
        group_macros = json.load(f)["groups"]
    with open(index_file) as f:
        addresses = {p["macro"]: p["address"] for p in json.load(f)["projects"]}
    groups = []
    for group in group_macros:
        if group not in addresses:
            continue
        with open(os.path.join(root, "projects", group, "subprojects.json")) as f:
            subprojects = json.load(f)["projects"]
        tiles = []
        for index in sorted(subprojects, key=int):
            if subprojects[index] is None:
                continue  # empty slot
            macro = subprojects[index]["macro"]
            vectors_file = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "micro_tiles", f"{macro}.json"
            )
            with open(vectors_file) as f:
                tiles.append((int(index), macro, json.load(f)["vectors"]))
        groups.append((addresses[group], group, tiles))
    return groups


@cocotb.test(skip=len(load_micro_tile_groups()) == 0)
async def test_micro_tile_groups(dut):
    """Selects each micro tile of each micro tile group through the mux and checks its outputs."""
    clock = Clock(dut.clk, 100, unit="ns")  # 10 MHz
    cocotb.start_soon(clock.start())

    for address, group, tiles in load_micro_tile_groups():
        dut.uio_in.value = 0
        dut.ui_in.value = 0
        dut.rst_n.value = 0
        await enable_design(dut, address)
        await ClockCycles(dut.clk, 5)

        for sel, macro, vectors in tiles:
            dut._log.info(f"{group} at {address}, micro tile {sel}: {macro}")
            # uio_in[1:0] selects the micro tile
            dut.uio_in.value = sel
            dut.rst_n.value = 0
            dut.ui_in.value = 0
            await ClockCycles(dut.clk, 2)
            await FallingEdge(dut.clk)
            checked = 0
            for step, vector in enumerate(vectors):
                # Inputs change on the falling edge; the outputs are checked on the next one
                dut.rst_n.value = vector["rst_n"]
                dut.ui_in.value = vector["ui_in"]
                await FallingEdge(dut.clk)
                actual = str(dut.uo_out.value).lower()
                for bit, (expected_bit, actual_bit) in enumerate(
                    zip(vector["uo_out"], actual)
                ):
                    if expected_bit in "01":
                        assert (
                            actual_bit == expected_bit
                        ), f"{macro} step {step}: uo_out[{7 - bit}] is {actual_bit}, expected {expected_bit}"
                        checked += 1
            dut._log.info(f"{macro}: {checked} output bits checked")
            assert checked >= len(vectors)
