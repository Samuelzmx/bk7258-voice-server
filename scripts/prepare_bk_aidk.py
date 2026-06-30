#!/usr/bin/env python3
"""Patch a BK AIDK checkout for the BK7258 voice server workflow.

This helper updates the BK7258 firmware tree so another engineer can point the
chip at their own MacBook/server and optionally set a development fallback
Wi-Fi network.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def replace_once(path: Path, pattern: str, repl: str, *, description: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"Could not update {description} in {path}")
    path.write_text(new_text, encoding="utf-8")


def c_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Patch bk_aidk for the working BK7258 websocket server setup."
    )
    parser.add_argument(
        "--sdk",
        required=True,
        help="Path to the bk_aidk checkout, for example ~/armino/bk_aidk",
    )
    parser.add_argument(
        "--server-ip",
        required=True,
        help="LAN IP address of the computer running wss_server.py",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="WebSocket port exposed by wss_server.py (default: 8765)",
    )
    parser.add_argument(
        "--wifi-ssid",
        help="Optional development fallback SSID to bake into the firmware",
    )
    parser.add_argument(
        "--wifi-password",
        help="Optional development fallback Wi-Fi password to bake into the firmware",
    )
    parser.add_argument(
        "--disable-countdown",
        action="store_true",
        help="Disable the stock 3-minute auto-sleep countdown in beken_wss_nopsram",
    )
    args = parser.parse_args()

    if bool(args.wifi_ssid) != bool(args.wifi_password):
        parser.error("--wifi-ssid and --wifi-password must be provided together")

    sdk_root = Path(args.sdk).expanduser().resolve()
    if not sdk_root.exists():
        parser.error(f"SDK path does not exist: {sdk_root}")

    ws_file = (
        sdk_root
        / "projects/common_components/network_transfer/bk_wss/bk_wss_main.c"
    )
    smart_config_file = (
        sdk_root
        / "projects/common_components/bk_smart_config/src/core/bk_smart_config_core.c"
    )
    countdown_file = sdk_root / "projects/beken_wss_nopsram/config/bk7258/config"

    for required in (ws_file, smart_config_file):
        if not required.exists():
            parser.error(f"Required file not found: {required}")

    server_uri = f'websocket_cfg.uri = "ws://{args.server_ip}:{args.port}";'
    replace_once(
        ws_file,
        r'websocket_cfg\.uri = "(?:ws|wss)://[^"]+";',
        server_uri,
        description="WebSocket server URI",
    )

    if args.wifi_ssid and args.wifi_password:
        replace_once(
            smart_config_file,
            r'#define WSS_DEV_WIFI_SSID\s+"[^"]+"',
            f'#define WSS_DEV_WIFI_SSID             "{c_string(args.wifi_ssid)}"',
            description="fallback Wi-Fi SSID",
        )
        replace_once(
            smart_config_file,
            r'#define WSS_DEV_WIFI_PASSWORD\s+"[^"]+"',
            f'#define WSS_DEV_WIFI_PASSWORD         "{c_string(args.wifi_password)}"',
            description="fallback Wi-Fi password",
        )

    if args.disable_countdown:
        if not countdown_file.exists():
            parser.error(f"Countdown config file not found: {countdown_file}")
        replace_once(
            countdown_file,
            r"CONFIG_COUNTDOWN=y",
            "CONFIG_COUNTDOWN=n",
            description="countdown auto-sleep setting",
        )

    print("Patched BK AIDK successfully.")
    print(f"- SDK: {sdk_root}")
    print(f"- Server URI: ws://{args.server_ip}:{args.port}")
    if args.wifi_ssid:
        print(f"- Fallback Wi-Fi SSID: {args.wifi_ssid}")
    else:
        print("- Fallback Wi-Fi SSID: unchanged (use Beken App provisioning)")
    print(
        "- Auto-sleep countdown: disabled"
        if args.disable_countdown
        else "- Auto-sleep countdown: unchanged"
    )
    print("")
    print("Next step:")
    print(f"  cd {sdk_root} && make bk7258 PROJECT=beken_wss_nopsram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
