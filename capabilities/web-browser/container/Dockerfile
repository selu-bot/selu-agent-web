# ---- Stage 1: Build gRPC stubs from proto ----
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

# ---- Stage 2: Runtime with Playwright + Firefox ----
# No platform constraint — runs natively on both amd64 and arm64.
# Firefox is available on all Playwright-supported architectures and avoids
# the bot-detection issues that plague headless Chromium.
FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# Install Python dependencies (playwright is already in the base image,
# but we pin our version via requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Firefox with system-level dependencies (fonts, libs).
RUN playwright install --with-deps firefox

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
