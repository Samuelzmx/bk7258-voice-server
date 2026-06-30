# bk7258-voice-server

This is the shortest working setup.

Goal:

1. start the server on your Mac
2. build firmware with your Mac IP and your Wi-Fi
3. flash the chip
4. talk to the chip

## What you need

- 1 MacBook
- 1 BK7258 / Agora R1 board
- 1 USB cable
- 1 Wi-Fi network
- 1 GitHub account
- `DEEPGRAM_API_KEY`
- `ANTHROPIC_API_KEY`

## What to download

On your Mac, download:

1. Python 3.13
   [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. VS Code
   [code.visualstudio.com/Download](https://code.visualstudio.com/Download)
3. BKFIL for macOS
   We used `BKFIL_macos_4.0.1.25123002`

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

## Step 2: Set up the server

1. Open the repo folder in Finder.
2. Double-click `setup_server.command`.

If macOS blocks it:

1. Right-click `setup_server.command`.
2. Click `Open`.
3. Click `Open` again.

## Step 3: Add your API keys

1. Copy `.env.example`.
2. Rename the copy to `.env`.
3. Open `.env` in VS Code.
4. Paste this:

```env
DEEPGRAM_API_KEY=your_deepgram_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

5. Save the file.

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
2. Open `bkfil.app`.
3. Hold `BOOT`.
4. Tap `RST` once.
5. Release `BOOT`.
6. In BKFIL:
   - choose the serial port
   - add `all-app.bin`
   - set start address to `0x0`
   - set link type to `BOOTROM`
   - set baud rate to `1500000`
   - turn `Erase before download` ON
   - turn `Reboot after download` ON
7. Click `Download`.

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

## Step 10: Power on the chip

1. Power on the chip.
2. Wait for it to connect.

Expected result:

- the server window shows a chip connection
- the chip should speak after connecting

## Step 11: Talk to the chip

1. Speak to the chip normally.
2. Wait a moment.
3. The chip should answer back.

## Project structure

These are the main files:

- `README.md`
  This guide
- `wss_server.py`
  The actual voice server
- `setup_server.command`
  Double-click this once to set up Python and install packages
- `start_server.command`
  Double-click this to start the server
- `.github/workflows/build-bk7258-firmware.yml`
  GitHub button that builds firmware with your Mac IP and Wi‑Fi
- `scripts/prepare_bk_aidk.py`
  Helper that edits the BK firmware source before build
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
9. The text goes to Claude `claude-haiku-4-5`.
10. Claude's reply goes to Deepgram for text-to-speech.
11. The server sends the reply audio back to the chip.
12. The chip speaks the reply.

What the GitHub firmware build does:

1. takes your Mac IP
2. takes your Wi‑Fi name
3. takes your Wi‑Fi password
4. puts them into the firmware
5. builds `all-app.bin`
6. gives you the exact file to flash

What the server needs to work:

- your Mac and chip must be on the same Wi‑Fi
- the chip firmware must contain the correct Mac IP
- `.env` must contain `DEEPGRAM_API_KEY` and `ANTHROPIC_API_KEY`
- `start_server.command` must be running

## If you want to force a test sentence

Open Terminal in the repo folder and run:

```bash
curl -G --data-urlencode "text=Hello this is a test from the server" http://127.0.0.1:8766/speak
```

The chip should say that sentence out loud.
