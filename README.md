# BK7258 AI Toy - Voice Server

Pipeline: Chip mic -> WebSocket -> Deepgram STT -> selectable LLM -> low-latency TTS -> OPUS/PCM -> Chip speaker

Product planning docs:
- `PRODUCT_BLUEPRINT.md`
- `PRODUCT_EXECUTION_PLAN.md`

Current control panel features:
- switch LLM provider between `Anthropic` and `OpenAI`
- set Anthropic and OpenAI model names in the panel
- paste Anthropic or OpenAI API keys directly in the panel
- switch TTS backend between `auto`, `local`, and `Deepgram`
- choose character presets like `companion`, `storyteller`, and `language_teacher`
- optionally protect the control panel with `BK7258_PANEL_ACCESS_CODE`
- open a phone-friendly onboarding page at `/onboarding`
- generate the onboarding QR locally on the Mac mini
- send direct speech to the chip
- run backend latency simulation from the browser
- see whether the panel is `LAN` or `local-only`
- use a phone-first control surface with quick mode buttons and quick speech buttons
- add the panel to a phone home screen like a lightweight app
- reload structured content files without editing Python code
- show recent turns, recent sessions, and average latency in the parent panel
- show richer parent dashboard summaries for latency, session length, top child, top mode, and top provider
- filter saved activity by child, provider, character, and search text
- recommend stories and learning packs based on child age, interests, and parent goals
- filter content in the panel with search and `recommended only`
- tighter low-latency defaults: shorter replies, shorter LLM history, and faster server-side VAD turn cutting

## Hardware
- Agora R1 / BK7258 chip
- Chip IP: 10.0.0.150
- Mac Mini IP: 10.0.0.62 (server)
- Server port: 8765

## Setup

1. Install system dependency (one time):
   `brew install opus`

2. Create virtualenv and install deps:
   `uv venv --python python3.14`
   `uv pip install opuslib requests python-dotenv loguru`

3. Copy `.env.example` to `.env` and fill in API keys:
   `cp .env.example .env`

   Required:
   - `DEEPGRAM_API_KEY`
   - `ANTHROPIC_API_KEY`

   Optional:
   - `OPENAI_API_KEY`
   - `BK7258_PANEL_ACCESS_CODE`

   You can also leave `OPENAI_API_KEY` out of `.env` and paste it later in the control panel.
   The panel can override the LLM API key in memory until the server restarts.

4. Start server:
   `./start_server.sh`

5. Open the control panel:
   `http://YOUR_MAC_IP:8766/`

   Example on this setup:
   `http://10.0.0.62:8766/`

   Note:
   - if admin host is `0.0.0.0`, the panel is reachable from other devices on the same Wi-Fi
   - it is not public internet access unless you add your own tunnel, VPN, or port forwarding
   - on iPhone or Android, open this URL in the browser and use `Add to Home Screen` for app-like use

## Firmware
- Project: `beken_wss` (not `beken_genie`)
- Repo: `~/armino/bk_aidk`, branch `ai_release/v2.0.1`
- Build: `make bk7258 PROJECT=beken_wss`
- Binary: `build/beken_wss/bk7258/all-app.bin`
- Flash: `BKFIL` macOS app

## Protocol (NOPSRAM WebSocket)
The chip speaks an OpenAI Realtime API-like protocol over raw WebSocket:

1. Chip -> `hello {interact_mode:4}` -> Server -> `hello_response {code:200}`
2. Chip -> `session.update` -> Server -> `session.updated` + greeting audio
3. Chip sends binary OPUS frames (16 kHz mic) + `input_audio_buffer.commit`
4. Server -> `input_audio_buffer.committed` (must be immediate or chip reconnects)
5. Chip -> `response.create` -> Server -> STT + LLM + TTS -> OPUS audio frames

## Character Modes
- `companion`: default playful toy voice
- `storyteller`: concise story-focused behavior
- `language_teacher`: gentle correction and short teaching examples
- `curious_friend`: question-driven conversational mode
- `bedtime_guide`: soft, calming replies

## Phone Control
- open the control panel from a phone on the same Wi-Fi
- open `http://YOUR_MAC_IP:8766/onboarding` for the parent handoff page
- use `Quick Modes` for one-tap personality switching
- use `Quick Speech` to make the chip speak without typing long text
- use the sticky buttons at the bottom of the phone screen for fast control
- choose a provider, model name, and paste that provider's API key directly in the panel
- use the family setup area to shape the toy for a specific child, goals, safety mode, story set, and learning packs

## Structured Content
- `content/learning_packs.json`
- `content/story_library.json`
- use the `Reload Content Files` button in the panel after editing these JSON files
- the server injects the selected packs and stories into the runtime prompt, which is the current product step before full RAG
- each content item can now include:
  - `age_bands`
  - `goal_tags`
  - `topics`
- the panel uses that metadata to suggest the best packs and stories for the current child profile

## Activity History
- recent activity is persisted to `activity_state.json`
- the control panel shows recent turns and recent sessions from that file
- the parent dashboard can filter saved activity by child, provider, character, and search text
- the parent dashboard shows filtered summary cards and filtered activity breakdowns
- the activity API is `GET /api/activity`
- you can change the path with `BK7258_ACTIVITY_STATE_PATH`

## Product Paths
- parent panel: `http://YOUR_MAC_IP:8766/`
- onboarding page: `http://YOUR_MAC_IP:8766/onboarding`
- onboarding QR image: `http://YOUR_MAC_IP:8766/onboarding-qr.png`
- auth endpoint: `POST /api/auth`
- onboarding data: `GET /api/onboarding`
- content reload: `POST /api/reload-content`

## Low Latency Mode
- default TTS mode is `auto`, which tries local macOS speech first for much faster replies
- if local speech is unavailable, the server falls back to Deepgram TTS
- a short processing prompt can be enabled so the chip says `One moment.` instead of staying silent
- the server now trims LLM history before each request to reduce response time
- the server now forces spoken replies to stay short and plain so LLM + TTS stay faster
- the server now cuts PCM turns more aggressively with faster VAD silence and minimum-speech settings
