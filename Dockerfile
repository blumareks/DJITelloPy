# Requires: docker login registry.redhat.io  (free Red Hat account)
# Multi-arch image, so the arm64 variant is pulled automatically on Raspberry Pi 5.
FROM registry.redhat.io/ubi9/python-312-minimal:latest

USER 1001
WORKDIR /opt/app-root/src

# Only prebuilt wheels (numpy, av, pillow all publish manylinux aarch64 wheels).
# --only-binary makes pip fail fast instead of trying to compile from source.
COPY --chown=1001:0 requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# --no-deps: setup.py also lists opencv-python, which the library itself never imports.
COPY --chown=1001:0 setup.py README.md ./
COPY --chown=1001:0 djitellopy ./djitellopy
RUN pip install --no-cache-dir --no-deps .

COPY --chown=1001:0 examples ./examples

CMD ["python", "examples/simple.py"]
