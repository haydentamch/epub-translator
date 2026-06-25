from typing import Any

import httpx


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT = 20.0
TRANSLATION_TIMEOUT = 120.0


class OpenRouterError(Exception):
    pass


async def test_api_key(api_key: str) -> dict[str, Any]:
    payload = await _get("/key", api_key)
    return payload.get("data", payload)


async def fetch_models(api_key: str) -> list[dict[str, Any]]:
    payload = await _get("/models/user", api_key)
    models = payload.get("data")

    if not isinstance(models, list):
        raise OpenRouterError("OpenRouter returned an unexpected model list.")

    return sorted(
        (
            {
                "id": model.get("id", ""),
                "name": model.get("name") or model.get("id", ""),
            }
            for model in models
            if isinstance(model, dict) and model.get("id")
        ),
        key=lambda model: model["name"].lower(),
    )


async def create_chat_completion(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
) -> str:
    payload = await _post(
        "/chat/completions",
        api_key,
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
        },
    )

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            "OpenRouter returned an unexpected translation response."
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("OpenRouter returned an empty translation.")

    return content


async def _get(path: str, api_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key.strip()}"}

    try:
        async with httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            response = await client.get(path, headers=headers)
    except httpx.RequestError as exc:
        raise OpenRouterError("Could not connect to OpenRouter.") from exc

    if response.status_code in {401, 403}:
        raise OpenRouterError("OpenRouter rejected this API key.")

    if response.is_error:
        raise OpenRouterError(
            f"OpenRouter request failed with status {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned an invalid response.") from exc

    if not isinstance(payload, dict):
        raise OpenRouterError("OpenRouter returned an unexpected response.")

    return payload


async def _post(
    path: str,
    api_key: str,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key.strip()}"}

    try:
        async with httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=TRANSLATION_TIMEOUT,
        ) as client:
            response = await client.post(path, headers=headers, json=json_body)
    except httpx.RequestError as exc:
        raise OpenRouterError("Could not connect to OpenRouter.") from exc

    if response.status_code in {401, 403}:
        raise OpenRouterError("OpenRouter rejected this API key.")

    if response.is_error:
        raise OpenRouterError(
            f"OpenRouter request failed with status {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned an invalid response.") from exc

    if not isinstance(payload, dict):
        raise OpenRouterError("OpenRouter returned an unexpected response.")

    return payload
