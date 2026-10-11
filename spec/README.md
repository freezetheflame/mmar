# MMAR shared protocol fixtures

These fixtures prepare Python/Rust conformance checks. They are implementation-neutral data, not evidence that a Rust backend exists.

`decision-vectors.json` contains raw model-output JSON strings, optional Limits overrides, and expected accepted decisions or the public `invalid_decision` code. Consumers must parse the raw string directly; decoding it into a generic map first would lose duplicate keys.

Rules preserved by the reference Python implementation:

- Exactly `actions` and `memory_replace` at the top level; no duplicate or unknown keys, including nested Action objects.
- Actions are `send` with unique nonempty targets, or `broadcast` with a group ID. No tools or commands.
- IDs use ASCII `[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}`.
- Content and replacement memory must be nonempty after the reference whitespace check, while their original text is preserved.
- Text and output limits count UTF-8 bytes, never characters.
- Topology, sender identity, group permission, and delivery capacity are Store checks; parsing success does not authorize an Action.
- Reference whitespace includes U+0009–000D, U+001C–0020, U+0085, U+00A0, U+1680, U+2000–200A, U+2028, U+2029, U+202F, U+205F, and U+3000. The control-whitespace case guards against differing language trim defaults.

Run the current reference validator with:

```bash
python -m unittest tests.test_conformance -v
```

Protocol fixtures cover parsing only. A Rust backend must also pass the runtime transaction, crash-recovery, budget, ownership, event, and lifecycle scenarios described in [the Rust mode plan](../docs/rust-mode-plan.md).
