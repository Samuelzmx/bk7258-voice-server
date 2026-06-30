# bk7258-voice-server

This guide is written for someone who is **not** a programmer.

Goal:

1. start the server on a MacBook
2. build firmware with **your own** Mac IP and **your own** Wi-Fi
3. flash the chip
4. hear the chip say something
5. talk to the chip and hear it answer back

This is the working voice path:

```text
Chip mic -> Wi-Fi -> this server -> Deepgram STT -> Claude haiku-4-5 -> Deepgram TTS -> chip speaker
```

We are **not** using Agora in the final runtime.
We are **not** using Pipecat in the final runtime.

## What You Need

- 1 MacBook
- 1 BK7258 / Agora R1 board
- 1 USB cable
- 1 Wi-Fi network
- 1 iPhone or Android phone for the BekenIoT app
- 1 GitHub account with access to this repo

## What To Download

Download these first.

### On your Mac

1. Python 3.13:
   [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. VS Code:
   [code.visualstudio.com/Download](https://code.visualstudio.com/Download)
3. GitHub Desktop:
   [desktop.github.com/download](https://desktop.github.com/download/)
4. This repo:
   [github.com/Samuelzmx/bk7258-voice-server](https://github.com/Samuelzmx/bk7258-voice-server)

### On your phone

1. iPhone BekenIoT app:
   [apps.apple.com search result for BekenIoT](https://apps.apple.com/us/search?term=BekenIoT)
2. Android BekenIoT app:
   [dl.bekencorp.com/apk/BekenIot.apk](https://dl.bekencorp.com/apk/BekenIot.apk)
3. BekenIoT app download page from vendor docs:
   [docs.riselink.ai BekenIoT app download](https://docs.riselink.ai/arminodoc/bk_app/app/zh_CN/v2.0.1/app_download/index.html)

### Flash tool on your Mac

Use **BKFIL for macOS**.

If your team already has the tool package:

- open the folder named `BKFIL_macos_4.0.1.25123002`
- double-click `bkfil.app`

If you do not have BKFIL yet:

- ask your workshop organizer, teammate, or Beken contact for the macOS BKFIL package
- the exact app name we used was `BKFIL_macos_4.0.1.25123002`

## Part 1: Download This Repo To Your Mac

If you do not use Git often, use GitHub Desktop.

1. Open GitHub Desktop.
2. Click `File`.
3. Click `Clone repository`.
4. Choose this repo.
5. Pick a folder on your Mac.
6. Click `Clone`.

If you prefer download-as-ZIP:

1. Open the GitHub repo page.
2. Click the green `Code` button.
3. Click `Download ZIP`.
4. Unzip it.

## Part 2: Set Up The Server On Your Mac

### 2.1 Open the repo folder

1. Open the repo folder.
2. Double-click `setup_server.command`.

What this does:

- creates a Python environment
- installs the server packages

If macOS blocks the file:

1. right-click `setup_server.command`
2. click `Open`
3. click `Open` again

### 2.2 Put in your API keys

1. In the repo folder, copy `.env.example`
2. Rename the copy to `.env`
3. Open `.env` in VS Code
4. Paste your keys like this:

```env
DEEPGRAM_API_KEY=your_deepgram_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

Save the file.

### 2.3 Find your Mac IP address

You will need this for the chip firmware.

On macOS:

1. Click the Apple menu.
2. Click `System Settings`.
3. Click `Wi-Fi`.
4. Click `Details` next to the Wi-Fi you are using.
5. Click `TCP/IP`.
6. Find `IPv4 Address`.

Example:

- `192.168.1.23`

Write this down.

### 2.4 Find your Wi-Fi name

You will also need your Wi-Fi name.

On macOS:

1. Click the Wi-Fi icon at the top right of the screen.
2. Look at the Wi-Fi name with the check mark.

Example:

- `MyHomeWiFi`

### 2.5 Start the server

1. Double-click `start_server.command`

If macOS blocks it:

1. right-click `start_server.command`
2. click `Open`
3. click `Open` again

Leave this window open.

## Part 3: Build Firmware The Easy Way

This is the easiest path.

You do **not** need local Docker.
You do **not** need a local compiler.

The GitHub repo has a button-driven firmware builder.

### 3.1 Open the repo on GitHub

1. Open the GitHub page for this repo.
2. Click the `Actions` tab.
3. In the left menu, click `Build BK7258 Firmware`.
4. Click the `Run workflow` button.

### 3.2 Fill in the form

Type these values:

- `server_ip`
  Put the Mac IP you found in Part 2.3
  Example: `192.168.1.23`
- `wifi_ssid`
  Put your Wi-Fi name
  Example: `MyHomeWiFi`
- `wifi_password`
  Put your Wi-Fi password
- `disable_countdown`
  Leave this as `true`

Then:

1. Click the green `Run workflow` button.

### 3.3 Wait for the firmware file

1. Wait until the workflow shows a green check mark.
2. Click the workflow run.
3. Scroll to `Artifacts`.
4. Download the artifact named `bk7258-firmware`.
5. Unzip it.

Inside you will see:

- `all-app.bin`
- `all-app.bin.sha256`
- `README.txt`

The file you will flash is:

- `all-app.bin`

## Part 4: Flash The Chip

### 4.1 Connect the board

1. Plug the BK7258 board into your Mac with USB.

### 4.2 Open BKFIL

1. Open `bkfil.app`

### 4.3 Put the chip into download mode

1. Hold the `BOOT` button on the board.
2. While still holding `BOOT`, tap the `RST` button once.
3. Release `BOOT`.

### 4.4 Set BKFIL exactly like this

In BKFIL:

1. Choose the serial port
   Example: `/dev/cu.usbserial-310`
2. Add the file `all-app.bin`
3. Set start address to `0x0`
4. Set link type to `BOOTROM`
5. Set baud rate to `1500000`
6. Turn `Erase before download` ON
7. Turn `Reboot after download` ON

### 4.5 Start flashing

1. Click the `Download` button

If BKFIL says `Please reset the chip`:

1. Hold `BOOT`
2. Tap `RST`
3. Release `BOOT`

Wait for success.

Expected success message:

- `Download complete, all pass.`

## Part 5: Put The Chip On Your Wi-Fi

The firmware build step already put your Wi-Fi into the firmware as a fallback.

That means in many cases the chip can connect by itself.

If it does **not** connect by itself, use the BekenIoT app.

### 5.1 Install the BekenIoT app

Use one of these:

- iPhone:
  [apps.apple.com search result for BekenIoT](https://apps.apple.com/us/search?term=BekenIoT)
- Android:
  [dl.bekencorp.com/apk/BekenIot.apk](https://dl.bekencorp.com/apk/BekenIot.apk)

### 5.2 Enter Wi-Fi setup mode on the chip

On the stock R1 board:

1. Long-press `Key 2` for about 3 seconds

If your board labels are different, use the board button that the workshop sheet uses for pairing / network setup.

### 5.3 Use the app

In the phone app:

1. Open BekenIoT
2. Tap `Add device`
3. Let the app scan
4. Tap the device it finds
5. Choose the model if asked
6. Choose your Wi-Fi
7. Enter your Wi-Fi password
8. Tap the final confirm / add button

## Part 6: First Test

When the chip connects correctly:

1. your Mac server window should show a new connection
2. the chip should connect to `ws://<your-mac-ip>:8765`
3. the server should answer the handshake
4. the chip should play a greeting sentence

### 6.1 Send a manual speech test

If you want to force the chip to speak:

1. Open `Terminal`
2. Go into the repo folder
3. Run this exact command:

```bash
curl -G --data-urlencode "text=Hello this is a test from the server" http://127.0.0.1:8766/speak
```

Expected result:

- the chip says the sentence out loud

### 6.2 Talk to the chip

Once connected:

1. Speak to the chip normally
2. Wait a moment
3. The chip should answer back

There is no extra talk button needed in the final working setup.

## If You Want To Edit The Firmware Manually

If you do not want to use the GitHub Actions builder, these are the exact places where the IP and Wi-Fi go.

### File 1: server IP

Open this file in the BK SDK:

`projects/common_components/network_transfer/bk_wss/bk_wss_main.c`

Find this line:

```c
websocket_cfg.uri = "ws://10.0.0.62:8765";
```

Replace it with your own Mac IP.

Example:

```c
websocket_cfg.uri = "ws://192.168.1.23:8765";
```

### File 2: Wi-Fi name and password

Open this file in the BK SDK:

`projects/common_components/bk_smart_config/src/core/bk_smart_config_core.c`

Find these two lines:

```c
#define WSS_DEV_WIFI_SSID             "225"
#define WSS_DEV_WIFI_PASSWORD         "aaa654321"
```

Replace them with your own Wi-Fi.

Example:

```c
#define WSS_DEV_WIFI_SSID             "MyHomeWiFi"
#define WSS_DEV_WIFI_PASSWORD         "MyPassword123"
```

### Faster way: use the helper script

If you are okay with one command, this repo already includes a helper that edits those files for you:

```bash
python3 ./scripts/prepare_bk_aidk.py --sdk ~/armino/bk_aidk --server-ip 192.168.1.23 --wifi-ssid "MyHomeWiFi" --wifi-password "MyPassword123" --disable-countdown
```

## What This Server Actually Uses

The final working server is:

- WebSocket transport written in Python
- Deepgram STT
- Claude `claude-haiku-4-5`
- Deepgram TTS

Important:

- the chip currently works with **PCM** audio in the final path
- the server no longer requires Homebrew `opus` just to start
- `wss_server.py` is still the main file

## Trouble Checklist

### Problem: chip does not speak at all

Check these first:

1. Is `start_server.command` still running?
2. Is the Mac and chip on the same Wi-Fi?
3. Did you put the correct `server_ip` in the GitHub firmware build?
4. Did BKFIL finish with `Download complete, all pass.`?

### Problem: chip keeps reconnecting

This usually means one of these is wrong:

1. wrong Mac IP in firmware
2. wrong Wi-Fi name
3. wrong Wi-Fi password
4. Mac changed networks

### Problem: chip shuts down after a few minutes

The firmware build flow in this repo already sets `disable_countdown=true` by default.

Keep it that way.

### Problem: I want the exact file to flash

The exact file is:

- `all-app.bin`

If you built firmware locally from the BK SDK, the path is:

- `build/beken_wss_nopsram/bk7258/all-app.bin`

## One-Line Summary

If you want the shortest possible version:

1. Download this repo
2. Double-click `setup_server.command`
3. Create `.env`
4. Double-click `start_server.command`
5. On GitHub, run `Build BK7258 Firmware`
6. Type your Mac IP and Wi-Fi into the workflow form
7. Download `all-app.bin`
8. Flash it in BKFIL
9. Power on the chip
10. Talk to it
