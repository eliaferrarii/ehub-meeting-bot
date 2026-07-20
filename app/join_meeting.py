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
import threading
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

# Flag settato dal signal handler SIGTERM/SIGINT (docker stop). Il loop
# di attesa in call lo controlla ad ogni tick e esce pulito, permettendo
# a ffmpeg di flushare l'mp3 nel blocco `finally` di main().
_SHUTDOWN = threading.Event()

# Testi che indicano "sei stato rimosso / riunione terminata" e
# comportano uscita immediata dal bot (chiudere la tab non basta:
# Teams mostra una schermata statica con bottoni Partecipa/Chiudi).
REMOVED_TEXTS = (
    "Sei stato rimosso dalla riunione",
    "Sei stato rimosso",
    "You have been removed",
    "You were removed",
    "Riunione terminata",
    "La riunione è terminata",
    "Meeting has ended",
    "Meeting ended",
    "Call ended",
)

# Testi che indicano "sei l'unico partecipante" DENTRO la call. Se
# persistono per piu' di ALONE_TIMEOUT_SEC esci: l'organizzatore e'
# uscito ma Teams non ha chiuso la stanza (tipico di teams.live.com).
ALONE_TEXTS = (
    "In attesa di altri partecipanti",
    "Waiting for others to join",
    "You're the only one here",
    "Sei l'unico partecipante",
)
ALONE_TIMEOUT_SEC = 60

# Testi che indicano "sei nella sala d'attesa / lobby", cioe' hai
# cliccato Partecipa ma l'host non ti ha ancora ammesso. Se restiamo
# piu' di LOBBY_TIMEOUT_SEC senza essere ammessi, esci: nessuno ci
# lascia entrare, inutile occupare risorse.
LOBBY_TEXTS = (
    # Teams Live consumer (confermato via screenshot 2026-07-20)
    "A breve qualcuno ti farà partecipare",
    "Someone will let you in shortly",
    # Teams enterprise
    "Attendi che ti facciano entrare",
    "Ti faranno entrare a breve",
    "Waiting for the host to let you in",
    "Waiting for the meeting to start",
    "When the meeting starts, we'll let people know you're waiting",
    # Meet
    "Ti stanno aspettando",
    "In attesa di essere ammesso",
    "In attesa di essere ammessi",
)
LOBBY_TIMEOUT_SEC = 300


def _install_signal_handlers() -> None:
    """SIGTERM (docker stop) e SIGINT -> shutdown pulito.

    Senza questo, docker stop uccide python al PID 1 senza eseguire il
    blocco `finally` di main() che chiude ffmpeg: risultato = mp3 a 0
    byte (o inesistente) e trascrizione fallita. Con questo, il loop di
    attesa vede _SHUTDOWN e ritorna, arriviamo al finally, ffmpeg riceve
    SIGTERM, chiude header/tail dell'mp3 e il file e' valido."""
    def _handler(sig, _frame):
        log.info("segnale %s ricevuto, shutdown pulito", sig)
        _SHUTDOWN.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _text_present(page, text: str) -> bool:
    """True se `text` e' presente nel DOM. Usa .count() invece di
    .is_visible() per evitare i timeout impliciti di Playwright: qui
    vogliamo un check istantaneo, non un wait."""
    try:
        return page.locator(f"text={text}").count() > 0
    except Exception:
        return False


def _wait_in_call(page, max_seconds: int) -> str:
    """Loop di attesa in call. Sostituisce il vecchio
    `page.wait_for_event('close', ...)` che blocca il thread e non
    reagisce ne' a SIGTERM ne' a schermate 'rimosso' / 'lobby' /
    'solo in call'.

    Ritorna il motivo di uscita per log:
      - 'shutdown'      : SIGTERM/SIGINT ricevuto (docker stop)
      - 'tab_closed'    : tab chiusa dal server (host kick / meeting ended)
      - 'removed'       : rilevato testo 'sei stato rimosso'
      - 'lobby_timeout' : bloccato in sala d'attesa per >LOBBY_TIMEOUT_SEC
      - 'alone'         : solo in call per >ALONE_TIMEOUT_SEC
      - 'timeout'       : raggiunto max_seconds (durata iCal + buffer)
    """
    deadline = time.time() + max_seconds
    lobby_since: Optional[float] = None
    alone_since: Optional[float] = None
    while time.time() < deadline:
        if _SHUTDOWN.is_set():
            return "shutdown"
        try:
            if page.is_closed():
                return "tab_closed"
        except Exception:
            return "tab_closed"
        for txt in REMOVED_TEXTS:
            if _text_present(page, txt):
                log.info("rilevato: '%s'", txt)
                return "removed"
        in_lobby = any(_text_present(page, t) for t in LOBBY_TEXTS)
        if in_lobby:
            if lobby_since is None:
                lobby_since = time.time()
                log.info(
                    "rilevato lobby/sala d'attesa, esco se dura >%ds",
                    LOBBY_TIMEOUT_SEC,
                )
            elif time.time() - lobby_since > LOBBY_TIMEOUT_SEC:
                return "lobby_timeout"
            # In lobby non sei in call: azzera l'altro counter.
            alone_since = None
        else:
            lobby_since = None
            is_alone = any(_text_present(page, t) for t in ALONE_TEXTS)
            if is_alone:
                if alone_since is None:
                    alone_since = time.time()
                    log.info(
                        "rilevato 'solo in call', esco se dura >%ds",
                        ALONE_TIMEOUT_SEC,
                    )
                elif time.time() - alone_since > ALONE_TIMEOUT_SEC:
                    return "alone"
            else:
                alone_since = None
        time.sleep(3)
    return "timeout"


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
    un margine e la passa qui.

    stderr redirect a file: senza questo non vediamo perche' ffmpeg
    fallisce (sink inesistente, codec mancante, ecc.) e ci ritroviamo
    con 'nessun audio prodotto' senza nessuna indicazione. Il file viene
    lasciato accanto all'output mp3, cosi' segue lo stesso volume mount
    e il chiamante puo' recuperarlo insieme all'audio."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = out_path.with_suffix(out_path.suffix + ".ffmpeg.log")
    log.info("avvio ffmpeg -> %s (max %ds, stderr=%s)",
             out_path, max_seconds, stderr_path)
    stderr_fd = open(stderr_path, "wb")
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "pulse", "-i", f"{SINK_NAME}.monitor",
            "-ac", "1", "-ar", "16000",
            "-codec:a", "libmp3lame", "-qscale:a", "5",
            "-t", str(max_seconds),
            str(out_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fd,
    )
    # Attach il fd al processo per keep-alive (chiuderlo prematuramente
    # farebbe perdere gli ultimi log).
    proc._ehub_stderr_fd = stderr_fd  # type: ignore[attr-defined]
    # Sanity check: dopo 2s ffmpeg deve essere ancora vivo. Se e' morto
    # subito e' quasi sempre "no such device" sul sink pulse.
    time.sleep(2)
    if proc.poll() is not None:
        log.error(
            "ffmpeg MORTO dopo 2s exit=%s. Controlla %s",
            proc.returncode, stderr_path,
        )
        try:
            tail = stderr_path.read_bytes()[-1500:].decode("utf-8", "replace")
            log.error("ffmpeg stderr tail:\n%s", tail)
        except Exception:
            pass
    return proc


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

        # In call: usa _wait_in_call per gestire SIGTERM, 'sei stato
        # rimosso', 'solo in call' e timeout con la stessa logica delle
        # altre piattaforme.
        log.info("in call — resto per max %ds", max_seconds)
        reason = _wait_in_call(page, max_seconds)
        log.info("uscita in call: motivo=%s", reason)
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

        # In call: usa _wait_in_call condiviso.
        log.info("in call — resto per max %ds", max_seconds)
        reason = _wait_in_call(page, max_seconds)
        log.info("uscita in call: motivo=%s", reason)
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
        # NB: teams.live.com sul pre-join tiene un overlay SVG di splash
        # (`<div data-portal-node="true">` con il logo Teams #464775) che
        # copre il campo per qualche secondo e intercetta i pointer events.
        # `click() + type()` fallisce con "subtree intercepts pointer
        # events" e 60 retry inutili. `fill()` scrive direttamente sul
        # value senza click, aggira lo splash. Se serve triggerare l'evento
        # keydown (React di Teams usa keyup per abilitare "Unisciti"),
        # tentiamo un focus + type finale che non richiede pointer events.
        try:
            inp = _first_visible_input(page, "input[type='text']", timeout_ms=15_000)
            inp.fill(guest_name)
            try:
                inp.focus()
                # `press` singola tastiera per far scattare gli handler
                # onChange/onKeyUp senza riscrivere tutto il nome.
                inp.press("End")
            except Exception:
                pass
            log.info("nome inserito: %s", guest_name)
        except Exception as exc:
            log.warning("input nome non trovato: %s", exc)
        _shot(page, "teams_03_after_name")

        # 2b) Teams Live consumer, nonostante `--use-fake-ui-for-media-stream`,
        # apre una modale "Non vuoi consentire l'audio o il video?" con un
        # bottone "Continua senza audio o video" che COPRE il bottone
        # "Partecipa ora" in basso a destra. Se non la chiudiamo, il click
        # finale fallisce con "subtree intercepts pointer events" e il bot
        # resta a registrare aria per tutta la durata del meeting.
        # Il click va bene per noi: siamo un bot di ascolto, non abbiamo
        # niente da trasmettere.
        _click_first_visible_button(
            page,
            [
                "Continua senza audio o video",
                "Continue without audio or video",
            ],
            timeout_ms=5_000,
        )
        time.sleep(1)

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
        # _wait_in_call gestisce anche 'Sei stato rimosso' e 'In attesa
        # di altri partecipanti' (organizzatore uscito ma stanza aperta).
        log.info("in call — resto per max %ds", max_seconds)
        reason = _wait_in_call(page, max_seconds)
        log.info("uscita in call: motivo=%s", reason)
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

    # SIGTERM/SIGINT -> shutdown pulito (docker stop). Deve stare prima
    # di start_xvfb altrimenti un docker stop precoce salta il cleanup.
    _install_signal_handlers()

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
            # Se ffmpeg ha lasciato un log stderr, ne prendo il tail cosi'
            # il modulo del hub lo vede nelle notes del meeting invece di
            # dover fare docker logs (che non c'e' piu' col remove=True).
            stderr_path = out.with_suffix(out.suffix + ".ffmpeg.log")
            if stderr_path.exists():
                try:
                    tail = stderr_path.read_bytes()[-1500:].decode("utf-8", "replace")
                    log.warning("ffmpeg stderr tail:\n%s", tail)
                except Exception:
                    pass
            exit_code = exit_code or 3

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
