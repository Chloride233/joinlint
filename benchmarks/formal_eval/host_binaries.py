from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from benchmarks.formal_eval.contracts import Host, StrictModel
from joinlint.contracts import canonical_json


SandboxPlatform = Literal["linux-x64"]


class HostBinaryRecord(StrictModel):
    host: Host
    version: str
    platform: SandboxPlatform
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class HostBinaryAttestation(StrictModel):
    schema_version: Literal[1] = 1
    records: tuple[HostBinaryRecord, ...]


async def install_host_binaries(
    host_versions: dict[Host, str],
    destination: Path,
    *,
    platform: SandboxPlatform = "linux-x64",
) -> HostBinaryAttestation:
    destination.mkdir(parents=True, exist_ok=True)
    records: list[HostBinaryRecord] = []
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
            HostBinaryRecord(
                host=host,
                version=version,
                platform=platform,
                sha256=hashlib.sha256(binary).hexdigest(),
                size_bytes=len(binary),
            )
        )
    return HostBinaryAttestation(records=tuple(records))


def install_and_attest(
    host_versions: dict[Host, str],
    destination: Path,
    output: Path,
) -> HostBinaryAttestation:
    if output.exists() or output.is_symlink():
        raise ValueError("host binary attestation output already exists")
    attestation = asyncio.run(install_host_binaries(host_versions, destination))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json(attestation.model_dump(mode="json")) + b"\n")
    return attestation


def _binary_source(host: Host) -> Any:
    if host == "codex":
        from inspect_swe._codex_cli.agentbinary import codex_cli_binary_source

        return codex_cli_binary_source()
    from inspect_swe._claude_code.agentbinary import claude_code_binary_source

    return claude_code_binary_source()


async def _download_binary(
    source: Any,
    version: str,
    platform: SandboxPlatform,
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
    print(canonical_json(attestation.model_dump(mode="json")).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
