"""Entrypoint del sidecar `ehub-meeting-bot`.

Riceve il link di un meeting via CLI, lo apre in Chromium dentro Xvfb
(non-headless perche' molti siti WebRTC rifiutano il flag `--headless`),
entra come guest, cattura l'audio riprodotto dalla tab via PulseAudio
null-sink + ffmpeg, e scrive un mp3 nel path indicato da `--out`.

Il modulo `conference_transcriber` del hub spawna questo container via
`docker.sock`, aspetta la sua terminazione e prende l'mp3 per la
trascrizione. L'output va nella dir del volume del hub, cosi' il file e'
subito visibile senza mount separati.

CLI:
    python join_meeting.py \\
        --link <url_meeting> \\
        --out <path_mp3> \\
        --duration 3600 \\
        --name "Hub Trascrizioni" \\
        --platform auto

Piattaforme supportate (join automatico):
- Google Meet
- Microsoft Teams
- Wildix Collaboration 7 / x-bees (soggetto a Cloudflare, non affidabile
  senza sessione persistente autenticata: v1.3.x)

Piattaforme in stub (accodate ma bot non entra ancora):
- Zoom
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


def _first_visible_input(page, selector: str, timeout_ms: int = 10_000):
    """Ritorna il primo input visibile che matcha `selector`. Utile quando
    il DOM contiene input nascosti (form login) accanto a quelli attivi."""
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        # `loc.count()` puo' esplodere con "Execution context was destroyed"
        # se la pagina sta facendo una redirect JS proprio mentre lo
        # chiamiamo (Teams lo fa dopo il landing). Trattiamolo come "riprova
        # al prossimo tick" invece di far cadere l'intero join.
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception:
            time.sleep(0.3)
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
        time.sleep(0.3)
    raise TimeoutError(f"nessun elemento visibile per {selector} entro {timeout_ms}ms")


def _click_first_visible_button(page, texts, timeout_ms: int = 10_000) -> bool:
    """Cerca fra i button un testo tra quelli forniti, cliccando il PRIMO
    visibile. Torna True se ha cliccato, False in caso di timeout.

    Wildix Collaboration 7 tiene nel DOM anche i bottoni di flow paralleli
    (login email, password, ecc.) come primi hit del selettore: se usiamo
    `.first`, prendiamo quelli nascosti e falliamo su actionability."""
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        context_lost = False
        for txt in texts:
            # Vedi commento in _first_visible_input: la .count() e'
            # sensibile alle navigation in corso.
            try:
                loc = page.locator(f"button:has-text('{txt}')")
                count = loc.count()
            except Exception:
                context_lost = True
                break
            for i in range(count):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        el.click()
                        return True
                except Exception:
                    continue
        if context_lost:
            time.sleep(0.5)
            continue
        time.sleep(0.3)
    return False


# Directory dove salvare gli screenshot di debug. Impostata in main()
# a partire dal path dell'output mp3, cosi' segue il volume mount del
# chiamante invece di essere hardcoded a /shared.
_SCREENSHOTS_DIR: Optional[Path] = None


def _shot(page, name: str) -> None:
    """Salva uno screenshot di debug accanto al file audio di output.
    Silenzioso in caso di errore (Xvfb potrebbe non essere pronto)."""
    if _SCREENSHOTS_DIR is None:
        return
    try:
        _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(_SCREENSHOTS_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass


def join_wildix(link: str, guest_name: str, max_seconds: int) -> None:
    """Wildix Collaboration 7 / x-bees guest join via Playwright.

    Flusso confermato dal cliente (2026-07-18):
      1. Landing di `app.wildix.com/inbox/<uuid>?ref=calendar&join` (o
         `*.x-bees.com/...`) mostra opzioni login + un bottone "Entra
         come ospite" (o equivalente in inglese).
      2. Dopo aver cliccato "ospite" appare un input per il nome guest.
      3. Bottone finale "Collegati alla riunione" / "Join meeting" fa
         entrare in call.

    Non serve login: Wildix identifica la stanza dall'UUID nella URL.
    """
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            permissions=[],
            locale="it-IT",
            timezone_id="Europe/Rome",
        )
        page = ctx.new_page()
        log.info("navigazione a %s", link)
        # `load` aspetta l'onload event, non solo il DOM: Wildix e' MUI/React,
        # con `domcontentloaded` i bottoni non sono ancora stati montati.
        page.goto(link, wait_until="load", timeout=45_000)

        _shot(page, "01_landing")

        # 1) Bottone "Continua come ospite" / "Entra come ospite" / varianti.
        # Wildix Collaboration 7 lo mostra dopo i 3 bottoni "Accedi con
        # Google/Microsoft/email". Usiamo l'helper che aspetta il primo
        # visibile, cosi' evitiamo di partire mentre React sta ancora
        # montando e di prendere per errore un bottone nascosto.
        guest_ok = _click_first_visible_button(
            page,
            [
                "Continua come ospite", "Entra come ospite",
                "Continue as guest", "Join as guest",
                "Ospite", "Guest",
            ],
            timeout_ms=20_000,
        )
        if guest_ok:
            log.info("click guest OK")
        else:
            log.warning("bottone 'ospite' non trovato in 20s")
        time.sleep(2)
        _shot(page, "02_after_guest")

        # 2) Dopo il click 'ospite' Wildix mostra un form con un input
        # testuale e un bottone submit 'Prossimo'. Compiliamo il campo
        # con .type() (simula tasti reali che triggerano input+keydown+keyup
        # richiesti dalla validazione React di MUI). .fill() a volte non
        # attiva il submit su form MUI/React.
        try:
            inp = _first_visible_input(page, "input[type='text']", timeout_ms=15_000)
            inp.click()  # focus reale
            inp.type(guest_name, delay=30)
            log.info("nome guest inserito: %s", guest_name)
        except Exception as exc:
            log.warning("input nome non trovato: %s", exc)
        _shot(page, "03_after_name")

        # 3) Click 'Prossimo'. Il selettore matcha 3 elementi, il primo del
        # DOM e' un bottone nascosto del flow email/password. Serve il
        # primo VISIBILE.
        if not _click_first_visible_button(
            page, ["Prossimo", "Next"], timeout_ms=10_000,
        ):
            log.warning("bottone 'Prossimo' non trovato visibile in 10s")

        # 4) Dopo 'Prossimo' Wildix presenta lo step "Collegati alla riunione":
        # avatar, dettagli meeting, "Configurazione dispositivi in corso..."
        # che diventa "I tuoi dispositivi funzionano correttamente", e il
        # bottone BLU "Collegati alla riunione". Serve tempo per il setup
        # dispositivi via WebRTC (senza `--use-fake-ui-for-media-stream`
        # Chrome mostrerebbe un prompt permessi mic). Aspettiamo ~10s.
        time.sleep(8)
        _shot(page, "04_after_prossimo")

        # 5) Click sul bottone finale "Collegati alla riunione" (o
        # equivalenti in EN). Ordine: prima "Collegati alla riunione"
        # esatto, poi variazioni. Escluso "Prossimo" (che a questo punto
        # non deve piu' matchare).
        final_ok = _click_first_visible_button(
            page,
            [
                "Collegati alla riunione", "Collegati",
                "Join meeting", "Join the meeting", "Join now",
                "Entra ora", "Entra nella riunione", "Entra",
                "Partecipa alla riunione", "Partecipa",
            ],
            timeout_ms=15_000,
        )
        if final_ok:
            log.info("click 'Collegati alla riunione' OK")
        else:
            log.warning("bottone finale 'Collegati alla riunione' non trovato in 15s")
        time.sleep(3)
        _shot(page, "05_after_final_click")

        # In call: aspettiamo max_seconds o chiusura tab (kick host)
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


def join_teams(link: str, guest_name: str, max_seconds: int) -> None:
    """Microsoft Teams guest join via Playwright.

    Flusso (v1.3, browser web):
      1. Landing "Come vuoi partecipare?" con 3 opzioni: "Scarica app" /
         "Apri app Teams" / "Continua su questo browser". Clicchiamo la
         terza.
      2. Pre-join: input nome guest + toggle mic/cam + bottone
         "Unisciti ora" / "Join now".
      3. In call. Se la riunione ha waiting room / lobby, restiamo in
         attesa dell'admit.

    Teams non ha Cloudflare Turnstile sui guest link, e i selettori sono
    piu' stabili di Wildix. Ma la UI ha 3-4 varianti che coesistono per
    A/B testing: matchiamo con testi in IT e EN in cascata."""
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            locale="it-IT",
            timezone_id="Europe/Rome",
            permissions=["microphone", "camera"],
        )
        page = ctx.new_page()
        log.info("navigazione a %s", link)
        page.goto(link, wait_until="load", timeout=45_000)
        # Il landing di Teams fa 1-2 redirect JS (a `teams.microsoft.com/dl/launcher`
        # e poi al pre-join). Se iniziamo a cliccare prima che la catena
        # finisca, `locator.count()` esplode con "Execution context was
        # destroyed". `networkidle` non e' garantito su Teams, quindi
        # best-effort con timeout corto: se scade partiamo comunque, gli
        # helper `_click_first_visible_button` ora sanno riprovare.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            log.info("networkidle non raggiunto in 10s, proseguo comunque")
        _shot(page, "teams_01_landing")

        # 1) Landing puo' mostrare vari CTA. Prova nell'ordine:
        # "Continua su questo browser" e' quello che vogliamo.
        # Se compare direttamente la pre-join, questo step non fa nulla
        # e ce ne accorgiamo al step 2.
        _click_first_visible_button(
            page,
            [
                "Continua su questo browser",
                "Continue on this browser",
                "Guarda su Web",
                "Watch on web",
                "Partecipa sul Web",
                "Join on the web instead",
            ],
            timeout_ms=15_000,
        )
        time.sleep(3)
        _shot(page, "teams_02_after_browser")

        # 2) Campo nome guest. Teams lo mostra come `<input type="text">`
        # (a volte con placeholder "Type your name" o "Digita il tuo nome").
        try:
            inp = _first_visible_input(page, "input[type='text']", timeout_ms=15_000)
            inp.click()
            inp.type(guest_name, delay=30)
            log.info("nome inserito: %s", guest_name)
        except Exception as exc:
            log.warning("input nome non trovato: %s", exc)
        _shot(page, "teams_03_after_name")

        # 3) Bottone finale "Unisciti ora" / "Join now"
        if _click_first_visible_button(
            page,
            [
                "Unisciti ora", "Unisciti alla riunione", "Unisciti",
                "Partecipa ora", "Partecipa alla riunione", "Partecipa",
                "Entra ora", "Entra",
                "Join now", "Join meeting", "Join the meeting", "Join",
            ],
            timeout_ms=15_000,
        ):
            log.info("click 'Unisciti ora' OK")
        else:
            log.warning("bottone 'Unisciti' non trovato in 15s")
        time.sleep(3)
        _shot(page, "teams_04_after_join")

        # In call: se lobby, resta in attesa dell'admit fino al timeout.
        log.info("in call — resto per max %ds", max_seconds)
        try:
            page.wait_for_event("close", timeout=max_seconds * 1000)
            log.info("tab chiusa dal server/kick")
        except Exception:
            log.info("timeout durata max: esco")
        finally:
            try:
                ctx.close()
                browser.close()
            except Exception:
                pass


def join_stub(link: str, platform: str, guest_name: str, max_seconds: int) -> None:
    """Stub per Zoom/Teams: log e sleep. Le implementazioni arriveranno
    in v1.3.x. Per ora il sidecar esiste e cattura audio zero, cosi' il
    hub puo' validare il flusso di spawn/attesa/pickup."""
    log.warning("piattaforma %s: join non implementato ancora, resto %ds passivo", platform, max_seconds)
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
    ap.add_argument("--platform", default="auto",
                    choices=["auto", "meet", "wildix", "xbees", "zoom", "teams"],
                    help="Piattaforma (auto = detect dal dominio del link)")
    args = ap.parse_args()

    global _SCREENSHOTS_DIR
    _SCREENSHOTS_DIR = Path(args.out).parent / "screenshots"

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
        elif platform == "teams":
            join_teams(args.link, args.name, args.duration)
        elif platform in ("wildix", "xbees"):
            join_wildix(args.link, args.name, args.duration)
        elif platform in ("zoom", "unknown"):
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
