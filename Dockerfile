FROM python:3.12-slim

# Build as root, run as an unprivileged user with /work as the working
# directory so SQLite files, JSON reports and checkpoints land on a volume.
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,mysql]"

RUN useradd --create-home --uid 1000 reconciler
USER reconciler
WORKDIR /work
VOLUME ["/work"]

ENTRYPOINT ["reconciler"]
CMD ["--help"]
