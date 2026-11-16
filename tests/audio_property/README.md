# AYT audio property tests

These tests exercise the AY-3-8910 audio path in `chips/ay38910.h` with a
deterministic register-frame container named `AYT`. The container is an input
fixture for this repository; it is not a claim that `chips` implements an
external music-file loader.

## Corpus

`gen_corpus.py` creates:

- 50 valid files with tone, noise, envelope, direct register writes, and
  acyclic shared-frame references.
- 200 malformed files, exactly 50 for each corruption class, interleaved
  `truncated`, `invalid_register`, `oversized`, `cycle`.
- `malformed_manifest.tsv` and `valid_manifest.tsv` with the expected frame
  and byte offset for every malformed file.

The generated files live under
`artifacts/audio-property/corpus/`. Four representative negatives are copied
to `tests/audio_property/counterexamples/` on each run.

## AYT layout

All multi-byte fields are little-endian.

Header (40 bytes):

| offset | type | meaning |
|---:|---|---|
| 0 | `u32` | magic `AYT1` |
| 4 | `u16` | version, must be 1 |
| 6 | `u16` | header size, must be 40 |
| 8 | `u32` | AY clock in Hz, 100000..4000000 |
| 12 | `u16` | frame rate, 1..1000 |
| 14 | `u16` | flags, must be 0 |
| 16 | `u32` | directory frame count, 1..4096 |
| 20 | `u32` | top-level frames to play, 1..frame count |
| 24 | `u32` | loop frame or `0xffffffff` |
| 28 | `u32` | directory offset, must be 40 |
| 32 | `u32` | payload byte count |
| 36 | `u32` | reserved, must be 0 |

Frame directory entry (8 bytes): `u32 absolute_offset`, `u16 op_count`,
`u16 reserved`. `op_count` is limited to 256.

Operation (8 bytes): `u8 kind`, `u8 reg`, `u16 value`, `u32 target`.

- kind 0: write `value` to AY register `reg`; `reg` must be 0..15.
- kind 1: expand frame `target`; `reg`, `value`, and the target's frame time
  are reserved/zero. References must form a DAG.

Each played top-level frame applies its expanded writes, then emits
`44100 / frame_rate` samples through `ay38910_tick()`.

## Execution

From this directory:

```sh
make test
```

The harness is compiled with ASan, UBSan, and LeakSanitizer. Every malformed
file runs in a separate process so a crash is attributable to one input. A
pass requires:

- no signal, ASan/UBSan report, or leak report;
- parser rejection with the expected error class and frame offset;
- valid files produce the same sample count and every sample within `1e-6`
  of the independent Python model;
- 10,000 frames of raw 0..255 register addresses reach the AY chip without a
  crash, sanitizer finding, or non-finite sample.

Reports are written to:

- `artifacts/audio-property/summary.json`
- `artifacts/audio-property/summary.md`
- `artifacts/audio-property/malformed_report.tsv`
- `artifacts/audio-property/valid_report.tsv`

The reference model in `ayt_reference.py` intentionally does not include or
link `chips/ay38910.h`; it independently implements the register semantics and
AY sample state used by the fixture.
