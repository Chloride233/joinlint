FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/joinlint/src:/workspace/joinlint

WORKDIR /opt/joinlint-host
RUN python -m pip install --no-cache-dir inspect-swe==0.2.66
COPY benchmarks/formal_eval/host_binaries.py ./host_binaries.py
RUN python host_binaries.py install \
    --codex-version 0.144.1 \
    --claude-version 2.1.212 \
    --destination /usr/local/bin \
    --output /usr/local/share/joinlint/host-binaries.json

WORKDIR /workspace/joinlint
COPY . .
RUN python -m pip install --no-cache-dir '.[eval,remote]'
