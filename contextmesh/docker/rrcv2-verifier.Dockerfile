# syntax=docker/dockerfile:1.7
FROM --platform=linux/amd64 node:22.18.0-alpine3.22@sha256:1b2479dd35a99687d6638f5976fd235e26c5b37e8122f786fcd5fe231d63de5b AS node

FROM --platform=linux/amd64 python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844

COPY contextmesh/docker/rrcv2-verifier-requirements.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /build/requirements.txt \
    && rm -rf /build /root/.cache \
    && addgroup -S -g 65532 verifier \
    && adduser -S -D -H -u 65532 -G verifier verifier

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/lib/libstdc++.so.6 /usr/lib/libstdc++.so.6
COPY --from=node /usr/lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1

RUN test "$(python --version)" = "Python 3.13.7" \
    && test "$(ruff --version)" = "ruff 0.16.2" \
    && test "$(pytest --version | head -n 1)" = "pytest 9.1.1" \
    && test "$(node --version)" = "v22.18.0" \
    && test "$(node /usr/local/lib/python3.13/site-packages/pyright/dist/index.js --version)" = "pyright 1.1.411"

USER 65532:65532
WORKDIR /scratch
ENV HOME=/scratch/home PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
