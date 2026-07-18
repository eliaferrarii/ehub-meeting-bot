"""Entrypoint del sidecar `ehub-meeting-bot`.

Riceve il link di un meeting via CLI, lo apre in Chromium headless
(dentro Xvfb per far vedere il DOM ai siti che rifiutano headless puro),
entra come guest con un nome custom, cattura l'audio riprodotto dalla
tab tramite PulseAudio null-sink e ffmpeg, e ritorna l'mp3 nella
directory `/shared`.

Il modulo `conference_transcriber` del hub spawna questo container via
`docker.sock`, aspetta la sua terminazione e prende `/shared/audio.mp3`
per la trascrizione.

CLI:
    python join_meeting.py \\
        --link <url_meeting> \\
        --out /shared/audio.mp3 \\
        --duration 3600 \\
        --name "Hub Trascrizioni" \\
        --platform auto

--platform accetta: auto | meet | zoom | teams. v1.3.0: gestisce Google
Meet come pilot. Zoom/Teams: entry stub, gestione completa in v1.3.x.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("meeting-bot")


DISPLAY = ":99"
SINK_NAME = "hub_capture"


def detect_platform(link: str) -> str:
    """Deduce la piattaforma dal dominio del link. `auto` -> best guess."""
    host = (urlparse(link).netloc or "").lower()
    if "meet.google.com" in host:
        return "meet"
    if "zoom.us" in host or "zoom.com" in host:
        return "zoom"
    if "teams.microsoft.com" in host or "teams.live.com" in host:
        return "teams"
    if "x-bees.com" in host:
        return "xbees"
    if "wildix.com" in host:
        return "wildix"
    return "unknown"


def start_xvfb() -> subprocess.Popen:
    log.info("avvio Xvfb su %s", DISPLAY)
    proc = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    os.environ["DISPLAY"] = DISPLAY
    return proc


def start_pulseaudio() -> subprocess.Popen:
    """Avvia PulseAudio in user-mode con un null-sink dedicato per la
    cattura. Chromium instrada il suo audio a quel sink, ffmpeg legge dal
    `.monitor`."""
    log.info("avvio PulseAudio con null-sink %s", SINK_NAME)
    # Config minimale scritta a runtime per avere solo cio' che serve.
    conf_dir = Path("/tmp/pulse")
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf = conf_dir / "default.pa"
    conf.write_text(
        "load-module module-native-protocol-unix\n"
        f"load-module module-null-sink sink_name={SINK_NAME} "
        f"sink_properties=device.description={SINK_NAME}\n"
        f"set-default-sink {SINK_NAME}\n"
    )
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = "/tmp"
    proc = subprocess.Popen(
        ["pulseaudio", "--exit-idle-time=-1", "--file", str(conf)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    return proc


def start_ffmpeg(out_path: Path, max_seconds: int) -> subprocess.Popen:
    """Registra dal monitor del null-sink in mp3. Durata limite = max_seconds
    (hard timeout): il modulo del hub calcola la durata attesa dall'iCal +
    un margine e la passa qui."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("avvio ffmpeg -> %s (max %ds)", out_path, max_seconds)
    return subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "pulse", "-i", f"{SINK_NAME}.monitor",
            "-ac", "1", "-ar", "16000",
            "-codec:a", "libmp3lame", "-qscale:a", "5",
            "-t", str(max_seconds),
            str(out_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def join_meet(link: str, guest_name: str, max_seconds: int) -> None:
    """Google Meet guest join via Playwright. Compila il campo nome,
    disabilita mic/camera (siamo in ascolto) e clicca Join/Ask to join."""
    # Import qui: teniamo il costo di import fuori dal path di failure
    # veloce (chromium missing, ecc.)
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # servono le fake device di Chrome per l'audio
            args=[
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            permissions=[],  # niente mic/cam
            locale="it-IT",
            timezone_id="Europe/Rome",
        )
        page = ctx.new_page()
        log.info("navigazione a %s", link)
        page.goto(link, wait_until="domcontentloaded", timeout=45_000)

        # Meet ha 2 varianti di landing: nickname prompt o direct pre-join.
        # Tentiamo di riempire il nome se lo chiede.
        try:
            name_input = page.locator("input[aria-label*='Nome' i], input[placeholder*='name' i]").first
            name_input.wait_for(state="visible", timeout=8_000)
            name_input.fill(guest_name)
        except Exception:
            log.info("nessun campo nome (utente non-anonimo o UI cambiata)")

        # Disabilita mic/camera se sono ON di default: cerca bottoni con
        # aria-label italiano o inglese.
        for label_pat in ("Disattiva microfono", "Turn off microphone",
                          "Disattiva videocamera", "Turn off camera"):
            try:
                btn = page.locator(f"button[aria-label*='{label_pat}' i]").first
                if btn.is_visible(timeout=2_000):
                    btn.click()
            except Exception:
                pass

        # Bottone di join (label variabili in base al ruolo)
        join_ok = False
        for label_pat in ("Partecipa", "Join", "Chiedi di partecipare",
                          "Ask to join"):
            try:
                btn = page.locator(f"button:has-text('{label_pat}')").first
                if btn.is_visible(timeout=4_000):
                    log.info("click join: %s", label_pat)
                    btn.click()
                    join_ok = True
                    break
            except Exception:
                continue

        if not join_ok:
            log.error("bottone Join non trovato: uscita anticipata")
            return

        # In call: attendiamo max_seconds oppure che la tab si chiuda
        # (host kick). Restiamo passivi.
        log.info("in call — resto per max %ds", max_seconds)
        try:
            page.wait_for_event("close", timeout=max_seconds * 1000)
            log.info("tab chiusa dal server/kick")
        except Exception:
            log.info("timeout durata max raggiunto: esco")
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass


def join_stub(link: str, platform: str, guest_name: str, max_seconds: int) -> None:
    """Stub per Zoom/Teams: log e sleep. v1.3.x implementera' il join
    completo. Per ora il sidecar esiste e cattura audio zero, cosi' il hub
    puo' validare il flusso di spawn/attesa/pickup."""
    log.warning("piattaforma %s: join non implementato in v1.3.0, resto %ds passivo", platform, max_seconds)
    time.sleep(min(max_seconds, 30))


def cleanup(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=5)
            except Exception:
                p.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sidecar meeting-bot per E-HUB")
    ap.add_argument("--link", required=True, help="URL del meeting")
    ap.add_argument("--out", default="/shared/audio.mp3", help="Path mp3 di output")
    ap.add_argument("--duration", type=int, default=3600, help="Timeout hard in secondi (default 1h)")
    ap.add_argument("--name", default="Hub Trascrizioni", help="Nome guest mostrato in call")
    ap.add_argument("--platform", default="auto", choices=["auto", "meet", "zoom", "teams"], help="Piattaforma (auto = detect)")
    args = ap.parse_args()

    platform = args.platform
    if platform == "auto":
        platform = detect_platform(args.link)
    log.info("meeting: platform=%s guest=%s duration=%ds out=%s",
             platform, args.name, args.duration, args.out)

    xvfb = pulse = ffmpeg = None
    exit_code = 0
    try:
        xvfb = start_xvfb()
        pulse = start_pulseaudio()
        ffmpeg = start_ffmpeg(Path(args.out), args.duration + 60)

        if platform == "meet":
            join_meet(args.link, args.name, args.duration)
        elif platform in ("zoom", "teams", "xbees", "wildix", "unknown"):
            join_stub(args.link, platform, args.name, args.duration)
        else:
            log.error("piattaforma sconosciuta: %s", platform)
            exit_code = 2
    except Exception:
        log.exception("errore fatale nel join")
        exit_code = 1
    finally:
        # Chiudo ffmpeg per prima (flush mp3), poi pulse, poi xvfb.
        cleanup([ffmpeg, pulse, xvfb])
        # Stampo dimensione file per debug lato hub
        out = Path(args.out)
        if out.exists():
            log.info("audio prodotto: %s (%d byte)", out, out.stat().st_size)
        else:
            log.warning("nessun audio prodotto")
            exit_code = exit_code or 3

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
