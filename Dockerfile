# ehub-meeting-bot
#
# Container sidecar spawnato dal modulo Store `conference_transcriber` del hub
# E-HUB. Riceve link di un meeting (Meet/Zoom/Teams), entra come guest,
# registra l'audio in mp3 e termina. L'mp3 finisce in un volume shared col
# modulo, che lo passa alla queue STT standard.
#
# Zero feature del hub qui dentro: quest'immagine e' isolata, sostituibile,
# aggiornabile senza toccare il hub.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Pacchetti di sistema: Xvfb (display virtuale), PulseAudio (audio server +
# null-sink per la cattura), ffmpeg (encoding mp3), dep runtime di chromium
# gestite da playwright con `--with-deps`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
        ca-certificates \
        procps \
        dbus-x11 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Playwright + chromium
RUN pip install --no-cache-dir playwright==1.49.0 \
    && playwright install --with-deps chromium

WORKDIR /app
COPY app/ /app/

# La directory dove viene scritto l'audio catturato. Il modulo del hub la
# monta come volume shared per prendere l'mp3 a fine call.
RUN mkdir -p /shared
VOLUME ["/shared"]

# Nessuna porta esposta: comunicazione col hub via volume + exit code.
ENTRYPOINT ["python", "/app/join_meeting.py"]
