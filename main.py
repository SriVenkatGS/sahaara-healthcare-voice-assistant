import os
import io
import re
import json
import time
import base64
import asyncio
from fastapi import FastAPI, WebSocket, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from dotenv import load_dotenv
from sarvamai import SarvamAI
import httpx

load_dotenv()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
sarvam = SarvamAI(api_subscription_key=SARVAM_API_KEY)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


SYSTEM_PROMPT = {"role": "system", "content": """You are Sahaara, a warm and knowledgeable voice-based healthcare aid assistant. You help people understand their symptoms, answer general health questions, and figure out sensible next steps. You are not a doctor and you do not give formal diagnoses or prescribe medication, but you are genuinely helpful and reassuring.

The caller is speaking to you over a voice call, not typing. They cannot see a screen, so never use lists, bullet points, or anything written for the eye. Keep replies short, 1 to 2 sentences at a time, like a real conversation, not a report.

Speak like a calm, experienced nurse who has time for the person: caring, practical, and unhurried. Ask a focused follow-up question when you genuinely need more to be useful, but don't interrogate — if someone asks a general question, just answer it helpfully.

Be supportive and confident with the everyday stuff. For common, mild things like a cold, a mild headache, a small cough, or a minor scrape, explain what's likely going on in plain language, reassure them, and offer sensible self-care (rest, fluids, time, comfort measures). Most minor things get better on their own — say so. Do NOT reflexively tell people to see a doctor for every little thing; only suggest professional care when it's actually warranted: symptoms that are severe, persistent, worsening, unusual, or that carry real risk. When you do suggest seeing a doctor, briefly say why so it doesn't feel like a brush-off.

Happily answer general health questions too — what a condition is, how something like the common cold works, what a procedure generally involves, what a symptom usually means. Being informative is part of helping.

If the caller mentions chest pain, difficulty breathing, severe bleeding, sudden confusion, or signs of stroke, say plainly that this needs emergency care now, and to call 108 or 112 or go to the nearest ER. Do this immediately, before the usual back-and-forth.

If a caller mentions thoughts of suicide or self-harm, drop the usual format. Respond with care and warmth, encourage them to reach out to a crisis line or someone they trust right now, and gently say you're not equipped to handle this alone.

You don't give a specific medical diagnosis, and you don't tell someone exactly what medication or dose to take or to stop a medication they're on — for those, point them to a doctor who can examine them. But you can still discuss what's generally going on and what usually helps.

If a caller asks about something clearly unrelated to health, gently let them know that health is what you're here for. Otherwise, stay helpful and engaged."""}
SENTENCE_END = re.compile(r'(?<=[.!?]) +')

GREETING_TEXT = (
    "Hello, I'm Sahaara, a healthcare aid assistant. I'm here to help you understand "
    "your symptoms and figure out the right next steps. Go ahead and tell me what's been going on."
)

# Cache the greeting's synthesized audio per voice so we only pay the TTS cost once.
_greeting_audio_cache: dict[str, bytes] = {}


def _tts_blocking(text: str, speaker: str) -> bytes:
    response = sarvam.text_to_speech.convert(
        text=text,
        target_language_code="en-IN",
        speaker=speaker,
        model="bulbul:v3",
    )
    return base64.b64decode(response.audios[0])


async def synthesize(text: str, speaker: str) -> bytes:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _tts_blocking(text, speaker))


async def get_greeting_audio(speaker: str) -> bytes:
    if speaker not in _greeting_audio_cache:
        print(f"Generating greeting audio for '{speaker}' (cached after this)...")
        _greeting_audio_cache[speaker] = await synthesize(GREETING_TEXT, speaker)
    return _greeting_audio_cache[speaker]


async def transcribe(audio_bytes: bytes) -> str:
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "audio.webm"
    response = sarvam.speech_to_text.transcribe(
        file=audio_file,
        model="saaras:v3",
        mode="transcribe",
    )
    return response.transcript


async def stream_llm_tokens(history: list):
    """Yields tokens one by one from OpenRouter streaming."""
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={"model": OPENROUTER_MODEL, "messages": [SYSTEM_PROMPT] + history, "stream": True},
            timeout=30.0,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: ") and "[DONE]" not in line:
                    try:
                        data = json.loads(line[6:])
                        token = data["choices"][0]["delta"].get("content", "")
                        if token:
                            yield token
                    except Exception:
                        pass


async def stream_response_and_audio(history: list, speaker: str, browser_ws: WebSocket) -> str:
    """Streams LLM → splits into sentences → TTS each sentence as it's ready → browser.

    The first sentence is synthesized and sent while the LLM is still generating the
    rest, so the user hears the start of the reply much sooner. A TURN_END marker tells
    the client no more audio is coming for this turn.
    """
    full_response = ""
    sentence_queue: asyncio.Queue = asyncio.Queue()
    metrics = {"text_ms": 0, "tts_ms": 0}
    t0 = time.perf_counter()

    async def produce_sentences():
        nonlocal full_response
        buffer = ""
        async for token in stream_llm_tokens(history):
            full_response += token
            buffer += token
            parts = SENTENCE_END.split(buffer)
            for sentence in parts[:-1]:
                if sentence.strip():
                    await sentence_queue.put(sentence.strip())
            buffer = parts[-1]
        if buffer.strip():
            await sentence_queue.put(buffer.strip())
        await sentence_queue.put(None)  # sentinel: no more sentences
        metrics["text_ms"] = int((time.perf_counter() - t0) * 1000)
        await browser_ws.send_text(f"ASSISTANT:{full_response}")

    async def consume_and_send():
        while True:
            sentence = await sentence_queue.get()
            if sentence is None:
                break
            ts = time.perf_counter()
            audio = await synthesize(sentence, speaker)
            metrics["tts_ms"] += int((time.perf_counter() - ts) * 1000)
            await browser_ws.send_bytes(audio)

    await asyncio.gather(produce_sentences(), consume_and_send())
    await browser_ws.send_text("TURN_END")
    return full_response, metrics


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, voice: str = Query(default="priya")):
    await ws.accept()
    conversation_history = []
    audio_buffer = bytearray()
    try:
        while True:
            message = await ws.receive()
            if "bytes" in message:
                audio_buffer.extend(message["bytes"])
                print(f"Buffered {len(message['bytes'])} bytes (total: {len(audio_buffer)})")
            elif "text" in message and message["text"] == "START_CONVERSATION":
                conversation_history.append({"role": "assistant", "content": GREETING_TEXT})
                await ws.send_text(f"ASSISTANT:{GREETING_TEXT}")
                await ws.send_bytes(await get_greeting_audio(voice))
                await ws.send_text("TURN_END")

            elif "text" in message and message["text"] == "END_OF_SPEECH":
                print("End of speech — transcribing...")
                if audio_buffer:
                    t_stt = time.perf_counter()
                    transcript = await transcribe(bytes(audio_buffer))
                    stt_ms = int((time.perf_counter() - t_stt) * 1000)
                    print(f"Transcript: {transcript}")
                    await ws.send_text(f"USER:{transcript}")
                    conversation_history.append({"role": "user", "content": transcript})
                    llm_response, metrics = await stream_response_and_audio(conversation_history, voice, ws)
                    conversation_history.append({"role": "assistant", "content": llm_response})
                    await ws.send_text("METRICS:" + json.dumps({
                        "stt": stt_ms,
                        "text": metrics["text_ms"],
                        "voice": metrics["tts_ms"],
                    }))
                    print(f"LLM: {llm_response}  [stt={stt_ms}ms text={metrics['text_ms']}ms tts={metrics['tts_ms']}ms]")
                audio_buffer = bytearray()
    except WebSocketDisconnect:
        print("Client disconnected")
