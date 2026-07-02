#!/usr/bin/env python3
"""BK7258 raw WebSocket voice server.

This server speaks the BK7258 / Agora R1 NOPSRAM protocol directly over
asyncio TCP sockets. It performs the HTTP upgrade manually, decodes and
encodes raw WebSocket frames, accepts OPUS microphone audio from the chip,
and returns OPUS audio responses wrapped in the chip's 16-byte transport
header.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes.util
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any


def _ensure_opus_library() -> None:
    """Help opuslib find Homebrew's libopus on macOS."""
    original_find_library = ctypes.util.find_library
    opus_candidates = (
        "/opt/homebrew/lib/libopus.dylib",
        "/opt/homebrew/lib/libopus.0.dylib",
        "/usr/local/lib/libopus.dylib",
        "/usr/local/lib/libopus.0.dylib",
    )

    def patched_find_library(name: str) -> str | None:
        if name == "opus":
            for candidate in opus_candidates:
                if os.path.exists(candidate):
                    return candidate
        return original_find_library(name)

    ctypes.util.find_library = patched_find_library


_ensure_opus_library()

import opuslib
import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv(Path(__file__).with_name(".env"))

PORT = 8765
HOST = "0.0.0.0"
CHIP_ENDPOINT = os.getenv("BK7258_CHIP_ENDPOINT", "ws://10.0.0.62:8765").strip()
ADMIN_HOST = os.getenv("BK7258_ADMIN_HOST", "0.0.0.0").strip() or "0.0.0.0"
ADMIN_PORT = int(os.getenv("BK7258_ADMIN_PORT", "8766"))
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_ANTHROPIC_MODEL = os.getenv(
    "BK7258_ANTHROPIC_MODEL", "claude-haiku-4-5"
).strip() or "claude-haiku-4-5"
DEFAULT_OPENAI_MODEL = os.getenv("BK7258_OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"
DEFAULT_LLM_PROVIDER = (
    os.getenv("BK7258_LLM_PROVIDER", "anthropic").strip().lower() or "anthropic"
)
DEFAULT_TTS_BACKEND = os.getenv("BK7258_TTS_BACKEND", "auto").strip().lower() or "auto"

SYSTEM_PROMPT = (
    "You are Dawn, a friendly AI voice toy companion. Keep responses short "
    "(1-3 sentences). They will be spoken aloud."
)
GREETING_TEXT = (
    "Hello Samuel. This is the BK seven two five eight test server speaking. "
    "If you can hear this full sentence clearly, then the server to chip audio "
    "path is working."
)
LISTENING_TEXT = "Go ahead."
PROCESSING_TEXT = "One moment."
ENABLE_STARTUP_GREETING = os.getenv("BK7258_STARTUP_GREETING", "0").strip() != "0"
ENABLE_STARTUP_LISTEN_PRIME = os.getenv("BK7258_STARTUP_LISTEN_PRIME", "1").strip() != "0"
WAIT_FOR_IDLE_BEFORE_STARTUP = (
    os.getenv("BK7258_WAIT_FOR_IDLE_BEFORE_STARTUP", "1").strip() != "0"
)
ENABLE_PROCESSING_PROMPT = os.getenv("BK7258_PROCESSING_PROMPT", "1").strip() != "0"

DEEPGRAM_LISTEN_PCM_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-2&smart_format=true&encoding=linear16&sample_rate=16000&channels=1"
)
DEEPGRAM_SPEAK_URL = (
    "https://api.deepgram.com/v1/speak"
    "?model=aura-2-andromeda-en&encoding=linear16&sample_rate=24000&container=none"
)
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

INBOUND_AUDIO_RATE = 16000
INBOUND_FRAME_MS = 60
INBOUND_FRAME_SAMPLES = INBOUND_AUDIO_RATE * INBOUND_FRAME_MS // 1000
OUTBOUND_AUDIO_RATE = 24000
DEFAULT_OUTBOUND_FRAME_MS = 60
OUTBOUND_OPUS_BITRATE = 64000
MIN_PCM_BYTES = 100
BOOTSTRAP_TONE_HZ = 880.0
BOOTSTRAP_TONE_MS = DEFAULT_OUTBOUND_FRAME_MS
BOOTSTRAP_TONE_AMPLITUDE = 0.18
STARTUP_ACTION_DELAY_SEC = 0.4
MIN_RESPONSE_AUDIO_DONE_DELAY_SEC = 0.05
PROCESSING_PROMPT_DELAY_SEC = 0.35
PCM_VAD_START_THRESHOLD = 900
PCM_VAD_CONTINUE_THRESHOLD = 600
PCM_VAD_SILENCE_MS = 450
PCM_VAD_MIN_SPEECH_MS = 220
PCM_VAD_MAX_UTTERANCE_MS = 8000
PCM_IGNORE_AFTER_PLAYBACK_MS = 250

HEAD_MAGIC = 0xF0D5
HEAD_FLAGS = 0x0001
AUDIO_HEADER_STRUCT = struct.Struct("<HHIHHBxxx")
AUDIO_HEADER_SIZE = AUDIO_HEADER_STRUCT.size
PROMPT_PCM_CACHE: dict[str, bytes] = {}
CHARACTER_PRESETS: dict[str, str] = {
    "companion": (
        "You are Dawn, a friendly AI voice toy companion. Keep responses short, warm, playful, "
        "and easy to understand when spoken aloud."
    ),
    "storyteller": (
        "You are Dawn the storyteller. Tell child-friendly stories with vivid but simple language, "
        "clear structure, and gentle energy. Keep spoken replies concise unless the user asks for a longer story."
    ),
    "language_teacher": (
        "You are Dawn the language teacher. Speak clearly, teach with short examples, gently correct mistakes, "
        "and encourage the learner. Keep spoken replies compact and practical."
    ),
    "curious_friend": (
        "You are Dawn the curious friend. Be upbeat, ask engaging follow-up questions, and celebrate curiosity "
        "without talking for too long."
    ),
    "bedtime_guide": (
        "You are Dawn the bedtime guide. Speak softly, calmly, and reassuringly, with cozy wording and very brief replies."
    ),
}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DEEPGRAM_API_KEY = require_env("DEEPGRAM_API_KEY")
ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()


def make_session_encoder() -> opuslib.Encoder:
    """Build a per-session 24 kHz mono Opus encoder for outbound audio."""
    return opuslib.Encoder(OUTBOUND_AUDIO_RATE, 1, opuslib.APPLICATION_VOIP)


@dataclass(slots=True)
class RuntimeConfig:
    llm_provider: str = DEFAULT_LLM_PROVIDER
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    openai_model: str = DEFAULT_OPENAI_MODEL
    tts_backend: str = DEFAULT_TTS_BACKEND
    character_preset: str = "companion"
    system_prompt: str = ""
    startup_greeting_enabled: bool = ENABLE_STARTUP_GREETING
    startup_listen_prime_enabled: bool = ENABLE_STARTUP_LISTEN_PRIME
    wait_for_idle_before_startup: bool = WAIT_FOR_IDLE_BEFORE_STARTUP
    processing_prompt_enabled: bool = ENABLE_PROCESSING_PROMPT
    processing_prompt_text: str = PROCESSING_TEXT


@dataclass(slots=True)
class Session:
    writer: asyncio.StreamWriter
    peer: str
    device_id: str = ""
    input_audio_format: str = "opus"
    input_audio_rate: int = INBOUND_AUDIO_RATE
    input_audio_duration_ms: int = INBOUND_FRAME_MS
    output_audio_format: str = "opus"
    output_audio_rate: int = OUTBOUND_AUDIO_RATE
    output_audio_duration_ms: int = DEFAULT_OUTBOUND_FRAME_MS
    seq: int = 0
    encoder: opuslib.Encoder = field(default_factory=make_session_encoder)
    audio_buf: bytearray = field(default_factory=bytearray)
    audio_packets: list[bytes] = field(default_factory=list)
    committed_audio: bytes = b""
    committed_packets: list[bytes] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    greeted: bool = False
    handshook: bool = False
    greeting_scheduled: bool = False
    idle_announced: bool = False
    ignore_input_until: float = 0.0
    speech_detected: bool = False
    speech_ms: float = 0.0
    silence_ms: float = 0.0
    last_activity: float = field(default_factory=time.time)
    connected_at: float = field(default_factory=time.time)
    last_commit_at: float = 0.0
    last_turn_metrics: dict[str, Any] = field(default_factory=dict)
    turn_counter: int = 0
    active_turn_id: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    response_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pipeline_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    closing: bool = False


ACTIVE_SESSIONS: dict[str, Session] = {}
RUNTIME_CONFIG = RuntimeConfig()


def llm_provider_available(provider: str) -> bool:
    normalized = provider.strip().lower()
    if normalized == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if normalized == "openai":
        return bool(OPENAI_API_KEY)
    return False


def effective_system_prompt() -> str:
    preset_name = RUNTIME_CONFIG.character_preset
    preset_prompt = CHARACTER_PRESETS.get(preset_name, CHARACTER_PRESETS["companion"])
    custom_prompt = RUNTIME_CONFIG.system_prompt.strip()
    if custom_prompt:
        return f"{preset_prompt}\n\nAdditional instructions:\n{custom_prompt}"
    return preset_prompt


def config_public_dict() -> dict[str, Any]:
    return {
        "llm_provider": RUNTIME_CONFIG.llm_provider,
        "anthropic_model": RUNTIME_CONFIG.anthropic_model,
        "openai_model": RUNTIME_CONFIG.openai_model,
        "tts_backend": RUNTIME_CONFIG.tts_backend,
        "system_prompt": RUNTIME_CONFIG.system_prompt,
        "effective_system_prompt": effective_system_prompt(),
        "character_preset": RUNTIME_CONFIG.character_preset,
        "character_presets": CHARACTER_PRESETS,
        "provider_availability": {
            "anthropic": llm_provider_available("anthropic"),
            "openai": llm_provider_available("openai"),
        },
        "startup_greeting_enabled": RUNTIME_CONFIG.startup_greeting_enabled,
        "startup_listen_prime_enabled": RUNTIME_CONFIG.startup_listen_prime_enabled,
        "wait_for_idle_before_startup": RUNTIME_CONFIG.wait_for_idle_before_startup,
        "processing_prompt_enabled": RUNTIME_CONFIG.processing_prompt_enabled,
        "processing_prompt_text": RUNTIME_CONFIG.processing_prompt_text,
    }


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def apply_runtime_config(update: dict[str, Any]) -> dict[str, Any]:
    if "llm_provider" in update:
        value = str(update["llm_provider"]).strip().lower()
        if value in {"anthropic", "openai"}:
            RUNTIME_CONFIG.llm_provider = value
    if "anthropic_model" in update:
        value = str(update["anthropic_model"]).strip()
        if value:
            RUNTIME_CONFIG.anthropic_model = value
    if "openai_model" in update:
        value = str(update["openai_model"]).strip()
        if value:
            RUNTIME_CONFIG.openai_model = value
    if "system_prompt" in update:
        value = str(update["system_prompt"]).strip()
        RUNTIME_CONFIG.system_prompt = value
    if "character_preset" in update:
        value = str(update["character_preset"]).strip()
        if value in CHARACTER_PRESETS:
            RUNTIME_CONFIG.character_preset = value
    if "tts_backend" in update:
        value = str(update["tts_backend"]).strip().lower()
        if value in {"auto", "local", "deepgram"}:
            RUNTIME_CONFIG.tts_backend = value
    if "startup_greeting_enabled" in update:
        RUNTIME_CONFIG.startup_greeting_enabled = parse_bool(
            update["startup_greeting_enabled"],
            RUNTIME_CONFIG.startup_greeting_enabled,
        )
    if "startup_listen_prime_enabled" in update:
        RUNTIME_CONFIG.startup_listen_prime_enabled = parse_bool(
            update["startup_listen_prime_enabled"],
            RUNTIME_CONFIG.startup_listen_prime_enabled,
        )
    if "wait_for_idle_before_startup" in update:
        RUNTIME_CONFIG.wait_for_idle_before_startup = parse_bool(
            update["wait_for_idle_before_startup"],
            RUNTIME_CONFIG.wait_for_idle_before_startup,
        )
    if "processing_prompt_enabled" in update:
        RUNTIME_CONFIG.processing_prompt_enabled = parse_bool(
            update["processing_prompt_enabled"],
            RUNTIME_CONFIG.processing_prompt_enabled,
        )
    if "processing_prompt_text" in update:
        value = str(update["processing_prompt_text"]).strip()
        if value:
            RUNTIME_CONFIG.processing_prompt_text = value
            PROMPT_PCM_CACHE.pop("__processing_prompt__", None)

    return config_public_dict()


def next_turn_id(session: Session) -> int:
    session.turn_counter += 1
    return session.turn_counter


def get_preferred_session(device_id: str = "") -> Session | None:
    if device_id:
        session = ACTIVE_SESSIONS.get(device_id)
        if session and session.handshook and not session.closing:
            return session
        return None

    candidates = [
        session
        for session in ACTIVE_SESSIONS.values()
        if session.handshook and not session.closing
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.last_activity)


def ws_accept_key(key: str) -> str:
    digest = hashlib.sha1((key.strip() + WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_encode_frame(opcode: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    if length < 126:
        header = bytes([(0x80 | opcode), length])
    elif length < 65536:
        header = bytes([(0x80 | opcode), 126]) + struct.pack(">H", length)
    else:
        header = bytes([(0x80 | opcode), 127]) + struct.pack(">Q", length)
    return header + payload


def ws_encode_text(text: str) -> bytes:
    return ws_encode_frame(0x1, text.encode("utf-8"))


def ws_encode_binary(data: bytes) -> bytes:
    return ws_encode_frame(0x2, data)


def ws_encode_ping() -> bytes:
    return ws_encode_frame(0x9)


def ws_encode_pong(payload: bytes = b"") -> bytes:
    return ws_encode_frame(0xA, payload)


def ws_encode_close() -> bytes:
    return ws_encode_frame(0x8)


def ws_decode_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode complete frames from *buf* and return leftover bytes."""
    frames: list[tuple[int, bytes]] = []
    while len(buf) >= 2:
        opcode = buf[0] & 0x0F
        masked = (buf[1] >> 7) & 0x01
        length = buf[1] & 0x7F
        offset = 2

        if length == 126:
            if len(buf) < 4:
                break
            length = struct.unpack(">H", buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                break
            length = struct.unpack(">Q", buf[2:10])[0]
            offset = 10

        if masked:
            if len(buf) < offset + 4:
                break
            mask = buf[offset : offset + 4]
            offset += 4
        else:
            mask = None

        if len(buf) < offset + length:
            break

        payload = buf[offset : offset + length]
        if mask is not None:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        frames.append((opcode, payload))
        buf = buf[offset + length :]

    return frames, buf


def make_audio_header(seq: int, payload: bytes) -> bytes:
    timestamp_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
    payload_len = len(payload)
    return AUDIO_HEADER_STRUCT.pack(
        HEAD_MAGIC,
        HEAD_FLAGS,
        timestamp_ms,
        seq,
        payload_len,
        0,
    )


def extract_audio_payload(packet: bytes) -> bytes | None:
    """Return the audio payload, stripping the chip transport header if present."""
    if len(packet) >= AUDIO_HEADER_SIZE:
        magic, flags, timestamp_ms, seq, payload_len, crc = AUDIO_HEADER_STRUCT.unpack(
            packet[:AUDIO_HEADER_SIZE]
        )
        if magic == HEAD_MAGIC and flags == HEAD_FLAGS:
            payload = packet[AUDIO_HEADER_SIZE:]
            if payload_len > len(payload):
                logger.warning(
                    "dropping truncated audio packet: seq={} ts={} declared={} actual={}",
                    seq,
                    timestamp_ms,
                    payload_len,
                    len(payload),
                )
                return None
            if payload_len != len(payload):
                logger.debug(
                    "audio packet had {} trailing bytes after declared payload {}",
                    len(payload) - payload_len,
                    payload_len,
                )
            return payload[:payload_len]

    return packet


def normalize_opus_frame_ms(requested_ms: int) -> int:
    supported = (10, 20, 40, 60)
    if requested_ms in supported:
        return requested_ms
    if requested_ms <= 0:
        return DEFAULT_OUTBOUND_FRAME_MS
    return min(supported, key=lambda candidate: abs(candidate - requested_ms))


def get_session_outbound_frame_ms(session: Session) -> int:
    # The chip only advertises the input frame duration, but the playback side
    # drains packets on that same cadence. Matching it prevents underflow.
    return normalize_opus_frame_ms(session.output_audio_duration_ms)


def normalize_audio_format(value: str) -> str:
    return value.strip().lower()


def session_uses_pcm_input(session: Session) -> bool:
    return normalize_audio_format(session.input_audio_format) == "pcm"


def session_uses_pcm_output(session: Session) -> bool:
    return normalize_audio_format(session.output_audio_format) == "pcm"


def resample_pcm_mono_s16le(
    pcm_bytes: bytes, source_rate: int, target_rate: int
) -> bytes:
    """Resample mono s16le PCM with linear interpolation."""
    if not pcm_bytes or source_rate <= 0 or target_rate <= 0:
        return pcm_bytes
    if source_rate == target_rate:
        return pcm_bytes

    source_samples = len(pcm_bytes) // 2
    if source_samples <= 1:
        return pcm_bytes

    samples = memoryview(pcm_bytes).cast("h")
    target_samples = max(1, round(source_samples * target_rate / source_rate))
    out = bytearray(target_samples * 2)

    for index in range(target_samples):
        src_pos = index * (source_samples - 1) / max(target_samples - 1, 1)
        left_index = int(src_pos)
        right_index = min(left_index + 1, source_samples - 1)
        frac = src_pos - left_index
        left = int(samples[left_index])
        right = int(samples[right_index])
        sample = int(round(left + (right - left) * frac))
        struct.pack_into("<h", out, index * 2, sample)

    return bytes(out)


def estimate_pcm_level(pcm_bytes: bytes) -> float:
    if len(pcm_bytes) < 2:
        return 0.0
    samples = memoryview(pcm_bytes).cast("h")
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += abs(int(sample))
    return total / len(samples)


def get_pcm_packet_duration_ms(session: Session, payload: bytes) -> float:
    rate = session.input_audio_rate or INBOUND_AUDIO_RATE
    if not payload:
        return 0.0
    return (len(payload) / 2) * 1000.0 / rate


def reset_server_vad(session: Session) -> None:
    session.speech_detected = False
    session.speech_ms = 0.0
    session.silence_ms = 0.0


async def commit_current_utterance(session: Session, reason: str) -> None:
    if session.closing or not session.audio_packets:
        return
    session.last_commit_at = time.perf_counter()
    session.committed_audio = bytes(session.audio_buf)
    session.committed_packets = list(session.audio_packets)
    session.audio_buf.clear()
    session.audio_packets.clear()
    reset_server_vad(session)
    logger.info(
        "[{}] server-side commit: {} bytes across {} packets ({})",
        session.peer,
        len(session.committed_audio),
        len(session.committed_packets),
        reason,
    )
    packets_copy = list(session.committed_packets)
    audio_copy = session.committed_audio
    session.committed_packets.clear()
    session.committed_audio = b""
    await send_json(session, {"type": "input_audio_buffer.committed"})
    spawn_session_task(session, run_pipeline_with_audio(session, packets_copy, audio_copy, next_turn_id(session)))


async def maybe_auto_commit_pcm(session: Session, payload: bytes) -> None:
    if not session_uses_pcm_input(session):
        return
    now = time.time()
    if now < session.ignore_input_until:
        session.audio_buf.clear()
        session.audio_packets.clear()
        reset_server_vad(session)
        return

    packet_ms = get_pcm_packet_duration_ms(session, payload)
    if packet_ms <= 0.0:
        return

    level = estimate_pcm_level(payload)
    threshold = (
        PCM_VAD_CONTINUE_THRESHOLD if session.speech_detected else PCM_VAD_START_THRESHOLD
    )

    if level >= threshold:
        session.speech_detected = True
        session.speech_ms += packet_ms
        session.silence_ms = 0.0
    elif session.speech_detected:
        session.silence_ms += packet_ms

    if (
        session.speech_detected
        and session.speech_ms >= PCM_VAD_MIN_SPEECH_MS
        and session.silence_ms >= PCM_VAD_SILENCE_MS
    ):
        await commit_current_utterance(session, "vad silence")
        return

    if session.speech_detected and session.speech_ms >= PCM_VAD_MAX_UTTERANCE_MS:
        await commit_current_utterance(session, "vad max utterance")


def pcm_to_transport_frames(
    pcm_bytes: bytes,
    *,
    start_seq: int,
    sample_rate: int,
    frame_ms: int,
) -> tuple[list[bytes], int]:
    """Chunk linear16 PCM into framed transport packets for the chip."""
    frames: list[bytes] = []
    seq = start_seq
    frame_samples = sample_rate * frame_ms // 1000
    frame_bytes = max(2, frame_samples * 2)

    for offset in range(0, len(pcm_bytes), frame_bytes):
        chunk = pcm_bytes[offset : offset + frame_bytes]
        if not chunk:
            continue
        chunk = chunk.ljust(frame_bytes, b"\x00")
        frames.append(make_audio_header(seq, chunk) + chunk)
        seq = (seq + 1) & 0xFFFF

    return frames, seq


def pcm_to_opus_frames(
    pcm_bytes: bytes, encoder: opuslib.Encoder, start_seq: int, frame_ms: int
) -> tuple[list[bytes], int]:
    """Convert 24 kHz mono PCM into OPUS frames with chip transport headers."""
    frames: list[bytes] = []
    seq = start_seq
    frame_samples = OUTBOUND_AUDIO_RATE * frame_ms // 1000
    frame_bytes = frame_samples * 2

    for offset in range(0, len(pcm_bytes), frame_bytes):
        chunk = pcm_bytes[offset : offset + frame_bytes]
        chunk = chunk.ljust(frame_bytes, b"\x00")
        opus_payload = encoder.encode(chunk, frame_samples)
        frames.append(make_audio_header(seq, opus_payload) + opus_payload)
        seq = (seq + 1) & 0xFFFF

    return frames, seq


def parse_ogg_opus_packets(ogg_bytes: bytes) -> list[bytes]:
    """Extract Opus packets from an Ogg Opus stream."""
    packets: list[bytes] = []
    partial = bytearray()
    offset = 0

    while offset < len(ogg_bytes):
        if ogg_bytes[offset : offset + 4] != b"OggS":
            raise ValueError(f"invalid Ogg capture pattern at byte {offset}")
        if offset + 27 > len(ogg_bytes):
            raise ValueError("truncated Ogg page header")

        page_segments = ogg_bytes[offset + 26]
        segment_table_start = offset + 27
        segment_table_end = segment_table_start + page_segments
        if segment_table_end > len(ogg_bytes):
            raise ValueError("truncated Ogg segment table")

        lacing_values = ogg_bytes[segment_table_start:segment_table_end]
        payload_start = segment_table_end
        payload_end = payload_start + sum(lacing_values)
        if payload_end > len(ogg_bytes):
            raise ValueError("truncated Ogg page payload")

        payload = memoryview(ogg_bytes)[payload_start:payload_end]
        payload_offset = 0
        for lace in lacing_values:
            partial.extend(payload[payload_offset : payload_offset + lace])
            payload_offset += lace
            if lace < 255:
                packet = bytes(partial)
                if not (
                    packet.startswith(b"OpusHead") or packet.startswith(b"OpusTags")
                ):
                    packets.append(packet)
                partial.clear()

        offset = payload_end

    if partial:
        raise ValueError("unterminated Ogg Opus packet")

    return packets


def pcm_to_opus_frames_ffmpeg(
    pcm_bytes: bytes, start_seq: int, frame_ms: int
) -> tuple[list[bytes], int] | None:
    """Encode 24 kHz mono PCM with libopus and extract raw packets from Ogg."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return None

    cmd = [
        ffmpeg_path,
        "-v",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(OUTBOUND_AUDIO_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-application",
        "voip",
        "-frame_duration",
        str(frame_ms),
        "-b:a",
        "24k",
        "-vbr",
        "on",
        "-compression_level",
        "10",
        "-f",
        "ogg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            input=pcm_bytes,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg Opus encode failed to start: {}", exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "ffmpeg Opus encode failed: {}",
            result.stderr.decode("utf-8", "ignore").strip(),
        )
        return None

    try:
        opus_packets = parse_ogg_opus_packets(result.stdout)
    except ValueError as exc:
        logger.warning("failed to parse ffmpeg Ogg Opus output: {}", exc)
        return None

    frames: list[bytes] = []
    seq = start_seq
    for opus_payload in opus_packets:
        frames.append(make_audio_header(seq, opus_payload) + opus_payload)
        seq = (seq + 1) & 0xFFFF

    return frames, seq


def looks_like_raw_pcm(pcm_bytes: bytes, content_type: str) -> bool:
    """Best-effort guard against JSON / HTML error bodies from TTS."""
    if len(pcm_bytes) < MIN_PCM_BYTES or len(pcm_bytes) % 2:
        return False

    lowered_type = content_type.lower()
    if lowered_type.startswith("audio/"):
        return True

    if (
        lowered_type.startswith("text/")
        or "json" in lowered_type
        or "xml" in lowered_type
        or "html" in lowered_type
    ):
        return False

    prefix = pcm_bytes[:16].lstrip()
    bad_prefixes = (
        b"{\"",
        b"{\n",
        b"[{",
        b"<!DOCTYPE",
        b"<html",
        b"<?xml",
        b"RIFF",
        b"OggS",
    )
    return not any(prefix.startswith(bad) for bad in bad_prefixes)


def decode_opus_packets_to_pcm(opus_packets: list[bytes]) -> bytes:
    """Decode packetized 16 kHz mono OPUS into linear16 PCM."""
    decoder = opuslib.Decoder(INBOUND_AUDIO_RATE, 1)
    pcm = bytearray()

    for packet in opus_packets:
        if not packet:
            continue
        try:
            pcm.extend(decoder.decode(packet, INBOUND_FRAME_SAMPLES, False))
        except Exception as exc:
            logger.warning("OPUS decode failed for packet len {}: {}", len(packet), exc)

    return bytes(pcm)


def transcribe_pcm(pcm_bytes: bytes) -> str:
    """Send raw 16 kHz linear16 PCM to Deepgram STT."""
    if len(pcm_bytes) < MIN_PCM_BYTES:
        return ""

    try:
        response = requests.post(
            DEEPGRAM_LISTEN_PCM_URL,
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "application/octet-stream",
            },
            data=pcm_bytes,
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.exception("Deepgram STT request failed: {}", exc)
        return ""

    if not response.ok:
        logger.warning(
            "Deepgram STT returned {}: {}",
            response.status_code,
            response.text[:200],
        )
        return ""

    payload = response.json()
    return (
        payload.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
        .strip()
    )


def ask_anthropic(user_text: str, history: list[dict[str, str]]) -> str:
    """Generate a short assistant reply with Anthropic."""
    try:
        response = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": RUNTIME_CONFIG.anthropic_model,
                "max_tokens": 200,
                "system": effective_system_prompt(),
                "messages": history + [{"role": "user", "content": user_text}],
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.exception("Anthropic request failed: {}", exc)
        return "Sorry, I had a problem. Try again."

    if not response.ok:
        logger.warning(
            "Anthropic returned {}: {}",
            response.status_code,
            response.text[:200],
        )
        return "Sorry, I had a problem. Try again."

    payload = response.json()
    try:
        return payload["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("Anthropic response shape was unexpected: {}", payload)
        return "Sorry, I had a problem. Try again."


def build_openai_messages(
    user_text: str,
    history: list[dict[str, str]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": effective_system_prompt()}],
        }
    ]
    for item in history:
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        messages.append(
            {
                "role": role,
                "content": [{"type": "input_text", "text": content}],
            }
        )
    messages.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_text}],
        }
    )
    return messages


def extract_openai_output_text(payload: dict[str, Any]) -> str:
    direct = str(payload.get("output_text", "")).strip()
    if direct:
        return direct

    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = str(content.get("text", "")).strip()
            if text:
                return text
    return ""


def ask_openai(user_text: str, history: list[dict[str, str]]) -> str:
    """Generate a short assistant reply with OpenAI Responses API."""
    if not OPENAI_API_KEY:
        return "OpenAI API key is not configured on this server."

    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": RUNTIME_CONFIG.openai_model,
                "input": build_openai_messages(user_text, history),
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.exception("OpenAI request failed: {}", exc)
        return "Sorry, I had a problem. Try again."

    if not response.ok:
        logger.warning(
            "OpenAI returned {}: {}",
            response.status_code,
            response.text[:200],
        )
        return "Sorry, I had a problem. Try again."

    payload = response.json()
    text = extract_openai_output_text(payload)
    if text:
        return text
    logger.warning("OpenAI response shape was unexpected: {}", payload)
    return "Sorry, I had a problem. Try again."


def ask_llm(user_text: str, history: list[dict[str, str]]) -> str:
    provider = RUNTIME_CONFIG.llm_provider
    if provider == "openai":
        return ask_openai(user_text, history)
    return ask_anthropic(user_text, history)


def synthesize_speech_deepgram(text: str) -> bytes | None:
    """Return raw 24 kHz linear16 PCM from Deepgram TTS."""
    try:
        response = requests.post(
            DEEPGRAM_SPEAK_URL,
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"text": text},
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.exception("Deepgram TTS request failed: {}", exc)
        return None

    if not response.ok:
        logger.warning(
            "Deepgram TTS returned {}: {}",
            response.status_code,
            response.text[:200],
        )
        return None

    pcm = response.content
    content_type = response.headers.get("Content-Type", "")
    if not looks_like_raw_pcm(pcm, content_type):
        logger.error(
            "Deepgram TTS returned non-PCM body: content-type={!r} len={} preview={!r}",
            content_type,
            len(pcm),
            pcm[:80],
        )
        return None

    return pcm


def synthesize_speech_locally(text: str) -> bytes | None:
    """Fallback local TTS for startup prompts when remote TTS is unavailable."""
    say_path = shutil.which("say")
    ffmpeg_path = shutil.which("ffmpeg")
    if not say_path or not ffmpeg_path:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="bk7258-tts-") as tmpdir:
            aiff_path = Path(tmpdir) / "prompt.aiff"
            say_result = subprocess.run(
                [say_path, "-v", "Samantha", "-r", "220", "-o", str(aiff_path), text],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if say_result.returncode != 0 or not aiff_path.exists():
                logger.warning(
                    "local say synthesis failed for {!r}: {}",
                    text,
                    say_result.stderr.strip() or say_result.stdout.strip(),
                )
                return None

            ffmpeg_result = subprocess.run(
                [
                    ffmpeg_path,
                    "-v",
                    "error",
                    "-i",
                    str(aiff_path),
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    str(OUTBOUND_AUDIO_RATE),
                    "-ac",
                    "1",
                    "-",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if ffmpeg_result.returncode != 0:
                logger.warning(
                    "local ffmpeg conversion failed for {!r}: {}",
                    text,
                    ffmpeg_result.stderr.decode("utf-8", "ignore").strip(),
                )
                return None

            pcm = ffmpeg_result.stdout
            if not looks_like_raw_pcm(pcm, "audio/raw"):
                logger.warning(
                    "local TTS produced invalid PCM for {!r}: len={}",
                    text,
                    len(pcm),
                )
                return None
            return pcm
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("local TTS fallback failed for {!r}: {}", text, exc)
        return None


def synthesize_speech(text: str) -> bytes | None:
    """Return 24 kHz linear16 PCM using the configured TTS backend."""
    backend = RUNTIME_CONFIG.tts_backend
    if backend in {"auto", "local"}:
        pcm = synthesize_speech_locally(text)
        if pcm:
            return pcm
        if backend == "local":
            logger.warning("local TTS unavailable, falling back to Deepgram")
    return synthesize_speech_deepgram(text)


def generate_tone_pcm(
    *, frequency_hz: float, duration_ms: int, amplitude: float
) -> bytes:
    total_samples = OUTBOUND_AUDIO_RATE * duration_ms // 1000
    pcm = bytearray()
    for index in range(total_samples):
        sample = int(
            max(-1.0, min(1.0, math.sin(2.0 * math.pi * frequency_hz * index / OUTBOUND_AUDIO_RATE)))
            * amplitude
            * 32767
        )
        pcm.extend(struct.pack("<h", sample))
    return bytes(pcm)


def get_prompt_pcm(text: str) -> bytes:
    cached = PROMPT_PCM_CACHE.get(text)
    if cached:
        return cached

    pcm = synthesize_speech(text)
    if not pcm:
        pcm = synthesize_speech_locally(text)
    if not pcm:
        logger.warning("falling back to generated tone for prompt {!r}", text)
        pcm = generate_tone_pcm(
            frequency_hz=BOOTSTRAP_TONE_HZ,
            duration_ms=max(180, BOOTSTRAP_TONE_MS),
            amplitude=BOOTSTRAP_TONE_AMPLITUDE,
        )

    PROMPT_PCM_CACHE[text] = pcm
    return pcm


def get_greeting_pcm() -> bytes:
    """Use a compact local prompt for the startup greeting when available."""
    cache_key = "__startup_greeting__"
    cached = PROMPT_PCM_CACHE.get(cache_key)
    if cached:
        return cached

    pcm = synthesize_speech_locally(GREETING_TEXT)
    if not pcm:
        pcm = get_prompt_pcm(GREETING_TEXT)
    PROMPT_PCM_CACHE[cache_key] = pcm
    return pcm


def get_processing_prompt_pcm() -> bytes:
    cache_key = "__processing_prompt__"
    cached = PROMPT_PCM_CACHE.get(cache_key)
    if cached:
        return cached

    prompt_text = RUNTIME_CONFIG.processing_prompt_text
    pcm = synthesize_speech_locally(prompt_text)
    if not pcm:
        pcm = generate_tone_pcm(
            frequency_hz=BOOTSTRAP_TONE_HZ,
            duration_ms=180,
            amplitude=BOOTSTRAP_TONE_AMPLITUDE,
        )
    PROMPT_PCM_CACHE[cache_key] = pcm
    return pcm


async def send_raw(session: Session, payload: bytes) -> None:
    async with session.send_lock:
        if session.closing:
            return
        session.writer.write(payload)
        await session.writer.drain()


async def send_json(session: Session, message: dict[str, Any]) -> None:
    msg_type = str(message.get("type", "<missing>"))
    logger.info("[{}] → {}", session.peer, msg_type)
    await send_raw(session, ws_encode_text(json.dumps(message)))


def spawn_session_task(
    session: Session, coro: Coroutine[Any, Any, Any]
) -> None:
    task = asyncio.create_task(coro)
    session.tasks.add(task)
    task.add_done_callback(session.tasks.discard)


async def session_keepalive_loop(session: Session) -> None:
    while not session.closing:
        await asyncio.sleep(20.0)
        if session.closing:
            return
        await send_raw(session, ws_encode_ping())


def schedule_startup_action(session: Session, *, delay_sec: float, reason: str) -> None:
    if (
        not RUNTIME_CONFIG.startup_greeting_enabled
        and not RUNTIME_CONFIG.startup_listen_prime_enabled
    ):
        return
    if session.greeted or session.greeting_scheduled or session.closing:
        return
    if RUNTIME_CONFIG.wait_for_idle_before_startup and not session.idle_announced:
        return
    session.greeting_scheduled = True
    spawn_session_task(
        session,
        maybe_run_startup_action(session, delay_sec=delay_sec, reason=reason),
    )


async def send_audio_response_locked(session: Session, text: str) -> None:
    await send_pcm_response_locked(session, text, None)


async def send_response_created(
    session: Session,
    *,
    text: str,
    audio_frame_count: int,
    audio_frame_ms: int,
    playback_duration_ms: int,
) -> None:
    await send_json(
        session,
        {
            "type": "response.created",
            "user_text": text,
        },
    )


async def send_pcm_response_locked(
    session: Session,
    text: str,
    pcm: bytes | None,
    *,
    done_delay_sec: float = 0.0,
) -> None:
    """Run TTS, frame audio for the active session, and stream it back.

    Callers must hold session.response_lock.
    """
    if session.closing:
        return

    if pcm is None:
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(None, synthesize_speech, text)
    if not pcm:
        logger.error("[{}] TTS failed for reply: {}", session.peer, text)
        return

    frame_ms = get_session_outbound_frame_ms(session)
    pacing_sec = frame_ms / 1000.0

    if session_uses_pcm_output(session):
        output_rate = session.output_audio_rate or INBOUND_AUDIO_RATE
        pcm_for_chip = resample_pcm_mono_s16le(pcm, OUTBOUND_AUDIO_RATE, output_rate)
        frames, next_seq = pcm_to_transport_frames(
            pcm_for_chip,
            start_seq=session.seq,
            sample_rate=output_rate,
            frame_ms=frame_ms,
        )
        frame_kind = f"PCM {output_rate}Hz"
    else:
        encoded = pcm_to_opus_frames(pcm, session.encoder, session.seq, frame_ms)
        if not encoded[0]:
            logger.warning("[{}] opuslib produced no frames; trying ffmpeg", session.peer)
            fallback = pcm_to_opus_frames_ffmpeg(pcm, session.seq, frame_ms)
            if fallback is None:
                logger.error("[{}] no OPUS frames available for reply", session.peer)
                return
            encoded = fallback
        frames, next_seq = encoded
        frame_kind = "OPUS"

    if not frames:
        logger.error("[{}] no {} frames available for reply", session.peer, frame_kind)
        return

    session.seq = next_seq
    playback_duration_ms = len(frames) * frame_ms
    session.ignore_input_until = (
        time.time() + (playback_duration_ms + PCM_IGNORE_AFTER_PLAYBACK_MS) / 1000.0
    )

    logger.info(
        "[{}] → response.created + {} {} frames @ {}ms",
        session.peer,
        len(frames),
        frame_kind,
        frame_ms,
    )
    await send_response_created(
        session,
        text=text,
        audio_frame_count=len(frames),
        audio_frame_ms=frame_ms,
        playback_duration_ms=playback_duration_ms,
    )

    for frame in frames:
        await send_raw(session, ws_encode_binary(frame))
        await asyncio.sleep(pacing_sec)

    await asyncio.sleep(max(done_delay_sec, MIN_RESPONSE_AUDIO_DONE_DELAY_SEC))
    await send_json(session, {"type": "response.audio.done"})
    logger.info("[{}] → response.audio.done", session.peer)


async def send_greeting_response(session: Session) -> None:
    async with session.response_lock:
        if session.closing:
            return
        greeting_pcm = get_greeting_pcm()
        logger.info("[{}] sending cached greeting audio", session.peer)
        await send_pcm_response_locked(
            session,
            GREETING_TEXT,
            greeting_pcm,
            done_delay_sec=0.0,
        )


async def send_startup_listening_prime(session: Session) -> None:
    async with session.response_lock:
        if session.closing:
            return
        await send_response_created(
            session,
            text="",
            audio_frame_count=0,
            audio_frame_ms=get_session_outbound_frame_ms(session),
            playback_duration_ms=0,
        )
        logger.info("[{}] → response.created (startup listen prime)", session.peer)
        await asyncio.sleep(0.05)
        await send_json(session, {"type": "response.audio.done"})
        logger.info("[{}] → response.audio.done (startup listen prime)", session.peer)


async def maybe_run_startup_action(
    session: Session, *, delay_sec: float, reason: str
) -> None:
    try:
        await asyncio.sleep(delay_sec)
        if (
            session.greeted
            or session.closing
            or not session.handshook
            or session.audio_packets
            or session.committed_packets
            or session.committed_audio
        ):
            return
        session.greeted = True
        if RUNTIME_CONFIG.startup_greeting_enabled:
            logger.info("[{}] starting greeting after {}", session.peer, reason)
            await send_greeting_response(session)
            return
        if RUNTIME_CONFIG.startup_listen_prime_enabled:
            logger.info("[{}] priming hands-free listening after {}", session.peer, reason)
            await send_startup_listening_prime(session)
            return
    finally:
        session.greeting_scheduled = False


async def send_audio_response(session: Session, text: str) -> None:
    async with session.response_lock:
        await send_audio_response_locked(session, text)


async def maybe_send_processing_prompt(
    session: Session, turn_id: int, processing_started_at: float
) -> None:
    await asyncio.sleep(PROCESSING_PROMPT_DELAY_SEC)
    if (
        session.closing
        or not RUNTIME_CONFIG.processing_prompt_enabled
        or session.active_turn_id != turn_id
    ):
        return

    async with session.response_lock:
        if session.closing or session.active_turn_id != turn_id:
            return
        logger.info(
            "[{}] turn {} sending processing prompt after {:.0f}ms",
            session.peer,
            turn_id,
            (time.perf_counter() - processing_started_at) * 1000.0,
        )
        await send_pcm_response_locked(
            session,
            RUNTIME_CONFIG.processing_prompt_text,
            get_processing_prompt_pcm(),
            done_delay_sec=0.0,
        )


async def run_pipeline_with_audio(
    session: Session,
    opus_packets: list[bytes],
    raw_audio: bytes,
    turn_id: int,
) -> None:
    """STT -> LLM -> TTS for one committed microphone utterance."""
    if not opus_packets and not raw_audio:
        return

    async with session.pipeline_lock:
        if session.closing:
            return

        session.active_turn_id = turn_id
        processing_started_at = time.perf_counter()
        processing_prompt_task = None
        try:
            if RUNTIME_CONFIG.processing_prompt_enabled:
                processing_prompt_task = asyncio.create_task(
                    maybe_send_processing_prompt(session, turn_id, processing_started_at)
                )

            loop = asyncio.get_running_loop()
            transcript = ""
            stt_started_at = time.perf_counter()
            if session_uses_pcm_input(session) and raw_audio:
                logger.info(
                    "[{}] transcribing {} PCM bytes from chip microphone",
                    session.peer,
                    len(raw_audio),
                )
                transcript = await loop.run_in_executor(None, transcribe_pcm, raw_audio)
            elif opus_packets:
                decode_started_at = time.perf_counter()
                pcm_audio = await loop.run_in_executor(
                    None, decode_opus_packets_to_pcm, list(opus_packets)
                )
                logger.info(
                    "[{}] turn {} decoded {} OPUS packets into {} PCM bytes in {:.0f}ms",
                    session.peer,
                    turn_id,
                    len(opus_packets),
                    len(pcm_audio),
                    (time.perf_counter() - decode_started_at) * 1000.0,
                )
                transcript = await loop.run_in_executor(None, transcribe_pcm, pcm_audio)
            stt_ms = (time.perf_counter() - stt_started_at) * 1000.0

            # Fallback for non-binary append senders that provide a pre-framed buffer.
            if not transcript and raw_audio:
                logger.warning(
                    "[{}] no transcript from packetized audio; retrying raw buffer fallback",
                    session.peer,
                )
                fallback_started_at = time.perf_counter()
                transcript = await loop.run_in_executor(None, transcribe_pcm, raw_audio)
                stt_ms += (time.perf_counter() - fallback_started_at) * 1000.0

            if not transcript:
                logger.warning("[{}] Empty transcript", session.peer)
                reply = "Sorry, I didn't catch that. Could you say it again?"
                reply_pcm = None
                llm_ms = 0.0
                tts_ms = 0.0
            else:
                logger.info("[{}] You said: {}", session.peer, transcript)
                llm_started_at = time.perf_counter()
                reply = await loop.run_in_executor(
                    None, ask_llm, transcript, list(session.history)
                )
                llm_ms = (time.perf_counter() - llm_started_at) * 1000.0
                logger.info("[{}] Dawn: {}", session.peer, reply)

                session.history.append({"role": "user", "content": transcript})
                session.history.append({"role": "assistant", "content": reply})

                tts_started_at = time.perf_counter()
                reply_pcm = await loop.run_in_executor(None, synthesize_speech, reply)
                tts_ms = (time.perf_counter() - tts_started_at) * 1000.0

            if processing_prompt_task is not None:
                processing_prompt_task.cancel()
                await asyncio.gather(processing_prompt_task, return_exceptions=True)

            total_ms = (time.perf_counter() - processing_started_at) * 1000.0
            commit_to_reply_ms = 0.0
            if session.last_commit_at:
                commit_to_reply_ms = (
                    time.perf_counter() - session.last_commit_at
                ) * 1000.0

            session.last_turn_metrics = {
                "turn_id": turn_id,
                "transcript": transcript,
                "reply": reply,
                "stt_ms": round(stt_ms, 1),
                "llm_ms": round(llm_ms, 1),
                "tts_ms": round(tts_ms, 1),
                "total_ms": round(total_ms, 1),
                "commit_to_reply_ms": round(commit_to_reply_ms, 1),
                "audio_bytes": len(raw_audio),
                "opus_packets": len(opus_packets),
            }
            logger.info(
                "[{}] turn {} latency stt={:.0f}ms llm={:.0f}ms tts={:.0f}ms total={:.0f}ms",
                session.peer,
                turn_id,
                stt_ms,
                llm_ms,
                tts_ms,
                total_ms,
            )

            async with session.response_lock:
                await send_pcm_response_locked(session, reply, reply_pcm)
        finally:
            if processing_prompt_task is not None:
                processing_prompt_task.cancel()
                await asyncio.gather(processing_prompt_task, return_exceptions=True)
            if session.active_turn_id == turn_id:
                session.active_turn_id = 0


async def handle_text_message(session: Session, payload: bytes) -> None:
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("[{}] Invalid JSON: {!r}", session.peer, payload[:100])
        return

    msg_type = message.get("type", "")
    logger.info("[{}] ← {}", session.peer, msg_type)

    if msg_type == "hello":
        await send_json(session, {"type": "hello_response", "code": 200, "msg": "OK"})
        return

    if msg_type == "session.update":
        session_info = message.get("session") or message
        device_id = str(session_info.get("devId", "")).strip()
        session.input_audio_format = str(session_info.get("input_audio_format", "opus"))
        session.input_audio_rate = int(
            session_info.get("input_audio_rate", INBOUND_AUDIO_RATE)
        )
        session.input_audio_duration_ms = int(
            session_info.get("input_audio_duration", INBOUND_FRAME_MS)
        )
        session.output_audio_format = str(session_info.get("output_audio_format", "opus"))
        session.output_audio_rate = int(
            session_info.get("output_audio_rate", OUTBOUND_AUDIO_RATE)
        )
        requested_output_duration = session_info.get(
            "output_audio_duration",
            session_info.get("input_audio_duration", DEFAULT_OUTBOUND_FRAME_MS),
        )
        session.output_audio_duration_ms = normalize_opus_frame_ms(
            int(requested_output_duration)
        )
        logger.info(
            "[{}] session.update devId={} input={} {}ms output={} {}Hz frame={}ms pack_size={}",
            session.peer,
            device_id,
            session.input_audio_format,
            session.input_audio_duration_ms,
            session.output_audio_format,
            session.output_audio_rate,
            session.output_audio_duration_ms,
            session_info.get("pack_size"),
        )
        session.device_id = device_id
        session.handshook = True
        if device_id:
            await register_active_session(session, device_id)
        updated_session = {
            "devId": device_id,
            "nfcId": session_info.get("nfcId", ""),
            "input_audio_format": session.input_audio_format,
            "input_audio_rate": session.input_audio_rate,
            "input_audio_duration": session.input_audio_duration_ms,
            "output_audio_format": session.output_audio_format,
            "output_audio_rate": session.output_audio_rate,
            "output_audio_duration": session.output_audio_duration_ms,
            "cloud_vad": int(session_info.get("cloud_vad", 1)),
            "source": str(session_info.get("source", "BKR1")),
        }
        await send_json(
            session,
            {
                "type": "session.updated",
                "session": updated_session,
            },
        )
        schedule_startup_action(
            session,
            delay_sec=STARTUP_ACTION_DELAY_SEC,
            reason="session.updated",
        )
        return

    if msg_type == "input_audio_buffer.append":
        session.audio_buf.clear()
        session.audio_packets.clear()
        reset_server_vad(session)
        logger.info("[{}] input audio append: starting new utterance", session.peer)
        audio_b64 = message.get("audio", "")
        if audio_b64:
            try:
                audio_chunk = base64.b64decode(audio_b64)
                session.audio_buf.extend(audio_chunk)
                session.audio_packets.append(audio_chunk)
            except Exception:
                logger.warning("[{}] invalid base64 audio append", session.peer)
        return

    if msg_type == "input_audio_buffer.commit":
        session.last_commit_at = time.perf_counter()
        session.committed_audio = bytes(session.audio_buf)
        session.committed_packets = list(session.audio_packets)
        session.audio_buf.clear()
        session.audio_packets.clear()
        reset_server_vad(session)
        await send_json(session, {"type": "input_audio_buffer.committed"})
        logger.info(
            "[{}] audio commit: {} bytes across {} packets",
            session.peer,
            len(session.committed_audio),
            len(session.committed_packets),
        )
        return

    if msg_type == "response.create":
        packets_copy = list(session.committed_packets)
        audio_copy = session.committed_audio
        session.committed_packets.clear()
        session.committed_audio = b""
        if packets_copy or len(audio_copy) > 100:
            spawn_session_task(
                session,
                run_pipeline_with_audio(
                    session,
                    packets_copy,
                    audio_copy,
                    next_turn_id(session),
                ),
            )
        else:
            spawn_session_task(session, send_audio_response(session, LISTENING_TEXT))
        return

    if msg_type == "input_audio_buffer.clear":
        session.audio_buf.clear()
        session.audio_packets.clear()
        session.committed_audio = b""
        session.committed_packets.clear()
        reset_server_vad(session)
        logger.info("[{}] audio buffer cleared", session.peer)
        return

    if msg_type == "flow_control":
        flag = str(message.get("flag", ""))
        logger.info("[{}] flow_control flag={}", session.peer, flag or "<missing>")
        if flag == "idle":
            session.idle_announced = True
            if session.handshook:
                schedule_startup_action(
                    session,
                    delay_sec=STARTUP_ACTION_DELAY_SEC,
                    reason="flow_control idle",
                )
        return

    logger.debug("[{}] unhandled message: {}", session.peer, message)


async def perform_websocket_upgrade(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[str, bytes]:
    """Read the HTTP upgrade request and return the peer plus leftover bytes."""
    peer = writer.get_extra_info("peername")
    peer_text = str(peer)
    buffer = b""

    while b"\r\n\r\n" not in buffer:
        chunk = await asyncio.wait_for(reader.read(1024), timeout=10.0)
        if not chunk:
            raise ConnectionError("connection closed before WebSocket upgrade")
        buffer += chunk
        if len(buffer) > 65536:
            raise ValueError("HTTP upgrade request too large")

    head, leftover = buffer.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    if not lines:
        raise ValueError("empty HTTP upgrade request")

    request_line = lines[0]
    if "HTTP/" not in request_line:
        raise ValueError(f"invalid HTTP request line: {request_line!r}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    ws_key = headers.get("sec-websocket-key")
    if not ws_key:
        raise ValueError("missing Sec-WebSocket-Key header")

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {ws_accept_key(ws_key)}\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(response)
    await writer.drain()
    logger.info("[{}] WebSocket upgrade complete", peer_text)
    return peer_text, leftover


async def register_active_session(session: Session, device_id: str) -> None:
    previous = ACTIVE_SESSIONS.get(device_id)
    ACTIVE_SESSIONS[device_id] = session
    if previous is None or previous is session or previous.closing:
        return

    logger.info(
        "[{}] replacing previous active session {} for devId={}",
        session.peer,
        previous.peer,
        device_id,
    )
    await close_session(previous)


async def close_session(session: Session) -> None:
    if session.closing:
        return

    session.closing = True
    if session.device_id and ACTIVE_SESSIONS.get(session.device_id) is session:
        ACTIVE_SESSIONS.pop(session.device_id, None)
    for task in tuple(session.tasks):
        task.cancel()
    if session.tasks:
        await asyncio.gather(*session.tasks, return_exceptions=True)

    try:
        async with session.send_lock:
            session.writer.write(ws_encode_close())
            await session.writer.drain()
    except Exception:
        pass

    session.writer.close()
    try:
        await session.writer.wait_closed()
    except Exception:
        pass


def detect_local_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def session_public_dict(session: Session) -> dict[str, Any]:
    return {
        "peer": session.peer,
        "device_id": session.device_id,
        "handshook": session.handshook,
        "closing": session.closing,
        "connected_seconds": round(time.time() - session.connected_at, 1),
        "last_activity_age_sec": round(time.time() - session.last_activity, 1),
        "idle_announced": session.idle_announced,
        "input_audio_format": session.input_audio_format,
        "input_audio_rate": session.input_audio_rate,
        "input_audio_duration_ms": session.input_audio_duration_ms,
        "output_audio_format": session.output_audio_format,
        "output_audio_rate": session.output_audio_rate,
        "output_audio_duration_ms": session.output_audio_duration_ms,
        "seq": session.seq,
        "active_turn_id": session.active_turn_id,
        "last_turn_metrics": session.last_turn_metrics,
    }


def server_status_dict() -> dict[str, Any]:
    sessions = [
        session_public_dict(session)
        for session in sorted(
            ACTIVE_SESSIONS.values(),
            key=lambda item: item.last_activity,
            reverse=True,
        )
        if not session.closing
    ]
    return {
        "transport": {
            "voice_path": "wifi-websocket",
            "chip_endpoint": CHIP_ENDPOINT,
            "usb_role": "power-and-flash-only",
            "admin_panel_url": f"http://{detect_local_ipv4()}:{ADMIN_PORT}/",
            "control_panel_scope": (
                "lan" if ADMIN_HOST in {"0.0.0.0", "::", ""} else "local-only"
            ),
        },
        "config": config_public_dict(),
        "sessions": sessions,
        "connected_session_count": len(sessions),
    }


def make_http_response(
    status: str,
    body: bytes,
    *,
    content_type: str,
) -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8") + body


def make_text_response(status: str, body: str) -> bytes:
    return make_http_response(
        status,
        body.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )


def make_json_response(status: str, payload: dict[str, Any]) -> bytes:
    return make_http_response(
        status,
        json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )


def web_manifest_dict() -> dict[str, Any]:
    panel_url = f"http://{detect_local_ipv4()}:{ADMIN_PORT}/"
    return {
        "name": "BK7258 Voice Control",
        "short_name": "BK7258",
        "start_url": panel_url,
        "display": "standalone",
        "background_color": "#f4efe5",
        "theme_color": "#0f766e",
        "description": "Control the BK7258 voice toy from your phone on the same Wi-Fi.",
    }


def render_control_panel() -> str:
    initial_status = json.dumps(server_status_dict(), ensure_ascii=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f766e">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="BK7258">
  <link rel="manifest" href="/manifest.webmanifest">
  <title>BK7258 Voice Control</title>
  <style>
    :root {{
      --bg: #f4efe5;
      --card: #fffaf0;
      --ink: #1f2933;
      --soft: #52606d;
      --accent: #0f766e;
      --accent-2: #b45309;
      --accent-3: #155e75;
      --line: #d9cbb8;
      --danger: #a16207;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(180,83,9,0.14), transparent 24%),
        linear-gradient(180deg, #faf5eb, var(--bg));
      min-height: 100vh;
    }}
    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 20px 14px 96px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(2rem, 6vw, 3.8rem);
      line-height: 0.98;
      letter-spacing: -0.03em;
    }}
    p {{
      margin: 0;
      color: var(--soft);
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,250,240,0.94), rgba(255,244,220,0.98));
      border: 1px solid var(--line);
      border-radius: 26px;
      padding: 20px;
      box-shadow: 0 18px 48px rgba(31,41,51,0.08);
    }}
    .hero-copy {{
      max-width: 760px;
    }}
    .hero p {{
      font-size: 1rem;
      line-height: 1.45;
    }}
    .banner {{
      margin-top: 14px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(15,118,110,0.1);
      color: var(--accent);
      font-weight: 600;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      margin-top: 20px;
    }}
    .card {{
      background: rgba(255,250,240,0.92);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 18px;
      box-shadow: 0 16px 40px rgba(31,41,51,0.08);
      backdrop-filter: blur(8px);
    }}
    .card h2 {{
      margin: 0 0 12px;
      font-size: 1.1rem;
    }}
    .card p + p {{
      margin-top: 8px;
    }}
    .pill {{
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(15,118,110,0.12);
      color: var(--accent);
      font-size: 0.92rem;
      margin: 0 8px 8px 0;
    }}
    label {{
      display: block;
      font-size: 0.92rem;
      margin: 12px 0 6px;
      color: var(--soft);
    }}
    input, textarea, select, button {{
      width: 100%;
      font: inherit;
    }}
    input, textarea, select {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      background: #fffdf8;
      color: var(--ink);
    }}
    textarea {{
      min-height: 100px;
      resize: vertical;
    }}
    button {{
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      background: linear-gradient(135deg, var(--accent), #155e75);
      color: white;
      cursor: pointer;
      margin-top: 12px;
    }}
    button.secondary {{
      background: linear-gradient(135deg, var(--accent-2), #92400e);
    }}
    button.ghost {{
      background: rgba(15,118,110,0.1);
      color: var(--accent);
      border: 1px solid rgba(15,118,110,0.18);
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.86rem;
      line-height: 1.45;
      color: #13212d;
    }}
    .row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .row > * {{
      flex: 1 1 180px;
    }}
    .muted {{
      color: var(--soft);
      font-size: 0.92rem;
      margin-top: 8px;
    }}
    .status-dot {{
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      margin-right: 8px;
      vertical-align: middle;
      background: #b45309;
    }}
    .status-dot.live {{
      background: #0f766e;
      box-shadow: 0 0 0 6px rgba(15,118,110,0.12);
    }}
    .mode-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .mode-btn {{
      min-height: 76px;
      text-align: left;
      padding: 14px;
      border-radius: 16px;
      border: 1px solid rgba(15,118,110,0.18);
      background: linear-gradient(180deg, rgba(15,118,110,0.08), rgba(255,255,255,0.92));
      color: var(--ink);
    }}
    .mode-btn strong {{
      display: block;
      font-size: 1rem;
      margin-bottom: 4px;
    }}
    .mode-btn span {{
      display: block;
      font-size: 0.88rem;
      color: var(--soft);
      line-height: 1.35;
    }}
    .quick-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .quick-grid button {{
      margin-top: 0;
      min-height: 58px;
    }}
    .install-note {{
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(180,83,9,0.08);
      color: #8b5e08;
      font-size: 0.92rem;
      line-height: 1.35;
    }}
    .section-stack {{
      display: grid;
      gap: 16px;
    }}
    .sticky-actions {{
      position: fixed;
      left: 12px;
      right: 12px;
      bottom: 12px;
      z-index: 20;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      padding: 10px;
      border-radius: 18px;
      background: rgba(255,250,240,0.92);
      border: 1px solid var(--line);
      box-shadow: 0 18px 40px rgba(31,41,51,0.14);
      backdrop-filter: blur(12px);
    }}
    .sticky-actions button {{
      margin-top: 0;
      min-height: 56px;
      font-weight: 700;
    }}
    .hidden {{
      display: none;
    }}
    @media (min-width: 900px) {{
      .sticky-actions {{
        max-width: 420px;
        left: auto;
        right: 24px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-copy">
        <h1>BK7258 Voice Control</h1>
        <p>Use your phone as the remote control for the chip. This runs on the Mac mini, the chip talks over Wi-Fi WebSocket, and this page lets you change character, send test speech, and tune latency-sensitive behavior.</p>
      </div>
      <div id="statusBanner" class="banner"><span class="status-dot"></span>Checking chip connection…</div>
      <div class="install-note">
        Phone tip: open this page in Safari or Chrome on the same Wi-Fi, then use “Add to Home Screen” to make it feel like an app.
      </div>
    </section>
    <div class="grid">
      <section class="card section-stack">
        <h2>Connection</h2>
        <div id="transport"></div>
        <p class="muted">This panel is LAN-accessible right now, so phones on the same Wi-Fi can control the chip. It is not public on the internet.</p>
      </section>
      <section class="card section-stack">
        <h2>Quick Modes</h2>
        <div class="mode-grid">
          <button class="mode-btn ghost" data-mode="companion" data-sample="Hello, I am ready to chat.">
            <strong>Companion</strong>
            <span>Friendly toy mode for general conversation.</span>
          </button>
          <button class="mode-btn ghost" data-mode="storyteller" data-sample="Tell me a short bedtime story about a brave little robot.">
            <strong>Storyteller</strong>
            <span>Great for short stories and imagination play.</span>
          </button>
          <button class="mode-btn ghost" data-mode="language_teacher" data-sample="Teach me three easy English phrases for meeting a new friend.">
            <strong>Teacher</strong>
            <span>Short lessons, gentle correction, easy examples.</span>
          </button>
          <button class="mode-btn ghost" data-mode="bedtime_guide" data-sample="Say a calm good night message for a child.">
            <strong>Bedtime</strong>
            <span>Soft and calming for quiet moments.</span>
          </button>
        </div>
      </section>
      <section class="card section-stack">
        <h2>Quick Speech</h2>
        <div class="quick-grid">
          <button class="secondary quick-say" data-text="Hello. I am connected and ready.">Test Voice</button>
          <button class="secondary quick-say" data-text="Good morning. The BK7258 chip is online.">Morning</button>
          <button class="secondary quick-say" data-text="Let us play a guessing game together.">Play Game</button>
          <button class="secondary quick-say" data-text="Can you repeat after me: hello, thank you, and goodbye.">Repeat Words</button>
        </div>
        <p id="quickResult" class="muted"></p>
      </section>
      <section class="card section-stack">
        <h2>Speak To Chip</h2>
        <label for="speakText">Text</label>
        <textarea id="speakText">Hello from the BK7258 control panel.</textarea>
        <button id="speakBtn">Send Speech</button>
        <p id="speakResult" class="muted"></p>
      </section>
      <section class="card section-stack">
        <h2>Runtime Config</h2>
        <label for="llmProvider">LLM provider</label>
        <select id="llmProvider">
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
        </select>
        <label for="anthropicModel">Anthropic model</label>
        <input id="anthropicModel" value="">
        <label for="openaiModel">OpenAI model</label>
        <input id="openaiModel" value="">
        <label for="characterPreset">Character</label>
        <select id="characterPreset"></select>
        <label for="ttsBackend">TTS backend</label>
        <select id="ttsBackend">
          <option value="auto">Auto (local first)</option>
          <option value="local">Local macOS voice</option>
          <option value="deepgram">Deepgram cloud voice</option>
        </select>
        <label for="processingText">Processing prompt</label>
        <input id="processingText" value="">
        <div class="row">
          <label><input id="startupGreeting" type="checkbox"> Startup greeting</label>
          <label><input id="startupPrime" type="checkbox"> Startup listen prime</label>
          <label><input id="waitForIdle" type="checkbox"> Wait for idle before startup</label>
          <label><input id="processingPrompt" type="checkbox"> Processing prompt</label>
        </div>
        <label for="systemPrompt">Extra instructions</label>
        <textarea id="systemPrompt"></textarea>
        <button id="saveConfig">Save Config</button>
        <p id="configResult" class="muted"></p>
      </section>
      <section class="card section-stack">
        <h2>Latency Simulation</h2>
        <label for="simulateText">Transcript to simulate</label>
        <input id="simulateText" value="Tell me a short story about a brave little robot.">
        <button id="simulateBtn" class="secondary">Run Simulation</button>
        <p class="muted">This measures backend timing without the physical chip.</p>
        <pre id="simulationOutput"></pre>
      </section>
      <section class="card section-stack" style="grid-column: 1 / -1;">
        <h2>Live Sessions</h2>
        <pre id="sessions"></pre>
      </section>
    </div>
    <div class="sticky-actions">
      <button id="stickySpeak">Send Speech</button>
      <button id="stickySave" class="secondary">Save Mode</button>
    </div>
  </div>
  <script>
    const initialStatus = {initial_status};
  </script>
  <script>
    const state = {{ status: initialStatus }};

    function showTransport(status) {{
      const transport = status.transport;
      const availability = status.config.provider_availability;
      document.getElementById("transport").innerHTML = `
        <span class="pill">Voice: ${{
          transport.voice_path
        }}</span>
        <span class="pill">Chip target: ${{
          transport.chip_endpoint
        }}</span>
        <span class="pill">Admin: ${{
          transport.admin_panel_url
        }}</span>
        <span class="pill">Panel scope: ${{
          transport.control_panel_scope
        }}</span>
        <span class="pill">Sessions: ${{
          status.connected_session_count
        }}</span>
        <span class="pill">Anthropic key: ${{
          availability.anthropic ? "ready" : "missing"
        }}</span>
        <span class="pill">OpenAI key: ${{
          availability.openai ? "ready" : "missing"
        }}</span>
      `;
    }}

    function showStatusBanner(status) {{
      const banner = document.getElementById("statusBanner");
      const connected = status.connected_session_count > 0;
      const dotClass = connected ? "status-dot live" : "status-dot";
      const sessionWord = status.connected_session_count === 1 ? "session" : "sessions";
      const text = connected
        ? "Chip connected. " + status.connected_session_count + " live " + sessionWord + ". You can control it from this phone."
        : "Chip not connected right now. Keep the phone and chip on the same Wi-Fi, then power the chip on.";
      banner.innerHTML = `<span class="${{dotClass}}"></span>${{text}}`;
    }}

    function populateCharacters(status) {{
      const presets = status.config.character_presets || {{}};
      const select = document.getElementById("characterPreset");
      const current = status.config.character_preset || "companion";
      select.innerHTML = "";
      for (const [key, prompt] of Object.entries(presets)) {{
        const option = document.createElement("option");
        option.value = key;
        option.textContent = key.replaceAll("_", " ");
        option.title = prompt;
        if (key === current) option.selected = true;
        select.appendChild(option);
      }}
    }}

    function populateConfig(status) {{
      const cfg = status.config;
      document.getElementById("llmProvider").value = cfg.llm_provider || "anthropic";
      document.getElementById("anthropicModel").value = cfg.anthropic_model || "";
      document.getElementById("openaiModel").value = cfg.openai_model || "";
      document.getElementById("ttsBackend").value = cfg.tts_backend || "auto";
      document.getElementById("processingText").value = cfg.processing_prompt_text || "";
      document.getElementById("systemPrompt").value = cfg.system_prompt || "";
      document.getElementById("startupGreeting").checked = !!cfg.startup_greeting_enabled;
      document.getElementById("startupPrime").checked = !!cfg.startup_listen_prime_enabled;
      document.getElementById("waitForIdle").checked = !!cfg.wait_for_idle_before_startup;
      document.getElementById("processingPrompt").checked = !!cfg.processing_prompt_enabled;
      populateCharacters(status);
    }}

    function showSessions(status) {{
      document.getElementById("sessions").textContent = JSON.stringify(status.sessions, null, 2);
    }}

    async function refreshStatus() {{
      const response = await fetch("/api/status");
      state.status = await response.json();
      showTransport(state.status);
      showStatusBanner(state.status);
      populateConfig(state.status);
      showSessions(state.status);
    }}

    async function postSpeech(text, resultId = "speakResult") {{
      const result = document.getElementById(resultId);
      result.textContent = "Sending...";
      const response = await fetch("/api/speak", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ text }})
      }});
      const data = await response.json();
      result.textContent = JSON.stringify(data);
      await refreshStatus();
      return data;
    }}

    async function sendSpeech() {{
      const text = document.getElementById("speakText").value.trim();
      return postSpeech(text, "speakResult");
    }}

    async function saveConfig() {{
      const payload = {{
        llm_provider: document.getElementById("llmProvider").value,
        anthropic_model: document.getElementById("anthropicModel").value.trim(),
        openai_model: document.getElementById("openaiModel").value.trim(),
        character_preset: document.getElementById("characterPreset").value,
        tts_backend: document.getElementById("ttsBackend").value,
        processing_prompt_text: document.getElementById("processingText").value.trim(),
        system_prompt: document.getElementById("systemPrompt").value,
        startup_greeting_enabled: document.getElementById("startupGreeting").checked,
        startup_listen_prime_enabled: document.getElementById("startupPrime").checked,
        wait_for_idle_before_startup: document.getElementById("waitForIdle").checked,
        processing_prompt_enabled: document.getElementById("processingPrompt").checked
      }};
      const response = await fetch("/api/config", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      document.getElementById("configResult").textContent = JSON.stringify(data);
      await refreshStatus();
      return data;
    }}

    async function runSimulation() {{
      const text = document.getElementById("simulateText").value.trim();
      document.getElementById("simulationOutput").textContent = "Running...";
      const response = await fetch("/api/simulate?text=" + encodeURIComponent(text));
      const data = await response.json();
      document.getElementById("simulationOutput").textContent = JSON.stringify(data, null, 2);
    }}

    async function setModeAndOptionallySpeak(mode, sampleText = "") {{
      document.getElementById("characterPreset").value = mode;
      await saveConfig();
      if (sampleText) {{
        document.getElementById("speakText").value = sampleText;
        await postSpeech(sampleText, "quickResult");
      }}
    }}

    document.getElementById("speakBtn").addEventListener("click", sendSpeech);
    document.getElementById("saveConfig").addEventListener("click", saveConfig);
    document.getElementById("simulateBtn").addEventListener("click", runSimulation);
    document.getElementById("stickySpeak").addEventListener("click", sendSpeech);
    document.getElementById("stickySave").addEventListener("click", saveConfig);
    document.querySelectorAll(".quick-say").forEach((button) => {{
      button.addEventListener("click", () => postSpeech(button.dataset.text || "", "quickResult"));
    }});
    document.querySelectorAll(".mode-btn").forEach((button) => {{
      button.addEventListener("click", () => setModeAndOptionallySpeak(
        button.dataset.mode || "companion",
        button.dataset.sample || "",
      ));
    }});
    showTransport(state.status);
    showStatusBanner(state.status);
    populateConfig(state.status);
    showSessions(state.status);
    setInterval(refreshStatus, 2500);
  </script>
</body>
</html>""".replace("{initial_status}", initial_status)


async def read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes]:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
    request_line, raw_headers = raw.decode("latin-1").split("\r\n", 1)
    method, target, _http_version = request_line.split(" ", 2)
    headers: dict[str, str] = {}
    for line in raw_headers.split("\r\n"):
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0") or "0")
    body = b""
    if content_length:
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)
    return method, target, headers, body


def synthesize_local_input_pcm(text: str) -> bytes | None:
    local_pcm = synthesize_speech_locally(text)
    if not local_pcm:
        return None
    return resample_pcm_mono_s16le(local_pcm, OUTBOUND_AUDIO_RATE, INBOUND_AUDIO_RATE)


def simulate_turn_from_text(user_text: str) -> dict[str, Any]:
    user_text = user_text.strip()
    if not user_text:
        return {"ok": False, "error": "missing text"}

    transcript = user_text
    simulated_stt_ms = 0.0
    stt_mode = "direct-text"

    input_pcm = synthesize_local_input_pcm(user_text)
    if input_pcm:
        stt_mode = "local-voice-to-deepgram"
        stt_started_at = time.perf_counter()
        detected = transcribe_pcm(input_pcm)
        simulated_stt_ms = (time.perf_counter() - stt_started_at) * 1000.0
        if detected:
            transcript = detected

    llm_started_at = time.perf_counter()
    reply = ask_llm(transcript, [])
    llm_ms = (time.perf_counter() - llm_started_at) * 1000.0

    tts_started_at = time.perf_counter()
    reply_pcm = synthesize_speech(reply)
    tts_ms = (time.perf_counter() - tts_started_at) * 1000.0

    encode_started_at = time.perf_counter()
    frame_count = 0
    if reply_pcm:
        frames, _next_seq = pcm_to_opus_frames(reply_pcm, make_session_encoder(), 0, 20)
        frame_count = len(frames)
    encode_ms = (time.perf_counter() - encode_started_at) * 1000.0

    return {
        "ok": True,
        "mode": stt_mode,
        "llm_provider": RUNTIME_CONFIG.llm_provider,
        "character_preset": RUNTIME_CONFIG.character_preset,
        "tts_backend": RUNTIME_CONFIG.tts_backend,
        "transcript": transcript,
        "reply": reply,
        "stt_ms": round(simulated_stt_ms, 1),
        "llm_ms": round(llm_ms, 1),
        "tts_ms": round(tts_ms, 1),
        "encode_ms": round(encode_ms, 1),
        "total_ms": round(simulated_stt_ms + llm_ms + tts_ms + encode_ms, 1),
        "reply_frame_count_20ms": frame_count,
    }


async def handle_admin_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        method, target, _headers, body = await read_http_request(reader)
    except Exception:
        writer.write(make_text_response("400 Bad Request", "bad request\n"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    try:
        parsed = urlsplit(target)
        params = parse_qs(parsed.query, keep_blank_values=False)

        if method == "GET" and parsed.path == "/":
            writer.write(
                make_http_response(
                    "200 OK",
                    render_control_panel().encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/manifest.webmanifest":
            writer.write(
                make_http_response(
                    "200 OK",
                    json.dumps(web_manifest_dict(), ensure_ascii=True, indent=2).encode("utf-8"),
                    content_type="application/manifest+json; charset=utf-8",
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/api/status":
            writer.write(make_json_response("200 OK", server_status_dict()))
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/api/config":
            writer.write(make_json_response("200 OK", {"ok": True, "config": config_public_dict()}))
            await writer.drain()
            return

        if method == "POST" and parsed.path == "/api/config":
            payload = json.loads(body.decode("utf-8") or "{}")
            writer.write(
                make_json_response(
                    "200 OK",
                    {"ok": True, "config": apply_runtime_config(payload)},
                )
            )
            await writer.drain()
            return

        if (method == "GET" and parsed.path == "/speak") or (
            method == "POST" and parsed.path == "/api/speak"
        ):
            payload: dict[str, Any] = {}
            if method == "POST" and body:
                payload = json.loads(body.decode("utf-8") or "{}")
            text = str(payload.get("text") or (params.get("text") or [""])[0]).strip()
            device_id = str(
                payload.get("device_id") or (params.get("device_id") or [""])[0]
            ).strip()
            if not text:
                writer.write(make_json_response("400 Bad Request", {"ok": False, "error": "missing text"}))
                await writer.drain()
                return

            session = get_preferred_session(device_id)
            if session is None:
                writer.write(
                    make_json_response(
                        "409 Conflict",
                        {"ok": False, "error": "no active chip session"},
                    )
                )
                await writer.drain()
                return

            logger.info("[{}] admin speak request: {}", session.peer, text)
            spawn_session_task(session, send_audio_response(session, text))
            writer.write(
                make_json_response(
                    "200 OK",
                    {"ok": True, "queued": True, "device_id": session.device_id},
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/api/simulate":
            text = (params.get("text") or [""])[0].strip()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, simulate_turn_from_text, text)
            writer.write(make_json_response("200 OK", result))
            await writer.drain()
            return

        writer.write(make_text_response("404 Not Found", "not found\n"))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("Connection from {}", peer)

    session: Session | None = None
    buffer = b""
    started_at = time.time()

    try:
        peer_text, buffer = await perform_websocket_upgrade(reader, writer)
        session = Session(writer=writer, peer=peer_text)
        spawn_session_task(session, session_keepalive_loop(session))

        while True:
            frames, buffer = ws_decode_frames(buffer)
            if frames:
                session.last_activity = time.time()
                for opcode, payload in frames:
                    if opcode == 0x8:
                        logger.info("[{}] ← close", session.peer)
                        await send_raw(session, ws_encode_close())
                        return

                    if opcode == 0x9:
                        logger.debug("[{}] ← ping", session.peer)
                        await send_raw(session, ws_encode_pong(payload))
                        continue

                    if opcode == 0xA:
                        logger.debug("[{}] ← pong", session.peer)
                        continue

                    if opcode == 0x1:
                        await handle_text_message(session, payload)
                        continue

                    if opcode == 0x2:
                        if session.handshook:
                            audio_payload = extract_audio_payload(payload)
                            if audio_payload is not None:
                                session.audio_buf.extend(audio_payload)
                                session.audio_packets.append(audio_payload)
                                logger.info(
                                    "[{}] ← binary audio packet: frame={} packets={} total={}",
                                    session.peer,
                                    len(audio_payload),
                                    len(session.audio_packets),
                                    len(session.audio_buf),
                                )
                                await maybe_auto_commit_pcm(session, audio_payload)
                        else:
                            logger.warning(
                                "[{}] ignoring binary audio before handshake", session.peer
                            )
                        continue

                    logger.debug("[{}] ignoring opcode {}", session.peer, opcode)
                continue

            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
            except asyncio.TimeoutError:
                if session and (time.time() - session.last_activity) > 300.0:
                    logger.info("[{}] idle timeout", session.peer)
                    break
                continue

            if not chunk:
                logger.info("[{}] client disconnected", session.peer)
                break

            buffer += chunk
            session.last_activity = time.time()

    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        peer_text = session.peer if session else str(peer)
        logger.info("[{}] connection closed: {}", peer_text, exc)
    except Exception as exc:
        peer_text = session.peer if session else str(peer)
        logger.exception("[{}] connection error: {}", peer_text, exc)
    finally:
        if session is not None:
            await close_session(session)
            logger.info(
                "[{}] session ended after {:.1f}s",
                session.peer,
                time.time() - started_at,
            )
        else:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def main() -> None:
    if RUNTIME_CONFIG.startup_greeting_enabled:
        logger.info("preloading greeting audio")
        get_greeting_pcm()

    server = await asyncio.start_server(handle_connection, HOST, PORT)
    admin_server = await asyncio.start_server(
        handle_admin_connection,
        ADMIN_HOST,
        ADMIN_PORT,
    )
    logger.info("BK7258 WebSocket server listening on {}:{}", HOST, PORT)
    logger.info("Chip should connect to {}", CHIP_ENDPOINT)
    logger.info(
        "Admin panel listening on http://{}:{}/",
        detect_local_ipv4(),
        ADMIN_PORT,
    )
    async with server, admin_server:
        await asyncio.gather(server.serve_forever(), admin_server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
