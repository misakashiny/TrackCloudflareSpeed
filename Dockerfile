FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends dnsmasq-base && rm -rf /var/lib/apt/lists/*
COPY tools/ tools/
COPY build/ build/
RUN python tools/fetch_cfst.py /usr/local/bin/cfst && rm -rf build
COPY *.py /app/
COPY web/ /app/web/
COPY config/ /app/config/
COPY docs/CloudflareSpeedTest-LICENSE /usr/share/doc/cfst/LICENSE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPTIMIZER_ROOT=/service
USER 1000:1000
ENTRYPOINT ["python", "/app/entry.py"]
CMD ["worker"]
