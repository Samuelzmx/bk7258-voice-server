# BK7258 Voice Server Workflow Status

## Current Status

The BK7258 chip is now working end to end with the Python WebSocket server.

Working now:

- chip connects to `ws://10.0.0.62:8765`
- server sends greeting / manual speech to chip
- chip plays server audio clearly
- chip microphone sends audio back to server
- server runs STT -> selectable LLM -> low-latency TTS
- chip speaks the reply back

No firmware flash is required for the final working server-side fix.

## Main Working File

- `wss_server.py`
- `bk7258_product_runtime.py`

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
- add a LAN control panel at `http://10.0.0.62:8766/`
- add low-latency local macOS TTS mode with Deepgram fallback
- allow runtime provider and character switching from the control panel

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
    - Anthropic or OpenAI, depending on control panel selection
    - local macOS TTS first by default, with Deepgram fallback
13. Server sends framed PCM audio back to the chip.
14. Chip speaks the reply.

## Important Server Behavior

- startup greeting is disabled by default for better connection stability
- startup listen prime is enabled by default
- LAN control panel is enabled:
  - `http://10.0.0.62:8766/`
- local manual speech endpoint is still enabled:
  - `http://127.0.0.1:8766/speak?text=...`
- server ignores a short window of microphone input after playback to reduce
  self-echo loops
- a short processing prompt can be enabled so the chip says `One moment.`

## Significant Checkpoints

### 2026-07-01

- added richer parent dashboard summaries and filters
- added filtered activity breakdowns and history views in the control panel
- tightened low-latency defaults for shorter replies and faster turn cutting
- pushed product-branch checkpoint `34a7570`

### 2026-07-05

- upgraded content selection from simple tag overlap to ranked local retrieval
- added diversity-aware prompt context so mixed requests can pull complementary story guidance
- extracted product/content state and retrieval logic into `bk7258_product_runtime.py`
- added `setup_server.command` and `start_server.command` to reduce Mac tester setup friction
- pushed product-branch checkpoints `fb90bc9` and the current refactor/setup follow-up

## How To Run

From the project directory:

```bash
./setup_server.command
./start_server.command
```

Shell fallback:

```bash
./.venv/bin/python3 ./wss_server.py
```

## How To Test

1. Start the server.
2. Power on the chip.
3. Open `http://10.0.0.62:8766/` on the Mac or phone.
4. Confirm the chip session appears in the panel.
5. Use `Send Speech` to test server -> chip audio.
6. Say a short phrase to the chip.
7. Wait for the spoken reply.

Optional manual speech test:

```bash
curl -G --data-urlencode "text=Hello Samuel this is a test" http://127.0.0.1:8766/speak
```

## Notes

- The chip can now both hear and speak through the new workflow.
- The working fix was on the server side, not `.env`, not `pyproject.toml`, and
  not a firmware reflash requirement.
- The current control panel scope is `LAN`, not public internet.
