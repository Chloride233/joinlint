from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any


async def install_host_binaries(
    host_versions: dict[str, str],
    destination: Path,
    *,
    platform: str = "linux-x64",
) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str | int]] = []
    for host in sorted(host_versions):
        version = host_versions[host]
        source = _binary_source(host)
        binary, resolved_version = await _download_binary(source, version, platform)
        if resolved_version != version:
            raise ValueError(f"resolved {host} version does not match the frozen version")
        target = destination / source.binary
        if target.exists() or target.is_symlink():
            raise ValueError(f"host binary destination already exists: {source.binary}")
        target.write_bytes(binary)
        target.chmod(0o755)
        records.append(
            {
                "host": host,
                "version": version,
                "platform": platform,
                "sha256": hashlib.sha256(binary).hexdigest(),
                "size_bytes": len(binary),
            }
        )
    return {"schema_version": 1, "records": records}


def install_and_attest(
    host_versions: dict[str, str],
    destination: Path,
    output: Path,
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise ValueError("host binary attestation output already exists")
    attestation = asyncio.run(install_host_binaries(host_versions, destination))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_json(attestation) + b"\n")
    return attestation


def _binary_source(host: str) -> Any:
    if host == "codex":
        from inspect_swe._codex_cli.agentbinary import codex_cli_binary_source

        return codex_cli_binary_source()
    from inspect_swe._claude_code.agentbinary import claude_code_binary_source

    return claude_code_binary_source()


async def _download_binary(
    source: Any,
    version: str,
    platform: str,
) -> tuple[bytes, str]:
    from inspect_swe._util.agentbinary import download_agent_binary_async

    return await download_agent_binary_async(source, version, platform)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install frozen Agent host binaries")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--codex-version", required=True)
    install.add_argument("--claude-version", required=True)
    install.add_argument("--destination", type=Path, required=True)
    install.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    attestation = install_and_attest(
        {
            "codex": arguments.codex_version,
            "claude_code": arguments.claude_version,
        },
        arguments.destination,
        arguments.output,
    )
    print(_canonical_json(attestation).decode("utf-8"))
    return 0


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
