# ehub-meeting-bot

Container sidecar usato dal modulo Store `conference_transcriber` di
E-HUB per entrare in una call, registrare l'audio in mp3 e uscire.

Non e' un modulo del hub, non ha UI, non ha DB. E' solo l'"utensile"
che il modulo Store lancia via `docker.sock` al momento del meeting.

## Uso

Il modulo del hub spawna il container piu' o meno cosi':

```bash
docker run --rm \
  -v /app/chroma_data/transcriber_audio:/shared \
  ghcr.io/eliaferrarii/ehub-meeting-bot:latest \
  --link "https://meet.google.com/xxx-yyyy-zzz" \
  --out /shared/audio.mp3 \
  --duration 3600 \
  --name "Hub Trascrizioni" \
  --platform auto
```

- `--link` URL del meeting.
- `--out` path del mp3 dentro `/shared` (che il hub monta come volume
  condiviso per prendere il file a fine call).
- `--duration` timeout hard in secondi (default 3600).
- `--name` nome guest mostrato in call (default "Hub Trascrizioni").
- `--platform` `auto` | `meet` | `zoom` | `teams`.

Exit code 0 = mp3 prodotto. Non zero = errore, il modulo del hub segna
lo stato del meeting come `failed`.

## Piattaforme supportate

- **Google Meet** — guest join implementato in v1.3.0 (pilot).
- **Zoom / Teams / x-bees / Wildix** — stub, join da implementare in
  v1.3.x.

## Build

CI su push a `main` builda e pubblica
`ghcr.io/eliaferrarii/ehub-meeting-bot:latest`.

Build locale:

```bash
docker build -t ehub-meeting-bot:local .
docker run --rm -v $(pwd)/out:/shared ehub-meeting-bot:local \
  --link "https://meet.google.com/abc-defg-hij" \
  --out /shared/test.mp3 \
  --duration 60
```

## Architettura

Xvfb + PulseAudio + Chromium (via Playwright) + ffmpeg:

1. `Xvfb :99` — display virtuale per rendere il DOM (Meet rifiuta
   `--headless` puro).
2. `pulseaudio` con `module-null-sink sink_name=hub_capture` come sink
   di default. Chromium riproduce il suo audio la'.
3. `chromium` via Playwright entra, disabilita mic/cam, compila nome
   guest, clicca "Partecipa".
4. `ffmpeg -f pulse -i hub_capture.monitor` cattura il flusso audio e
   scrive `/shared/<out>.mp3`.
5. A `--duration` scaduta (o su tab-close del server) il processo esce,
   il container termina, il hub raccoglie il file.
