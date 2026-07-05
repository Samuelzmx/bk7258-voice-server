from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

ContentCatalog = dict[str, dict[str, Any]]

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


class ProductRuntime:
    def __init__(
        self,
        *,
        content_dir: Path,
        product_state_path: Path,
        child_age_bands: list[str],
        safety_modes: dict[str, str],
        default_learning_packs: ContentCatalog,
        default_story_library: ContentCatalog,
        default_product_state_fields: dict[str, Any],
    ) -> None:
        self.content_dir = content_dir
        self.product_state_path = product_state_path
        self.child_age_bands = list(child_age_bands)
        self.safety_modes = dict(safety_modes)
        self.default_learning_packs = clone_content_catalog(default_learning_packs)
        self.default_story_library = clone_content_catalog(default_story_library)
        self.default_product_state_fields = dict(default_product_state_fields)
        self.learning_packs_path = self.content_dir / "learning_packs.json"
        self.story_library_path = self.content_dir / "story_library.json"
        self.learning_packs = self.load_content_catalog(
            self.learning_packs_path,
            self.default_learning_packs,
        )
        self.story_library = self.load_content_catalog(
            self.story_library_path,
            self.default_story_library,
        )
        self.product_state = self.load_product_state()

    def normalize_content_entry(self, entry_id: str, raw: Any) -> dict[str, Any] | None:
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
                if age_band in self.child_age_bands
            ],
            "goal_tags": normalize_string_sequence(raw.get("goal_tags")),
            "topics": normalize_string_sequence(raw.get("topics")),
        }

    def load_content_catalog(
        self,
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
            entry = self.normalize_content_entry(key, raw_entry)
            if entry is None:
                logger.warning("skipping invalid content entry '{}' in {}", key, path)
                continue
            normalized[key] = entry
        if normalized:
            return normalized
        logger.warning("content catalog at {} had no valid entries", path)
        return clone_content_catalog(fallback)

    def default_selected_ids(
        self,
        catalog: ContentCatalog,
        preferred: list[str],
    ) -> list[str]:
        selected = [entry_id for entry_id in preferred if entry_id in catalog]
        if selected:
            return selected
        return list(catalog)[:1]

    def default_product_state(self) -> dict[str, Any]:
        base = dict(self.default_product_state_fields)
        base["active_learning_pack_ids"] = self.default_selected_ids(
            self.learning_packs,
            ["english_starter"],
        )
        base["active_story_ids"] = self.default_selected_ids(
            self.story_library,
            ["forest_friends"],
        )
        return base

    def sanitize_string_list(
        self,
        value: Any,
        *,
        allowed: set[str],
        fallback: list[str],
    ) -> list[str]:
        if not isinstance(value, list):
            return list(fallback)
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text in allowed and text not in cleaned:
                cleaned.append(text)
        return cleaned or list(fallback)

    def normalize_product_state(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        base = self.default_product_state()
        raw = raw or {}
        base["device_name"] = str(raw.get("device_name", base["device_name"])).strip() or base["device_name"]
        base["parent_name"] = str(raw.get("parent_name", base["parent_name"])).strip()
        base["child_name"] = str(raw.get("child_name", base["child_name"])).strip() or base["child_name"]
        age_band = str(raw.get("child_age_band", base["child_age_band"])).strip()
        base["child_age_band"] = age_band if age_band in self.child_age_bands else base["child_age_band"]
        base["child_interests"] = str(raw.get("child_interests", base["child_interests"])).strip()
        base["parent_goals"] = str(raw.get("parent_goals", base["parent_goals"])).strip()
        safety_mode = str(raw.get("safety_mode", base["safety_mode"])).strip()
        base["safety_mode"] = safety_mode if safety_mode in self.safety_modes else base["safety_mode"]
        base["active_learning_pack_ids"] = self.sanitize_string_list(
            raw.get("active_learning_pack_ids"),
            allowed=set(self.learning_packs),
            fallback=list(base["active_learning_pack_ids"]),
        )
        base["active_story_ids"] = self.sanitize_string_list(
            raw.get("active_story_ids"),
            allowed=set(self.story_library),
            fallback=list(base["active_story_ids"]),
        )
        return base

    def load_product_state(self) -> dict[str, Any]:
        if not self.product_state_path.exists():
            return self.normalize_product_state(None)
        try:
            payload = json.loads(self.product_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("failed to load product state from {}", self.product_state_path)
            return self.normalize_product_state(None)
        if not isinstance(payload, dict):
            return self.normalize_product_state(None)
        return self.normalize_product_state(payload)

    def save_product_state(self) -> None:
        try:
            self.product_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.product_state_path.write_text(
                json.dumps(self.product_state, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("failed to save product state to {}: {}", self.product_state_path, exc)

    def product_keyword_text(self) -> str:
        return " ".join(
            value.strip().lower()
            for value in (
                str(self.product_state.get("child_interests", "")),
                str(self.product_state.get("parent_goals", "")),
            )
            if value.strip()
        )

    def content_query_dict(
        self,
        *,
        character_preset: str,
        user_text: str = "",
    ) -> dict[str, Any]:
        request_text = normalized_content_phrase(user_text)
        profile_text = normalized_content_phrase(self.product_keyword_text())
        request_tokens = set(content_tokens(user_text))
        profile_tokens = set(content_tokens(self.product_keyword_text()))
        mode_groups = set(CHARACTER_CONTENT_HINTS.get(character_preset, set()))
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
        self,
        target_age_band: str,
        entry_age_bands: list[str],
    ) -> tuple[int, str]:
        if not target_age_band or target_age_band not in self.child_age_bands:
            return 0, ""
        candidate_indexes = [
            self.child_age_bands.index(age_band)
            for age_band in entry_age_bands
            if age_band in self.child_age_bands
        ]
        if not candidate_indexes:
            return 0, ""
        target_index = self.child_age_bands.index(target_age_band)
        distance = min(abs(target_index - candidate_index) for candidate_index in candidate_indexes)
        if distance == 0:
            return 4, f"age {target_age_band}"
        if distance == 1:
            return 2, f"near age {target_age_band}"
        return 0, ""

    def score_catalog_entry(
        self,
        entry_id: str,
        entry: dict[str, Any],
        *,
        selected_ids: list[str],
        character_preset: str,
        user_text: str = "",
    ) -> dict[str, Any] | None:
        query = self.content_query_dict(
            character_preset=character_preset,
            user_text=user_text,
        )
        score = 0
        reasons: list[str] = []
        matched_terms: list[str] = []

        age_score, age_reason = self.age_band_retrieval_score(
            str(self.product_state.get("child_age_band", "")).strip(),
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
            [
                semantic_group_label(group)
                for group in sorted(
                    entry_groups
                    & (query["request_groups"] | query["profile_groups"] | query["mode_groups"])
                )
            ]
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
        self,
        catalog: ContentCatalog,
        *,
        selected_ids: list[str],
        character_preset: str,
        limit: int = 3,
        user_text: str = "",
    ) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for entry_id, entry in catalog.items():
            scored = self.score_catalog_entry(
                entry_id,
                entry,
                selected_ids=selected_ids,
                character_preset=character_preset,
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

    def content_prompt_limits(
        self,
        *,
        character_preset: str,
        user_text: str = "",
    ) -> tuple[int, int]:
        query = self.content_query_dict(
            character_preset=character_preset,
            user_text=user_text,
        )
        request_groups = set(query["request_groups"])
        story_focus = bool(request_groups & {"story", "bedtime", "calm", "space"}) or (
            character_preset in {"storyteller", "bedtime_guide"}
        )
        learning_focus = bool(request_groups & {"english", "phonics", "science", "social"}) or (
            character_preset in {"language_teacher", "curious_friend"}
        )
        if story_focus and not learning_focus:
            return 0, 2
        if learning_focus and not story_focus:
            return 2, 0
        if character_preset in {"storyteller", "bedtime_guide"}:
            return 1, 2
        if character_preset in {"language_teacher", "curious_friend"}:
            return 2, 1
        return 1, 1

    def active_catalog_prompt_entries(
        self,
        catalog: ContentCatalog,
        active_ids: list[str],
        *,
        character_preset: str,
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
        ranked = self.recommend_catalog_entries(
            active_catalog,
            selected_ids=list(active_catalog),
            character_preset=character_preset,
            limit=max(limit * 3, limit),
            user_text=user_text,
        )
        return select_diverse_ranked_entries(ranked, limit)

    def runtime_content_context(
        self,
        *,
        character_preset: str,
        user_text: str = "",
    ) -> dict[str, Any]:
        learning_limit, story_limit = self.content_prompt_limits(
            character_preset=character_preset,
            user_text=user_text,
        )
        return {
            "strategy": "ranked-local-library",
            "learning_packs": self.active_catalog_prompt_entries(
                self.learning_packs,
                list(self.product_state["active_learning_pack_ids"]),
                character_preset=character_preset,
                user_text=user_text,
                limit=learning_limit,
            ),
            "story_library": self.active_catalog_prompt_entries(
                self.story_library,
                list(self.product_state["active_story_ids"]),
                character_preset=character_preset,
                user_text=user_text,
                limit=story_limit,
            ),
            "learning_limit": learning_limit,
            "story_limit": story_limit,
        }

    def recommendations_dict(self, *, character_preset: str) -> dict[str, Any]:
        learning_recommendations = self.recommend_catalog_entries(
            self.learning_packs,
            selected_ids=list(self.product_state["active_learning_pack_ids"]),
            character_preset=character_preset,
        )
        story_recommendations = self.recommend_catalog_entries(
            self.story_library,
            selected_ids=list(self.product_state["active_story_ids"]),
            character_preset=character_preset,
        )
        return {
            "strategy": "ranked-local-library",
            "learning_packs": learning_recommendations,
            "learning_pack_ids": [item["id"] for item in learning_recommendations],
            "story_library": story_recommendations,
            "story_ids": [item["id"] for item in story_recommendations],
        }

    def public_dict(
        self,
        *,
        character_preset: str = "companion",
        user_text: str = "",
    ) -> dict[str, Any]:
        return {
            "setup": dict(self.product_state),
            "child_age_bands": self.child_age_bands,
            "safety_modes": self.safety_modes,
            "learning_packs": self.learning_packs,
            "story_library": self.story_library,
            "recommendations": self.recommendations_dict(character_preset=character_preset),
            "retrieval": self.runtime_content_context(
                character_preset=character_preset,
                user_text=user_text,
            ),
            "content_files": {
                "content_dir": str(self.content_dir),
                "learning_packs_path": str(self.learning_packs_path),
                "story_library_path": str(self.story_library_path),
            },
            "rag_mode": "ranked-local-library",
        }

    def apply_update(
        self,
        update: dict[str, Any],
        *,
        character_preset: str = "companion",
    ) -> dict[str, Any]:
        self.product_state = self.normalize_product_state({**self.product_state, **update})
        self.save_product_state()
        return self.public_dict(character_preset=character_preset)

    def reload_content(
        self,
        *,
        character_preset: str = "companion",
    ) -> dict[str, Any]:
        self.learning_packs = self.load_content_catalog(
            self.learning_packs_path,
            self.default_learning_packs,
        )
        self.story_library = self.load_content_catalog(
            self.story_library_path,
            self.default_story_library,
        )
        self.product_state = self.normalize_product_state(self.product_state)
        self.save_product_state()
        return self.public_dict(character_preset=character_preset)
