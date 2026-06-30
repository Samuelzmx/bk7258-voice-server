# bk7258-voice-server

Working BK7258 / Agora R1 voice server that replaces the original Agora or TEN cloud path with a local Python WebSocket server.

This repo is the server side that we actually verified with the chip:

- chip connects over Wi-Fi to `ws://<your-mac-ip>:8765`
- chip sends microphone audio to the server
- server runs `Deepgram STT -> Claude haiku-4-5 -> Deepgram TTS`
- chip speaks the reply back

This final working version does **not** use Pipecat in the live runtime. We kept a custom transport layer because the BK7258 chip needs a custom WebSocket handshake, a custom 16-byte audio header, chip-specific framing, and server-side handling for the chip's commit behavior.

## What Matches The Feishu Workshop

The setup flow is intentionally close to the Feishu `R1+TEN document [eng ver]`:

1. set up the BK SDK
2. install the toolchain
3. configure Wi-Fi
4. compile and flash firmware
5. configure the server address

The difference is the backend:

- the Feishu workshop points the hardware at a TEN / Agora workflow
- this repo points the hardware at `wss_server.py`
- the final voice path here is `Deepgram -> Claude -> Deepgram`, not TEN and not Agora

## Final Architecture

```text
BK7258 mic
  -> BK7258 firmware
  -> Wi-Fi
  -> raw WebSocket
  -> wss_server.py
  -> Deepgram STT
  -> Claude haiku-4-5
  -> Deepgram TTS
  -> raw WebSocket
  -> BK7258 speaker
```

## Repo Contents

- `wss_server.py`: the production server
- `start_server.sh`: simple launcher
- `requirements.txt`: Python packages
- `.env.example`: required API keys
- `WORKFLOW_STATUS.md`: working-state notes
- `scripts/prepare_bk_aidk.py`: helper that patches the Beken SDK for your own Mac IP and optional fallback Wi-Fi

## Before You Start

You need:

- a BK7258 / Agora R1 board
- a USB cable for flashing and power
- a MacBook on the same Wi-Fi network as the chip
- a 2.4 GHz Wi-Fi network
- a phone for BLE Wi-Fi provisioning if you use the stock Beken App path
- a Deepgram API key
- an Anthropic API key

Recommended Mac software:

- Homebrew
- Python 3.11 or newer
- `ffmpeg`
- `opus`
- Git
- Docker Desktop
- BKFIL for macOS, or the Beken UART flash tool from the workshop / vendor package

Useful upstream links:

- Beken SDK source: [github.com/bekencorp/bk_aidk](https://github.com/bekencorp/bk_aidk)
- Beken SDK docs: [docs.bekencorp.com Armino AIDK](https://docs.bekencorp.com/arminodoc/bk_aidk/bk7258/en/v2.0.1/projects/beken_genie/index.html)
- Beken App download page used by the workshop flow: [docs.riselink.ai app download](https://docs.riselink.ai/arminodoc/bk_app/app/zh_CN/v2.0.1/app_download/index.html)
- BK7258 burn-code docs: [docs.riselink.ai burn code](https://docs.riselink.ai/arminodoc/bk_ai_smp/bk7258/en/v3.1.1/get-started/index.html)

## Step-By-Step: From Raw Chip To Talking Device

### 1. Clone This Repo

```bash
git clone git@github.com:Samuelzmx/bk7258-voice-server.git
cd bk7258-voice-server
```

### 2. Prepare The Python Server

Install native packages:

```bash
brew install ffmpeg opus
```

Create a virtualenv and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your environment file:

```bash
cp .env.example .env
```

Fill in:

- `DEEPGRAM_API_KEY`
- `ANTHROPIC_API_KEY`

Do not commit `.env`.

### 3. Find Your Mac's LAN IP

The chip must connect to the IP of the Mac that is running `wss_server.py`.

Common commands:

```bash
ipconfig getifaddr en0
ipconfig getifaddr en1
```

Pick the interface that is actually on the same Wi-Fi network as the chip.

### 4. Start The Server

```bash
./start_server.sh
```

Or:

```bash
./.venv/bin/python3 ./wss_server.py
```

What the server exposes:

- chip websocket listener: `0.0.0.0:8765`
- local admin speak endpoint: `http://127.0.0.1:8766/speak?text=...`

Leave this running before you power the chip for testing.

### 5. Download The BK Firmware SDK

The working firmware target we used is:

- branch: `ai_release/v2.0.1`
- project: `beken_wss_nopsram`

Clone it:

```bash
mkdir -p ~/armino
cd ~/armino
git clone --recurse-submodules https://github.com/bekencorp/bk_aidk.git -b ai_release/v2.0.1
```

Note on the Feishu doc:

- the workshop page references `ai_server/v2.0.1`
- our verified local working setup was built from `ai_release/v2.0.1`

### 6. Patch The Firmware For Your Server

The firmware has to know where your server is.

Recommended command:

```bash
python3 ./scripts/prepare_bk_aidk.py \
  --sdk ~/armino/bk_aidk \
  --server-ip 192.168.1.23 \
  --disable-countdown
```

Replace `192.168.1.23` with your Mac's LAN IP.

What this does:

- updates the WebSocket URI inside the BK SDK to `ws://<your-mac-ip>:8765`
- optionally disables the stock 3-minute countdown sleep

Optional: also set a development fallback Wi-Fi directly in the firmware:

```bash
python3 ./scripts/prepare_bk_aidk.py \
  --sdk ~/armino/bk_aidk \
  --server-ip 192.168.1.23 \
  --wifi-ssid "Your2GNetwork" \
  --wifi-password "YourPassword" \
  --disable-countdown
```

Use the `--wifi-ssid/--wifi-password` option only if you want a baked-in fallback.

If you skip those options, the firmware can still use the Beken App provisioning flow.

### 7. Build The Firmware

The safest build path is Docker with GCC 10, because the BK SDK bundles precompiled libraries that matched GCC 10 in our testing.

We verified the Docker image:

- `bekencorp/armino-idk:1.2`

Build with:

```bash
docker run --rm -it \
  -v ~/armino/bk_aidk:/armino/bk_aidk \
  -w /armino/bk_aidk \
  bekencorp/armino-idk:1.2 \
  bash -lc 'export PATH=/opt/gcc-arm-none-eabi-10.3-2021.10/bin:$PATH && export TOOLCHAIN_DIR=/opt/gcc-arm-none-eabi-10.3-2021.10/bin && make bk7258 PROJECT=beken_wss_nopsram'
```

The file to flash is:

```text
~/armino/bk_aidk/build/beken_wss_nopsram/bk7258/all-app.bin
```

That is the main answer to "which file do I flash?"

### 8. Install / Open The Flash Tool

In our working tests on macOS, we used **BKFIL**.

What to use:

- if your workshop bundle or internal package includes BKFIL for macOS, use that
- otherwise use the BK7258 UART flash tool referenced in the Beken burn-code documentation

Important note:

- Beken's public docs are clearer for the general UART flashing flow than for the exact macOS BKFIL distribution
- if you do not already have BKFIL, the fastest path is usually the workshop package or your Beken contact / FAE

### 9. Flash The Chip

The working BKFIL settings we used were:

- image: `all-app.bin`
- start address: `0x0`
- link type: `BOOTROM`
- reboot after download: enabled
- erase before download: enabled
- baud rate: `1500000`

Expected file path:

```text
~/armino/bk_aidk/build/beken_wss_nopsram/bk7258/all-app.bin
```

Typical board procedure:

1. connect the board over USB
2. put it into download mode
3. in BKFIL, choose the serial port like `/dev/cu.usbserial-*`
4. select `all-app.bin`
5. click download

The usual manual reset sequence is:

1. hold `BOOT`
2. tap `RST`
3. release `BOOT`

If the flash tool says "Please reset the chip", repeat that sequence.

### 10. Provision Wi-Fi

You have two choices.

Choice A, recommended for other engineers:

- use the Beken App BLE provisioning flow
- this is closest to the workshop / Feishu setup

Choice B, development fallback:

- bake a fallback SSID/password with `scripts/prepare_bk_aidk.py`

For the Beken App path, the vendor docs show this flow:

1. open the Beken App on your phone
2. put the device into provisioning mode
3. scan / select the device
4. choose the device model
5. choose the local Wi-Fi network
6. send credentials to the chip

Vendor doc note:

- the stock R1 kit documentation says to long-press `Key 2` for 3 seconds to enter provisioning mode
- if your board revision labels the buttons differently, follow the workshop sheet or board silkscreen

### 11. Power On And Check The Connection

If everything is correct:

1. the chip joins Wi-Fi
2. the chip opens a websocket to your Mac on port `8765`
3. the server logs a new chip session
4. the server replies to `hello` and `session.update`
5. the chip should play the greeting audio

The startup greeting is enabled by default in `wss_server.py`.

### 12. Send A Manual Speech Test

Once the chip is connected, you can force speech from the server side:

```bash
curl -G --data-urlencode "text=Hello this is a server side test" \
  http://127.0.0.1:8766/speak
```

Expected result:

- the chip speaks the sentence clearly

If you only hear `dedede` or noise, the audio path is wrong.

### 13. Talk To The Chip

Once connected, the device is hands-free.

There is no separate talk button required in the final working path.

Expected live workflow:

1. you speak to the chip
2. chip sends microphone PCM audio to the server
3. server commits the utterance
4. server runs `Deepgram STT -> Claude -> Deepgram TTS`
5. chip speaks the answer

## What The Working Server Is Actually Doing

The important runtime behavior in `wss_server.py` is:

- manual websocket upgrade and frame handling
- chip handshake:
  - `hello -> hello_response`
  - `session.update -> session.updated`
- custom 16-byte transport header on binary audio
- PCM input from the chip at 16 kHz
- PCM output back to the chip at 16 kHz
- server-side VAD fallback when the chip does not commit cleanly
- Deepgram REST calls for STT and TTS
- Anthropic messages API call for Claude
- local `/speak` endpoint for forced output testing

## Program Structure

High-level structure inside `wss_server.py`:

1. transport layer
   - websocket handshake
   - websocket frame encode/decode
   - BK audio header pack/unpack
2. session state
   - one `Session` object per chip connection
   - per-session sequencing and audio buffers
3. inbound audio handling
   - receive chip frames
   - strip 16-byte header
   - accumulate PCM
   - auto-commit by VAD if needed
4. AI pipeline
   - Deepgram STT
   - Claude haiku-4-5
   - Deepgram TTS
5. outbound audio handling
   - resample if needed
   - frame audio
   - send back to chip
6. debug / operations
   - startup greeting
   - local admin speak endpoint
   - detailed logs

## Troubleshooting

### Problem: chip connects to Wi-Fi but says nothing

Check:

- server is running on port `8765`
- firmware server IP matches your real Mac LAN IP
- Mac firewall is not blocking incoming connections
- chip and Mac are on the same LAN

### Problem: chip keeps reconnecting

Most common causes:

- wrong server IP in firmware
- Mac changed IP after sleeping or changing Wi-Fi
- Wi-Fi credentials are wrong
- chip is failing provisioning and looping

### Problem: chip shuts down after a few minutes

This is usually firmware-side countdown sleep, not the Python server.

The stock `beken_wss_nopsram` project enables a countdown that can deep-sleep the board after a standby timeout.

Recommended fix:

```bash
python3 ./scripts/prepare_bk_aidk.py \
  --sdk ~/armino/bk_aidk \
  --server-ip 192.168.1.23 \
  --disable-countdown
```

Then rebuild and reflash.

### Problem: chip speaks noise like `dedede`

That usually means the audio format is wrong.

The final working server uses:

- PCM input from chip
- PCM output to chip
- 16-byte BK transport header on each outbound frame

### Problem: BKFIL is missing

That is a real setup friction point.

What we know:

- the workshop flow assumes a flashing tool is available
- our successful macOS flashing used BKFIL
- Beken's public docs are easier to find than the exact macOS BKFIL package

Practical next step:

- use the workshop package if you have it
- otherwise request the macOS tool from the vendor / field app engineer

## Current Working Test Result

This repo has already been validated with the chip for:

- chip hears greeting audio
- server can manually make the chip speak
- user speech reaches the server
- server runs STT -> LLM -> TTS
- chip speaks the response back

## Manual Server Test Commands

Syntax check:

```bash
python3 -c "import ast; ast.parse(open('wss_server.py').read()); print('syntax ok')"
```

Force speech:

```bash
curl -G --data-urlencode "text=Tell me a short story" \
  http://127.0.0.1:8766/speak
```
