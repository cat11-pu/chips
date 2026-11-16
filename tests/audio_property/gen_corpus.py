#!/usr/bin/env python3
"""Generate deterministic AYT valid and corrupted corpora."""

from __future__ import annotations

import argparse
import hashlib
import random
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple


HEADER_SIZE = 40
FRAME_ENTRY_SIZE = 8
OP_SIZE = 8
MAGIC = 0x31545941
VERSION = 1
NO_LOOP = 0xFFFFFFFF
VALID_COUNT = 50
MALFORMED_COUNT = 200
KINDS = ("truncated", "invalid_register", "oversized", "cycle")


@dataclass(frozen=True)
class Op:
    kind: int
    reg: int = 0
    value: int = 0
    target: int = 0


def write_op(op: Op) -> bytes:
    return struct.pack("<BBHI", op.kind, op.reg, op.value, op.target)


def build_ayt(
    frames: Sequence[Sequence[Op]],
    clock_hz: int,
    frame_rate: int,
    play_frames: int,
    loop_frame: int = NO_LOOP,
) -> bytes:
    frame_count = len(frames)
    directory_end = HEADER_SIZE + frame_count * FRAME_ENTRY_SIZE
    payload = bytearray()
    entries = []
    for ops in frames:
        offset = directory_end + len(payload)
        entries.append((offset, len(ops)))
        for op in ops:
            payload += write_op(op)

    data = bytearray(HEADER_SIZE)
    struct.pack_into("<IHHIHHIIIIII", data, 0,
                     MAGIC, VERSION, HEADER_SIZE, clock_hz, frame_rate, 0,
                     frame_count, play_frames, loop_frame, HEADER_SIZE,
                     len(payload), 0)
    assert len(data) == HEADER_SIZE
    for offset, op_count in entries:
        data += struct.pack("<IHH", offset, op_count, 0)
    data += payload
    return bytes(data)


def make_valid(seed: int) -> Tuple[bytes, str]:
    rng = random.Random(0xA7_0000 + seed)
    top_count = rng.randint(6, 12)
    shared_count = rng.randint(1, 3)
    clock_hz = rng.choice((1500000, 1773400, 2000000))
    frame_rate = 50
    frames: List[List[Op]] = []

    for _ in range(top_count):
        tone = rng.randint(25, 900)
        volume = rng.randint(4, 15)
        ops = [
            Op(0, 0, tone & 0xFF),
            Op(0, 1, (tone >> 8) & 0x0F),
            Op(0, 7, 0x38),
            Op(0, 8, volume),
        ]
        if rng.random() < 0.7:
            ops.append(Op(0, 2, rng.randint(0, 255)))
            ops.append(Op(0, 9, rng.randint(0, 15)))
        if rng.random() < 0.4:
            ops.append(Op(0, 6, rng.randint(1, 31)))
            ops.append(Op(0, 7, 0x30))
        if rng.random() < 0.5:
            env_period = rng.randint(1, 4095)
            ops.extend((
                Op(0, 11, env_period & 0xFF),
                Op(0, 12, (env_period >> 8) & 0xFF),
                Op(0, 13, rng.randint(0, 15)),
                Op(0, 10, 0x10),
            ))
        ops.append(Op(1, target=top_count + rng.randrange(shared_count)))
        frames.append(ops)

    for _ in range(shared_count):
        period = rng.randint(1, 4095)
        frames.append([
            Op(0, 4, period & 0xFF),
            Op(0, 5, (period >> 8) & 0x0F),
            Op(0, 10, rng.randint(0, 15)),
        ])

    data = build_ayt(frames, clock_hz, frame_rate, top_count)
    return data, "valid"


def make_cycle(seed: int) -> Tuple[bytes, int, int]:
    rng = random.Random(0xC1_0000 + seed)
    frames = [
        [Op(0, 8, rng.randint(0, 15)), Op(1, target=1)],
        [Op(0, 9, rng.randint(0, 15)), Op(1, target=2)],
        [Op(0, 10, rng.randint(0, 15)), Op(1, target=0)],
    ]
    data = build_ayt(frames, 1500000, 50, 1)
    # Frame 2 starts at 40 + 3*8 + 4*8 = 96; its ref op is the second op.
    ref_byte = HEADER_SIZE + 3 * FRAME_ENTRY_SIZE + 5 * OP_SIZE
    return data, 2, ref_byte


def choose_write(frame: Sequence[Op]) -> int:
    for index, op in enumerate(frame):
        if op.kind == 0:
            return index
    raise ValueError("frame has no write op")


def make_truncated(base: bytes, seed: int) -> Tuple[bytes, int, int]:
    rng = random.Random(0x7A_0000 + seed)
    frame_count = struct.unpack_from("<I", base, 16)[0]
    directory_end = HEADER_SIZE + frame_count * FRAME_ENTRY_SIZE
    case = seed % 3
    if case == 0:
        cut = rng.randint(1, HEADER_SIZE - 1)
        frame = 0
    elif case == 1:
        cut = rng.randint(HEADER_SIZE, directory_end - 1)
        frame = min(frame_count - 1, (cut - HEADER_SIZE) // FRAME_ENTRY_SIZE)
    else:
        cut = rng.randint(directory_end + 1, len(base) - 1)
        frame = min(frame_count - 1, (cut - directory_end) // OP_SIZE)
    return base[:cut], frame, cut


def make_invalid_register(base: bytes, seed: int) -> Tuple[bytes, int, int]:
    rng = random.Random(0x1A_0000 + seed)
    data = bytearray(base)
    frame_count = struct.unpack_from("<I", data, 16)[0]
    frame_index = rng.randrange(frame_count)
    entry_offset = HEADER_SIZE + frame_index * FRAME_ENTRY_SIZE
    frame_offset, op_count = struct.unpack_from("<IH", data, entry_offset)

    write_ops = []
    for op_index in range(op_count):
        op_offset = frame_offset + op_index * OP_SIZE
        if data[op_offset] == 0:
            write_ops.append(op_offset)
    if not write_ops:
        op_offset = frame_offset
        data[op_offset] = 0
    else:
        op_offset = rng.choice(write_ops)
    data[op_offset + 1] = rng.randint(16, 255)
    return bytes(data), frame_index, op_offset + 1


def make_oversized(base: bytes, seed: int) -> Tuple[bytes, int, int]:
    rng = random.Random(0x05_0000 + seed)
    data = bytearray(base)
    frame_count = struct.unpack_from("<I", data, 16)[0]
    frame_index = rng.randrange(frame_count)
    entry_offset = HEADER_SIZE + frame_index * FRAME_ENTRY_SIZE
    struct.pack_into("<H", data, entry_offset + 4, rng.choice((257, 1024, 65535)))
    return bytes(data), frame_index, entry_offset + 4


def write_manifest(path: Path, rows: Sequence[Tuple[Path, str, int, int, str]]) -> None:
    lines = ["path\tkind\tframe\tbyte\tsha256"]
    for file_path, kind, frame, byte_offset, digest in rows:
        lines.append(
            f"{file_path.relative_to(path.parent)}\t{kind}\t{frame}\t"
            f"{byte_offset}\t{digest}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--counterexamples", type=Path, required=True)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    args.counterexamples.mkdir(parents=True, exist_ok=True)
    for old in args.output.glob("*.ayt"):
        old.unlink()
    for old in args.counterexamples.glob("*.ayt"):
        old.unlink()

    valid_rows = []
    valid_dir = args.output / "valid"
    valid_dir.mkdir(exist_ok=True)
    for index in range(VALID_COUNT):
        data, _ = make_valid(index)
        path = valid_dir / f"valid_{index:03d}.ayt"
        path.write_bytes(data)
        valid_rows.append((
            path,
            "valid",
            struct.unpack_from("<I", data, 20)[0],
            0,
            hashlib.sha256(data).hexdigest(),
        ))
    write_manifest(args.output / "valid_manifest.tsv", valid_rows)

    bad_rows = []
    counterexample_rows = []
    for index in range(MALFORMED_COUNT):
        kind = KINDS[index % len(KINDS)]
        if kind == "cycle":
            data, frame, byte_offset = make_cycle(index)
        else:
            base, _ = make_valid(index + 1000)
            if kind == "truncated":
                data, frame, byte_offset = make_truncated(base, index)
            elif kind == "invalid_register":
                data, frame, byte_offset = make_invalid_register(base, index)
            else:
                data, frame, byte_offset = make_oversized(base, index)
        path = args.output / f"bad_{index:03d}_{kind}.ayt"
        path.write_bytes(data)
        row = (path, kind, frame, byte_offset, hashlib.sha256(data).hexdigest())
        bad_rows.append(row)
        if index < len(KINDS):
            counterexample_rows.append(row)

    write_manifest(args.output / "malformed_manifest.tsv", bad_rows)
    for row in counterexample_rows:
        source, kind, frame, byte_offset, digest = row
        dest = args.counterexamples / source.name
        shutil.copyfile(source, dest)
        print(f"{dest.name}: kind={kind} frame={frame} byte={byte_offset} sha256={digest}")
    print(f"generated {VALID_COUNT} valid and {MALFORMED_COUNT} malformed AYT files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
