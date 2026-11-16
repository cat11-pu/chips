#!/usr/bin/env python3
"""
Independent AYT parser and AY-3-8910 sample model used as the oracle.

The model follows the AY register semantics and the fixed-point float
rounding used by the property-test fixture. It intentionally does not import
or link against chips/ay38910.h.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


HEADER_SIZE = 40
FRAME_ENTRY_SIZE = 8
OP_SIZE = 8
MAX_FILE_SIZE = 4 * 1024 * 1024
MAX_FRAMES = 4096
MAX_OPS_PER_FRAME = 256
MAX_REF_DEPTH = 64
SAMPLE_RATE = 44100
MAGIC = 0x31545941
VERSION = 1
NO_LOOP = 0xFFFFFFFF


class AytError(Exception):
    def __init__(self, kind: str, frame: int = 0, byte: int = 0, message: str = ""):
        super().__init__(message)
        self.kind = kind
        self.frame = frame
        self.byte = byte
        self.message = message


@dataclass(frozen=True)
class Op:
    kind: int
    reg: int
    value: int
    target: int
    byte_offset: int


@dataclass
class Frame:
    offset: int
    ops: Tuple[Op, ...]


@dataclass
class AytFile:
    data: bytes
    clock_hz: int
    frame_rate: int
    play_frames: int
    loop_frame: int
    frames: Tuple[Frame, ...]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _validate_graph(frames: Tuple[Frame, ...]) -> None:
    color = bytearray(len(frames))
    for root in range(len(frames)):
        if color[root] != 0:
            continue
        color[root] = 1
        frame_stack = [root]
        op_stack = [0]
        while frame_stack:
            frame_index = frame_stack[-1]
            op_index = op_stack[-1]
            frame = frames[frame_index]
            if op_index >= len(frame.ops):
                color[frame_index] = 2
                frame_stack.pop()
                op_stack.pop()
                continue
            op_stack[-1] = op_index + 1
            op = frame.ops[op_index]
            if op.kind != 1:
                continue
            if op.target >= len(frames):
                raise AytError(
                    "bad_format",
                    frame_index,
                    op.byte_offset,
                    f"reference target {op.target} out of range",
                )
            if color[op.target] == 1:
                raise AytError(
                    "cycle",
                    frame_index,
                    op.byte_offset,
                    f"frame {frame_index} references frame {op.target} while it is active",
                )
            if color[op.target] == 0:
                if len(frame_stack) >= MAX_REF_DEPTH:
                    raise AytError(
                        "cycle",
                        frame_index,
                        op.byte_offset,
                        f"reference depth exceeds {MAX_REF_DEPTH}",
                    )
                color[op.target] = 1
                frame_stack.append(op.target)
                op_stack.append(0)


def parse_ayt(path: Path) -> AytFile:
    data = path.read_bytes()
    if len(data) > MAX_FILE_SIZE:
        raise AytError("oversized", 0, 0, "file exceeds test size limit")
    if len(data) < HEADER_SIZE:
        raise AytError("truncated", 0, len(data), "truncated header")
    if _u32(data, 0) != MAGIC:
        raise AytError("bad_format", 0, 0, "bad magic")

    version = _u16(data, 4)
    header_size = _u16(data, 6)
    clock_hz = _u32(data, 8)
    frame_rate = _u16(data, 12)
    flags = _u16(data, 14)
    frame_count = _u32(data, 16)
    play_frames = _u32(data, 20)
    loop_frame = _u32(data, 24)
    directory_offset = _u32(data, 28)
    payload_bytes = _u32(data, 32)
    reserved = _u32(data, 36)

    if version != VERSION or header_size != HEADER_SIZE or flags != 0 or reserved != 0:
        raise AytError("bad_format", 0, 0, "unsupported header")
    if not 100000 <= clock_hz <= 4000000:
        raise AytError("bad_format", 0, 8, "clock rate out of range")
    if not 1 <= frame_rate <= 1000:
        raise AytError("bad_format", 0, 12, "frame rate out of range")
    if not 1 <= frame_count <= MAX_FRAMES:
        raise AytError("oversized", 0, 16, "frame count out of range")
    if not 1 <= play_frames <= MAX_FRAMES or play_frames > frame_count:
        raise AytError("bad_format", 0, 20, "play frame count out of range")
    if loop_frame != NO_LOOP and loop_frame >= frame_count:
        raise AytError("bad_format", 0, 24, "loop frame out of range")
    if directory_offset != HEADER_SIZE:
        raise AytError("bad_format", 0, 28, "bad directory offset")
    if payload_bytes > MAX_FILE_SIZE:
        raise AytError("oversized", 0, 32, "payload too large")

    directory_end = directory_offset + frame_count * FRAME_ENTRY_SIZE
    if directory_end > len(data):
        available = max(0, len(data) - directory_offset)
        frame = min(frame_count - 1, available // FRAME_ENTRY_SIZE)
        raise AytError("truncated", frame, len(data), "truncated frame directory")
    payload_end = directory_end + payload_bytes
    if payload_end > len(data):
        available = max(0, len(data) - directory_end)
        frame = min(frame_count - 1, available // OP_SIZE)
        raise AytError("truncated", frame, len(data), "truncated payload")

    frames: List[Frame] = []
    for frame_index in range(frame_count):
        entry_offset = directory_offset + frame_index * FRAME_ENTRY_SIZE
        frame_offset = _u32(data, entry_offset)
        op_count = _u16(data, entry_offset + 4)
        frame_reserved = _u16(data, entry_offset + 6)
        if op_count > MAX_OPS_PER_FRAME:
            raise AytError("oversized", frame_index, entry_offset + 4, "too many ops")
        if frame_reserved != 0:
            raise AytError("bad_format", frame_index, entry_offset + 6, "nonzero reserved")
        frame_end = frame_offset + op_count * OP_SIZE
        if frame_offset < directory_end or frame_end > payload_end or frame_end > len(data):
            raise AytError("truncated", frame_index, entry_offset, "frame outside payload")

        ops: List[Op] = []
        for op_index in range(op_count):
            op_offset = frame_offset + op_index * OP_SIZE
            kind = data[op_offset]
            reg = data[op_offset + 1]
            value = _u16(data, op_offset + 2)
            target = _u32(data, op_offset + 4)
            if kind == 0:
                if reg >= 16:
                    raise AytError(
                        "invalid_register",
                        frame_index,
                        op_offset + 1,
                        f"register {reg} is outside AY's 16-register bank",
                    )
                if target != 0:
                    raise AytError("bad_format", frame_index, op_offset + 4, "write target")
            elif kind == 1:
                if reg != 0 or value != 0:
                    raise AytError("bad_format", frame_index, op_offset + 1, "reference fields")
            else:
                raise AytError("bad_format", frame_index, op_offset, "unknown op kind")
            ops.append(Op(kind, reg, value, target, op_offset))
        frames.append(Frame(frame_offset, tuple(ops)))

    result = AytFile(data, clock_hz, frame_rate, play_frames, loop_frame, tuple(frames))
    _validate_graph(result.frames)
    return result


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


AY_VOLUMES = tuple(
    f32(value)
    for value in (
        0.0,
        0.00999465934234,
        0.0144502937362,
        0.0210574502174,
        0.0307011520562,
        0.0455481803616,
        0.0644998855573,
        0.107362478065,
        0.126588845655,
        0.20498970016,
        0.292210269322,
        0.372838941024,
        0.492530708782,
        0.635324635691,
        0.805584802014,
        1.0,
    )
)

SHAPES = (
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
    (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
)

REG_MASKS = (
    0xFF, 0x0F, 0xFF, 0x0F, 0xFF, 0x0F, 0x1F, 0xFF,
    0x1F, 0x1F, 0x1F, 0xFF, 0xFF, 0x0F, 0xFF, 0xFF,
)


class Ay:
    def __init__(self, clock_hz: int, sound_hz: int = SAMPLE_RATE, magnitude: float = 0.5):
        self.reg = [0] * 16
        self.tick_count = 0
        self.addr = 0
        self.tone_period = [0, 0, 0]
        self.tone_counter = [0, 0, 0]
        self.tone_bit = [0, 0, 0]
        self.tone_disable = [0, 0, 0]
        self.noise_disable = [0, 0, 0]
        self.noise_period = 0
        self.noise_counter = 0
        self.noise_rng = 1
        self.noise_bit = 0
        self.env_period = 0
        self.env_counter = 0
        self.env_shape_holding = False
        self.env_shape_hold = False
        self.env_shape_counter = 0
        self.env_shape_state = 0
        self.sample_period = (clock_hz * 16) // sound_hz
        self.sample_counter = self.sample_period
        self.mag = f32(magnitude)
        self.sample = f32(0.0)
        self.dcadj_sum = f32(0.0)
        self.dcadj_pos = 0
        self.dcadj_buf = [f32(0.0)] * 512
        self._update_values()
        self._restart_env_shape()

    def _update_values(self) -> None:
        for channel in range(3):
            period = (self.reg[2 * channel + 1] << 8) | self.reg[2 * channel]
            self.tone_period[channel] = 1 if period == 0 else period
            self.tone_disable[channel] = (self.reg[7] >> channel) & 1
            self.noise_disable[channel] = (self.reg[7] >> (3 + channel)) & 1
        self.noise_period = 1 if self.reg[6] == 0 else self.reg[6]
        period = (self.reg[12] << 8) | self.reg[11]
        self.env_period = 1 if period == 0 else period

    def _restart_env_shape(self) -> None:
        self.env_shape_holding = False
        self.env_shape_counter = 0
        if not (self.reg[13] & 0x08) or (self.reg[13] & 0x01):
            self.env_shape_hold = True
        else:
            self.env_shape_hold = False

    def set_reg(self, reg: int, value: int) -> None:
        if reg >= 16:
            return
        self.reg[reg] = value & REG_MASKS[reg]
        self._update_values()
        if reg == 13:
            self._restart_env_shape()

    def write(self, reg: int, value: int) -> None:
        self.addr = reg & 0xFF
        self.set_reg(self.addr, value)

    def _dcadjust(self, value: float) -> float:
        old = self.dcadj_buf[self.dcadj_pos]
        self.dcadj_sum = f32(f32(self.dcadj_sum - old) + value)
        self.dcadj_buf[self.dcadj_pos] = value
        self.dcadj_pos = (self.dcadj_pos + 1) & 511
        avg = f32(self.dcadj_sum / f32(512.0))
        return f32(value - avg)

    def tick(self) -> bool:
        self.tick_count = (self.tick_count + 1) & 0xFFFFFFFF
        if (self.tick_count & 7) == 0:
            for channel in range(3):
                counter = self.tone_counter[channel] + 1
                if counter >= self.tone_period[channel]:
                    counter = 0
                    self.tone_bit[channel] ^= 1
                self.tone_counter[channel] = counter
            self.noise_counter += 1
            if self.noise_counter >= self.noise_period:
                self.noise_counter = 0
                self.noise_bit ^= 1
                if self.noise_bit:
                    feedback = ((self.noise_rng & 1) ^ ((self.noise_rng >> 3) & 1)) << 17
                    self.noise_rng = (self.noise_rng ^ feedback) >> 1
        if (self.tick_count & 15) == 0:
            self.env_counter += 1
            if self.env_counter >= self.env_period:
                self.env_counter = 0
                if not self.env_shape_holding:
                    self.env_shape_counter = (self.env_shape_counter + 1) & 0x1F
                    if self.env_shape_hold and self.env_shape_counter == 0x1F:
                        self.env_shape_holding = True
                self.env_shape_state = SHAPES[self.reg[13]][self.env_shape_counter]

        self.sample_counter -= 16
        if self.sample_counter <= 0:
            self.sample_counter += self.sample_period
            total = f32(0.0)
            for channel in range(3):
                amp = self.reg[8 + channel]
                if (amp & 0x10) == 0:
                    volume = AY_VOLUMES[amp & 0x0F]
                else:
                    volume = AY_VOLUMES[self.env_shape_state]
                enabled = (
                    (self.tone_bit[channel] | self.tone_disable[channel])
                    & ((self.noise_rng & 1) | self.noise_disable[channel])
                )
                if enabled:
                    total = f32(total + volume)
            self.sample = f32(self._dcadjust(total) * self.mag)
            return True
        return False


def _apply_frame(ay: Ay, frames: Tuple[Frame, ...], index: int, depth: int = 0) -> None:
    if depth > MAX_REF_DEPTH:
        raise AytError("cycle", index, 0, "reference depth exceeded")
    for op in frames[index].ops:
        if op.kind == 0:
            ay.write(op.reg, op.value & 0xFF)
        else:
            _apply_frame(ay, frames, op.target, depth + 1)


def render(path: Path) -> List[float]:
    ayt = parse_ayt(path)
    samples_per_frame = SAMPLE_RATE // ayt.frame_rate
    if samples_per_frame == 0:
        raise AytError("bad_format", 0, 12, "frame rate too high")
    ay = Ay(ayt.clock_hz)
    samples: List[float] = []
    for frame_index in range(ayt.play_frames):
        _apply_frame(ay, ayt.frames, frame_index)
        for _ in range(samples_per_frame):
            while not ay.tick():
                pass
            samples.append(ay.sample)
    return samples


def write_pcm(path: Path, samples: List[float]) -> None:
    with path.open("wb") as fp:
        fp.write(struct.pack("<I", len(samples)))
        fp.write(struct.pack(f"<{len(samples)}f", *samples))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        samples = render(args.input)
    except AytError as exc:
        print(
            f"{exc.kind} at frame {exc.frame} byte {exc.byte}: {exc.message}",
            file=sys.stderr,
        )
        return 2
    write_pcm(args.output, samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
