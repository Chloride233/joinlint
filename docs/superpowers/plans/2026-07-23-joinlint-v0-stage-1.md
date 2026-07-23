# JoinLint v0 Stage 1: Contract and Safe Snapshots Implementation Plan

> **For agentic workers:** Execute this plan in the current session by default, task by task. Use checkbox (- [ ]) syntax for tracking. Use subagents only when the user explicitly requests delegation or parallel agent work.

**Goal:** Deliver a deterministic, safe source boundary for registered CSV directories and SQLite files, with v1 source fingerprints and cross-adapter catalog conformance.

**Architecture:** Project-root and descriptor-relative path safety belong in one module. Pydantic owns versioned contracts, while the two source adapters own only private snapshots and catalog extraction; fingerprinting is adapter-independent. Typer stays a thin command adapter. This plan deliberately creates no candidate, validation, report, baseline, or MCP code.

**Tech Stack:** Python 3.12+, Typer, Pydantic, PyYAML, rfc8785, csv, hashlib, os, sqlite3, tempfile, pytest, ruff, mypy.

## Global Constraints

- Support only macOS and Linux. If descriptor-relative no-follow guarantees are unavailable, fail closed.
- Read only configured, project-relative CSV directories and SQLite files.
- Reject symlinks in every source-path component; use directory descriptors, O_NOFOLLOW, fstat, and stable device/inode checks.
- Copy source bytes into a private temporary snapshot before inspection.
- Treat CSV membership or identity changes and failed SQLite backups as inconclusive, never as passing evidence.
- Create one monotonic deadline from each source's `max_scan_seconds`; check it before and after each blocking copy, rewalk, hash, and SQLite backup progress callback. Expiry returns `INCONCLUSIVE_SCAN`.
- Use the design document's framed SHA-256 source fingerprint exactly: joinlint-source-v1, path byte length, relative UTF-8 bytes, snapshot size, raw digest bytes.
- Config and model are strict schema-v1 YAML with no unknown keys or credentials.
- Use `rfc8785.dumps()` for every model and evidence digest; standard-library JSON serialization is not an RFC 8785 substitute.
- Sort identifiers and relative object paths by UTF-8 byte order.
- Do not expose scan, candidates, accept/reject, validate, check, baseline, report, or MCP commands until their required stage.

## Delivery Map

The design defines five ordered gates. This plan covers only Stage 1. Stage 2 implements profiles, candidates, confirmation/rejection, and join safety; Stage 3 adds generated state, baseline, check, drift, JSON results, and reports; Stage 4 adds STDIO MCP; Stage 5 adds release/performance/packaging gates. Do not start a later plan until this plan's full gate is reproducible.

## File Structure

- pyproject.toml: package metadata, Python floor, PyYAML dependency, dev tools, and console script.
- src/joinlint/errors.py: stable error codes and CLI exit categorization.
- src/joinlint/contracts.py: Pydantic versioned source, snapshot, catalog, configuration, model, and envelope records.
- src/joinlint/canonical.py: RFC 8785 canonical JSON bytes and SHA-256 contract digest.
- src/joinlint/config.py: strict YAML load/validation, lock, and atomic write.
- src/joinlint/project.py: trusted-root discovery and descriptor-relative source opening.
- src/joinlint/fingerprint.py: source v1 binary framing.
- src/joinlint/sources/csv_directory.py: CSV membership checks, private snapshot, catalog.
- src/joinlint/sources/sqlite.py: read-only backup snapshot, catalog.
- src/joinlint/sources/catalog.py: normalized catalog types and ordering.
- src/joinlint/cli.py: Typer init and source-registration commands.
- tests/unit: deterministic unit coverage.
- tests/integration: CLI, path boundary, source mutation, and adapter-conformance coverage.
- tests/fixtures/conformance: the four-table logical source in CSV and a deterministic SQLite fixture builder.
- docs/development.md: only the commands verified by the Stage 1 gate.

## Interfaces

    SourceConfig(id: str, kind: Literal['csv_directory', 'sqlite'],
                 path: PurePosixPath, limits: SourceLimits)
    SourceSnapshot(source_id: str, objects: Sequence[SnapshotObject],
                   directory: Path, fingerprint: str)
    Catalog(source_id: str, objects: Sequence[CatalogObject])
    open_project(project: Path) -> ProjectRoot
    snapshot_source(project: ProjectRoot, source: SourceConfig) -> SourceSnapshot
    read_catalog(snapshot: SourceSnapshot, kind: SourceKind) -> Catalog
    source_fingerprint(objects: Iterable[SnapshotObject]) -> str

Digest functions return a lower-case 64-character SHA-256 string. ProjectRoot and SourceSnapshot are context managers and always release their descriptors or temporary directories.

### Task 1: Bootstrap package and stable errors

**Files:**
- Create: pyproject.toml
- Create: src/joinlint/__init__.py
- Create: src/joinlint/errors.py
- Create: src/joinlint/contracts.py
- Test: tests/unit/test_errors.py

- [ ] Step 1: Add a failing test that constructs JoinLintError(ErrorCode.SOURCE_CHANGED_DURING_SCAN, 'source changed', source_id='sales') and asserts the code string, source identifier, and exit code 3.

- [ ] Step 2: Run: python -m pytest tests/unit/test_errors.py -q. Expected: failure because the package is absent.

- [ ] Step 3: Create the package with Python >=3.12; runtime dependencies Typer >=0.15, Pydantic >=2.10, PyYAML >=6.0.2, and rfc8785 >=0.1.4; and dev dependencies pytest >=8.3,<9, ruff >=0.6,<1, and mypy >=1.11,<2. Add error enum values INVALID_ARGUMENT, MALFORMED_CONFIG, UNSUPPORTED_PLATFORM, UNSUPPORTED_SOURCE, SOURCE_OUTSIDE_PROJECT, SOURCE_SYMLINK, SOURCE_KIND_MISMATCH, SOURCE_CHANGED_DURING_SCAN, INCONCLUSIVE_SCAN, SOURCE_IN_USE, and CONFIG_RACE. Map only SOURCE_CHANGED_DURING_SCAN and INCONCLUSIVE_SCAN to exit 3; all other defined errors map to exit 2. Define immutable Pydantic contract models for the Interfaces section, and expose the initial Typer application without registering later-stage commands.

- [ ] Step 4: Run: python -m ruff check src tests; python -m mypy; python -m pytest tests/unit/test_errors.py -q. Expected: all pass.

- [ ] Step 5: Commit with message: build: bootstrap JoinLint package contracts.

### Task 2: Implement strict v1 config/model contracts and canonical digesting

**Files:**
- Create: src/joinlint/canonical.py
- Create: src/joinlint/config.py
- Test: tests/unit/test_config.py
- Test: tests/unit/test_canonical.py

**Consumes:** errors and contracts from Task 1.

**Produces:** strict config/model parsing and atomic rewrite functions.

- [ ] Step 1: Write tests that reject an unknown config top-level key, duplicate source ID, non-integer version, non-positive limit, absolute path, parent path component, NUL byte, model entity with zero or two grain keys, and malformed relationship endpoint. Add two YAML documents that differ only in key order and assert equal model digests.

- [ ] Step 2: Run: python -m pytest tests/unit/test_config.py tests/unit/test_canonical.py -q. Expected: failure because modules are absent.

- [ ] Step 3: Parse with yaml.safe_load and validate with Pydantic models configured to reject extra fields. Require exact config top-level keys version and sources; permit source keys kind, path, and limits; permit limit keys max_source_bytes and max_scan_seconds. Require exact model top-level keys version, entities, and relationships. Require entities to contain source, object, and confirmed single-key grain. Permit only directed cardinalities one_to_one, one_to_many, many_to_one, and many_to_many. Pass only Pydantic JSON-compatible primitive trees to rfc8785.dumps(), then hash its bytes with SHA-256; do not use json.dumps for a canonical digest. Atomic writes use a same-directory temporary file, flush, fsync, os.replace, then fsync the parent directory.

- [ ] Step 4: Test an induced serialization failure leaves original YAML bytes unchanged. Run the Task 2 tests plus ruff and mypy. Expected: all pass.

- [ ] Step 5: Commit with message: feat: add strict v1 configuration contracts.

### Task 3: Anchor the trusted project root and open paths safely

**Files:**
- Create: src/joinlint/project.py
- Test: tests/unit/test_project.py
- Test: tests/integration/test_path_boundary.py

**Consumes:** validated project-relative paths from Task 2.

**Produces:** open_project, discover_project, and ProjectRoot.open_relative.

- [ ] Step 1: Write tests that reject an intermediate symlink, final-component symlink, FIFO, directory-as-file, file-as-directory, parent component, and absolute path. Test that discovery selects the nearest ancestor containing a regular non-symlink .joinlint/config.yaml.

- [ ] Step 2: Run: python -m pytest tests/unit/test_project.py tests/integration/test_path_boundary.py -q. Expected: failure because project.py is absent.

- [ ] Step 3: On supported POSIX platforms, open the resolved root once with O_RDONLY, O_DIRECTORY, and O_CLOEXEC, record fstat device/inode, and retain the descriptor. Split validated relative paths into components; use os.open for each component with dir_fd, O_RDONLY, O_CLOEXEC, O_NOFOLLOW, and O_DIRECTORY for every intermediate directory. Immediately fstat every descriptor and require directory or regular-file type as requested. An unavailable O_NOFOLLOW, O_DIRECTORY, dir_fd, or fstat facility returns UNSUPPORTED_PLATFORM. Any symlink returns SOURCE_SYMLINK. No fallback may use string containment checks.

- [ ] Step 4: Add a test that replaces the root path after ProjectRoot opens it and confirms a descriptor-relative read still observes the original root. Run the Task 3 tests. Expected: all pass with stable error codes.

- [ ] Step 5: Commit with message: feat: add descriptor-relative project boundary.

### Task 4: Implement initialization and source registration

**Files:**
- Create: src/joinlint/cli.py
- Modify: src/joinlint/config.py
- Test: tests/integration/test_cli_sources.py

**Consumes:** trusted-root and atomic configuration interfaces.

**Produces:** joinlint init, source add, source set, and source remove.

- [ ] Step 1: Write lifecycle tests: init creates .joinlint/state, .joinlint/analyses, .joinlint/generated, config.yaml, and model.yaml without scanning; source add sales data recognizes a safe directory as csv_directory; generated/manifest.json remains absent.

- [ ] Step 2: Run: python -m pytest tests/integration/test_cli_sources.py -q. Expected: failure because cli.py is absent.

- [ ] Step 3: Add a Typer root application with init, source add, source set, and source remove, each accepting --project. init refuses existing tracked files unless --force. source add accepts only a safely opened regular .sqlite file or directory; source set preserves kind and emits SOURCE_KIND_MISMATCH when it changes; source remove reads model entities and emits SOURCE_IN_USE with bounded dependent identifiers. Take an exclusive advisory lock on .joinlint/.lock before every mutation, reread config under the lock, then use Task 2 atomic writes. Map JoinLintError to safe stderr text and its prescribed exit code through the Typer adapter. Do not register any later-stage command.

- [ ] Step 4: Test duplicate IDs, outside path, symlink, kind mismatch, source in use, config race, init without force, and init with force. Run the Task 4 test suite. Expected: all valid mutations return 0, invalid paths return 2, and none creates generated evidence.

- [ ] Step 5: Commit with message: feat: add safe project initialization and source commands.

### Task 5: Snapshot CSV directories and calculate v1 source fingerprints

**Files:**
- Create: src/joinlint/fingerprint.py
- Create: src/joinlint/sources/__init__.py
- Create: src/joinlint/sources/csv_directory.py
- Test: tests/unit/test_fingerprint.py
- Test: tests/integration/test_csv_snapshot.py

**Consumes:** ProjectRoot and SourceConfig.

**Produces:** SourceSnapshot for a stable CSV directory.

- [ ] Step 1: Write a binary-frame test for a single object orders.csv, size 8, digest SHA-256(contents). Construct the expected bytes as the ASCII protocol prefix plus unsigned big-endian path length, UTF-8 path, unsigned big-endian size, and raw digest, then assert equal hex SHA-256. Add a mutation test with an explicit before_rescan callback that creates late.csv immediately before the second membership walk.

- [ ] Step 2: Run: python -m pytest tests/unit/test_fingerprint.py tests/integration/test_csv_snapshot.py -q. Expected: failure because the source modules are absent.

- [ ] Step 3: Create `deadline = time.monotonic() + max_scan_seconds` before the first directory walk. Walk directory entries only through descriptor-relative descriptors. Recursively enumerate only regular .csv files, reject symlinks and unsupported objects, and sort by UTF-8 relative-path bytes. Record each file device, inode, size, and nanosecond modification time. Check the deadline before and after every os.read/copy chunk, hash update, metadata capture, and the second membership walk. Copy from opened descriptors to a private TemporaryDirectory while enforcing max_source_bytes and calculating SHA-256. Rewalk membership and restat every original descriptor; if any membership or identity tuple differs, delete the snapshot and raise SOURCE_CHANGED_DURING_SCAN. If the deadline expires, delete the snapshot and raise INCONCLUSIVE_SCAN. Construct the source fingerprint from copied snapshot metadata only. Do not read originals after copying.

- [ ] Step 4: Test nested paths, bytewise order where Z.csv precedes a.csv, empty directory, unsupported regular file, symlinked file, content change, inode replacement, size change, mtime change, source-limit exceedance, an injected monotonic clock that expires during a copy, and that changing originals after a successful snapshot cannot change snapshot reads. Run Task 5 tests. Expected: stable cases pass and every mutation, limit, or deadline case is inconclusive.

- [ ] Step 5: Commit with message: feat: snapshot CSV sources with deterministic fingerprints.

### Task 6: Snapshot SQLite including committed WAL state

**Files:**
- Create: src/joinlint/sources/sqlite.py
- Test: tests/unit/test_sqlite_snapshot.py
- Test: tests/integration/test_sqlite_snapshot.py

**Consumes:** safe file opens, SourceSnapshot, and fingerprinting.

**Produces:** private SQLite backups without arbitrary SQL execution.

- [ ] Step 1: Write a test that creates a WAL database, commits an orders row, snapshots it, and reads row count 1 from the private copy. Add a test that no change occurs to the original database's bytes.

- [ ] Step 2: Run: python -m pytest tests/unit/test_sqlite_snapshot.py tests/integration/test_sqlite_snapshot.py -q. Expected: failure because the adapter is absent.

- [ ] Step 3: Use a ProjectRoot API that returns and retains a verified parent-directory FD, validated database leaf name, and pre-backup fstat identity. Construct the source URI as `file:/dev/fd/<parent_fd>/<percent-encoded-leaf>?mode=ro` while that parent FD remains open, so SQLite resolves the conventional sibling `-wal` file through the verified directory rather than through a bare database-file FD. Set PRAGMA query_only=ON, begin a read transaction, and invoke the online backup API to a private temporary database. Use a progress callback that checks the same source monotonic deadline after each page batch. In a finally block, close connections, then re-open the leaf descriptor-relatively and compare device, inode, size, and nanosecond mtime with the pre-backup identity; a mismatch is SOURCE_CHANGED_DURING_SCAN. A failed, interrupted, active, or expired backup maps to INCONCLUSIVE_SCAN. Hash and fingerprint only the backup using the configured fixed relative object name. Schema extraction may use only application-owned sqlite_master table enumeration, PRAGMA table_info with internal identifier quoting, and PRAGMA index_list with internal identifier quoting. Exclude sqlite_ internal tables.

- [ ] Step 4: Test a committed WAL row, malformed database, source symlink, identity change before and after backup, an injected deadline expiry from the backup progress callback, a table name containing quote characters, excluded internal tables, and no original writes. Run Task 6 tests. Expected: valid WAL snapshots pass; unsafe, incomplete, or timed-out sources produce safe failures.

- [ ] Step 5: Commit with message: feat: add read-only SQLite snapshot adapter.

### Task 7: Normalize catalogs and pass cross-adapter conformance

**Files:**
- Create: src/joinlint/sources/catalog.py
- Modify: src/joinlint/sources/csv_directory.py
- Modify: src/joinlint/sources/sqlite.py
- Create: tests/fixtures/conformance/customers.csv
- Create: tests/fixtures/conformance/orders.csv
- Create: tests/fixtures/conformance/order_items.csv
- Create: tests/fixtures/conformance/payments.csv
- Create: tests/fixtures/build_conformance_sqlite.py
- Test: tests/integration/test_adapter_conformance.py

**Consumes:** both snapshot adapters.

**Produces:** equal normalized Catalog values for one logical source.

- [ ] Step 1: Write a test that snapshots the four-table CSV fixture and the SQLite fixture built from it, calls read_catalog for each, and asserts exactly equal ordered objects, columns, physical types, nullability, and row counts.

- [ ] Step 2: Run: python -m pytest tests/integration/test_adapter_conformance.py -q. Expected: failure because the normalized catalog is absent.

- [ ] Step 3: Define CatalogObject(name, columns, row_count) and CatalogColumn(name, physical_type, nullable). Permit physical types exactly integer, real, text, boolean, unknown. CSV catalog parsing requires unique nonempty headers, equal row width, and whole-file lexical inference over non-null values. SQLite catalog normalizes SQLite affinity to the same enum and counts rows from the private backup. CSV object names are relative paths minus .csv; SQLite object names are table names. Sort objects and columns by UTF-8 bytes. Build SQLite from CSV with checked-in Python fixture code rather than committing a binary database. The fixture uses customers, orders, order_items, and payments with controlled null, orphan, and duplicate values.

- [ ] Step 4: Test deterministic order, duplicate headers, malformed quote/row-width failures, type conflicts, empty tables, and quoted SQLite identifiers. Run: python -m ruff check src tests; python -m mypy; python -m pytest -q. Expected: all pass and fixture catalogs are byte-for-byte equivalent after normalization.

- [ ] Step 5: Commit with message: test: prove CSV and SQLite adapter conformance.

### Task 8: Freeze the Stage 1 CI gate and document the delivered slice

**Files:**
- Create: .github/workflows/stage-1.yml
- Create: docs/development.md
- Modify: README.md
- Test: tests/integration/test_stage_1_gate.py

**Consumes:** completed Tasks 1 through 7.

**Produces:** reproducible Linux CI and accurate documentation.

- [ ] Step 1: Write a test that joinlint scan and joinlint serve-mcp each return exit 2 because no later-stage command is exposed.

- [ ] Step 2: Run: python -m pytest tests/integration/test_stage_1_gate.py -q. Expected: failure until unsupported commands are mapped to a Typer user-correctable result.

- [ ] Step 3: Add a GitHub Actions Ubuntu workflow running checkout, setup-python 3.12, python -m pip install '.[dev]', python -m ruff check src tests, python -m mypy, and python -m pytest -q. Document those exact commands and state that only safe configuration, snapshots, fingerprints, and catalog conformance are implemented. Update README from design phase to Stage 1 implementation only after Tasks 1 through 7 pass. Do not claim profiles, candidate discovery, validation, reports, CI checks, or MCP are shipped.

- [ ] Step 4: From a clean virtual environment run: python -m pip install -e '.[dev]'; python -m ruff check src tests; python -m mypy; python -m pytest -q. Expected: all checks pass; conformance catalogs compare equal; each symlink/mutation fixture fails closed.

- [ ] Step 5: Commit with message: ci: add stage one security and conformance gate.

## Stage 1 Acceptance Checklist

- [ ] Version-1 config/model validation, digesting, and atomic write tests pass.
- [ ] Every approved path is opened descriptor-relatively without following symlinks.
- [ ] CSV snapshots are immutable and mutations fail with an inconclusive code.
- [ ] SQLite snapshots include committed WAL state and do not modify originals.
- [ ] CSV and SQLite snapshot deadlines use a monotonic clock and return INCONCLUSIVE_SCAN without publishing evidence.
- [ ] The exact framed fingerprint test passes.
- [ ] CSV and SQLite yield exactly equal normalized catalogs for the same four-table fixture.
- [ ] Only init and source registration commands are public.
- [ ] Ruff, mypy, pytest, and Ubuntu CI all pass from clean setup.

## Self-Review

**Spec coverage:** The plan covers exactly Stage 1 from design section 16.1: project discovery, config/model, descriptor-relative access, CSV/SQLite snapshots, source fingerprints, and adapter conformance. It includes only the source-registration CLI needed to create safe inputs.

**Deferred work:** Profiles, candidates, confirmed relationship edits, validation, generated artifact transactions, baseline, drift, JSON envelopes, report, MCP, benchmark suite, performance fixture, packaging, and release claims are reserved for their named later plans.

**Placeholder scan:** Every task specifies files, a test-first step, a command with expected result, behavior to implement, a verification step, and a commit. The explicit callback in Task 5 replaces timing-dependent race testing.

**Type consistency:** SourceConfig, ProjectRoot, SourceSnapshot, Catalog, snapshot_source, read_catalog, and source_fingerprint are the only cross-task interfaces. Every later task uses the records established in Tasks 1 and 2.

## Execution Handoff

Execute Tasks 1 through 8 sequentially, preserving each test gate. Stop after the Stage 1 acceptance checklist passes, then prepare the Stage 2 plan; do not start a later-stage feature in this implementation slice.
