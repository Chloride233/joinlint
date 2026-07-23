# JoinLint

**Stop AI agents from guessing database joins.**

JoinLint is a local, Git-native data relationship linter that helps developers
and AI agents build safer multi-table queries. It scans tabular data, proposes
relationship candidates with evidence, validates cardinality and fan-out risk,
and preserves human-confirmed semantics in a reviewable model file.

JoinLint is currently in the design phase. Implementation has not started.

## v0 direction

- CLI-first and local-only
- deterministic scanning with no required LLM
- human-confirmed relationship model stored in Git
- Join and schema-drift validation for local use and CI
- local STDIO MCP adapter for existing coding and data agents
- no arbitrary SQL execution through MCP

See the approved [JoinLint v0 design](docs/superpowers/specs/2026-07-23-joinlint-v0-design.md).

## License

MIT
