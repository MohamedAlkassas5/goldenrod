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
from typing import Any

from jsonschema import Draft202012Validator

from services.extractor.backend import SceneRequest
from services.extractor.prompt import SCENE_SCHEMA, SYSTEM_PROMPT, build_prompt

DEFAULT_MODEL = "gemini-2.5-pro"


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
    ):
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.max_attempts = max_attempts
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

    # -- the backend interface ---------------------------------------------
    def extract_scene(self, request: SceneRequest) -> dict:
        _, types = self._import_sdk()
        prompt = build_prompt(
            request.script, request.scene, request.known_facts, request.known_entities
        )
        complaint = ""
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt + complaint,
                config=self._config(types),
            )
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
