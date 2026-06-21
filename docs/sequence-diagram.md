# Sahaara — Voice Assistant Sequence Diagram

End-to-end flow of a voice conversation, from clicking the orb through each
spoken turn. Entry points: `startConversation()` in `static/index.html` (browser
side) and `websocket_endpoint` in `main.py` (server side).

![Sahaara voice assistant sequence diagram](sequence-diagram.png)

<details>
<summary>Mermaid source (renders on GitHub / Mermaid-capable viewers)</summary>

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant Backend as FastAPI /ws
    participant STT as Sarvam STT
    participant LLM as OpenRouter
    participant TTS as Sarvam TTS

    User->>Browser: Click orb to start
    Browser->>Backend: Connect + START_CONVERSATION
    Backend->>TTS: Synthesize greeting
    TTS-->>Backend: Audio
    Backend-->>Browser: Greeting text + audio
    Browser-->>User: Play greeting, open mic

    loop Each turn
        User->>Browser: Speak
        Browser->>Backend: Stream audio, then END_OF_SPEECH
        Backend->>STT: Transcribe
        STT-->>Backend: Transcript
        Backend->>LLM: Stream reply (history)
        LLM-->>Backend: Text
        Backend->>TTS: Synthesize (per sentence)
        TTS-->>Backend: Audio
        Backend-->>Browser: Reply text + audio + metrics
        Browser-->>User: Play reply, reopen mic
    end

    User->>Browser: Click orb to end
    Browser-->>User: Call summary
```

</details>

## Notes

This is a simplified view. Two details collapsed in the diagram but worth knowing:

- **Streaming overlap:** the LLM→TTS→Browser steps actually run as a pipeline,
  not in sequence. `stream_response_and_audio` (`main.py`) splits the LLM stream
  into sentences and synthesizes each one as it's ready, so the user hears the
  start of the reply while the LLM is still generating the rest.
- **Turn handoff:** the browser only reopens the mic once the server's `TURN_END`
  marker has arrived *and* all queued audio has finished playing
  (`maybeFinishTurn` in `index.html`).
