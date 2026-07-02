# bk7258-voice-server

This is the shortest working setup.

Goal:

1. start the server on your Mac
2. build firmware with your Mac IP and your Wi-Fi
3. flash the chip
4. talk to the chip

Current server features:

- low-latency local TTS by default, with Deepgram fallback
- browser control panel at `http://YOUR_MAC_IP:8766/`
- phone-first control panel with quick mode buttons and sticky actions
- selectable LLM provider in the panel: `Anthropic` or `OpenAI`
- paste the selected provider API key directly in the panel
- selectable character presets like `companion`, `storyteller`, and `language_teacher`
- direct `Send Speech` testing from the panel
- backend latency simulation from the panel

## What you need

- 1 MacBook
- 1 BK7258 / Agora R1 board
- 1 USB cable
- 1 Wi-Fi network
- 1 GitHub account
- `DEEPGRAM_API_KEY`
- `ANTHROPIC_API_KEY`
- optional `OPENAI_API_KEY`

## What to download

On your Mac, download:

1. Python 3.13
   [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. VS Code
   [code.visualstudio.com/Download](https://code.visualstudio.com/Download)
3. The Beken flashing tool `BKFIL`
   - Feishu guide download step for Windows:
     open [https://dl.bekencorp.com/tools/flash](https://dl.bekencorp.com/tools/flash), download `BEKEN_BKFIL_V2.1.11.15_20241114`, unzip it, and open `BKFIL`
   - For this Mac setup:
     get `BKFIL_macos_4.0.1.25123002` from the project owner, unzip it, and open `bkfil.app`

## Step 1: Download this repo

1. Open the repo website on GitHub.
2. Click the green `Code` button.
3. Copy the repo URL.
4. Open VS Code.
5. Press `Command + Shift + P`.
6. Type `Git: Clone`.
7. Paste the repo URL.
8. Choose a folder on your Mac.
9. Click `Open` when VS Code asks to open the cloned repo.

If your Mac says Git or Command Line Tools are missing:

1. Click `Install`.
2. Wait for the install to finish.
3. Open VS Code again.
4. Repeat Step 1.

## Step 2: Set up the server

1. Open the repo folder in Finder.
2. Double-click `setup_server.command`.

If macOS blocks it:

1. Right-click `setup_server.command`.
2. Click `Open`.
3. Click `Open` again.

Important:

- use Python `3.13`
- do not use Python `3.14` for this project

## Step 3: Add your API keys

1. Copy `.env.example`.
2. Rename the copy to `.env`.
3. Open `.env` in VS Code.
4. Paste this:

```env
DEEPGRAM_API_KEY=your_deepgram_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
OPENAI_API_KEY=your_openai_api_key_here
```

5. Save the file.

You can also leave the OpenAI key out of `.env` and paste it later in the control panel.
The panel can hold provider API keys in server memory until restart.

For testers:

- keep the GitHub repo version generic
- get the real API keys directly from the project owner
- get the BKFIL package directly from the project owner

## Step 4: Find your Mac IP address

1. Click the Apple menu.
2. Click `System Settings`.
3. Click `Wi-Fi`.
4. Click `Details` next to your Wi-Fi.
5. Click `TCP/IP`.
6. Find `IPv4 Address`.

Example:

- `192.168.1.23`

You need this number in Step 6.

## Step 5: Find your Wi-Fi name and password

You need:

- your Wi-Fi name
- your Wi-Fi password

Your Wi-Fi name is the one your Mac is currently connected to.

## Step 6: Build the firmware on GitHub

1. Open this repo on GitHub.
2. Click the `Actions` tab.
3. Click `Build BK7258 Firmware`.
4. Click `Run workflow`.
5. Fill in:
   - `server_ip`
     Put your Mac IP from Step 4
   - `wifi_ssid`
     Put your Wi-Fi name from Step 5
   - `wifi_password`
     Put your Wi-Fi password from Step 5
   - `disable_countdown`
     Leave it as `true`
6. Click the green `Run workflow` button.

## Step 7: Download the firmware file

1. Wait for the workflow to finish.
2. Open that workflow run.
3. Scroll down to `Artifacts`.
4. Download `bk7258-firmware`.
5. Unzip it.

The file you need is:

- `all-app.bin`

## Step 8: Flash the chip

1. Plug the chip into your Mac with USB.
2. If you are on Windows and want the same BKFIL download step shown in Feishu:
   - open [https://dl.bekencorp.com/tools/flash](https://dl.bekencorp.com/tools/flash)
   - download `BEKEN_BKFIL_V2.1.11.15_20241114`
   - unzip it
   - open `BKFIL`
3. If you are on Mac:
   - get `BKFIL_macos_4.0.1.25123002` from the project owner
   - unzip it
   - open `bkfil.app`
4. Hold `BOOT`.
5. Tap `RST` once.
6. Release `BOOT`.
7. In BKFIL:
   - choose the serial port
   - add `all-app.bin`
   - set start address to `0x0`
   - set link type to `BOOTROM`
   - set baud rate to `1500000`
   - turn `Erase before download` ON
   - turn `Reboot after download` ON
8. Click `Download`.

Expected success message:

- `Download complete, all pass.`

## Step 9: Start the server

1. Go back to the repo folder.
2. Double-click `start_server.command`.

If macOS blocks it:

1. Right-click `start_server.command`.
2. Click `Open`.
3. Click `Open` again.

Leave this window open.

You can then open the control panel on the same Wi-Fi at:

- `http://YOUR_MAC_IP:8766/`

Example:

- `http://10.0.0.62:8766/`

Phone tip:

- open that URL on your phone
- use `Add to Home Screen`
- it will behave like a lightweight app for controlling the chip

## Step 10: Power on the chip

1. Power on the chip.
2. Wait for it to connect.

Expected result:

- the server window shows a chip connection
- the control panel can show the chip connection
- the chip may stay quiet on connect because startup greeting is disabled by default for better stability

## Step 11: Talk to the chip

1. Speak to the chip normally.
2. Wait a moment.
3. The chip should answer back.

If you want to test server to chip audio first:

1. open the control panel
2. use `Send Speech`
3. the chip should speak that text out loud

If processing takes a moment:

- the chip can say `One moment.` first
- then it speaks the real reply

## Step 12: Use your phone as the remote control

1. Make sure your phone is on the same Wi-Fi as the Mac and the chip.
2. Open `http://YOUR_MAC_IP:8766/` on your phone.
3. Tap `Quick Modes` to switch the chip personality.
4. Tap `Quick Speech` to send instant test phrases.
5. Use the bottom sticky buttons to save the mode or send typed speech.
6. If you want a different LLM provider, choose the provider, fill in the model name, paste that provider API key, and tap `Save Mode`.

This is the easiest version of a phone app for the chip.

## Project structure

These are the main files:

- `README.md`
  This guide
- `wss_server.py`
  The actual voice server
- `WORKFLOW_STATUS.md`
  A short status and workflow summary
- `setup_server.command`
  Double-click this once to set up Python and install packages
- `start_server.command`
  Double-click this to start the server
- `.github/workflows/build-bk7258-firmware.yml`
  GitHub button that builds firmware with your Mac IP and Wi‑Fi
- `scripts/prepare_bk_aidk.py`
  Helper that copies the known-good BK7258 firmware overlay and then edits your server IP and Wi‑Fi before build
- `firmware/overlay/`
  The tested BK AIDK firmware files copied from the working local firmware tree
- `.env`
  Your API keys

## How everything works

This is the full runtime flow:

1. The chip connects to your Wi‑Fi.
2. The chip connects to your Mac at `ws://your-mac-ip:8765`.
3. The server answers the chip handshake.
4. The server sends startup audio to the chip.
5. The chip plays the audio.
6. You speak to the chip.
7. The chip sends microphone audio to `wss_server.py`.
8. `wss_server.py` sends that audio to Deepgram for speech-to-text.
9. The text goes to the selected LLM provider:
   - Anthropic
   - or OpenAI, if `OPENAI_API_KEY` is configured
10. The reply goes to local macOS TTS first by default, with Deepgram fallback.
11. The server sends the reply audio back to the chip.
12. The chip speaks the reply.

How the phone control works:

1. the phone opens the control panel from the Mac mini
2. the panel calls the server API on port `8766`
3. the server updates runtime config or queues speech
4. the chip stays connected to the voice server on port `8765`
5. the chip responds with the selected character and low-latency voice path

What the GitHub firmware build does:

1. takes your Mac IP
2. takes your Wi‑Fi name
3. takes your Wi‑Fi password
4. copies the tested BK7258 firmware overlay
5. puts your Mac IP into the firmware
6. puts your Wi‑Fi name and password into the firmware fallback
7. builds `all-app.bin`
8. gives you the exact file to flash

What the server needs to work:

- your Mac and chip must be on the same Wi‑Fi
- the chip firmware must contain the correct Mac IP
- `.env` must contain `DEEPGRAM_API_KEY` and `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` is only needed if you want to switch the panel to OpenAI
- `start_server.command` must be running
- the control panel is LAN-accessible by default, not public internet accessible

## If you want to force a test sentence

Open Terminal in the repo folder and run:

```bash
curl -G --data-urlencode "text=Hello this is a test from the server" http://127.0.0.1:8766/speak
```

The chip should say that sentence out loud.
