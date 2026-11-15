"""MemoDrop - receives audio via webhook, transcribes it with Whisper, shows notes."""

import asyncio
import logging
import mimetypes
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import UploadFile

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "memodrop.db"
STATIC_DIR = Path(__file__).parent / "static"

WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:9000").rstrip("/")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "de").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
FORWARD_URL = os.getenv("FORWARD_URL", "").strip()
FORWARD_TOKEN = os.getenv("FORWARD_TOKEN", "").strip()
# Recipient for notes without a sender (e.g. uploaded via the web UI)
DEFAULT_RECIPIENT = os.getenv("DEFAULT_RECIPIENT", "").strip()
# Send the transcript automatically after the first successful transcription
AUTO_SEND = os.getenv("AUTO_SEND", "true").strip().lower() in ("1", "true", "yes", "on")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "200")) * 1024 * 1024

log = logging.getLogger("memodrop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

queue: asyncio.Queue[str] = asyncio.Queue()


# --------------------------------------------------------------------------- db

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id                TEXT PRIMARY KEY,
                created_at        TEXT NOT NULL,
                text              TEXT NOT NULL DEFAULT '',
                audio_file        TEXT NOT NULL,
                original_filename TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|error
                error             TEXT,
                trashed_at        TEXT,
                sent_at           TEXT
            )
            """
        )
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
        for col in ("sender", "caption"):
            if col not in existing:
                conn.execute(f"ALTER TABLE notes ADD COLUMN {col} TEXT")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_note(note_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Notiz nicht gefunden")
    return row


def update_note(note_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE notes SET {cols} WHERE id = ?", (*fields.values(), note_id))


# ---------------------------------------------------------------- transcription

async def prepare_audio(path: Path) -> bytes | None:
    """Loudness-normalise and convert to 16 kHz mono WAV.

    Quiet phone recordings otherwise come back from Whisper as empty text.
    Returns None if ffmpeg fails, so the original file is used instead.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
        "-af", "loudnorm", "-ar", "16000", "-ac", "1", "-f", "wav", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        log.warning("ffmpeg preprocessing failed for %s: %s", path.name, err.decode(errors="replace")[:300])
        return None
    return out


async def transcribe(client: httpx.AsyncClient, note_id: str) -> None:
    note = get_note(note_id)
    path = AUDIO_DIR / note["audio_file"]
    update_note(note_id, status="processing", error=None)

    filename = note["original_filename"] or path.name
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    audio = await prepare_audio(path)
    if audio is not None:
        filename, mime = "audio.wav", "audio/wav"
    else:
        audio = path.read_bytes()
    params = {"task": "transcribe", "output": "json", "encode": "true"}
    if WHISPER_LANGUAGE:
        params["language"] = WHISPER_LANGUAGE

    # Whisper may still be starting / downloading its model -> retry connection errors.
    for attempt in range(60):
        try:
            resp = await client.post(
                f"{WHISPER_URL}/asr",
                params=params,
                files={"audio_file": (filename, audio, mime)},
            )
            break
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            log.warning("Whisper not reachable (%s), retry %d", exc, attempt + 1)
            await asyncio.sleep(10)
    else:
        raise RuntimeError("Whisper-Dienst nicht erreichbar")

    resp.raise_for_status()
    try:
        text = resp.json().get("text", "")
    except ValueError:
        text = resp.text
    update_note(note_id, text=text.strip(), status="done", error=None)
    log.info("Transcribed note %s (%d chars)", note_id, len(text))

    # Auto-send only the first time: a note that was already sent keeps its sent_at.
    if AUTO_SEND and FORWARD_URL and note["sent_at"] is None and text.strip():
        try:
            await forward_note(client, note_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-send failed for %s: %s", note_id, exc)
            update_note(note_id, error=f"Senden fehlgeschlagen: {exc}"[:500])


async def forward_note(client: httpx.AsyncClient, note_id: str) -> None:
    """POST the transcript: {"recipient": ..., "message": ...}."""
    note = get_note(note_id)
    recipient = note["sender"] or DEFAULT_RECIPIENT
    if not recipient:
        raise RuntimeError("Kein Empfänger (weder Absender noch DEFAULT_RECIPIENT)")
    if not note["text"].strip():
        raise RuntimeError("Kein Text zum Senden")
    headers = {"Authorization": f"Bearer {FORWARD_TOKEN}"} if FORWARD_TOKEN else {}
    resp = await client.post(
        FORWARD_URL,
        json={"recipient": recipient, "message": note["text"]},
        headers=headers,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Hook antwortete mit {resp.status_code}: {resp.text[:200]}")
    update_note(note_id, sent_at=now(), error=None)
    log.info("Sent note %s to %s", note_id, recipient)


async def worker() -> None:
    timeout = httpx.Timeout(3600, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            note_id = await queue.get()
            try:
                await transcribe(client, note_id)
            except HTTPException:
                pass  # note was deleted meanwhile
            except Exception as exc:  # noqa: BLE001
                log.exception("Transcription failed for %s", note_id)
                update_note(note_id, status="error", error=str(exc)[:500])
            finally:
                queue.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM notes WHERE status IN ('pending', 'processing') ORDER BY created_at"
        ).fetchall()
    for row in rows:
        queue.put_nowait(row["id"])
    task = asyncio.create_task(worker())
    yield
    task.cancel()


app = FastAPI(title="MemoDrop", lifespan=lifespan)


# ---------------------------------------------------------------------- intake

def safe_ext(filename: str | None, content_type: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    if not ext and content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".bin"


async def store_audio(request: Request) -> dict:
    """Accepts multipart/form-data (any file field, optional text fields
    `sender` and `text`) or a raw audio body."""
    content_type = request.headers.get("content-type", "")
    sender = caption = None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = next((v for v in form.values() if isinstance(v, UploadFile)), None)
        if upload is None:
            raise HTTPException(400, "Keine Datei im Formular gefunden")
        data = await upload.read()
        filename, ftype = upload.filename, upload.content_type
        sender = form.get("sender") if isinstance(form.get("sender"), str) else None
        caption = form.get("text") if isinstance(form.get("text"), str) else None
        caption = (caption or "").strip() or None
    else:
        data = await request.body()
        filename = request.query_params.get("filename") or request.headers.get("x-filename")
        ftype = content_type

    if not data:
        raise HTTPException(400, "Leere Datei")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Datei zu groß")

    note_id = uuid.uuid4().hex
    audio_file = note_id + safe_ext(filename, ftype)
    (AUDIO_DIR / audio_file).write_bytes(data)
    with db() as conn:
        conn.execute(
            "INSERT INTO notes (id, created_at, audio_file, original_filename, sender, caption)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, now(), audio_file, filename, sender, caption),
        )
    await queue.put(note_id)
    log.info("Stored audio %s (%d bytes) as note %s", filename, len(data), note_id)
    return {"id": note_id, "status": "pending"}


@app.post("/api/webhook", status_code=202)
async def webhook(request: Request):
    if WEBHOOK_TOKEN:
        token = request.headers.get("x-webhook-token") or request.query_params.get("token")
        if token != WEBHOOK_TOKEN:
            raise HTTPException(401, "Ungültiges Token")
    return await store_audio(request)


@app.post("/api/upload", status_code=202)
async def upload(request: Request):
    """Upload from the web UI."""
    return await store_audio(request)


# ------------------------------------------------------------------------ notes

def to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["audio_url"] = f"/api/notes/{row['id']}/audio"
    return d


@app.get("/api/config")
def config():
    return {"forward_enabled": bool(FORWARD_URL)}


@app.get("/api/notes")
def list_notes(view: str = "active"):
    where = "trashed_at IS NOT NULL" if view == "trash" else "trashed_at IS NULL"
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM notes WHERE {where} ORDER BY created_at DESC").fetchall()
        trash_count = conn.execute("SELECT COUNT(*) FROM notes WHERE trashed_at IS NOT NULL").fetchone()[0]
    return {"notes": [to_dict(r) for r in rows], "trash_count": trash_count}


@app.get("/api/notes/{note_id}")
def read_note(note_id: str):
    return to_dict(get_note(note_id))


class NoteUpdate(BaseModel):
    text: str


@app.patch("/api/notes/{note_id}")
def edit_note(note_id: str, body: NoteUpdate):
    get_note(note_id)
    update_note(note_id, text=body.text)
    return to_dict(get_note(note_id))


@app.get("/api/notes/{note_id}/audio")
def note_audio(note_id: str):
    note = get_note(note_id)
    path = AUDIO_DIR / note["audio_file"]
    if not path.exists():
        raise HTTPException(404, "Audiodatei fehlt")
    return FileResponse(path, filename=note["original_filename"] or path.name)


@app.post("/api/notes/{note_id}/trash")
def trash_note(note_id: str):
    get_note(note_id)
    update_note(note_id, trashed_at=now())
    return {"ok": True}


@app.post("/api/notes/{note_id}/restore")
def restore_note(note_id: str):
    get_note(note_id)
    update_note(note_id, trashed_at=None)
    return {"ok": True}


def purge(row: sqlite3.Row) -> None:
    (AUDIO_DIR / row["audio_file"]).unlink(missing_ok=True)
    with db() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    purge(get_note(note_id))
    return {"ok": True}


@app.delete("/api/trash")
def empty_trash():
    with db() as conn:
        rows = conn.execute("SELECT * FROM notes WHERE trashed_at IS NOT NULL").fetchall()
    for row in rows:
        purge(row)
    return {"deleted": len(rows)}


@app.post("/api/notes/{note_id}/retranscribe", status_code=202)
async def retranscribe(note_id: str):
    get_note(note_id)
    update_note(note_id, status="pending", error=None)
    await queue.put(note_id)
    return {"ok": True}


@app.post("/api/notes/{note_id}/send")
async def send_note(note_id: str):
    if not FORWARD_URL:
        raise HTTPException(400, "FORWARD_URL ist nicht konfiguriert")
    get_note(note_id)
    try:
        async with httpx.AsyncClient() as client:
            await forward_note(client, note_id)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"Senden fehlgeschlagen: {exc}") from exc
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True, "queue": queue.qsize()}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
