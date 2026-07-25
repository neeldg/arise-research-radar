"""Paper summarization and ARISE-style LinkedIn draft generation.

Uses the Anthropic API (structured output via `client.messages.parse`) to turn
one paper's OpenAlex metadata and abstract into an internal summary and a
draft social post. The ARISE LinkedIn example is used only as a style
reference: every fact, number, name, or claim in the generated draft must
come from the paper's own source material, never from the example. This
module owns only the Anthropic call and prompt construction — it has no
knowledge of Notion or OpenAlex specifics (see sinks/notion.py and
sources/openalex.py for those).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

import anthropic
import httpx
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, ValidationError

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 4096

_REPO_ROOT = Path(__file__).resolve().parents[2]
STYLE_EXAMPLE_PATH = _REPO_ROOT / "config" / "prompts" / "arise_linkedin_example.md"
STYLE_GUIDE_PATH = _REPO_ROOT / "config" / "prompts" / "arise_linkedin_style.md"


class DraftingConfigError(RuntimeError):
    """Raised when required Anthropic environment variables are missing."""


class AnthropicConfig(BaseModel):
    api_key: str
    model: str


def load_anthropic_config(*, env: Mapping[str, str] | None = None) -> AnthropicConfig:
    """Load ANTHROPIC_API_KEY (required) and ANTHROPIC_MODEL (.env supported via
    python-dotenv). ANTHROPIC_MODEL defaults to DEFAULT_MODEL when absent or empty.

    Pass `env` explicitly in tests to avoid touching the real process environment.
    """
    if env is None:
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise DraftingConfigError("Missing required environment variable: ANTHROPIC_API_KEY")
    model = env.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    return AnthropicConfig(api_key=api_key, model=model)


def load_style_files(
    example_path: Path = STYLE_EXAMPLE_PATH, guide_path: Path = STYLE_GUIDE_PATH
) -> tuple[str, str]:
    """Load the ARISE LinkedIn style exemplar and the distilled style principles."""
    return example_path.read_text(), guide_path.read_text()


class DraftInput(BaseModel):
    """Everything needed to draft one paper — no Notion or OpenAlex shapes leak in here."""

    title: str
    researcher_names: list[str]
    publication_date: date | None
    venue: str | None
    doi: str | None
    openalex_id: str
    abstract: str | None


class DraftContent(BaseModel):
    """Structured output requested from the Anthropic API for one paper."""

    internal_summary: str = Field(
        description=(
            "A 2-4 sentence internal (not public-facing) summary of the paper for ARISE "
            "editorial review: what the study did and what it found. Written for an "
            "internal reviewer, not for social media."
        )
    )
    key_story_angle: str = Field(
        description=(
            "One sentence identifying the single most newsworthy or shareable angle of "
            "this paper — the hook an editor would use to decide whether and how to post "
            "about it."
        )
    )
    why_it_matters: str = Field(
        description=(
            "1-3 sentences on the practical significance of this finding for people "
            "building, evaluating, or deploying clinical AI."
        )
    )
    draft_social_post: str = Field(
        description=(
            "A complete, ready-to-review LinkedIn post about this paper, written in the "
            "ARISE house style shown in the STYLE EXAMPLE (structure, tone, emoji use, "
            "hashtags, closing call-to-action) — but every fact, number, researcher name, "
            "model name, and claim in the post must come only from THIS paper's own "
            "source material. Never reuse the facts, numbers, models, or claims from the "
            "STYLE EXAMPLE."
        )
    )
    limitations: str = Field(
        description=(
            "1-2 sentences noting limitations or open questions evident from the source "
            "material. If the source material does not indicate any, say so plainly "
            "rather than inventing any."
        )
    )


class DraftGenerationError(RuntimeError):
    """Raised when the Anthropic API fails, or declines to produce a usable draft."""


def determine_source_basis(abstract: str | None) -> Literal["OpenAlex Abstract", "Metadata Only"]:
    """Decided by the pipeline, not asked of the model: what data actually grounded the draft."""
    return "OpenAlex Abstract" if abstract else "Metadata Only"


# Matches a literal backslash, "u", and 4-or-more hex digits appearing as real
# text characters (six literal characters standing in for one em dash, for
# example) rather than an actual Unicode character. This is a detector only,
# never a fixer: correctly parsed JSON never leaves escape syntax behind, so
# any match here means the model itself emitted escape-looking text as content
# (a generation artifact) or some later step corrupted otherwise-valid
# Unicode. Requiring 4-or-more hex digits (not exactly 4) also catches
# malformed escapes like a 5-hex-digit attempt at an astral codepoint written
# outside a proper UTF-16 surrogate pair.
_LITERAL_ESCAPE_PATTERN = re.compile(r"\\u[0-9a-fA-F]{4,}")


def find_residual_escape_sequences(text: str) -> list[str]:
    """Detect literal Unicode-escape-looking substrings and orphaned surrogate
    code points in already-parsed text. Never used to transform or "fix" text
    — see the module docstring and AnthropicClient.generate_draft_content:
    text should already be ordinary Unicode by the time it reaches here, so
    any hit means something upstream (the model, or a future regression) put
    escape syntax or a broken surrogate directly into the content.
    """
    matches = _LITERAL_ESCAPE_PATTERN.findall(text)
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        matches = [*matches, "<orphaned surrogate code point>"]
    return matches


def find_residual_escape_sequences_in_content(content: DraftContent) -> dict[str, list[str]]:
    """Run find_residual_escape_sequences over every DraftContent text field.
    Returns a field-name -> matches mapping; empty when the content is clean.
    """
    fields = {
        "internal_summary": content.internal_summary,
        "key_story_angle": content.key_story_angle,
        "why_it_matters": content.why_it_matters,
        "draft_social_post": content.draft_social_post,
        "limitations": content.limitations,
    }
    problems: dict[str, list[str]] = {}
    for field_name, value in fields.items():
        matches = find_residual_escape_sequences(value)
        if matches:
            problems[field_name] = matches
    return problems


def build_prompt(draft_input: DraftInput, style_example: str, style_guide: str) -> str:
    researchers = ", ".join(draft_input.researcher_names) or "unknown"
    lines = [
        "You are drafting ARISE Research Radar editorial content for one newly detected paper.",
        "",
        "=== STYLE EXAMPLE (tone/structure reference ONLY — never reuse its facts, "
        "numbers, model names, or claims) ===",
        style_example,
        "",
        "=== STYLE PRINCIPLES ===",
        style_guide,
        "",
        "=== THIS PAPER'S SOURCE MATERIAL (the only source of facts for your output) ===",
        f"Title: {draft_input.title}",
        f"ARISE-affiliated researcher(s): {researchers}",
        f"Venue: {draft_input.venue or 'unknown'}",
        "Publication date: "
        + (draft_input.publication_date.isoformat() if draft_input.publication_date else "unknown"),
        f"DOI: {draft_input.doi or 'none'}",
        f"OpenAlex work ID: {draft_input.openalex_id}",
        "",
    ]
    if draft_input.abstract:
        lines.append("Abstract:")
        lines.append(draft_input.abstract)
    else:
        lines.append(
            "No abstract is available for this paper — you have title and metadata only. "
            "Do not invent findings, numbers, or methodology; keep the draft appropriately general."
        )
    lines.append("")
    lines.append(
        "Every factual claim, number, name, or example in your output must come from the "
        "source material above. Do not copy or adapt facts, statistics, model names, or "
        "claims from the STYLE EXAMPLE — it is a style reference only."
    )
    return "\n".join(lines)


class AnthropicClient:
    """Thin, injectable wrapper around the official Anthropic SDK.

    Pass `http_client` (e.g. `anthropic.Anthropic`'s `http_client` backed by
    `httpx.MockTransport`) to avoid live network calls in tests, mirroring the
    OpenAlex/Notion client pattern used elsewhere in this project.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
    ) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key, http_client=http_client, max_retries=max_retries
        )
        self.model = model

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AnthropicClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def generate_draft_content(self, prompt: str) -> DraftContent:
        """Returns the SDK's already-parsed structured output directly.

        `messages.parse(..., output_format=DraftContent)` has the SDK itself
        parse the model's JSON text response exactly once, via pydantic's real
        JSON parser (`TypeAdapter(DraftContent).validate_json(...)`), and
        `response.parsed_output` is already a fully-built `DraftContent` with
        ordinary Python Unicode string fields. This method must never
        re-parse, regex-extract, or otherwise reprocess that text — doing so
        would risk exactly the kind of double-decoding corruption (literal
        "\\uXXXX" text, mangled surrogate pairs) this module exists to avoid.
        """
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=DraftContent,
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise DraftGenerationError(f"Anthropic API request failed: {exc}") from exc
        except ValidationError as exc:
            # The model's text content didn't parse into DraftContent's shape
            # (e.g. malformed or double-encoded JSON) — surfaced as a clean,
            # per-item DraftGenerationError rather than an uncaught exception
            # that would crash the whole run.
            raise DraftGenerationError(
                f"Anthropic response could not be parsed as structured output: {exc}"
            ) from exc

        if response.stop_reason == "refusal":
            raise DraftGenerationError("Anthropic declined to generate a draft (safety refusal)")
        if response.parsed_output is None:
            raise DraftGenerationError(
                f"Anthropic response had no structured output (stop_reason={response.stop_reason})"
            )
        return response.parsed_output


def generate_draft_for_paper(
    anthropic_client: AnthropicClient,
    draft_input: DraftInput,
    style_example: str,
    style_guide: str,
) -> DraftContent:
    prompt = build_prompt(draft_input, style_example, style_guide)
    return anthropic_client.generate_draft_content(prompt)
