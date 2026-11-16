# Intentional counterexamples

`gen_corpus.py` refreshes these four negatives on every test run:

| file | expected rejection | frame | byte |
|---|---|---:|---:|
| `bad_000_truncated.ayt` | `truncated` | 0 | 13 |
| `bad_001_invalid_register.ayt` | `invalid_register` | 0 | 121 |
| `bad_002_oversized.ayt` | `oversized` | 5 | 84 |
| `bad_003_cycle.ayt` | `cycle` | 2 | 104 |

The frame and byte offsets are asserted against the parser. The files are
also hashed in the generated `malformed_manifest.tsv`; changing any byte
invalidates the expected offset and should be treated as a corpus change,
not as a parser fix.

These are counterexamples to the *acceptance* property: a correct parser must
never return them as valid audio. They are not evidence of a `chips` defect.
