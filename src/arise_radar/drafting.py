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
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

import anthropic
import httpx
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field

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
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=DraftContent,
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise DraftGenerationError(f"Anthropic API request failed: {exc}") from exc

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
