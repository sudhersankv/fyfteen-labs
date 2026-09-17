"""Speech-to-text for FYFTEN. Default is Groq whisper-large-v3-turbo."""

from __future__ import annotations

import httpx

from .fyften_keys import stt_config


async def transcribe_audio(audio: bytes, content_type: str = "audio/wav",
                           dictionary: list[str] | None = None) -> dict:
    cfg = stt_config()
    if not cfg["key"]:
        raise ValueError(
            "No speech key yet. Set GROQ_API_KEY or FYFTEN_STT_API_KEY "
            "for Groq whisper-large-v3-turbo.")
    if cfg["provider"] == "whisper":
        return await _openai_whisper(audio, content_type, cfg)
    return await _groq(audio, content_type, cfg, dictionary)


def _http_error(name: str, response: httpx.Response) -> ValueError:
    return ValueError(f"{name} {response.status_code}: {(response.text or '')[:280]}")


async def _groq(audio: bytes, content_type: str, cfg: dict[str, str],
                dictionary: list[str] | None = None) -> dict:
    filename = "speech.wav" if "wav" in (content_type or "") else "speech.webm"
    names = " ".join(w for w in (dictionary or []) if w)[:400]
    prompt = (
        "fyfteen labs FYFTEN fleet commands. Jobs: Direction, Both agree, Late closer. "
        "Words: leftover, deploy, pause, retire, budget, cash. "
        + names)
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            cfg["url"] or "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {cfg['key']}"},
            files={"file": (filename, audio, content_type or "audio/wav")},
            data={
                "model": cfg["model"] or "whisper-large-v3-turbo",
                "language": "en",
                "temperature": "0",
                "response_format": "verbose_json",
                "prompt": prompt[:800],
            })
        if response.status_code >= 400:
            raise _http_error("Groq", response)
        try:
            body = response.json()
        except ValueError:
            body = {"text": (response.text or "").strip()}
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("Groq returned no text.")
    return {"text": text, "provider": "groq",
            "detected_language": body.get("language")}


async def _openai_whisper(audio: bytes, content_type: str, cfg: dict[str, str]) -> dict:
    filename = "speech.wav" if "wav" in content_type else "speech.webm"
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            cfg["url"],
            headers={"Authorization": f"Bearer {cfg['key']}"},
            files={"file": (filename, audio, content_type or "application/octet-stream")},
            data={"model": cfg["model"], "language": "en"})
        if response.status_code >= 400:
            raise _http_error("Whisper", response)
        body = response.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("Whisper returned no text.")
    return {"text": text, "provider": "whisper"}
