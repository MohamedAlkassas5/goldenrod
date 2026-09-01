"""Gemini backend for the Extractor.

The reasoning layer, called at runtime through the google-genai SDK — imported
and actually called, per the hackathon's runtime rule (SPEC §5).

Two ways in, chosen from the environment so the same code runs locally and on
Google Cloud:

    GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION   -> Vertex AI (service account)
    GEMINI_API_KEY / GOOGLE_API_KEY                -> Gemini API

`GEMINI_MODEL` picks the model; see .env.example. Decoding is schema-constrained
against prompt.SCENE_SCHEMA and temperature is 0, because this is an extraction
pass and two runs over the same locked pages should agree.

A schema-constrained decode is a strong prior, not a guarantee, so the response
is parsed and validated here and retried with the validator's own complaint fed
back. Whatever survives is still checked against the script line by line in
extract.py — this class does not decide what is true, only what the model said.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from jsonschema import Draft202012Validator

from services.extractor.backend import SceneRequest
from services.extractor.prompt import SCENE_SCHEMA, SYSTEM_PROMPT, build_prompt

# Was gemini-2.5-pro until 2 September 2026, when a run against a fresh API key
# came back 404: "no longer available to new users". The model list still
# advertises it, so the only way to find out is to call it.
#
# Flash rather than pro as the default, which is a deliberate trade against
# extraction quality. The pro tier reports a free-tier quota of exactly zero, so
# a pro default makes `python -m services.extractor` fail for anyone following
# the README with a fresh AI Studio key — and the run instructions are graded on
# working, not on being ambitious. Anyone with billing, or running on Vertex,
# should reach for pro:
#
#     GEMINI_MODEL=gemini-3.1-pro-preview python -m services.extractor ...
DEFAULT_MODEL = "gemini-3.7-flash"

# Worth retrying: rate limits and the provider having a bad moment. Everything
# else — a retired model, a rejected key — is a fact about the configuration and
# retrying it only makes the error take longer to read.
TRANSIENT_STATUSES = frozenset({429, 500, 503, 504})
INITIAL_BACKOFF_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0


def _retry_after(error: Exception) -> float | None:
    """The delay the API asked for, if it named one.

    It arrives inside a nested RetryInfo detail rather than a header, so it is
    read out of the rendered error. Believing the server beats guessing at it:
    on a per-minute quota the server knows exactly when the window reopens.
    """
    match = re.search(r"'retryDelay': '(\d+(?:\.\d+)?)s'", str(error))
    return float(match.group(1)) if match else None


class GeminiError(RuntimeError):
    """The model could not be reached, or would not return a usable object."""


class GeminiBackend:
    """One structured-output call per scene."""

    name = "gemini"

    def __init__(
        self,
        model: str | None = None,
        *,
        temperature: float = 0.0,
        max_attempts: int = 3,
        client: Any = None,
        max_transient_retries: int = 5,
    ):
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        # Two separate budgets, because they are two separate failures: one is
        # the model returning something the schema rejects, the other is the
        # call not landing at all.
        self.max_attempts = max_attempts
        self.max_transient_retries = max_transient_retries
        self._client = client
        self._validator = Draft202012Validator(SCENE_SCHEMA)

    # -- the SDK ------------------------------------------------------------
    @staticmethod
    def _import_sdk():
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise GeminiError(
                "google-genai is not installed. It is an optional dependency so "
                "the loader and the test suite run without it:\n"
                "    pip install -e \".[gemini]\""
            ) from exc
        return genai, types

    @property
    def client(self):
        if self._client is None:
            genai, _ = self._import_sdk()
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if project:
                self._client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
                )
            else:
                api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                if not api_key:
                    raise GeminiError(
                        "No Gemini credentials. Set GOOGLE_CLOUD_PROJECT (Vertex AI) "
                        "or GEMINI_API_KEY. See .env.example."
                    )
                self._client = genai.Client(api_key=api_key)
        return self._client

    def _config(self, types):
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=SCENE_SCHEMA,
            temperature=self.temperature,
        )

    # -- surviving the network ---------------------------------------------
    def _generate(self, prompt: str, types):
        """One model call, retried through transient failures and nothing else.

        A batch pass is a long line of calls where any one of them can come back
        503 "high demand, usually temporary" — and without this, scene 2 failing
        for half a second throws away the whole run. That is merely annoying
        while tuning and fatal while recording a demo.

        Only transient statuses are retried. A retired model (404) or a bad key
        (401/403) is not going to fix itself, and quietly retrying it five times
        turns a clear error message into a slow mysterious one.
        """
        _, _ = self._import_sdk()
        import httpx
        from google.genai import errors as genai_errors

        delay = INITIAL_BACKOFF_SECONDS
        for attempt in range(1, self.max_transient_retries + 1):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=prompt, config=self._config(types)
                )
            # Two layers fail differently and both have to be caught here. The
            # API answering 503 is a `genai` error; the connection being reset
            # mid-request never becomes one, because no response arrived to turn
            # into it. A pass that survives the first and dies on the second has
            # not been made robust, only made to look it — which is exactly what
            # happened on scene 36 of the first full run.
            except (genai_errors.APIError, httpx.TransportError) as exc:
                status = getattr(exc, "code", None)
                transport = isinstance(exc, httpx.TransportError)
                if not transport and status not in TRANSIENT_STATUSES:
                    raise GeminiError(
                        f"{self.model} refused the call ({status}). This is not a "
                        f"transient error and was not retried: {exc}\n"
                        f"If the model has been retired, set GEMINI_MODEL to one "
                        f"your credentials can reach."
                    ) from exc
                if attempt == self.max_transient_retries:
                    raise GeminiError(
                        f"{self.model} still failing after "
                        f"{self.max_transient_retries} attempts "
                        f"({'connection' if transport else status}): {exc}"
                    ) from exc
                # The API often says how long to wait. Believe it over our guess.
                wait = _retry_after(exc) or delay
                time.sleep(min(wait, BACKOFF_CAP_SECONDS))
                delay = min(delay * 2, BACKOFF_CAP_SECONDS)

    # -- the backend interface ---------------------------------------------
    def extract_scene(self, request: SceneRequest) -> dict:
        _, types = self._import_sdk()
        prompt = build_prompt(
            request.script, request.scene, request.known_facts, request.known_entities
        )
        complaint = ""
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            response = self._generate(prompt + complaint, types)
            payload, last_error = self._decode(getattr(response, "text", "") or "")
            if payload is not None:
                return payload
            complaint = (
                f"\n\nYour previous reply for scene {request.scene.scene_number} was "
                f"rejected: {last_error}\nReturn JSON matching the schema exactly."
            )
        raise GeminiError(
            f"scene {request.scene.scene_number}: no schema-valid response after "
            f"{self.max_attempts} attempts. Last error: {last_error}"
        )

    def _decode(self, text: str) -> tuple[dict | None, str]:
        stripped = text.strip()
        if stripped.startswith("```"):  # some responses still arrive fenced
            stripped = stripped.split("```")[1].removeprefix("json").strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return None, f"not JSON ({exc})"
        if not isinstance(payload, dict):
            return None, f"expected an object, got {type(payload).__name__}"
        errors = sorted(self._validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            path = "/".join(str(p) for p in first.path) or "<root>"
            return None, f"{path}: {first.message}"
        return payload, ""
