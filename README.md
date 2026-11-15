# MemoDrop

Nimmt Audiodateien per Webhook entgegen, transkribiert sie mit (Faster-)Whisper und zeigt sie als Notizliste an. Die neuesten Notizen stehen oben.

```
anderes Programm ──POST audio──▶ MemoDrop (app) ──▶ whisper (faster_whisper)
                                    │
                                    ├─ data/memodrop.db   (Text, Status, Papierkorb)
                                    ├─ data/audio/        (Original-Audiodateien)
                                    └─ Web-UI  http://localhost:8080
```

## Starten

```bash
cp .env.example .env      # optional, Werte anpassen
docker compose up -d --build
```

Beim ersten Start lädt der Whisper-Container das Modell herunter (bei `small` ca. 500 MB). Bis dahin bleiben Notizen auf „In Warteschlange“ und werden danach automatisch verarbeitet.

## Webhook

`POST http://<host>:8080/api/webhook`

Akzeptiert werden:

- **multipart/form-data** mit einem beliebigen Dateifeld:
  ```bash
  curl -F "file=@memo.m4a" http://localhost:8080/api/webhook
  ```
- **Rohe Audiodaten** im Body (Dateiname optional über `?filename=` oder den Header `X-Filename`):
  ```bash
  curl --data-binary @memo.m4a -H "Content-Type: audio/mp4" "http://localhost:8080/api/webhook?filename=memo.m4a"
  ```

Ist `WEBHOOK_TOKEN` gesetzt, muss das Token als Header `X-Webhook-Token` oder als `?token=` mitgeschickt werden.

Antwort: `202 {"id": "...", "status": "pending"}`

Alle Formate, die ffmpeg lesen kann, funktionieren (mp3, m4a, wav, ogg, opus, webm usw.).

## Weboberfläche

- Notizen nach Datum sortiert, die neuesten oben; Suche über den Text
- Auf den Text klicken, um ihn zu bearbeiten
- Audio abspielen, Text kopieren, neu transkribieren
- **Papierkorb**: Notizen landen dort statt auf der Startseite, lassen sich wiederherstellen oder endgültig löschen (mit Audiodatei)
- **Senden**: schickt den Text per HTTP POST an `FORWARD_URL` (nur sichtbar, wenn konfiguriert)
- Audiodateien lassen sich auch direkt in der Oberfläche hochladen

## Webhook-Felder (Signal-Bot)

Der Signal-Bot schickt multipart/form-data mit:

| Feld | Inhalt |
|---|---|
| `file` | die Audiodatei |
| `sender` | Telefonnummer des Absenders (wird zum Empfänger der Antwort) |
| `text` | optional: Nachrichtentext zur Sprachnachricht, wird über dem Transkript angezeigt |

## Transkript zurückschicken

Nach der **ersten** erfolgreichen Transkription schickt MemoDrop automatisch (`AUTO_SEND=true`) einen POST an `FORWARD_URL`:

```json
{ "recipient": "<sender der Notiz oder DEFAULT_RECIPIENT>", "message": "<Transkript>" }
```

„Neu transkribieren“ schickt eine bereits gesendete Notiz nicht erneut. Über den Button „Senden“ lässt sich der (ggf. bearbeitete) Text jederzeit manuell schicken. Schlägt das Senden fehl, steht der Fehler rot an der Notiz.

## REST-API

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/notes?view=active\|trash` | Notizen auflisten |
| GET | `/api/notes/{id}` | Einzelne Notiz |
| PATCH | `/api/notes/{id}` | Text ändern (`{"text": "…"}`) |
| GET | `/api/notes/{id}/audio` | Audiodatei |
| POST | `/api/notes/{id}/trash` / `/restore` | In den Papierkorb / zurück |
| DELETE | `/api/notes/{id}` | Endgültig löschen |
| DELETE | `/api/trash` | Papierkorb leeren |
| POST | `/api/notes/{id}/retranscribe` | Erneut transkribieren |
| POST | `/api/notes/{id}/send` | An `FORWARD_URL` senden |

## Hinweise

- Die Weboberfläche hat **keinen Login**. Nur im lokalen Netz betreiben oder hinter einen Reverse-Proxy mit Authentifizierung stellen.
- GPU: im `docker-compose.yml` das Image `onerahmet/openai-whisper-asr-webservice:latest-gpu` verwenden und eine GPU-Reservierung hinzufügen.
- Daten liegen in `./data` – für ein Backup reicht es, diesen Ordner zu sichern.
