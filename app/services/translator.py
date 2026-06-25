import json
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.db import (
    save_paragraph_translation,
    set_paragraphs_failed,
    set_paragraphs_status,
)
from app.services.openrouter_client import create_chat_completion


BATCH_SIZE = 8
JSON_ATTEMPTS = 2
ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "i",
    "p",
    "q",
    "rp",
    "rt",
    "ruby",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "*": {"dir", "lang"}}

SYSTEM_PROMPT = """You are a faithful literary translator.
Translate the provided EPUB text from {source_language} into {target_language}.

Rules:

Translate only the original text.
Do not summarize.
Do not explain.
Do not rewrite the author's style.
Do not improve, simplify, or modernize the writing.
Preserve paragraph order.
Preserve names, numbers, dates, symbols, punctuation, links, emphasis, and HTML tags.
Preserve dialogue formatting.
If a sentence is ambiguous, translate it as closely as possible without adding interpretation.
Return only valid JSON.
Do not include markdown.

Input format:
[
  {{
    "id": "paragraph_id",
    "html": "Original HTML here"
  }}
]

Output format:
[
  {{
    "id": "paragraph_id",
    "translation_html": "Translated HTML here"
  }}
]"""


class TranslationResponseError(Exception):
    pass


class TranslationPaused(Exception):
    pass


async def translate_preview(
    job: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    api_key: str,
    completion: Callable[
        [str, str, list[dict[str, str]]],
        Awaitable[str],
    ] = create_chat_completion,
) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []

    for offset in range(0, len(paragraphs), BATCH_SIZE):
        batch = paragraphs[offset : offset + BATCH_SIZE]
        translations = await _translate_batch(
            job=job,
            batch=batch,
            api_key=api_key,
            completion=completion,
        )

        for paragraph in batch:
            translated_html = translations[str(paragraph["id"])]
            save_paragraph_translation(paragraph["id"], translated_html)
            preview.append(
                {
                    **paragraph,
                    "original_html": sanitize_html(paragraph["original_html"]),
                    "translated_html": translated_html,
                }
            )

    return preview


async def translate_paragraphs(
    job: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    api_key: str,
    completion: Callable[
        [str, str, list[dict[str, str]]],
        Awaitable[str],
    ] = create_chat_completion,
    should_pause: Callable[[], bool] | None = None,
) -> None:
    for offset in range(0, len(paragraphs), BATCH_SIZE):
        if should_pause and should_pause():
            raise TranslationPaused

        batch = paragraphs[offset : offset + BATCH_SIZE]
        paragraph_ids = [int(paragraph["id"]) for paragraph in batch]
        set_paragraphs_status(paragraph_ids, "Translating")

        try:
            translations = await _translate_batch(
                job=job,
                batch=batch,
                api_key=api_key,
                completion=completion,
            )
        except Exception as exc:
            set_paragraphs_failed(
                paragraph_ids,
                _safe_error_message(exc),
            )
            raise

        for paragraph in batch:
            save_paragraph_translation(
                int(paragraph["id"]),
                translations[str(paragraph["id"])],
            )

        if should_pause and should_pause():
            raise TranslationPaused


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message[:500] if message else "Translation failed."


async def _translate_batch(
    job: dict[str, Any],
    batch: list[dict[str, Any]],
    api_key: str,
    completion: Callable[
        [str, str, list[dict[str, str]]],
        Awaitable[str],
    ],
) -> dict[str, str]:
    input_payload = [
        {"id": str(paragraph["id"]), "html": paragraph["original_html"]}
        for paragraph in batch
    ]
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                source_language=job["source_language"],
                target_language=job["target_language"],
            ),
        },
        {
            "role": "user",
            "content": json.dumps(input_payload, ensure_ascii=False),
        },
    ]
    last_error: TranslationResponseError | None = None

    for _ in range(JSON_ATTEMPTS):
        content = await completion(api_key, job["model"], messages)

        try:
            return _parse_translations(content, input_payload)
        except TranslationResponseError as exc:
            last_error = exc

    raise last_error or TranslationResponseError(
        "OpenRouter returned invalid translation JSON."
    )


def _parse_translations(
    content: str,
    input_payload: list[dict[str, str]],
) -> dict[str, str]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TranslationResponseError(
            "OpenRouter returned invalid translation JSON."
        ) from exc

    if not isinstance(payload, list):
        raise TranslationResponseError(
            "OpenRouter translation JSON must be an array."
        )

    expected_ids = [item["id"] for item in input_payload]
    translations: dict[str, str] = {}

    for item in payload:
        if not isinstance(item, dict):
            raise TranslationResponseError(
                "OpenRouter returned an invalid translation item."
            )

        paragraph_id = str(item.get("id", ""))
        translated_html = item.get("translation_html")

        if paragraph_id in translations or not isinstance(translated_html, str):
            raise TranslationResponseError(
                "OpenRouter returned an invalid translation item."
            )

        translations[paragraph_id] = sanitize_html(translated_html)

    if list(translations) != expected_ids:
        raise TranslationResponseError(
            "OpenRouter did not preserve the requested paragraph order."
        )

    return translations


def sanitize_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in list(soup.find_all(True)):
        if tag.name not in ALLOWED_TAGS:
            if tag.name in {"script", "style", "iframe", "object", "embed"}:
                tag.decompose()
            else:
                tag.unwrap()
            continue

        allowed = ALLOWED_ATTRIBUTES.get("*", set()) | ALLOWED_ATTRIBUTES.get(
            tag.name,
            set(),
        )
        for attribute in list(tag.attrs):
            if attribute not in allowed:
                del tag.attrs[attribute]

        if tag.name == "a" and tag.get("href"):
            parsed = urlparse(str(tag["href"]))
            if parsed.scheme and parsed.scheme not in {"http", "https", "mailto"}:
                del tag.attrs["href"]

    return str(soup)
