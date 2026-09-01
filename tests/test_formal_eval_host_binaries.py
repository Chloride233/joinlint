from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.formal_eval import host_binaries
from benchmarks.formal_eval.pilot import frozen_pilot_registration


def test_image_install_attests_exact_host_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {"codex": b"codex-binary", "claude_code": b"claude-binary"}
    sources: dict[str, SimpleNamespace] = {}

    def source(host: str) -> SimpleNamespace:
        value = SimpleNamespace(binary="codex" if host == "codex" else "claude")
        value.host = host
        sources[host] = value
        return value

    async def download(
        binary_source: SimpleNamespace,
        version: str,
        platform: str,
    ) -> tuple[bytes, str]:
        del platform
        payload = payloads[binary_source.host]
        return payload, version

    monkeypatch.setattr(host_binaries, "_binary_source", source)
    monkeypatch.setattr(host_binaries, "_download_binary", download)

    destination = tmp_path / "bin"
    attestation = asyncio.run(
        host_binaries.install_host_binaries(
            {"claude_code": "2.1.212", "codex": "0.144.1"},
            destination,
        )
    )

    records = attestation["records"]
    assert isinstance(records, list)
    assert [record["host"] for record in records] == ["claude_code", "codex"]
    assert {record["host"]: record["sha256"] for record in records} == {
        host: hashlib.sha256(payload).hexdigest() for host, payload in payloads.items()
    }
    assert set(sources) == {"codex", "claude_code"}
    assert (destination / "codex").read_bytes() == payloads["codex"]
    assert (destination / "claude").read_bytes() == payloads["claude_code"]
    assert (destination / "codex").stat().st_mode & 0o777 == 0o755


def test_image_install_rejects_resolved_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(binary="codex")

    async def download(
        binary_source: SimpleNamespace,
        version: str,
        platform: str,
    ) -> tuple[bytes, str]:
        del binary_source, version, platform
        return b"binary", "latest"

    monkeypatch.setattr(host_binaries, "_binary_source", lambda host: source)
    monkeypatch.setattr(host_binaries, "_download_binary", download)

    with pytest.raises(ValueError, match="does not match the frozen version"):
        asyncio.run(
            host_binaries.install_host_binaries(
                {"codex": "0.144.1"},
                tmp_path,
            )
        )


def test_formal_images_install_the_frozen_pilot_host_versions() -> None:
    registration = frozen_pilot_registration("a" * 40)
    dockerfiles = (
        Path("Dockerfile.formal-pilot"),
        Path("benchmarks/formal_eval/Dockerfile"),
    )

    assert dockerfiles[0].read_bytes() == dockerfiles[1].read_bytes()
    for path in dockerfiles:
        dockerfile = path.read_text(encoding="utf-8")
        assert (
            f"--codex-version {registration.host_versions['codex']}" in dockerfile
        )
        assert (
            f"--claude-version {registration.host_versions['claude_code']}" in dockerfile
        )
        assert dockerfile.index("RUN python host_binaries.py install") < dockerfile.index(
            "COPY . ."
        )
