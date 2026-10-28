FROM node:22-bookworm-slim AS node
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless python3 python3-venv ca-certificates xvfb \
    libgl1-mesa-dri libglx-mesa0 libgl1 libxrender1 libxtst6 libxi6 libxrandr2 \
    libxcursor1 libxinerama1 libasound2t64 libopenal1 libfreetype6 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && python3 -m venv /opt/python \
    && /opt/python/bin/pip install --no-cache-dir minecraft-launcher-lib==8.0
ENV PATH="/opt/python/bin:${PATH}" LIBGL_ALWAYS_SOFTWARE=1 DISPLAY=:99 \
    VIBRATO_FLEET_HOME=/opt/game
COPY proxy/package*.json /opt/proxy/
RUN cd /opt/proxy && npm ci --omit=dev
COPY vibrato/gradle.properties /opt/vibrato/gradle.properties
COPY vibrato/scripts/fleet/launch.py /opt/vibrato/scripts/fleet/launch.py
COPY install.py preextract_server_bundle.py /opt/
RUN python3 -u /opt/install.py && python3 /opt/preextract_server_bundle.py
COPY vibrato/build/libs/ /opt/game/mods/
COPY vibrato/build/server-mods/ /opt/server/mods/
RUN python3 /opt/install.py --finalize-mods
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN (getent passwd 1000 >/dev/null || useradd -m -u 1000 worker) \
    && mkdir /work /artifacts && chown 1000:1000 /work /artifacts
COPY proxy/ /opt/proxy/
COPY conductor/ /opt/conductor/
COPY build-info.json /opt/build-info.json
USER 1000:1000
WORKDIR /work
HEALTHCHECK --interval=15s --timeout=3s --start-period=240s --retries=3 \
    CMD ["python3", "-m", "conductor.worker", "--healthcheck"]
ENV PYTHONPATH=/opt
ENTRYPOINT ["python3", "-m", "conductor.worker"]
