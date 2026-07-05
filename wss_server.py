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
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unicodedata
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

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("BK7258_PORT", "8765"))
HOST = os.getenv("BK7258_HOST", "0.0.0.0").strip() or "0.0.0.0"
CHIP_ENDPOINT = os.getenv("BK7258_CHIP_ENDPOINT", "ws://10.0.0.62:8765").strip()
ADMIN_HOST = os.getenv("BK7258_ADMIN_HOST", "0.0.0.0").strip() or "0.0.0.0"
ADMIN_PORT = int(os.getenv("BK7258_ADMIN_PORT", "8766"))
PANEL_ACCESS_CODE = os.getenv("BK7258_PANEL_ACCESS_CODE", "").strip()
CONTENT_DIR = Path(
    os.getenv(
        "BK7258_CONTENT_DIR",
        str(BASE_DIR / "content"),
    )
).expanduser()
ACTIVITY_STATE_PATH = Path(
    os.getenv(
        "BK7258_ACTIVITY_STATE_PATH",
        str(BASE_DIR / "activity_state.json"),
    )
).expanduser()
RECENT_TURN_LIMIT = int(os.getenv("BK7258_RECENT_TURN_LIMIT", "24"))
RECENT_SESSION_LIMIT = int(os.getenv("BK7258_RECENT_SESSION_LIMIT", "16"))
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
    "(1-2 short sentences). They will be spoken aloud. Use plain spoken text only."
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
PCM_VAD_SILENCE_MS = 240
PCM_VAD_MIN_SPEECH_MS = 140
PCM_VAD_MAX_UTTERANCE_MS = 5000
PCM_IGNORE_AFTER_PLAYBACK_MS = 250
DEFAULT_LLM_HISTORY_MESSAGES = int(os.getenv("BK7258_LLM_HISTORY_MESSAGES", "4"))
DEFAULT_REPLY_SENTENCE_LIMIT = int(os.getenv("BK7258_REPLY_SENTENCE_LIMIT", "2"))
STORY_REPLY_SENTENCE_LIMIT = int(os.getenv("BK7258_STORY_REPLY_SENTENCE_LIMIT", "2"))
DEFAULT_REPLY_WORD_LIMIT = int(os.getenv("BK7258_REPLY_WORD_LIMIT", "24"))
STORY_REPLY_WORD_LIMIT = int(os.getenv("BK7258_STORY_REPLY_WORD_LIMIT", "40"))
DEFAULT_LLM_MAX_TOKENS = int(os.getenv("BK7258_LLM_MAX_TOKENS", "64"))
STORY_LLM_MAX_TOKENS = int(os.getenv("BK7258_STORY_LLM_MAX_TOKENS", "84"))

HEAD_MAGIC = 0xF0D5
HEAD_FLAGS = 0x0001
AUDIO_HEADER_STRUCT = struct.Struct("<HHIHHBxxx")
AUDIO_HEADER_SIZE = AUDIO_HEADER_STRUCT.size
PROMPT_PCM_CACHE: dict[str, bytes] = {}
CHARACTER_PRESETS: dict[str, str] = {
    "companion": (
        "You are Dawn, a friendly AI voice toy companion. Keep responses short, warm, playful, "
        "and easy to understand when spoken aloud. Use plain spoken text only, no markdown, no headings, and no emoji."
    ),
    "storyteller": (
        "You are Dawn the storyteller. Tell child-friendly stories with vivid but simple language, "
        "clear structure, and gentle energy. Tell only a very short scene per turn, then pause. Use plain spoken text only, with no markdown or emoji."
    ),
    "language_teacher": (
        "You are Dawn the language teacher. Speak clearly, teach with short examples, gently correct mistakes, "
        "and encourage the learner. Keep spoken replies compact and practical. Use plain spoken text only, no markdown, no emoji."
    ),
    "curious_friend": (
        "You are Dawn the curious friend. Be upbeat, ask engaging follow-up questions, and celebrate curiosity "
        "without talking for too long. Use plain spoken text only, no markdown, no emoji."
    ),
    "bedtime_guide": (
        "You are Dawn the bedtime guide. Speak softly, calmly, and reassuringly, with cozy wording and very brief replies. Use plain spoken text only, no markdown, no emoji."
    ),
}
PRODUCT_STATE_PATH = Path(
    os.getenv(
        "BK7258_PRODUCT_STATE_PATH",
        str(BASE_DIR / "product_state.json"),
    )
).expanduser()
CHILD_AGE_BANDS = ["3-4", "5-6", "7-8", "9-10"]
SAFETY_MODES = {
    "balanced": "Keep replies age-appropriate, warm, and safe. Avoid scary, violent, or mature themes.",
    "gentle": "Keep replies extra gentle, emotionally safe, and reassuring for young children.",
    "independent_reader": "Encourage curiosity and learning while still staying child-safe and easy to understand.",
}
ContentCatalog = dict[str, dict[str, Any]]


DEFAULT_LEARNING_PACKS: ContentCatalog = {
    "english_starter": {
        "title": "English Starter",
        "summary": "Teach greetings, simple vocabulary, and short repeat-after-me phrases.",
        "prompt": "When helpful, include very short English practice phrases with repetition and encouragement.",
        "age_bands": ["3-4", "5-6", "7-8"],
        "goal_tags": ["english", "speaking", "confidence"],
        "topics": ["greetings", "daily words", "repeat after me"],
    },
    "phonics_fun": {
        "title": "Phonics Fun",
        "summary": "Help children notice sounds, letters, and simple pronunciation patterns.",
        "prompt": "Use playful sound-based examples, simple letter-sound links, and short phonics games when appropriate.",
        "age_bands": ["5-6", "7-8"],
        "goal_tags": ["phonics", "reading", "pronunciation"],
        "topics": ["letter sounds", "beginning reading", "word play"],
    },
    "social_skills": {
        "title": "Social Skills",
        "summary": "Model kindness, turn-taking, empathy, and friendly conversation.",
        "prompt": "Reinforce kind words, patience, sharing, and respectful communication through simple examples.",
        "age_bands": ["3-4", "5-6", "7-8", "9-10"],
        "goal_tags": ["kindness", "conversation", "manners"],
        "topics": ["sharing", "taking turns", "friendly words"],
    },
    "curiosity_science": {
        "title": "Curiosity Science",
        "summary": "Answer 'why' questions in child-friendly ways and suggest mini observations.",
        "prompt": "Explain simple science ideas in an easy, vivid way and invite the child to notice things around them.",
        "age_bands": ["5-6", "7-8", "9-10"],
        "goal_tags": ["science", "curiosity", "questions"],
        "topics": ["nature", "experiments", "observation"],
    },
}
DEFAULT_STORY_LIBRARY: ContentCatalog = {
    "forest_friends": {
        "title": "Forest Friends",
        "summary": "Gentle stories about animal friends helping each other in a bright forest.",
        "prompt": "If telling a story, you may draw on a warm forest world with helpful animal friends and simple morals.",
        "age_bands": ["3-4", "5-6", "7-8"],
        "goal_tags": ["bedtime", "kindness", "friendship"],
        "topics": ["animals", "forest", "helping others"],
    },
    "space_scouts": {
        "title": "Space Scouts",
        "summary": "Imaginative stories about brave young explorers solving kind problems in space.",
        "prompt": "If telling a story, you may use colorful space adventures that stay cozy, optimistic, and child-safe.",
        "age_bands": ["5-6", "7-8", "9-10"],
        "goal_tags": ["storytelling", "curiosity", "imagination"],
        "topics": ["space", "adventure", "problem solving"],
    },
    "everyday_bravery": {
        "title": "Everyday Bravery",
        "summary": "Stories about small acts of courage like trying new words, making friends, or asking questions.",
        "prompt": "If telling a story, emphasize everyday courage, kindness, and trying again after mistakes.",
        "age_bands": ["5-6", "7-8", "9-10"],
        "goal_tags": ["confidence", "resilience", "friendship"],
        "topics": ["new situations", "school", "trying again"],
    },
    "bedtime_breeze": {
        "title": "Bedtime Breeze",
        "summary": "Soft bedtime stories with quiet pacing and calm endings.",
        "prompt": "If telling a bedtime story, use calm language, soft imagery, and a peaceful ending.",
        "age_bands": ["3-4", "5-6", "7-8"],
        "goal_tags": ["bedtime", "calm", "sleep"],
        "topics": ["night", "calm breathing", "gentle imagery"],
    },
}
CONTENT_TOKEN_RE = re.compile(r"[a-z0-9]+")
CONTENT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "about",
    "at",
    "be",
    "can",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "please",
    "the",
    "to",
    "we",
    "with",
    "you",
    "your",
}
CONTENT_SEMANTIC_GROUPS: dict[str, set[str]] = {
    "story": {"story", "stories", "storytelling", "storyteller", "tale", "tales", "adventure"},
    "bedtime": {"bedtime", "sleep", "sleepy", "night", "goodnight", "dream", "dreams"},
    "calm": {"calm", "quiet", "gentle", "soft", "peaceful", "breathing"},
    "english": {"english", "language", "speak", "speaking", "word", "words", "phrase", "phrases", "vocabulary"},
    "phonics": {"phonics", "reading", "pronunciation", "letter", "letters", "sound", "sounds"},
    "social": {"social", "kind", "kindness", "friend", "friends", "friendship", "sharing", "manners", "polite", "conversation"},
    "science": {"science", "curious", "curiosity", "why", "question", "questions", "nature", "experiment", "experiments", "observation", "observe"},
    "space": {"space", "star", "stars", "planet", "planets", "moon", "rocket"},
    "confidence": {"confidence", "brave", "bravery", "courage", "resilience", "school", "trying", "try"},
}
CHARACTER_CONTENT_HINTS: dict[str, set[str]] = {
    "companion": {"social"},
    "storyteller": {"story", "space", "confidence"},
    "language_teacher": {"english", "phonics"},
    "curious_friend": {"science", "social"},
    "bedtime_guide": {"bedtime", "calm", "story"},
}
LEARNING_PACKS_PATH = CONTENT_DIR / "learning_packs.json"
STORY_LIBRARY_PATH = CONTENT_DIR / "story_library.json"
DEFAULT_PRODUCT_STATE_FIELDS: dict[str, Any] = {
    "device_name": "Dawn",
    "parent_name": "",
    "child_name": "Friend",
    "child_age_band": "5-6",
    "child_interests": "",
    "parent_goals": "",
    "safety_mode": "balanced",
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
    anthropic_api_key_override: str = ""
    openai_api_key_override: str = ""
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
ONBOARDING_QR_CACHE: dict[str, bytes] = {}
QR_HELPER_EXECUTABLE: Path | None = None


def iso_timestamp(timestamp: float | None = None) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S%z",
        time.localtime(timestamp if timestamp is not None else time.time()),
    )


DEFAULT_ACTIVITY_STATE: dict[str, Any] = {
    "recent_turns": [],
    "recent_sessions": [],
}


def trim_recent_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return items[: max(1, limit)]


def normalize_activity_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "recent_turns": [],
        "recent_sessions": [],
    }
    raw = raw or {}
    for key, limit in (
        ("recent_turns", RECENT_TURN_LIMIT),
        ("recent_sessions", RECENT_SESSION_LIMIT),
    ):
        value = raw.get(key)
        if isinstance(value, list):
            base[key] = trim_recent_items(
                [item for item in value if isinstance(item, dict)],
                limit,
            )
    return base


def load_activity_state() -> dict[str, Any]:
    if not ACTIVITY_STATE_PATH.exists():
        return normalize_activity_state(None)
    try:
        payload = json.loads(ACTIVITY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("failed to load activity state from {}", ACTIVITY_STATE_PATH)
        return normalize_activity_state(None)
    if not isinstance(payload, dict):
        return normalize_activity_state(None)
    return normalize_activity_state(payload)


ACTIVITY_STATE = load_activity_state()


def save_activity_state() -> None:
    try:
        ACTIVITY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_STATE_PATH.write_text(
            json.dumps(ACTIVITY_STATE, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("failed to save activity state to {}: {}", ACTIVITY_STATE_PATH, exc)


def append_activity_item(key: str, item: dict[str, Any], limit: int) -> None:
    bucket = ACTIVITY_STATE.setdefault(key, [])
    if not isinstance(bucket, list):
        bucket = []
        ACTIVITY_STATE[key] = bucket
    bucket.insert(0, item)
    del bucket[limit:]
    save_activity_state()


def activity_values(items: list[dict[str, Any]], field: str) -> list[float]:
    values: list[float] = []
    for item in items:
        value = item.get(field)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def average_value(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 1)


def median_value(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return round(values[mid], 1)
    return round((values[mid - 1] + values[mid]) / 2.0, 1)


def count_by_field(items: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        text = str(item.get(field, "")).strip()
        if not text:
            continue
        counts[text] = counts.get(text, 0) + 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(
            counts.items(),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )
    ]


def facet_values(items: list[dict[str, Any]], field: str) -> list[str]:
    return [item["value"] for item in count_by_field(items, field)]


def top_count_label(items: list[dict[str, Any]], field: str, fallback: str = "") -> str:
    ranked = count_by_field(items, field)
    if ranked:
        return str(ranked[0]["value"])
    return fallback


def activity_public_dict() -> dict[str, Any]:
    recent_turns = ACTIVITY_STATE.get("recent_turns", [])
    recent_sessions = ACTIVITY_STATE.get("recent_sessions", [])
    completed_turns = [item for item in recent_turns if isinstance(item, dict)]
    completed_sessions = [item for item in recent_sessions if isinstance(item, dict)]
    total_ms_values = activity_values(completed_turns, "total_ms")
    session_duration_values = activity_values(completed_sessions, "duration_sec")
    child_counts = count_by_field(completed_turns, "child_name")
    mode_counts = count_by_field(completed_turns, "character_preset")
    provider_counts = count_by_field(completed_turns, "llm_provider")
    return {
        "recent_turns": recent_turns,
        "recent_sessions": recent_sessions,
        "summary": {
            "recent_turn_count": len(recent_turns),
            "recent_session_count": len(recent_sessions),
            "average_total_ms": average_value(total_ms_values),
            "median_total_ms": median_value(total_ms_values),
            "fastest_total_ms": round(min(total_ms_values), 1) if total_ms_values else 0.0,
            "slowest_total_ms": round(max(total_ms_values), 1) if total_ms_values else 0.0,
            "average_session_duration_sec": average_value(session_duration_values),
            "top_child_name": top_count_label(completed_turns, "child_name"),
            "top_character_preset": top_count_label(completed_turns, "character_preset"),
            "top_llm_provider": top_count_label(completed_turns, "llm_provider"),
        },
        "facets": {
            "child_names": facet_values(completed_turns + completed_sessions, "child_name"),
            "character_presets": facet_values(completed_turns + completed_sessions, "character_preset"),
            "llm_providers": facet_values(completed_turns + completed_sessions, "llm_provider"),
        },
        "breakdowns": {
            "child_names": child_counts,
            "character_presets": mode_counts,
            "llm_providers": provider_counts,
        },
        "storage_path": str(ACTIVITY_STATE_PATH),
    }


def clone_content_catalog(catalog: ContentCatalog) -> ContentCatalog:
    return {key: dict(value) for key, value in catalog.items()}


def normalize_string_sequence(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def normalize_content_entry(entry_id: str, raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title", "")).strip() or entry_id.replace("_", " ").title()
    summary = str(raw.get("summary", "")).strip()
    prompt = str(raw.get("prompt", "")).strip()
    if not summary or not prompt:
        return None
    return {
        "title": title,
        "summary": summary,
        "prompt": prompt,
        "age_bands": [
            age_band
            for age_band in normalize_string_sequence(raw.get("age_bands"))
            if age_band in CHILD_AGE_BANDS
        ],
        "goal_tags": normalize_string_sequence(raw.get("goal_tags")),
        "topics": normalize_string_sequence(raw.get("topics")),
    }


def load_content_catalog(
    path: Path,
    fallback: ContentCatalog,
) -> ContentCatalog:
    if not path.exists():
        return clone_content_catalog(fallback)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("failed to load content catalog from {}", path)
        return clone_content_catalog(fallback)
    if not isinstance(payload, dict):
        logger.warning("content catalog at {} was not a JSON object", path)
        return clone_content_catalog(fallback)
    normalized: ContentCatalog = {}
    for entry_id, raw_entry in payload.items():
        key = str(entry_id).strip()
        if not key:
            continue
        entry = normalize_content_entry(key, raw_entry)
        if entry is None:
            logger.warning("skipping invalid content entry '{}' in {}", key, path)
            continue
        normalized[key] = entry
    if normalized:
        return normalized
    logger.warning("content catalog at {} had no valid entries", path)
    return clone_content_catalog(fallback)


LEARNING_PACKS = load_content_catalog(LEARNING_PACKS_PATH, DEFAULT_LEARNING_PACKS)
STORY_LIBRARY = load_content_catalog(STORY_LIBRARY_PATH, DEFAULT_STORY_LIBRARY)


def default_selected_ids(
    catalog: ContentCatalog,
    preferred: list[str],
) -> list[str]:
    selected = [entry_id for entry_id in preferred if entry_id in catalog]
    if selected:
        return selected
    return list(catalog)[:1]


def default_product_state() -> dict[str, Any]:
    base = dict(DEFAULT_PRODUCT_STATE_FIELDS)
    base["active_learning_pack_ids"] = default_selected_ids(
        LEARNING_PACKS,
        ["english_starter"],
    )
    base["active_story_ids"] = default_selected_ids(
        STORY_LIBRARY,
        ["forest_friends"],
    )
    return base


def sanitize_string_list(value: Any, *, allowed: set[str], fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text in allowed and text not in cleaned:
            cleaned.append(text)
    return cleaned or list(fallback)


def normalize_product_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_product_state()
    raw = raw or {}
    base["device_name"] = str(raw.get("device_name", base["device_name"])).strip() or base["device_name"]
    base["parent_name"] = str(raw.get("parent_name", base["parent_name"])).strip()
    base["child_name"] = str(raw.get("child_name", base["child_name"])).strip() or base["child_name"]
    age_band = str(raw.get("child_age_band", base["child_age_band"])).strip()
    base["child_age_band"] = age_band if age_band in CHILD_AGE_BANDS else base["child_age_band"]
    base["child_interests"] = str(raw.get("child_interests", base["child_interests"])).strip()
    base["parent_goals"] = str(raw.get("parent_goals", base["parent_goals"])).strip()
    safety_mode = str(raw.get("safety_mode", base["safety_mode"])).strip()
    base["safety_mode"] = safety_mode if safety_mode in SAFETY_MODES else base["safety_mode"]
    base["active_learning_pack_ids"] = sanitize_string_list(
        raw.get("active_learning_pack_ids"),
        allowed=set(LEARNING_PACKS),
        fallback=list(base["active_learning_pack_ids"]),
    )
    base["active_story_ids"] = sanitize_string_list(
        raw.get("active_story_ids"),
        allowed=set(STORY_LIBRARY),
        fallback=list(base["active_story_ids"]),
    )
    return base


def load_product_state() -> dict[str, Any]:
    if not PRODUCT_STATE_PATH.exists():
        return normalize_product_state(None)
    try:
        payload = json.loads(PRODUCT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("failed to load product state from {}", PRODUCT_STATE_PATH)
        return normalize_product_state(None)
    if not isinstance(payload, dict):
        return normalize_product_state(None)
    return normalize_product_state(payload)


PRODUCT_STATE = load_product_state()


def save_product_state() -> None:
    try:
        PRODUCT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRODUCT_STATE_PATH.write_text(
            json.dumps(PRODUCT_STATE, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("failed to save product state to {}: {}", PRODUCT_STATE_PATH, exc)


def unique_texts(items: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
    return cleaned


def normalize_content_token(token: str) -> str:
    token = token.strip().lower()
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and len(token) > 4 and not token.endswith("ss"):
        return token[:-1]
    return token


def content_tokens(text: str) -> list[str]:
    return unique_texts(
        [
            normalized
            for raw in CONTENT_TOKEN_RE.findall(text.lower())
            if (normalized := normalize_content_token(raw)) and normalized not in CONTENT_STOPWORDS
        ]
    )


def normalized_content_phrase(text: str) -> str:
    return " ".join(content_tokens(text))


def semantic_groups_for_tokens(tokens: set[str]) -> set[str]:
    groups: set[str] = set()
    for group, members in CONTENT_SEMANTIC_GROUPS.items():
        if tokens & members:
            groups.add(group)
    return groups


def semantic_group_label(group: str) -> str:
    labels = {
        "story": "story",
        "bedtime": "bedtime",
        "calm": "calm",
        "english": "English",
        "phonics": "phonics",
        "social": "social skills",
        "science": "curiosity",
        "space": "space",
        "confidence": "confidence",
    }
    return labels.get(group, group.replace("_", " "))


def product_keyword_text() -> str:
    return " ".join(
        value.strip().lower()
        for value in (
            str(PRODUCT_STATE.get("child_interests", "")),
            str(PRODUCT_STATE.get("parent_goals", "")),
        )
        if value.strip()
    )


def content_query_dict(user_text: str = "") -> dict[str, Any]:
    request_text = normalized_content_phrase(user_text)
    profile_text = normalized_content_phrase(product_keyword_text())
    request_tokens = set(content_tokens(user_text))
    profile_tokens = set(content_tokens(product_keyword_text()))
    mode_groups = set(CHARACTER_CONTENT_HINTS.get(RUNTIME_CONFIG.character_preset, set()))
    return {
        "request_text": request_text,
        "profile_text": profile_text,
        "request_tokens": request_tokens,
        "profile_tokens": profile_tokens,
        "request_groups": semantic_groups_for_tokens(request_tokens),
        "profile_groups": semantic_groups_for_tokens(profile_tokens),
        "mode_groups": mode_groups,
    }


def age_band_retrieval_score(
    target_age_band: str,
    entry_age_bands: list[str],
) -> tuple[int, str]:
    if not target_age_band or target_age_band not in CHILD_AGE_BANDS:
        return 0, ""
    candidate_indexes = [
        CHILD_AGE_BANDS.index(age_band)
        for age_band in entry_age_bands
        if age_band in CHILD_AGE_BANDS
    ]
    if not candidate_indexes:
        return 0, ""
    target_index = CHILD_AGE_BANDS.index(target_age_band)
    distance = min(abs(target_index - candidate_index) for candidate_index in candidate_indexes)
    if distance == 0:
        return 4, f"age {target_age_band}"
    if distance == 1:
        return 2, f"near age {target_age_band}"
    return 0, ""


def content_phrase_matches(phrases: list[str], normalized_query_text: str) -> list[str]:
    if not normalized_query_text:
        return []
    matches: list[str] = []
    for phrase in phrases:
        normalized_phrase = normalized_content_phrase(phrase)
        if normalized_phrase and normalized_phrase in normalized_query_text:
            matches.append(str(phrase).strip())
    return unique_texts(matches)


def catalog_entry_tokens(entry: dict[str, Any]) -> set[str]:
    return set(
        content_tokens(
            " ".join(
                [
                    str(entry.get("title", "")),
                    str(entry.get("summary", "")),
                    str(entry.get("prompt", "")),
                    *[str(tag) for tag in entry.get("goal_tags", [])],
                    *[str(topic) for topic in entry.get("topics", [])],
                ]
            )
        )
    )


def score_catalog_entry(
    entry_id: str,
    entry: dict[str, Any],
    *,
    selected_ids: list[str],
    user_text: str = "",
) -> dict[str, Any] | None:
    query = content_query_dict(user_text)
    score = 0
    reasons: list[str] = []
    matched_terms: list[str] = []

    age_score, age_reason = age_band_retrieval_score(
        str(PRODUCT_STATE.get("child_age_band", "")).strip(),
        list(entry.get("age_bands") or []),
    )
    score += age_score
    if age_reason:
        reasons.append(age_reason)

    if entry_id in selected_ids:
        score += 2
        reasons.append("selected for this toy")

    entry_tokens = catalog_entry_tokens(entry)
    entry_groups = semantic_groups_for_tokens(entry_tokens)
    phrase_candidates = [
        str(entry.get("title", "")),
        *[str(tag) for tag in entry.get("goal_tags", [])],
        *[str(topic) for topic in entry.get("topics", [])],
    ]

    request_phrase_matches = content_phrase_matches(phrase_candidates, query["request_text"])
    if request_phrase_matches:
        score += 4 * len(request_phrase_matches[:2])
        reasons.append("request: " + ", ".join(request_phrase_matches[:2]))
        matched_terms.extend(request_phrase_matches[:2])

    profile_phrase_matches = content_phrase_matches(phrase_candidates, query["profile_text"])
    if profile_phrase_matches:
        score += 3 * len(profile_phrase_matches[:2])
        reasons.append("profile: " + ", ".join(profile_phrase_matches[:2]))
        matched_terms.extend(profile_phrase_matches[:2])

    request_token_matches = sorted(entry_tokens & query["request_tokens"])
    if request_token_matches:
        score += 2 * len(request_token_matches[:3])
        reasons.append("request terms: " + ", ".join(request_token_matches[:3]))
        matched_terms.extend(request_token_matches[:3])

    profile_token_matches = sorted((entry_tokens & query["profile_tokens"]) - set(request_token_matches))
    if profile_token_matches:
        score += len(profile_token_matches[:2])
        reasons.append("profile terms: " + ", ".join(profile_token_matches[:2]))
        matched_terms.extend(profile_token_matches[:2])

    request_group_matches = sorted(entry_groups & query["request_groups"])
    if request_group_matches:
        score += 3 * len(request_group_matches[:2])
        reasons.append(
            "request themes: "
            + ", ".join(semantic_group_label(group) for group in request_group_matches[:2])
        )

    profile_group_matches = sorted((entry_groups & query["profile_groups"]) - set(request_group_matches))
    if profile_group_matches:
        score += 2 * len(profile_group_matches[:2])
        reasons.append(
            "profile themes: "
            + ", ".join(semantic_group_label(group) for group in profile_group_matches[:2])
        )

    mode_group_matches = sorted(
        (entry_groups & query["mode_groups"]) - set(request_group_matches) - set(profile_group_matches)
    )
    if mode_group_matches:
        score += len(mode_group_matches[:2])
        reasons.append(
            "character fit: "
            + ", ".join(semantic_group_label(group) for group in mode_group_matches[:2])
        )

    if score <= 0:
        return None

    matched_groups = unique_texts(
        [semantic_group_label(group) for group in sorted(entry_groups & (query["request_groups"] | query["profile_groups"] | query["mode_groups"]))]
    )
    return {
        "id": entry_id,
        "title": entry["title"],
        "summary": entry["summary"],
        "prompt": entry["prompt"],
        "score": score,
        "reasons": unique_texts(reasons)[:4],
        "matched_terms": unique_texts(matched_terms)[:4],
        "matched_groups": matched_groups[:3],
    }


def recommend_catalog_entries(
    catalog: ContentCatalog,
    *,
    selected_ids: list[str],
    limit: int = 3,
    user_text: str = "",
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for entry_id, entry in catalog.items():
        scored = score_catalog_entry(
            entry_id,
            entry,
            selected_ids=selected_ids,
            user_text=user_text,
        )
        if scored is not None:
            ranked.append(scored)
    ranked.sort(
        key=lambda item: (
            -int(item["score"]),
            -int(item["id"] in selected_ids),
            item["title"].lower(),
        ),
    )
    return ranked[:limit]


def content_prompt_limits(user_text: str = "") -> tuple[int, int]:
    query = content_query_dict(user_text)
    request_groups = set(query["request_groups"])
    story_focus = bool(request_groups & {"story", "bedtime", "calm", "space"}) or (
        RUNTIME_CONFIG.character_preset in {"storyteller", "bedtime_guide"}
    )
    learning_focus = bool(request_groups & {"english", "phonics", "science", "social"}) or (
        RUNTIME_CONFIG.character_preset in {"language_teacher", "curious_friend"}
    )
    if story_focus and not learning_focus:
        return 0, 2
    if learning_focus and not story_focus:
        return 2, 0
    if RUNTIME_CONFIG.character_preset in {"storyteller", "bedtime_guide"}:
        return 1, 2
    if RUNTIME_CONFIG.character_preset in {"language_teacher", "curious_friend"}:
        return 2, 1
    return 1, 1


def select_diverse_ranked_entries(
    ranked: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not ranked:
        return []
    remaining = list(ranked)
    selected: list[dict[str, Any]] = []
    covered_terms: set[str] = set()
    covered_groups: set[str] = set()
    while remaining and len(selected) < limit:
        best_index = 0
        best_adjusted_score = float("-inf")
        for index, item in enumerate(remaining):
            term_bonus = len(set(item.get("matched_terms", [])) - covered_terms) * 2
            group_bonus = len(set(item.get("matched_groups", [])) - covered_groups) * 3
            adjusted_score = float(item["score"]) + term_bonus + group_bonus
            if adjusted_score > best_adjusted_score:
                best_adjusted_score = adjusted_score
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        covered_terms.update(chosen.get("matched_terms", []))
        covered_groups.update(chosen.get("matched_groups", []))
    return selected


def active_catalog_prompt_entries(
    catalog: ContentCatalog,
    active_ids: list[str],
    *,
    user_text: str = "",
    limit: int = 1,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    active_catalog = {
        entry_id: catalog[entry_id]
        for entry_id in active_ids
        if entry_id in catalog
    }
    if not active_catalog:
        return []
    ranked = recommend_catalog_entries(
        active_catalog,
        selected_ids=list(active_catalog),
        limit=max(limit * 3, limit),
        user_text=user_text,
    )
    return select_diverse_ranked_entries(ranked, limit)


def runtime_content_context(user_text: str = "") -> dict[str, Any]:
    learning_limit, story_limit = content_prompt_limits(user_text)
    return {
        "strategy": "ranked-local-library",
        "learning_packs": active_catalog_prompt_entries(
            LEARNING_PACKS,
            list(PRODUCT_STATE["active_learning_pack_ids"]),
            user_text=user_text,
            limit=learning_limit,
        ),
        "story_library": active_catalog_prompt_entries(
            STORY_LIBRARY,
            list(PRODUCT_STATE["active_story_ids"]),
            user_text=user_text,
            limit=story_limit,
        ),
        "learning_limit": learning_limit,
        "story_limit": story_limit,
    }


def product_recommendations_dict() -> dict[str, Any]:
    learning_recommendations = recommend_catalog_entries(
        LEARNING_PACKS,
        selected_ids=list(PRODUCT_STATE["active_learning_pack_ids"]),
    )
    story_recommendations = recommend_catalog_entries(
        STORY_LIBRARY,
        selected_ids=list(PRODUCT_STATE["active_story_ids"]),
    )
    return {
        "strategy": "ranked-local-library",
        "learning_packs": learning_recommendations,
        "learning_pack_ids": [item["id"] for item in learning_recommendations],
        "story_library": story_recommendations,
        "story_ids": [item["id"] for item in story_recommendations],
    }


def product_public_dict() -> dict[str, Any]:
    return {
        "setup": dict(PRODUCT_STATE),
        "child_age_bands": CHILD_AGE_BANDS,
        "safety_modes": SAFETY_MODES,
        "learning_packs": LEARNING_PACKS,
        "story_library": STORY_LIBRARY,
        "recommendations": product_recommendations_dict(),
        "retrieval": runtime_content_context(),
        "content_files": {
            "content_dir": str(CONTENT_DIR),
            "learning_packs_path": str(LEARNING_PACKS_PATH),
            "story_library_path": str(STORY_LIBRARY_PATH),
        },
        "rag_mode": "ranked-local-library",
    }


def apply_product_state(update: dict[str, Any]) -> dict[str, Any]:
    global PRODUCT_STATE
    PRODUCT_STATE = normalize_product_state({**PRODUCT_STATE, **update})
    save_product_state()
    return product_public_dict()


def reload_product_content() -> dict[str, Any]:
    global LEARNING_PACKS, STORY_LIBRARY, PRODUCT_STATE
    LEARNING_PACKS = load_content_catalog(LEARNING_PACKS_PATH, DEFAULT_LEARNING_PACKS)
    STORY_LIBRARY = load_content_catalog(STORY_LIBRARY_PATH, DEFAULT_STORY_LIBRARY)
    PRODUCT_STATE = normalize_product_state(PRODUCT_STATE)
    save_product_state()
    return product_public_dict()


def record_turn_activity(session: Session, metrics: dict[str, Any]) -> None:
    append_activity_item(
        "recent_turns",
        {
            "timestamp": iso_timestamp(),
            "device_id": session.device_id,
            "peer": session.peer,
            "character_preset": RUNTIME_CONFIG.character_preset,
            "llm_provider": RUNTIME_CONFIG.llm_provider,
            "toy_name": PRODUCT_STATE["device_name"],
            "child_name": PRODUCT_STATE["child_name"],
            "turn_id": metrics.get("turn_id", 0),
            "transcript": metrics.get("transcript", ""),
            "reply": metrics.get("reply", ""),
            "stt_ms": metrics.get("stt_ms", 0.0),
            "llm_ms": metrics.get("llm_ms", 0.0),
            "tts_ms": metrics.get("tts_ms", 0.0),
            "total_ms": metrics.get("total_ms", 0.0),
            "commit_to_reply_ms": metrics.get("commit_to_reply_ms", 0.0),
        },
        RECENT_TURN_LIMIT,
    )


def record_session_activity(session: Session) -> None:
    append_activity_item(
        "recent_sessions",
        {
            "connected_at": iso_timestamp(session.connected_at),
            "ended_at": iso_timestamp(),
            "device_id": session.device_id,
            "peer": session.peer,
            "toy_name": PRODUCT_STATE["device_name"],
            "child_name": PRODUCT_STATE["child_name"],
            "character_preset": RUNTIME_CONFIG.character_preset,
            "llm_provider": RUNTIME_CONFIG.llm_provider,
            "duration_sec": round(time.time() - session.connected_at, 1),
            "turn_count": session.turn_counter,
            "last_turn_metrics": dict(session.last_turn_metrics),
        },
        RECENT_SESSION_LIMIT,
    )


def get_provider_api_key(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "anthropic":
        return RUNTIME_CONFIG.anthropic_api_key_override or ANTHROPIC_API_KEY
    if normalized == "openai":
        return RUNTIME_CONFIG.openai_api_key_override or OPENAI_API_KEY
    return ""


def provider_key_source(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "anthropic":
        if RUNTIME_CONFIG.anthropic_api_key_override:
            return "panel"
        return "env" if ANTHROPIC_API_KEY else "missing"
    if normalized == "openai":
        if RUNTIME_CONFIG.openai_api_key_override:
            return "panel"
        return "env" if OPENAI_API_KEY else "missing"
    return "missing"


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def llm_provider_available(provider: str) -> bool:
    return bool(get_provider_api_key(provider))


def effective_system_prompt(user_text: str = "") -> str:
    preset_name = RUNTIME_CONFIG.character_preset
    preset_prompt = CHARACTER_PRESETS.get(preset_name, CHARACTER_PRESETS["companion"])
    parent_name = PRODUCT_STATE["parent_name"]
    child_name = PRODUCT_STATE["child_name"]
    age_band = PRODUCT_STATE["child_age_band"]
    interests = PRODUCT_STATE["child_interests"]
    parent_goals = PRODUCT_STATE["parent_goals"]
    device_name = PRODUCT_STATE["device_name"]
    safety_prompt = SAFETY_MODES.get(
        PRODUCT_STATE["safety_mode"],
        SAFETY_MODES["balanced"],
    )
    retrieval = runtime_content_context(user_text)
    active_pack_prompts = retrieval["learning_packs"]
    active_story_prompts = retrieval["story_library"]
    custom_prompt = RUNTIME_CONFIG.system_prompt.strip()
    sections = [
        preset_prompt,
        f"Your toy name is {device_name}.",
        f"You are talking to a child named {child_name} in the age band {age_band}.",
        safety_prompt,
        (
            "Response rules: use plain spoken text only. No markdown, no bullet lists, no headings, and no emoji. "
            "Default to 1 or 2 short sentences. If the child asks for a story, tell only one very short scene in up to 2 short sentences, then stop."
        ),
    ]
    if interests:
        sections.append(f"Child interests: {interests}.")
    if parent_name:
        sections.append(f"The parent or guardian is {parent_name}.")
    if parent_goals:
        sections.append(f"Parent goals: {parent_goals}.")
    if active_pack_prompts:
        sections.append(
            "Relevant learning guidance:\n"
            + "\n".join(
                f"- {item['title']}: {item['prompt']}"
                for item in active_pack_prompts
            )
        )
    if active_story_prompts:
        sections.append(
            "Relevant story guidance:\n"
            + "\n".join(
                f"- {item['title']}: {item['prompt']}"
                for item in active_story_prompts
            )
        )
    if custom_prompt:
        sections.append(f"Additional runtime instructions:\n{custom_prompt}")
    return "\n\n".join(sections)


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
        "provider_key_source": {
            "anthropic": provider_key_source("anthropic"),
            "openai": provider_key_source("openai"),
        },
        "provider_key_masked": {
            "anthropic": mask_key(get_provider_api_key("anthropic")),
            "openai": mask_key(get_provider_api_key("openai")),
        },
        "startup_greeting_enabled": RUNTIME_CONFIG.startup_greeting_enabled,
        "startup_listen_prime_enabled": RUNTIME_CONFIG.startup_listen_prime_enabled,
        "wait_for_idle_before_startup": RUNTIME_CONFIG.wait_for_idle_before_startup,
        "processing_prompt_enabled": RUNTIME_CONFIG.processing_prompt_enabled,
        "processing_prompt_text": RUNTIME_CONFIG.processing_prompt_text,
        "product": product_public_dict(),
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
    if "anthropic_api_key" in update:
        RUNTIME_CONFIG.anthropic_api_key_override = str(
            update["anthropic_api_key"]
        ).strip()
    if "openai_api_key" in update:
        RUNTIME_CONFIG.openai_api_key_override = str(update["openai_api_key"]).strip()
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


def trim_history_for_llm(history: list[dict[str, str]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in history:
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            cleaned.append({"role": role, "content": content})
    if DEFAULT_LLM_HISTORY_MESSAGES <= 0:
        return []
    return cleaned[-DEFAULT_LLM_HISTORY_MESSAGES:]


def wants_story_reply(user_text: str) -> bool:
    lowered = user_text.strip().lower()
    if not lowered:
        return False
    story_keywords = (
        "story",
        "bedtime",
        "once upon",
        "tale",
        "adventure",
    )
    if any(keyword in lowered for keyword in story_keywords):
        return True
    return RUNTIME_CONFIG.character_preset in {"storyteller", "bedtime_guide"}


def llm_max_tokens_for_request(user_text: str) -> int:
    if wants_story_reply(user_text):
        return STORY_LLM_MAX_TOKENS
    return DEFAULT_LLM_MAX_TOKENS


def spoken_sentence_limit(user_text: str) -> int:
    if wants_story_reply(user_text):
        return STORY_REPLY_SENTENCE_LIMIT
    return DEFAULT_REPLY_SENTENCE_LIMIT


def spoken_word_limit(user_text: str) -> int:
    if wants_story_reply(user_text):
        return STORY_REPLY_WORD_LIMIT
    return DEFAULT_REPLY_WORD_LIMIT


def strip_unspoken_symbols(text: str) -> str:
    return "".join(
        ch for ch in text if unicodedata.category(ch) not in {"So", "Cs"}
    )


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_markdown_for_speech(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", text)
    text = re.sub(r"(^|\n)\s*[-*]\s+", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return text


def limit_sentences(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [sentence.strip() for sentence in sentences if sentence.strip()]
    if not sentences:
        return text.strip()
    return " ".join(sentences[:limit]).strip()


def limit_words(text: str, limit: int) -> str:
    words = text.split()
    if limit <= 0 or len(words) <= limit:
        return text
    trimmed = " ".join(words[:limit]).rstrip(",;:-")
    trailing_fillers = {"and", "but", "so", "because", "then", "or"}
    while trimmed:
        last_word = trimmed.split()[-1].strip(".,!?;:").lower()
        if last_word not in trailing_fillers:
            break
        trimmed = " ".join(trimmed.split()[:-1]).rstrip(",;:-")
    if trimmed and trimmed[-1] not in ".!?":
        trimmed += "."
    return trimmed


def finalize_spoken_reply(text: str, user_text: str) -> str:
    text = strip_markdown_for_speech(text)
    text = strip_unspoken_symbols(text)
    text = collapse_whitespace(text)
    text = limit_sentences(text, spoken_sentence_limit(user_text))
    text = limit_words(text, spoken_word_limit(user_text))
    text = collapse_whitespace(text)
    return text or "Sorry, I had a problem. Try again."


def ask_anthropic(user_text: str, history: list[dict[str, str]]) -> str:
    """Generate a short assistant reply with Anthropic."""
    api_key = get_provider_api_key("anthropic")
    trimmed_history = trim_history_for_llm(history)
    try:
        response = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": RUNTIME_CONFIG.anthropic_model,
                "max_tokens": llm_max_tokens_for_request(user_text),
                "system": effective_system_prompt(user_text),
                "messages": trimmed_history + [{"role": "user", "content": user_text}],
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
        return finalize_spoken_reply(payload["content"][0]["text"].strip(), user_text)
    except (KeyError, IndexError, TypeError):
        logger.warning("Anthropic response shape was unexpected: {}", payload)
        return "Sorry, I had a problem. Try again."


def build_openai_messages(
    user_text: str,
    history: list[dict[str, str]],
) -> list[dict[str, Any]]:
    history = trim_history_for_llm(history)
    messages: list[dict[str, Any]] = [
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": effective_system_prompt(user_text)}],
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
    api_key = get_provider_api_key("openai")
    if not api_key:
        return "OpenAI API key is not configured on this server."

    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": RUNTIME_CONFIG.openai_model,
                "input": build_openai_messages(user_text, history),
                "max_output_tokens": llm_max_tokens_for_request(user_text),
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
        return finalize_spoken_reply(text, user_text)
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
            record_turn_activity(session, session.last_turn_metrics)
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
    record_session_activity(session)
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
        "turn_counter": session.turn_counter,
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
            "admin_panel_url": admin_panel_url(),
            "onboarding_url": f"{admin_panel_url().rstrip('/')}/onboarding",
            "control_panel_scope": (
                "lan" if ADMIN_HOST in {"0.0.0.0", "::", ""} else "local-only"
            ),
            "panel_access": "protected" if PANEL_ACCESS_CODE else "open",
        },
        "config": config_public_dict(),
        "product": product_public_dict(),
        "activity": activity_public_dict(),
        "sessions": sessions,
        "connected_session_count": len(sessions),
    }


def make_http_response(
    status: str,
    body: bytes,
    *,
    content_type: str,
    extra_headers: list[tuple[str, str]] | None = None,
) -> bytes:
    header_lines = [
        f"HTTP/1.1 {status}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if extra_headers:
        for name, value in extra_headers:
            header_lines.append(f"{name}: {value}")
    return (
        ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + body
    )


def admin_panel_url() -> str:
    if ADMIN_HOST in {"0.0.0.0", "::", ""}:
        host = detect_local_ipv4()
    elif ADMIN_HOST in {"127.0.0.1", "localhost", "::1"}:
        host = "127.0.0.1"
    else:
        host = ADMIN_HOST
    return f"http://{host}:{ADMIN_PORT}/"


PANEL_AUTH_COOKIE_NAME = "bk7258_panel_auth"


def panel_auth_cookie_value() -> str:
    if not PANEL_ACCESS_CODE:
        return ""
    return hashlib.sha256(PANEL_ACCESS_CODE.encode("utf-8")).hexdigest()


def parse_cookie_header(raw_cookie: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def admin_request_authorized(headers: dict[str, str]) -> bool:
    if not PANEL_ACCESS_CODE:
        return True
    expected = panel_auth_cookie_value()
    header_code = headers.get("x-panel-code", "").strip()
    if header_code and hashlib.sha256(header_code.encode("utf-8")).hexdigest() == expected:
        return True
    cookies = parse_cookie_header(headers.get("cookie", ""))
    return cookies.get(PANEL_AUTH_COOKIE_NAME, "") == expected


def auth_required_json_response() -> bytes:
    return make_json_response(
        "401 Unauthorized",
        {"ok": False, "error": "panel authentication required"},
    )


def render_login_panel() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BK7258 Parent Access</title>
  <style>
    :root {
      --bg: #f4efe5;
      --card: #fffaf0;
      --ink: #1f2933;
      --soft: #52606d;
      --accent: #0f766e;
      --line: #d9cbb8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(180,83,9,0.14), transparent 24%),
        linear-gradient(180deg, #faf5eb, var(--bg));
    }
    .card {
      width: min(92vw, 420px);
      padding: 22px;
      border-radius: 24px;
      background: rgba(255,250,240,0.96);
      border: 1px solid var(--line);
      box-shadow: 0 18px 48px rgba(31,41,51,0.10);
    }
    h1 { margin: 0 0 10px; font-size: 2rem; line-height: 1; }
    p { color: var(--soft); line-height: 1.45; }
    label { display: block; margin: 14px 0 6px; color: var(--soft); }
    input, button {
      width: 100%;
      font: inherit;
      border-radius: 12px;
    }
    input {
      border: 1px solid var(--line);
      padding: 12px;
      background: #fffdf8;
      color: var(--ink);
    }
    button {
      margin-top: 14px;
      border: 0;
      padding: 12px;
      background: linear-gradient(135deg, var(--accent), #155e75);
      color: white;
      cursor: pointer;
    }
    .msg { margin-top: 10px; min-height: 1.3em; font-size: 0.95rem; }
  </style>
</head>
<body>
  <section class="card">
    <h1>Parent Access</h1>
    <p>Enter the local pairing code to open the BK7258 control panel on this network.</p>
    <label for="panelCode">Access code</label>
    <input id="panelCode" type="password" autocomplete="current-password">
    <button id="loginBtn">Open Control Panel</button>
    <div id="msg" class="msg"></div>
  </section>
  <script>
    async function login() {
      const msg = document.getElementById("msg");
      const code = document.getElementById("panelCode").value.trim();
      msg.textContent = "Checking...";
      const response = await fetch("/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code })
      });
      const data = await response.json();
      if (response.ok && data.ok) {
        window.location.href = "/";
        return;
      }
      msg.textContent = data.error || "Access denied.";
    }
    document.getElementById("loginBtn").addEventListener("click", login);
    document.getElementById("panelCode").addEventListener("keydown", (event) => {
      if (event.key === "Enter") login();
    });
  </script>
</body>
</html>"""


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


def qr_helper_executable_path() -> Path:
    global QR_HELPER_EXECUTABLE
    if QR_HELPER_EXECUTABLE and QR_HELPER_EXECUTABLE.exists():
        return QR_HELPER_EXECUTABLE

    helper_dir = Path(tempfile.gettempdir()) / "bk7258_qr_helper"
    helper_dir.mkdir(parents=True, exist_ok=True)
    source_path = helper_dir / "qr_helper.swift"
    executable_path = helper_dir / "qr_helper"
    source = """import Foundation
import CoreImage
import AppKit

let input = CommandLine.arguments[1]
let data = Data(input.utf8)
guard let filter = CIFilter(name: "CIQRCodeGenerator") else { fatalError("missing qr filter") }
filter.setValue(data, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let ciImage = filter.outputImage else { fatalError("missing qr image") }
let scaled = ciImage.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
let rep = NSCIImageRep(ciImage: scaled)
let image = NSImage(size: rep.size)
image.addRepresentation(rep)
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("failed to convert qr to png")
}
FileHandle.standardOutput.write(png)
"""
    try:
        if not source_path.exists() or source_path.read_text(encoding="utf-8") != source:
            source_path.write_text(source, encoding="utf-8")
        if not executable_path.exists():
            subprocess.run(
                ["swiftc", str(source_path), "-o", str(executable_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("failed to build local QR helper: {}", exc)
        raise RuntimeError("failed to build local QR helper") from exc
    QR_HELPER_EXECUTABLE = executable_path
    return executable_path


def generate_local_qr_png(value: str) -> bytes | None:
    cached = ONBOARDING_QR_CACHE.get(value)
    if cached is not None:
        return cached
    try:
        executable_path = qr_helper_executable_path()
        result = subprocess.run(
            [str(executable_path), value],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.warning("failed to generate local QR PNG: {}", exc)
        return None
    if not result.stdout:
        return None
    ONBOARDING_QR_CACHE[value] = result.stdout
    return result.stdout


def onboarding_public_dict() -> dict[str, Any]:
    panel_url = admin_panel_url()
    onboarding_url = f"{panel_url.rstrip('/')}/onboarding"
    chip_target = urlsplit(CHIP_ENDPOINT)
    chip_host = chip_target.hostname or ""
    return {
        "panel_url": panel_url,
        "onboarding_url": onboarding_url,
        "panel_access": "protected" if PANEL_ACCESS_CODE else "open",
        "access_code_required": bool(PANEL_ACCESS_CODE),
        "same_wifi_required": True,
        "server_ipv4": detect_local_ipv4(),
        "chip_host": chip_host,
        "chip_port": chip_target.port or PORT,
        "copy_text": (
            "BK7258 parent setup\n"
            f"Open this on your phone: {panel_url}\n"
            "Stay on the same Wi-Fi as the Mac and the toy.\n"
            + (
                "You will need the local access code after the page opens.\n"
                if PANEL_ACCESS_CODE
                else "No access code is currently required on this local network.\n"
            )
        ).strip(),
        "phone_steps": [
            "Connect your phone to the same Wi-Fi as the Mac and the toy.",
            f"Open {panel_url} or scan the QR code on this page.",
            (
                "Enter the local access code if the parent panel is protected."
                if PANEL_ACCESS_CODE
                else "The panel is currently open on the local network."
            ),
            "Use Add to Home Screen to make the panel feel like an app.",
        ],
        "qr_image_url": f"{panel_url.rstrip('/')}/onboarding-qr.png",
        "qr_mode": "local-macos",
    }


def render_onboarding_page() -> str:
    onboarding = onboarding_public_dict()
    panel_url = html.escape(onboarding["panel_url"])
    onboarding_url = html.escape(onboarding["onboarding_url"])
    qr_image_url = html.escape(onboarding["qr_image_url"])
    copy_text = json.dumps(onboarding["copy_text"], ensure_ascii=True)
    steps_html = "".join(
        f"<li>{html.escape(step)}</li>" for step in onboarding["phone_steps"]
    )
    access_line = (
        "This parent panel is currently protected with a local access code."
        if onboarding["access_code_required"]
        else "This parent panel is currently open to devices on the same Wi-Fi."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f766e">
  <title>BK7258 Parent Onboarding</title>
  <style>
    :root {{
      --bg: #f4efe5;
      --card: #fffaf0;
      --ink: #1f2933;
      --soft: #52606d;
      --accent: #0f766e;
      --accent-2: #b45309;
      --line: #d9cbb8;
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
      max-width: 1040px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }}
    .hero, .card {{
      background: rgba(255,250,240,0.94);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 18px 48px rgba(31,41,51,0.08);
    }}
    .hero {{
      padding: 22px;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(2rem, 6vw, 3.8rem);
      line-height: 0.98;
      letter-spacing: -0.03em;
    }}
    p, li {{
      color: var(--soft);
      line-height: 1.45;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }}
    .card {{
      padding: 18px;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }}
    a.button, button {{
      appearance: none;
      border: 0;
      border-radius: 12px;
      padding: 12px 14px;
      background: linear-gradient(135deg, var(--accent), #155e75);
      color: white;
      text-decoration: none;
      font: inherit;
      cursor: pointer;
    }}
    button.secondary {{
      background: linear-gradient(135deg, var(--accent-2), #92400e);
    }}
    .url-box {{
      margin-top: 12px;
      padding: 12px;
      border-radius: 16px;
      background: #fffdf8;
      border: 1px solid var(--line);
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .qr {{
      width: min(100%, 260px);
      border-radius: 18px;
      border: 1px solid var(--line);
      background: white;
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
    .pill a {{
      color: inherit;
      text-decoration: none;
      font-weight: 600;
    }}
    .muted {{
      margin-top: 10px;
      color: var(--soft);
      font-size: 0.92rem;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Parent Onboarding</h1>
      <p>This page is the product-style handoff for a phone. It gives the parent the local link, QR path, and the exact next step to reach the BK7258 control panel.</p>
      <div style="margin-top:14px;">
        <span class="pill">Panel: {panel_url}</span>
        <span class="pill">Onboarding: {onboarding_url}</span>
        <span class="pill">Access: {"protected" if onboarding["access_code_required"] else "open"}</span>
      </div>
    </section>
    <div class="grid">
      <section class="card">
        <h2>Phone Steps</h2>
        <ol>
          {steps_html}
        </ol>
        <p>{html.escape(access_line)}</p>
        <div class="actions">
          <a class="button" href="{panel_url}">Open Parent Panel</a>
          <button id="copyBtn" class="secondary">Copy Phone Link</button>
        </div>
        <div class="url-box">{panel_url}</div>
        <p id="copyResult" class="muted"></p>
      </section>
      <section class="card">
        <h2>Scan On Phone</h2>
        <img class="qr" src="{qr_image_url}" alt="QR code for the BK7258 parent panel">
        <p class="muted">This QR is generated locally on the Mac mini. If it does not load, open the link manually.</p>
      </section>
    </div>
  </div>
  <script>
    const copyText = {copy_text};
    document.getElementById("copyBtn").addEventListener("click", async () => {{
      try {{
        await navigator.clipboard.writeText(copyText);
        document.getElementById("copyResult").textContent = "Phone setup text copied.";
      }} catch (_error) {{
        document.getElementById("copyResult").textContent = "Copy failed. Copy the URL manually.";
      }}
    }});
  </script>
</body>
</html>"""


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
    .option-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .option-card {{
      display: block;
      padding: 12px;
      border-radius: 16px;
      border: 1px solid rgba(15,118,110,0.14);
      background: rgba(255,255,255,0.9);
    }}
    .option-card.recommended {{
      border-color: rgba(180,83,9,0.38);
      box-shadow: inset 0 0 0 1px rgba(180,83,9,0.16);
      background: linear-gradient(180deg, rgba(255,248,233,0.98), rgba(255,255,255,0.94));
    }}
    .option-card input {{
      width: auto;
      margin-right: 8px;
    }}
    .option-card strong {{
      display: block;
      font-size: 0.96rem;
      margin-bottom: 4px;
    }}
    .option-card span {{
      display: block;
      color: var(--soft);
      font-size: 0.86rem;
      line-height: 1.35;
    }}
    .meta-tags {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .mini-tag {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(15,118,110,0.08);
      color: var(--accent);
      font-size: 0.76rem;
      line-height: 1.2;
    }}
    .content-filter-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .content-filter-row label {{
      display: flex;
      gap: 8px;
      align-items: center;
      margin: 0;
    }}
    .content-filter-row input[type="checkbox"] {{
      width: auto;
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
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .summary-card {{
      padding: 12px;
      border-radius: 16px;
      border: 1px solid rgba(15,118,110,0.12);
      background: rgba(255,255,255,0.88);
    }}
    .summary-card strong {{
      display: block;
      font-size: 1.2rem;
      line-height: 1;
      margin-bottom: 4px;
    }}
    .summary-card span {{
      display: block;
      color: var(--soft);
      font-size: 0.82rem;
      line-height: 1.35;
    }}
    .activity-list {{
      display: grid;
      gap: 10px;
    }}
    .activity-item {{
      padding: 12px;
      border-radius: 16px;
      border: 1px solid rgba(15,118,110,0.12);
      background: rgba(255,255,255,0.84);
    }}
    .activity-item strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 0.97rem;
    }}
    .activity-item span {{
      display: block;
      color: var(--soft);
      font-size: 0.86rem;
      line-height: 1.35;
    }}
    .filter-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    @media (max-width: 720px) {{
      .summary-grid,
      .filter-grid {{
        grid-template-columns: 1fr;
      }}
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
        <a href="/onboarding" target="_blank" rel="noreferrer">Open phone onboarding page</a>
      </section>
      <section class="card section-stack">
        <h2>Family Setup</h2>
        <label for="deviceName">Toy name</label>
        <input id="deviceName" value="">
        <label for="parentName">Parent name</label>
        <input id="parentName" value="">
        <label for="childName">Child name</label>
        <input id="childName" value="">
        <label for="childAgeBand">Child age band</label>
        <select id="childAgeBand"></select>
        <label for="childInterests">Child interests</label>
        <input id="childInterests" value="" placeholder="dinosaurs, bedtime stories, colors">
        <label for="parentGoals">Parent goals</label>
        <textarea id="parentGoals" placeholder="Help with English speaking, bedtime calm-down, asking questions politely"></textarea>
        <label for="safetyMode">Safety mode</label>
        <select id="safetyMode"></select>
        <button id="saveFamily" class="secondary">Save Family Setup</button>
        <button id="reloadContent" class="ghost">Reload Content Files</button>
        <p id="familyResult" class="muted"></p>
        <p id="reloadContentResult" class="muted"></p>
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
        <h2>Family Dashboard</h2>
        <div id="activitySummaryGrid" class="summary-grid"></div>
        <div class="filter-grid">
          <div>
            <label for="activityChildFilter">Filter by child</label>
            <select id="activityChildFilter"></select>
          </div>
          <div>
            <label for="activityProviderFilter">Filter by provider</label>
            <select id="activityProviderFilter"></select>
          </div>
          <div>
            <label for="activityCharacterFilter">Filter by character</label>
            <select id="activityCharacterFilter"></select>
          </div>
          <div>
            <label for="activitySearch">Search recent activity</label>
            <input id="activitySearch" placeholder="story, bedtime, hello, english">
          </div>
        </div>
        <div id="activitySummary"></div>
        <div id="activityBreakdowns" class="activity-list"></div>
      </section>
      <section class="card section-stack">
        <h2>Recent Turns</h2>
        <div id="recentTurns" class="activity-list"></div>
      </section>
      <section class="card section-stack">
        <h2>Recent Sessions</h2>
        <div id="recentSessions" class="activity-list"></div>
      </section>
      <section class="card section-stack">
        <h2>Content Guide</h2>
        <label for="contentSearch">Search stories and lessons</label>
        <input id="contentSearch" placeholder="space, bedtime, English, kindness">
        <div class="content-filter-row">
          <label><input id="recommendedOnly" type="checkbox"> Show recommended only</label>
        </div>
        <div id="contentRecommendations"></div>
      </section>
      <section class="card section-stack">
        <h2>Learning Packs</h2>
        <div id="learningPackGrid" class="option-grid"></div>
        <p class="muted">These act like early curriculum packs for the toy and feed child-learning context into replies.</p>
      </section>
      <section class="card section-stack">
        <h2>Story Library</h2>
        <div id="storyLibraryGrid" class="option-grid"></div>
        <p class="muted">This is the first product version of a story knowledge library. Later we can replace this with full RAG and cloud content.</p>
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
        <label for="anthropicApiKey">Anthropic API key</label>
        <input id="anthropicApiKey" type="password" placeholder="Paste Anthropic key here if you want to override server .env">
        <label for="openaiModel">OpenAI model</label>
        <input id="openaiModel" value="">
        <label for="openaiApiKey">OpenAI API key</label>
        <input id="openaiApiKey" type="password" placeholder="Paste OpenAI key here if you want to override server .env">
        <p id="keyStatus" class="muted"></p>
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
    const state = {{
      status: initialStatus,
      contentFilters: {{
        search: "",
        recommendedOnly: false,
      }},
      activityFilters: {{
        childName: "",
        provider: "",
        character: "",
        search: "",
      }},
    }};

    function showTransport(status) {{
      const transport = status.transport;
      const availability = status.config.provider_availability;
      const keySource = status.config.provider_key_source;
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
        <span class="pill"><a href="${{
          transport.onboarding_url
        }}" target="_blank" rel="noreferrer">Phone onboarding</a></span>
        <span class="pill">Panel scope: ${{
          transport.control_panel_scope
        }}</span>
        <span class="pill">Panel access: ${{
          transport.panel_access
        }}</span>
        <span class="pill">Sessions: ${{
          status.connected_session_count
        }}</span>
        <span class="pill">Anthropic key: ${{
          availability.anthropic ? keySource.anthropic : "missing"
        }}</span>
        <span class="pill">OpenAI key: ${{
          availability.openai ? keySource.openai : "missing"
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

    function recommendationMapFromList(items) {{
      const map = {{}};
      for (const item of (items || [])) {{
        if (item && item.id) map[item.id] = item;
      }}
      return map;
    }}

    function contentMatchesSearch(item, searchText) {{
      if (!searchText) return true;
      const haystack = [
        item.title,
        item.summary,
        ...(item.age_bands || []),
        ...(item.goal_tags || []),
        ...(item.topics || []),
      ].join(" ").toLowerCase();
      return haystack.includes(searchText);
    }}

    function renderSelectableCards(containerId, items, selectedIds, recommendationList) {{
      const container = document.getElementById(containerId);
      container.innerHTML = "";
      const filters = state.contentFilters || {{}};
      const searchText = String(filters.search || "").trim().toLowerCase();
      const recommendedOnly = !!filters.recommendedOnly;
      const recommendationMap = recommendationMapFromList(recommendationList);
      let rendered = 0;
      for (const [key, item] of Object.entries(items || {{}})) {{
        const recommended = !!recommendationMap[key];
        if (recommendedOnly && !recommended) {{
          continue;
        }}
        if (!contentMatchesSearch(item, searchText)) {{
          continue;
        }}
        const label = document.createElement("label");
        label.className = "option-card" + (recommended ? " recommended" : "");
        const tags = [
          ...(recommended && recommendationMap[key].reasons?.length
            ? [`Recommended: ${{recommendationMap[key].reasons[0]}}`]
            : []),
          ...((item.age_bands || []).map((age) => `Age ${{age}}`)),
          ...((item.goal_tags || []).slice(0, 2)),
          ...((item.topics || []).slice(0, 2)),
        ];
        label.innerHTML = `
          <strong><input type="checkbox" value="${{key}}" ${{
            selectedIds.includes(key) ? "checked" : ""
          }}> ${{item.title}}</strong>
          <span>${{item.summary}}</span>
          <div class="meta-tags">${{
            tags.map((tag) => `<span class="mini-tag">${{tag}}</span>`).join("")
          }}</div>
        `;
        container.appendChild(label);
        rendered += 1;
      }}
      if (!rendered) {{
        container.innerHTML = '<div class="activity-item"><span>No content matched the current filter.</span></div>';
      }}
    }}

    function selectedCardValues(containerId) {{
      return Array.from(
        document.querySelectorAll(`#${{containerId}} input[type="checkbox"]:checked`)
      ).map((node) => node.value);
    }}

    function renderContentRecommendations(product) {{
      const recommendations = product.recommendations || {{}};
      const learning = recommendations.learning_packs || [];
      const stories = recommendations.story_library || [];
      const box = document.getElementById("contentRecommendations");
      box.innerHTML = `
        <span class="pill">Suggested lessons: ${{
          learning.map((item) => item.title).join(", ") || "none yet"
        }}</span>
        <span class="pill">Suggested stories: ${{
          stories.map((item) => item.title).join(", ") || "none yet"
        }}</span>
      `;
    }}

    function populateProduct(status) {{
      const product = status.product || status.config.product || {{}};
      const setup = product.setup || {{}};
      const ageSelect = document.getElementById("childAgeBand");
      const safetySelect = document.getElementById("safetyMode");
      ageSelect.innerHTML = "";
      for (const ageBand of (product.child_age_bands || [])) {{
        const option = document.createElement("option");
        option.value = ageBand;
        option.textContent = ageBand;
        if (ageBand === setup.child_age_band) option.selected = true;
        ageSelect.appendChild(option);
      }}
      safetySelect.innerHTML = "";
      for (const [key, value] of Object.entries(product.safety_modes || {{}})) {{
        const option = document.createElement("option");
        option.value = key;
        option.textContent = key.replaceAll("_", " ");
        option.title = value;
        if (key === setup.safety_mode) option.selected = true;
        safetySelect.appendChild(option);
      }}
      document.getElementById("deviceName").value = setup.device_name || "";
      document.getElementById("parentName").value = setup.parent_name || "";
      document.getElementById("childName").value = setup.child_name || "";
      document.getElementById("childInterests").value = setup.child_interests || "";
      document.getElementById("parentGoals").value = setup.parent_goals || "";
      document.getElementById("contentSearch").value = state.contentFilters.search || "";
      document.getElementById("recommendedOnly").checked = !!state.contentFilters.recommendedOnly;
      renderContentRecommendations(product);
      renderSelectableCards(
        "learningPackGrid",
        product.learning_packs || {{}},
        setup.active_learning_pack_ids || [],
        product.recommendations?.learning_packs || [],
      );
      renderSelectableCards(
        "storyLibraryGrid",
        product.story_library || {{}},
        setup.active_story_ids || [],
        product.recommendations?.story_library || [],
      );
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    function humanizeKey(value) {{
      const text = String(value || "").trim();
      if (!text) return "";
      return text.replaceAll("_", " ");
    }}

    function populateSelectOptions(selectId, values, allLabel, selectedValue) {{
      const select = document.getElementById(selectId);
      const uniqueValues = Array.from(new Set((values || []).filter((value) => String(value || "").trim())));
      const normalizedSelected = uniqueValues.includes(selectedValue) ? selectedValue : "";
      select.innerHTML = "";
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = allLabel;
      select.appendChild(allOption);
      for (const value of uniqueValues) {{
        const option = document.createElement("option");
        option.value = value;
        option.textContent = humanizeKey(value);
        if (value === normalizedSelected) option.selected = true;
        select.appendChild(option);
      }}
      if (!normalizedSelected) {{
        select.value = "";
      }}
      return normalizedSelected;
    }}

    function populateActivityFilters(status) {{
      const activity = status.activity || {{}};
      const facets = activity.facets || {{}};
      state.activityFilters.childName = populateSelectOptions(
        "activityChildFilter",
        facets.child_names || [],
        "All children",
        state.activityFilters.childName,
      );
      state.activityFilters.provider = populateSelectOptions(
        "activityProviderFilter",
        facets.llm_providers || [],
        "All providers",
        state.activityFilters.provider,
      );
      state.activityFilters.character = populateSelectOptions(
        "activityCharacterFilter",
        facets.character_presets || [],
        "All characters",
        state.activityFilters.character,
      );
      document.getElementById("activitySearch").value = state.activityFilters.search || "";
    }}

    function itemSearchText(item) {{
      return [
        item.child_name,
        item.character_preset,
        item.llm_provider,
        item.transcript,
        item.reply,
        item.connected_at,
        item.timestamp,
      ].join(" ").toLowerCase();
    }}

    function filterActivityItems(items) {{
      const filters = state.activityFilters || {{}};
      const searchText = String(filters.search || "").trim().toLowerCase();
      return (items || []).filter((item) => {{
        if (!item || typeof item !== "object") return false;
        if (filters.childName && item.child_name !== filters.childName) return false;
        if (filters.provider && item.llm_provider !== filters.provider) return false;
        if (filters.character && item.character_preset !== filters.character) return false;
        if (searchText && !itemSearchText(item).includes(searchText)) return false;
        return true;
      }});
    }}

    function numericActivityValues(items, field) {{
      return (items || [])
        .map((item) => Number(item?.[field]))
        .filter((value) => Number.isFinite(value));
    }}

    function averageNumber(values) {{
      if (!values.length) return 0;
      return Math.round((values.reduce((sum, value) => sum + value, 0) / values.length) * 10) / 10;
    }}

    function medianNumber(values) {{
      if (!values.length) return 0;
      const sorted = [...values].sort((left, right) => left - right);
      const mid = Math.floor(sorted.length / 2);
      if (sorted.length % 2) return Math.round(sorted[mid] * 10) / 10;
      return Math.round(((sorted[mid - 1] + sorted[mid]) / 2) * 10) / 10;
    }}

    function countActivityByField(items, field) {{
      const counts = new Map();
      for (const item of (items || [])) {{
        const value = String(item?.[field] || "").trim();
        if (!value) continue;
        counts.set(value, (counts.get(value) || 0) + 1);
      }}
      return Array.from(counts.entries())
        .map(([value, count]) => ({{ value, count }}))
        .sort((left, right) => right.count - left.count || left.value.localeCompare(right.value));
    }}

    function topActivityLabel(items, field) {{
      const ranked = countActivityByField(items, field);
      return ranked.length ? ranked[0].value : "";
    }}

    function summarizeFilteredActivity(turns, sessions) {{
      const combined = [...turns, ...sessions];
      const totalMsValues = numericActivityValues(turns, "total_ms");
      const durationValues = numericActivityValues(sessions, "duration_sec");
      return {{
        recent_turn_count: turns.length,
        recent_session_count: sessions.length,
        average_total_ms: averageNumber(totalMsValues),
        median_total_ms: medianNumber(totalMsValues),
        fastest_total_ms: totalMsValues.length ? Math.round(Math.min(...totalMsValues) * 10) / 10 : 0,
        slowest_total_ms: totalMsValues.length ? Math.round(Math.max(...totalMsValues) * 10) / 10 : 0,
        average_session_duration_sec: averageNumber(durationValues),
        top_child_name: topActivityLabel(combined, "child_name"),
        top_character_preset: topActivityLabel(combined, "character_preset"),
        top_llm_provider: topActivityLabel(combined, "llm_provider"),
      }};
    }}

    function formatMetricNumber(value, suffix = "") {{
      const number = Number(value);
      if (!Number.isFinite(number)) return "0" + suffix;
      const rounded = Math.round(number * 10) / 10;
      const text = Number.isInteger(rounded) ? String(Math.trunc(rounded)) : rounded.toFixed(1);
      return text + suffix;
    }}

    function renderSummaryCard(value, label) {{
      return `
        <div class="summary-card">
          <strong>${{escapeHtml(value)}}</strong>
          <span>${{escapeHtml(label)}}</span>
        </div>
      `;
    }}

    function populateConfig(status) {{
      const cfg = status.config;
      document.getElementById("llmProvider").value = cfg.llm_provider || "anthropic";
      document.getElementById("anthropicModel").value = cfg.anthropic_model || "";
      document.getElementById("openaiModel").value = cfg.openai_model || "";
      document.getElementById("anthropicApiKey").value = "";
      document.getElementById("openaiApiKey").value = "";
      document.getElementById("ttsBackend").value = cfg.tts_backend || "auto";
      document.getElementById("processingText").value = cfg.processing_prompt_text || "";
      document.getElementById("systemPrompt").value = cfg.system_prompt || "";
      document.getElementById("startupGreeting").checked = !!cfg.startup_greeting_enabled;
      document.getElementById("startupPrime").checked = !!cfg.startup_listen_prime_enabled;
      document.getElementById("waitForIdle").checked = !!cfg.wait_for_idle_before_startup;
      document.getElementById("processingPrompt").checked = !!cfg.processing_prompt_enabled;
      document.getElementById("keyStatus").textContent =
        "Anthropic key: " + (cfg.provider_key_source?.anthropic || "missing") +
        " (" + (cfg.provider_key_masked?.anthropic || "none") + ")" +
        " | OpenAI key: " + (cfg.provider_key_source?.openai || "missing") +
        " (" + (cfg.provider_key_masked?.openai || "none") + ")" +
        ". Keys entered here stay in server memory until restart.";
      populateCharacters(status);
    }}

    function showSessions(status) {{
      document.getElementById("sessions").textContent = JSON.stringify(status.sessions, null, 2);
    }}

    function shortenText(value, limit = 120) {{
      const text = String(value || "").trim();
      if (!text) return "No text yet.";
      return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
    }}

    function showActivity(status) {{
      const activity = status.activity || {{}};
      populateActivityFilters(status);
      const allTurns = activity.recent_turns || [];
      const allSessions = activity.recent_sessions || [];
      const filteredTurns = filterActivityItems(allTurns);
      const filteredSessions = filterActivityItems(allSessions);
      const summary = summarizeFilteredActivity(filteredTurns, filteredSessions);
      const filterLabels = [];
      if (state.activityFilters.childName) {{
        filterLabels.push(`Child: ${{state.activityFilters.childName}}`);
      }}
      if (state.activityFilters.provider) {{
        filterLabels.push(`Provider: ${{state.activityFilters.provider}}`);
      }}
      if (state.activityFilters.character) {{
        filterLabels.push(`Character: ${{humanizeKey(state.activityFilters.character)}}`);
      }}
      if (state.activityFilters.search) {{
        filterLabels.push(`Search: ${{state.activityFilters.search}}`);
      }}

      const summaryCards = [
        renderSummaryCard(String(summary.recent_turn_count), "Turns shown"),
        renderSummaryCard(String(summary.recent_session_count), "Sessions shown"),
        renderSummaryCard(formatMetricNumber(summary.average_total_ms, " ms"), "Average latency"),
        renderSummaryCard(formatMetricNumber(summary.median_total_ms, " ms"), "Median latency"),
        renderSummaryCard(formatMetricNumber(summary.fastest_total_ms, " ms"), "Fastest turn"),
        renderSummaryCard(formatMetricNumber(summary.slowest_total_ms, " ms"), "Slowest turn"),
        renderSummaryCard(formatMetricNumber(summary.average_session_duration_sec, " sec"), "Avg session"),
        renderSummaryCard(summary.top_child_name || "No data", "Top child"),
        renderSummaryCard(humanizeKey(summary.top_character_preset) || "No data", "Top character"),
        renderSummaryCard(summary.top_llm_provider || "No data", "Top provider"),
      ];
      document.getElementById("activitySummaryGrid").innerHTML = summaryCards.join("");

      document.getElementById("activitySummary").innerHTML = `
        <span class="pill">Showing ${{filteredTurns.length}} of ${{allTurns.length}} saved turns</span>
        <span class="pill">Showing ${{filteredSessions.length}} of ${{allSessions.length}} saved sessions</span>
        <span class="pill">${{
          filterLabels.length ? escapeHtml(filterLabels.join(" | ")) : "No dashboard filters active"
        }}</span>
        <span class="pill">Store: ${{escapeHtml(activity.storage_path || "unknown")}}</span>
      `;

      const combined = [...filteredTurns, ...filteredSessions];
      const breakdownGroups = [
        {{
          label: "Children",
          items: countActivityByField(combined, "child_name"),
        }},
        {{
          label: "Characters",
          items: countActivityByField(combined, "character_preset"),
          humanize: true,
        }},
        {{
          label: "Providers",
          items: countActivityByField(combined, "llm_provider"),
        }},
      ];
      const breakdownBox = document.getElementById("activityBreakdowns");
      breakdownBox.innerHTML = breakdownGroups.map((group) => {{
        const pills = group.items.length
          ? group.items.slice(0, 6).map((item) => `
              <span class="pill">${{
                escapeHtml(group.humanize ? humanizeKey(item.value) : item.value)
              }} (${{item.count}})</span>
            `).join("")
          : '<span class="muted">No data in this filter view yet.</span>';
        return `
          <div class="activity-item">
            <strong>${{escapeHtml(group.label)}}</strong>
            <span>${{pills}}</span>
          </div>
        `;
      }}).join("");

      const recentTurns = document.getElementById("recentTurns");
      recentTurns.innerHTML = "";
      for (const turn of filteredTurns.slice(0, 6)) {{
        const item = document.createElement("div");
        item.className = "activity-item";
        item.innerHTML = `
          <strong>${{escapeHtml(turn.child_name || "Child")}} asked: ${{escapeHtml(shortenText(turn.transcript, 80))}}</strong>
          <span>${{escapeHtml(shortenText(turn.reply, 110))}}</span>
          <span>${{escapeHtml(turn.timestamp || "")}} | ${{escapeHtml(humanizeKey(turn.character_preset || "companion"))}} | ${{escapeHtml(turn.llm_provider || "anthropic")}} | total ${{formatMetricNumber(turn.total_ms || 0, " ms")}}</span>
        `;
        recentTurns.appendChild(item);
      }}
      if (!recentTurns.innerHTML) {{
        recentTurns.innerHTML = '<div class="activity-item"><span>No turns matched the current dashboard filters.</span></div>';
      }}

      const recentSessions = document.getElementById("recentSessions");
      recentSessions.innerHTML = "";
      for (const session of filteredSessions.slice(0, 6)) {{
        const item = document.createElement("div");
        item.className = "activity-item";
        item.innerHTML = `
          <strong>${{escapeHtml(session.child_name || "Child")}} | ${{escapeHtml(humanizeKey(session.character_preset || "companion"))}}</strong>
          <span>Started: ${{escapeHtml(session.connected_at || "")}}</span>
          <span>Duration: ${{formatMetricNumber(session.duration_sec || 0, " sec")}} | Turns: ${{escapeHtml(String(session.turn_count || 0))}} | Provider: ${{escapeHtml(session.llm_provider || "anthropic")}}</span>
        `;
        recentSessions.appendChild(item);
      }}
      if (!recentSessions.innerHTML) {{
        recentSessions.innerHTML = '<div class="activity-item"><span>No sessions matched the current dashboard filters.</span></div>';
      }}
    }}

    function updateActivityFilters() {{
      state.activityFilters.childName = document.getElementById("activityChildFilter").value;
      state.activityFilters.provider = document.getElementById("activityProviderFilter").value;
      state.activityFilters.character = document.getElementById("activityCharacterFilter").value;
      state.activityFilters.search = document.getElementById("activitySearch").value.trim();
      showActivity(state.status);
    }}

    async function refreshStatus() {{
      const response = await fetch("/api/status");
      state.status = await response.json();
      showTransport(state.status);
      showStatusBanner(state.status);
      populateConfig(state.status);
      populateProduct(state.status);
      populateActivityFilters(state.status);
      showActivity(state.status);
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
        anthropic_api_key: document.getElementById("anthropicApiKey").value.trim(),
        openai_api_key: document.getElementById("openaiApiKey").value.trim(),
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

    async function saveProductSetup() {{
      const payload = {{
        device_name: document.getElementById("deviceName").value.trim(),
        parent_name: document.getElementById("parentName").value.trim(),
        child_name: document.getElementById("childName").value.trim(),
        child_age_band: document.getElementById("childAgeBand").value,
        child_interests: document.getElementById("childInterests").value.trim(),
        parent_goals: document.getElementById("parentGoals").value.trim(),
        safety_mode: document.getElementById("safetyMode").value,
        active_learning_pack_ids: selectedCardValues("learningPackGrid"),
        active_story_ids: selectedCardValues("storyLibraryGrid"),
      }};
      const response = await fetch("/api/product-state", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      document.getElementById("familyResult").textContent =
        data.ok ? "Family setup saved." : JSON.stringify(data);
      await refreshStatus();
      return data;
    }}

    async function reloadContent() {{
      document.getElementById("reloadContentResult").textContent = "Reloading content files...";
      const response = await fetch("/api/reload-content", {{
        method: "POST"
      }});
      const data = await response.json();
      document.getElementById("reloadContentResult").textContent =
        data.ok ? "Content files reloaded." : JSON.stringify(data);
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

    function updateContentFilters() {{
      state.contentFilters.search = document.getElementById("contentSearch").value;
      state.contentFilters.recommendedOnly = document.getElementById("recommendedOnly").checked;
      populateProduct(state.status);
    }}

    document.getElementById("speakBtn").addEventListener("click", sendSpeech);
    document.getElementById("saveConfig").addEventListener("click", saveConfig);
    document.getElementById("saveFamily").addEventListener("click", saveProductSetup);
    document.getElementById("reloadContent").addEventListener("click", reloadContent);
    document.getElementById("contentSearch").addEventListener("input", updateContentFilters);
    document.getElementById("recommendedOnly").addEventListener("change", updateContentFilters);
    document.getElementById("activityChildFilter").addEventListener("change", updateActivityFilters);
    document.getElementById("activityProviderFilter").addEventListener("change", updateActivityFilters);
    document.getElementById("activityCharacterFilter").addEventListener("change", updateActivityFilters);
    document.getElementById("activitySearch").addEventListener("input", updateActivityFilters);
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
    populateProduct(state.status);
    populateActivityFilters(state.status);
    showActivity(state.status);
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
        method, target, headers, body = await read_http_request(reader)
    except Exception:
        writer.write(make_text_response("400 Bad Request", "bad request\n"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    try:
        parsed = urlsplit(target)
        params = parse_qs(parsed.query, keep_blank_values=False)
        authorized = admin_request_authorized(headers)

        if method == "POST" and parsed.path == "/api/auth":
            payload = json.loads(body.decode("utf-8") or "{}")
            code = str(payload.get("code", "")).strip()
            if not PANEL_ACCESS_CODE:
                writer.write(make_json_response("200 OK", {"ok": True, "auth": "not-required"}))
                await writer.drain()
                return
            if hashlib.sha256(code.encode("utf-8")).hexdigest() != panel_auth_cookie_value():
                writer.write(
                    make_json_response(
                        "401 Unauthorized",
                        {"ok": False, "error": "wrong access code"},
                    )
                )
                await writer.drain()
                return
            writer.write(
                make_http_response(
                    "200 OK",
                    json.dumps({"ok": True}, ensure_ascii=True, indent=2).encode("utf-8"),
                    content_type="application/json; charset=utf-8",
                    extra_headers=[
                        (
                            "Set-Cookie",
                            f"{PANEL_AUTH_COOKIE_NAME}={panel_auth_cookie_value()}; Path=/; HttpOnly; SameSite=Lax",
                        )
                    ],
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/":
            if not authorized:
                writer.write(
                    make_http_response(
                        "200 OK",
                        render_login_panel().encode("utf-8"),
                        content_type="text/html; charset=utf-8",
                    )
                )
                await writer.drain()
                return
            writer.write(
                make_http_response(
                    "200 OK",
                    render_control_panel().encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/onboarding":
            writer.write(
                make_http_response(
                    "200 OK",
                    render_onboarding_page().encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            )
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/onboarding-qr.png":
            qr_png = generate_local_qr_png(admin_panel_url())
            if qr_png is None:
                writer.write(make_text_response("503 Service Unavailable", "local qr unavailable\n"))
            else:
                writer.write(
                    make_http_response(
                        "200 OK",
                        qr_png,
                        content_type="image/png",
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

        if method == "GET" and parsed.path == "/api/onboarding":
            writer.write(make_json_response("200 OK", {"ok": True, "onboarding": onboarding_public_dict()}))
            await writer.drain()
            return

        if not authorized:
            writer.write(auth_required_json_response())
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/api/status":
            writer.write(make_json_response("200 OK", server_status_dict()))
            await writer.drain()
            return

        if method == "GET" and parsed.path == "/api/activity":
            writer.write(make_json_response("200 OK", {"ok": True, "activity": activity_public_dict()}))
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

        if method == "GET" and parsed.path == "/api/product-state":
            writer.write(
                make_json_response(
                    "200 OK",
                    {"ok": True, "product": product_public_dict()},
                )
            )
            await writer.drain()
            return

        if method == "POST" and parsed.path == "/api/reload-content":
            writer.write(
                make_json_response(
                    "200 OK",
                    {"ok": True, "product": reload_product_content()},
                )
            )
            await writer.drain()
            return

        if method == "POST" and parsed.path == "/api/product-state":
            payload = json.loads(body.decode("utf-8") or "{}")
            writer.write(
                make_json_response(
                    "200 OK",
                    {"ok": True, "product": apply_product_state(payload)},
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
    logger.info("Admin panel listening on {}", admin_panel_url())
    async with server, admin_server:
        await asyncio.gather(server.serve_forever(), admin_server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
