"""Data models for the AgentFlow pipeline.

Field names are fixed by the specs (see specs/02-05-*.md) and MUST NOT be renamed.
All dataclasses expose `to_dict()` for JSON serialization (datetime -> ISO 8601).
Some also expose `from_dict()` for round-trip deserialization.

Style profile is intentionally NOT modeled as a strict dataclass — it stays a
dict to allow user-side schema evolution. See ``load_style_profile`` /
``dump_style_profile`` helpers at the bottom of the module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Raw data (shared across D1)
# ---------------------------------------------------------------------------


@dataclass
class RawSignal:
    source: str
    source_item_id: str
    author: str | None
    text: str
    url: str
    published_at: datetime
    engagement: dict[str, int] = field(default_factory=dict)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_item_id": self.source_item_id,
            "author": self.author,
            "text": self.text,
            "url": self.url,
            "published_at": _iso(self.published_at),
            "engagement": dict(self.engagement),
            "raw_metadata": dict(self.raw_metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawSignal":
        return cls(
            source=data["source"],
            source_item_id=data["source_item_id"],
            author=data.get("author"),
            text=data["text"],
            url=data["url"],
            published_at=_parse_iso(data["published_at"]),
            engagement=dict(data.get("engagement") or {}),
            raw_metadata=dict(data.get("raw_metadata") or {}),
        )


@dataclass
class TopicCluster:
    cluster_id: str
    signals: list[RawSignal]
    centroid_embedding: list[float]
    summary_one_liner: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "signals": [s.to_dict() for s in self.signals],
            "centroid_embedding": list(self.centroid_embedding),
            "summary_one_liner": self.summary_one_liner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TopicCluster":
        return cls(
            cluster_id=data["cluster_id"],
            signals=[RawSignal.from_dict(s) for s in data.get("signals", [])],
            centroid_embedding=list(data.get("centroid_embedding") or []),
            summary_one_liner=data.get("summary_one_liner", ""),
        )


# ---------------------------------------------------------------------------
# D1 output
# ---------------------------------------------------------------------------


@dataclass
class SuggestedAngle:
    angle: str
    fit_explanation: str
    depth: str
    difficulty: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestedAngle":
        return cls(
            angle=data["angle"],
            fit_explanation=data.get("fit_explanation", data.get("fit_with_style", "")),
            depth=data.get("depth", "medium"),
            difficulty=data.get("difficulty", "medium"),
        )


@dataclass
class Hotspot:
    id: str
    topic_one_liner: str
    source_references: list[dict[str, Any]]
    mainstream_views: list[str]
    overlooked_angles: list[str]
    recommended_series: str
    series_confidence: float
    suggested_angles: list[SuggestedAngle]
    freshness_score: float
    depth_potential: str
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic_one_liner": self.topic_one_liner,
            "source_references": list(self.source_references),
            "mainstream_views": list(self.mainstream_views),
            "overlooked_angles": list(self.overlooked_angles),
            "recommended_series": self.recommended_series,
            "series_confidence": self.series_confidence,
            "suggested_angles": [a.to_dict() for a in self.suggested_angles],
            "freshness_score": self.freshness_score,
            "depth_potential": self.depth_potential,
            "generated_at": _iso(self.generated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Hotspot":
        return cls(
            id=data["id"],
            topic_one_liner=data["topic_one_liner"],
            source_references=list(data.get("source_references") or []),
            mainstream_views=list(data.get("mainstream_views") or []),
            overlooked_angles=list(data.get("overlooked_angles") or []),
            recommended_series=data.get("recommended_series", ""),
            series_confidence=float(data.get("series_confidence", 0.0)),
            suggested_angles=[
                SuggestedAngle.from_dict(a) for a in data.get("suggested_angles") or []
            ],
            freshness_score=float(data.get("freshness_score", 0.0)),
            depth_potential=data.get("depth_potential", "medium"),
            generated_at=_parse_iso(data.get("generated_at")),
        )


@dataclass
class D1Output:
    generated_at: datetime
    hotspots: list[Hotspot]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.generated_at),
            "hotspots": [h.to_dict() for h in self.hotspots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "D1Output":
        return cls(
            generated_at=_parse_iso(data.get("generated_at")),
            hotspots=[Hotspot.from_dict(h) for h in data.get("hotspots") or []],
        )


# ---------------------------------------------------------------------------
# D2 output
# ---------------------------------------------------------------------------


@dataclass
class TitleCandidate:
    text: str
    style: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TitleCandidate":
        return cls(
            text=data.get("text") or data.get("title", ""),
            style=data.get("style", "declarative"),
            rationale=data.get("rationale", ""),
        )


@dataclass
class OpeningCandidate:
    opening_text: str
    style: str
    hook_strength: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpeningCandidate":
        return cls(
            opening_text=data["opening_text"],
            style=data.get("style", "data"),
            hook_strength=data.get("hook_strength", "medium"),
        )


@dataclass
class ClosingCandidate:
    closing_text: str
    style: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClosingCandidate":
        return cls(
            closing_text=data["closing_text"],
            style=data.get("style", "open"),
        )


@dataclass
class Section:
    heading: str
    key_arguments: list[str]
    estimated_words: int
    section_purpose: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Section":
        return cls(
            heading=data["heading"],
            key_arguments=list(data.get("key_arguments") or []),
            estimated_words=int(data.get("estimated_words", 400)),
            section_purpose=data.get("section_purpose", ""),
        )


@dataclass
class SkeletonOutput:
    title_candidates: list[TitleCandidate]
    opening_candidates: list[OpeningCandidate]
    section_outline: list[Section]
    closing_candidates: list[ClosingCandidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title_candidates": [c.to_dict() for c in self.title_candidates],
            "opening_candidates": [c.to_dict() for c in self.opening_candidates],
            "section_outline": [s.to_dict() for s in self.section_outline],
            "closing_candidates": [c.to_dict() for c in self.closing_candidates],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkeletonOutput":
        return cls(
            title_candidates=[
                TitleCandidate.from_dict(c) for c in data.get("title_candidates") or []
            ],
            opening_candidates=[
                OpeningCandidate.from_dict(c)
                for c in data.get("opening_candidates") or []
            ],
            section_outline=[
                Section.from_dict(s) for s in data.get("section_outline") or []
            ],
            closing_candidates=[
                ClosingCandidate.from_dict(c)
                for c in data.get("closing_candidates") or []
            ],
        )


@dataclass
class ImagePlaceholder:
    id: str
    description: str
    section_heading: str
    resolved_path: str | None = None
    role: str = "body"  # "cover" or "body"; cover wins for D4 cover_image_path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImagePlaceholder":
        # Back-compat: legacy entries with no `role` AND id ending in `_cover`
        # are inferred as cover. Everything else defaults to body.
        legacy_id = str(data.get("id", ""))
        inferred_role = "cover" if legacy_id.endswith("_cover") else "body"
        return cls(
            id=data["id"],
            description=data["description"],
            section_heading=data.get("section_heading", ""),
            resolved_path=data.get("resolved_path"),
            role=str(data.get("role") or inferred_role),
        )


@dataclass
class FilledSection:
    heading: str
    content_markdown: str
    word_count: int
    compliance_score: float
    taboo_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "content_markdown": self.content_markdown,
            "word_count": self.word_count,
            "compliance_score": self.compliance_score,
            "taboo_violations": list(self.taboo_violations),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilledSection":
        return cls(
            heading=data["heading"],
            content_markdown=data["content_markdown"],
            word_count=int(data.get("word_count", 0)),
            compliance_score=float(data.get("compliance_score", 1.0)),
            taboo_violations=list(data.get("taboo_violations") or []),
        )


@dataclass
class DraftOutput:
    article_id: str
    title: str
    sections: list[FilledSection]
    total_word_count: int
    image_placeholders: list[ImagePlaceholder] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "title": self.title,
            "sections": [s.to_dict() for s in self.sections],
            "total_word_count": self.total_word_count,
            "image_placeholders": [p.to_dict() for p in self.image_placeholders],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftOutput":
        return cls(
            article_id=data["article_id"],
            title=data["title"],
            sections=[FilledSection.from_dict(s) for s in data.get("sections") or []],
            total_word_count=int(data.get("total_word_count", 0)),
            image_placeholders=[
                ImagePlaceholder.from_dict(p)
                for p in data.get("image_placeholders") or []
            ],
        )


# ---------------------------------------------------------------------------
# D3 output
# ---------------------------------------------------------------------------


@dataclass
class PlatformVersion:
    platform: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    formatting_changes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "content": self.content,
            "metadata": dict(self.metadata),
            "formatting_changes": list(self.formatting_changes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlatformVersion":
        return cls(
            platform=data["platform"],
            content=data["content"],
            metadata=dict(data.get("metadata") or {}),
            formatting_changes=list(data.get("formatting_changes") or []),
        )


@dataclass
class D3Output:
    article_id: str
    platform_versions: list[PlatformVersion]

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "platform_versions": [v.to_dict() for v in self.platform_versions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "D3Output":
        return cls(
            article_id=data["article_id"],
            platform_versions=[
                PlatformVersion.from_dict(v)
                for v in data.get("platform_versions") or []
            ],
        )


# ---------------------------------------------------------------------------
# D4 output
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    platform: str
    status: str
    published_url: str | None = None
    platform_post_id: str | None = None
    published_at: datetime | None = None
    failure_reason: str | None = None
    raw_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status,
            "published_url": self.published_url,
            "platform_post_id": self.platform_post_id,
            "published_at": _iso(self.published_at),
            "failure_reason": self.failure_reason,
            "raw_response": self.raw_response,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishResult":
        return cls(
            platform=data["platform"],
            status=data["status"],
            published_url=data.get("published_url"),
            platform_post_id=data.get("platform_post_id"),
            published_at=_parse_iso(data.get("published_at")),
            failure_reason=data.get("failure_reason"),
            raw_response=data.get("raw_response"),
        )


# ---------------------------------------------------------------------------
# Style profile helpers (dict-based, intentionally loose)
# ---------------------------------------------------------------------------


def load_style_profile(path: str | Path) -> dict[str, Any]:
    """Load a style_profile.yaml file into a plain dict."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Style profile at {p} is not a YAML mapping")
    return data


def dump_style_profile(profile: dict[str, Any], path: str | Path) -> Path:
    """Serialize a style_profile dict to YAML (UTF-8, unicode-safe)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            profile,
            fh,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    return p
