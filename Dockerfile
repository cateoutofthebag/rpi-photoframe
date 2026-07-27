# A test rig for the photo frame, for use before (or alongside) the Pi.
#
# There's no framebuffer in a container, so this runs the display loop against
# SDL's dummy driver: the slideshow, transitions, overlay and quiet hours all
# behave exactly as they do on the Pi, and you watch it at /mirror instead of
# on an HDMI panel.
#
# Python 3.11 to match Raspberry Pi OS Bookworm. Every dependency has a
# prebuilt aarch64 wheel, so this builds in about a minute on Apple silicon or
# an arm64 host and needs no compiler.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PHOTOFRAME_DATA=/data \
    PHOTOFRAME_PORT=8080 \
    TZ=Europe/London

# DejaVu is what Raspberry Pi OS ships and what the overlay looks for first,
# so the clock and weather render here exactly as they will on the Pi.
# libheif is for HEIC photos straight off an iPhone.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        libheif1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so edits to the app don't invalidate this layer.
# The optional extras are commented out in requirements.txt; in a container
# they cost nothing, so take both.
COPY requirements.txt ./
RUN pip install -r requirements.txt smbprotocol pillow-heif

COPY photoframe/ ./photoframe/
COPY run.py ./

# Runs unprivileged. /data is a volume, so its ownership is set at build time
# and Docker carries it over when the volume is first created.
RUN useradd --create-home --uid 1000 frame \
    && mkdir -p /data \
    && chown -R frame:frame /data /app
USER frame
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/status', timeout=4)"

CMD ["python", "run.py", "--headless"]
