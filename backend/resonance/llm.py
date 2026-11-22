"""Configurable LLM provider boundary.

The LLM is used ONLY to turn structured, already-computed findings into plain English. It never receives
audio, never produces notes, lyrics or measurements, and is skipped entirely unless (a) a provider key is
configured on the server and (b) the project owner has allowed external AI for that project.
"""
from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.orm import Session

from . import models as m

DEFAULT_MODEL = "claude-opus-5-5"
SYSTEM = (
    "You are the Production Director of RESONANCE, a studio that reconstructs live church music recordings. "
    "You are given a JSON status report produced by deterministic audio tools. Explain it to a non-musician in "
    "plain English: what is done, what the findings mean, what is uncertain, and the single next action. "
    "Use only facts present in the JSON. Never state or imply that a transcription or reconstruction is correct, "
    "exact, or finished unless the JSON says a human approved it. Keep it under 180 words, no headings."
)


class LLMUnavailable(Exception):
    pass


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...
    def test(self) -> dict: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=90.0)
        self.model = model or DEFAULT_MODEL

    def complete(self, system: str, user: str) -> str:
        a = self._anthropic
        try:
            # Server-side refusal fallback is enabled so a declined request is re-run instead of failing.
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}],
            )
        except a.AuthenticationError as e:
            raise LLMUnavailable("The Anthropic API key was rejected.") from e
        except a.PermissionDeniedError as e:
            raise LLMUnavailable("The Anthropic API key lacks permission for this model.") from e
        except a.NotFoundError as e:
            raise LLMUnavailable(f"Model '{self.model}' was not found for this key.") from e
        except a.RateLimitError as e:
            raise LLMUnavailable("Anthropic rate limit reached; try again shortly.") from e
        except a.APIStatusError as e:
            raise LLMUnavailable(f"Anthropic API error {e.status_code}.") from e
        except a.APIConnectionError as e:
            raise LLMUnavailable("Could not reach the Anthropic API (network blocked or offline).") from e
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("The model declined this request.")
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def test(self) -> dict:
        try:
            text = self.complete("Reply with the single word OK.", "Connectivity test.")
            return {"ok": True, "detail": f"Model {self.model} responded: {text[:40]}"}
        except LLMUnavailable as e:
            return {"ok": False, "detail": str(e)}


def get_setting(db: Session, key: str) -> str:
    row = db.get(m.ProviderSetting, key)
    return row.value if row else ""


def get_provider(db: Session) -> LLMProvider | None:
    key = get_setting(db, "anthropic_api_key")
    if not key:
        return None
    return AnthropicProvider(key, get_setting(db, "anthropic_model") or DEFAULT_MODEL)


def narrate(db: Session, project: m.Project, status: dict) -> dict:
    """Returns {'text', 'source'}; source is 'llm:<model>' or 'template' so the UI can label it."""
    if not project.allow_external_ai:
        return {"text": None, "source": "disabled", "reason": "External AI is switched off for this project."}
    prov = get_provider(db)
    if not prov:
        return {"text": None, "source": "unconfigured", "reason": "No LLM provider key is configured in Settings."}
    try:
        text = prov.complete(SYSTEM, json.dumps(status, default=str)[:60000])
        return {"text": text, "source": f"llm:{getattr(prov, 'model', prov.name)}"}
    except LLMUnavailable as e:
        return {"text": None, "source": "error", "reason": str(e)}
