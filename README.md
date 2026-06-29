# bk7258-voice-server

Custom Python WebSocket voice server for the Agora R1 / BK7258 AI toy chip.

This working version uses a custom chip adapter layer, not Pipecat.

## What This Repo Does

- accepts the BK7258 chip's raw WebSocket connection
- handles the chip's 16-byte transport audio header
- uses PCM audio with the chip at 16 kHz
- runs:
  - Deepgram STT
  - Claude `claude-haiku-4-5`
  - Deepgram TTS
- sends spoken replies back to the chip

## Working Runtime Flow

1. Chip connects to `ws://10.0.0.62:8765`
2. Chip sends `hello`
3. Server replies `hello_response`
4. Chip sends `session.update`
5. Server replies `session.updated`
6. Server sends greeting or manual speech to chip
7. Chip plays the audio
8. User speaks to the chip
9. Chip streams microphone PCM to the server
10. Server detects the utterance, runs STT -> LLM -> TTS
11. Server sends framed PCM audio back
12. Chip speaks the reply

## Why Custom Code Instead Of Pipecat

Pipecat does not know how to speak the BK7258 chip's native protocol.

We still needed custom code for:

- the BK7258 WebSocket protocol
- the custom 16-byte audio header
- the chip's PCM framing and timing
- the chip's unusual microphone commit behavior

So this repo solves the chip adapter problem directly.

## Requirements

- Python 3.14
- `ffmpeg`
- `libopus`

macOS example:

```bash
brew install ffmpeg opus
```

## Python Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install opuslib requests python-dotenv loguru
```

## Environment

Copy `.env.example` to `.env` and fill in:

- `DEEPGRAM_API_KEY`
- `ANTHROPIC_API_KEY`

## Run

```bash
./start_server.sh
```

Or directly:

```bash
./.venv/bin/python3 ./wss_server.py
```

## Manual Speech Test

When the chip is connected, you can make it speak from the server side:

```bash
curl -G --data-urlencode "text=Hello Samuel this is a test" http://127.0.0.1:8766/speak
```

## Important Files

- `wss_server.py`
- `WORKFLOW_STATUS.md`

## Notes

- This repo intentionally does not include private API keys
- The final working fix was server-side
- The chip firmware may still contain its own auto deep-sleep logic
