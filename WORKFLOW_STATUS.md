# BK7258 Voice Server Workflow Status

## Current Status

The BK7258 chip is now working end to end with the Python WebSocket server.

Working now:

- chip connects to `ws://10.0.0.62:8765`
- server sends greeting / manual speech to chip
- chip plays server audio clearly
- chip microphone sends audio back to server
- server runs STT -> Claude -> TTS
- chip speaks the reply back

No firmware flash is required for the final working server-side fix.

## Main Working File

- `wss_server.py`

## What Was Fixed

The main issue was an audio format mismatch.

The live chip session advertised:

- `input_audio_format = pcm`
- `output_audio_format = pcm`
- `output_audio_rate = 16000`

The server was previously forcing OPUS on the output path, which caused the chip
to play garbled `dedede` audio instead of speech.

The server was updated to:

- honor PCM input from the chip
- honor PCM 16 kHz output back to the chip
- keep the BK transport header framing
- add server-side utterance auto-commit when the chip streams mic audio but does
  not explicitly send `input_audio_buffer.commit`

## Working Runtime Flow

1. Chip connects to the server over WebSocket.
2. Chip sends `hello`.
3. Server replies with `hello_response`.
4. Chip sends `session.update`.
5. Server replies with `session.updated`, matching the chip's real audio mode.
6. Server can send greeting audio or manual speech to the chip.
7. Chip plays the speech.
8. User talks to the chip.
9. Chip sends microphone PCM frames to the server.
10. If the chip does not send a commit, the server uses PCM VAD to cut the utterance.
11. Server sends `input_audio_buffer.committed`.
12. Server runs:
    - Deepgram STT
    - Claude `claude-haiku-4-5`
    - Deepgram TTS
13. Server sends framed PCM audio back to the chip.
14. Chip speaks the reply.

## Important Server Behavior

- startup greeting is enabled
- local manual speech endpoint is enabled:
  - `http://127.0.0.1:8766/speak?text=...`
- server ignores a short window of microphone input after playback to reduce
  self-echo loops

## How To Run

From the project directory:

```bash
./.venv/bin/python3 ./wss_server.py
```

## How To Test

1. Start the server.
2. Power on the chip.
3. Wait for the greeting.
4. Say a short phrase to the chip.
5. Wait for the spoken reply.

Optional manual speech test:

```bash
curl -G --data-urlencode "text=Hello Samuel this is a test" http://127.0.0.1:8766/speak
```

## Notes

- The chip can now both hear and speak through the new workflow.
- The working fix was on the server side, not `.env`, not `pyproject.toml`, and
  not a firmware reflash requirement.
