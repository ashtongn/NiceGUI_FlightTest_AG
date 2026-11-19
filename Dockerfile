FROM python:3.11

ENV DEBIAN_FRONTEND=noninteractive

# python3-tk for the engine modules that import tkinter at load
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-tk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt chains -r requirements-radio.txt -r requirements-gui.txt
COPY requirements.txt requirements-gui.txt requirements-radio.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Vendored wheels: pyshp (pure Python, the planner reads zipped shapefiles
# dropped on its map; installs everywhere) and, for the RF front end, the optional
# GPIO (gpiod) + USB HID (hidapi) wheels for the external
# PA / TX-RX switch / Mini-Circuits attenuator control. The radios build this
# image offline, so the aarch64 cp311 wheels are vendored in deploy/wheels/ and
# installed only when present (the x86 GUI image takes pyshp; the aarch64
# wheels do not install there and are skipped).
COPY deploy/wheels/ /tmp/wheels/
RUN set -e; if ls /tmp/wheels/*.whl >/dev/null 2>&1; then \
      for w in /tmp/wheels/*.whl; do pip install --no-cache-dir --no-deps "$w" || echo "skip $w (wrong arch?)"; done; \
    fi; rm -rf /tmp/wheels

# libsidekiq is not baked into the image. The radio service always runs with
# `-v /usr/lib/epiq:/usr/lib/epiq:ro`, so the radio's own tested libs are mounted
# in at runtime and resolved via LD_LIBRARY_PATH below (each radio supplies its
# own version: G40 4.25.0, X40 4.27.0). The x86 GUI never loads libsidekiq (the
# skiq bridge is optional), so it needs nothing here. Nothing to copy or ldconfig.
ENV LD_LIBRARY_PATH=/usr/lib/epiq

COPY . .
RUN mkdir -p /app/output

EXPOSE 8080
CMD ["python", "-m", "dpmg.web.nicegui_app"]
