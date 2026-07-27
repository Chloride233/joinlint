FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/joinlint/src:/workspace/joinlint

WORKDIR /workspace/joinlint
COPY . .
RUN python -m pip install --no-cache-dir '.[eval,remote]'
RUN python -m benchmarks.formal_eval.host_binaries install \
    --codex-version 0.144.1 \
    --claude-version 2.1.212 \
    --destination /usr/local/bin \
    --output /usr/local/share/joinlint/host-binaries.json
