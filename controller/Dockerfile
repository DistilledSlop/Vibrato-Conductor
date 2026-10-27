FROM docker:29-cli AS docker
FROM python:3.12-slim
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
RUN python3 - <<'PY'
import hashlib, urllib.request
from pathlib import Path
url = 'https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl'
data = urllib.request.urlopen(url, timeout=120).read()
expected = urllib.request.urlopen(url + '.sha256', timeout=30).read().decode().strip()
assert hashlib.sha256(data).hexdigest() == expected
path = Path('/usr/local/bin/kubectl'); path.write_bytes(data); path.chmod(0o755)
PY
RUN (getent passwd 1000 >/dev/null || useradd -m -u 1000 conductor) \
    && mkdir /data /artifacts /dataset && chown 1000:1000 /data /artifacts /dataset
WORKDIR /opt
COPY conductor/ /opt/conductor/
COPY catalogs/ /opt/catalogs/
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["python3", "-m", "conductor", "serve"]
CMD ["--config", "/config/fleet.json", "--host", "0.0.0.0", "--port", "8080", "--token-file", "/secrets/token"]
