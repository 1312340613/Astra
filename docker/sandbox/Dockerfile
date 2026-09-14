FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace \
    PYRIGHT_PYTHON_GLOBAL_NODE=0 \
    PYRIGHT_PYTHON_NODEJS_WHEEL=1

# Keep the runtime image useful for Linux-oriented Minimal tasks while keeping
# all network access disabled when Astra starts a container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY docker/sandbox/requirements.txt /tmp/astra-sandbox-requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r /tmp/astra-sandbox-requirements.txt \
    && rm -f /tmp/astra-sandbox-requirements.txt

WORKDIR /workspace
