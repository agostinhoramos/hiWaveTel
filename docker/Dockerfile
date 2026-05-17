FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        build-essential pkg-config \
        libpq-dev \
        dbus libdbus-1-3 libglib2.0-0 \
        modemmanager \
        udev \
        dumb-init curl ca-certificates \
        tzdata \
        usbutils iproute2 kmod \
    && dbus-uuidgen --ensure \
    && command -v mmcli >/dev/null \
    && test -x /lib/systemd/systemd-udevd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/venv

RUN python3 -m venv /opt/venv \
    && pip install --upgrade pip setuptools wheel

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -c "import rest_framework_simplejwt" \
    && python -c "from zoneinfo import ZoneInfo; ZoneInfo('UTC')"

WORKDIR /app
COPY . /app/

RUN cd /app && python manage.py collectstatic --noinput

COPY docker/entrypoint.sh /docker/entrypoint.sh
RUN chmod +x /docker/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/docker/entrypoint.sh"]
