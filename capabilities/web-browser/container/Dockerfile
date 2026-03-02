# ---- Stage 1: Build gRPC stubs from proto ----
FROM --platform=linux/amd64 python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

# ---- Stage 2: Runtime with Playwright + real Google Chrome ----
# Force amd64: Google Chrome does not ship for Linux ARM64.
# On ARM hosts (e.g. Apple Silicon) this runs under Rosetta/QEMU emulation.
FROM --platform=linux/amd64 mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install Python dependencies (playwright is already in the base image,
# but we pin our version via requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install real Google Chrome (not the bundled Chromium).
# Real Chrome passes more anti-bot fingerprint checks than Playwright's
# bundled Chromium, which is detected and blocked by services like
# Akamai Bot Manager (e.g. disneylandparis.de).
# --with-deps ensures system-level dependencies (fonts, libs) are installed.
RUN playwright install --with-deps chrome

# Copy generated gRPC stubs from builder
COPY --from=builder /build/capability_pb2.py /build/capability_pb2_grpc.py ./

# Copy application code
COPY server.py .

# Create a non-root user matching Selu's expectations (UID 1000)
# The Playwright base image may already have a user with UID 1000;
# if so we just use it.
RUN id -u 1000 2>/dev/null || useradd --uid 1000 --create-home selu

# gRPC port
EXPOSE 50051

USER 1000:1000

CMD ["python", "server.py"]
