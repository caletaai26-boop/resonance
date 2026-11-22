"""Persistent entities.

Design notes
- `Asset` is the single table for every stored file (source recording, working copy, stems, renders,
  mixes, masters, imported scores, exports). `kind` distinguishes them; provenance fields are mandatory.
- `ScoreVersion`, `SectionSet`, `LyricsVersion`, `Arrangement`, `MixSettings`, `MasterSettings` are
  append-only: an edit creates a new row pointing at its parent. Nothing approved is overwritten.
- Instrument and vocal tracks live inside `Arrangement.data` (JSON) because they are always edited and
  versioned together.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="faithful")  # restore | faithful | creative
    language: Mapped[str] = mapped_column(String(16), default="la")
    rights: Mapped[dict] = mapped_column(JSON, default=dict)  # rights + consent metadata
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_external_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    # pointers to the current version of each versioned document: score, sections, lyrics, arrangement, mix, master
    heads: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    assets: Mapped[list["Asset"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class Asset(Base):
    """Any stored file. `kind` values: source, working, waveform, spectrogram, stem, restored,
    score_import, score_reference_pdf, render_stem, rehearsal, human_stem, mix, master, export."""

    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    immutable: Mapped[bool] = mapped_column(Boolean, default=False)
    # provenance
    source_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the source recording
    parent_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    method: Mapped[str] = mapped_column(String(255), default="upload")
    engine: Mapped[str] = mapped_column(String(255), default="")
    engine_version: Mapped[str] = mapped_column(String(100), default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # unverified | stale | approved | draft | final
    verification: Mapped[str] = mapped_column(String(16), default="unverified")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued | running | succeeded | failed | blocked | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    step: Mapped[str] = mapped_column(String(255), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentOutput(Base):
    """Structured (schema-validated) result of one agent run."""

    __tablename__ = "agent_outputs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class _Versioned:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin: Mapped[str] = mapped_column(String(64), default="")  # e.g. auto-transcription, import:musicxml, user-edit
    note: Mapped[str] = mapped_column(String(500), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    verification: Mapped[str] = mapped_column(String(16), default="unverified")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScoreVersion(_Versioned, Base):
    __tablename__ = "score_versions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class SectionSet(_Versioned, Base):
    __tablename__ = "section_sets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class LyricsVersion(_Versioned, Base):
    __tablename__ = "lyrics_versions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class Arrangement(_Versioned, Base):
    __tablename__ = "arrangements"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class MixSettings(_Versioned, Base):
    __tablename__ = "mix_settings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class MasterSettings(_Versioned, Base):
    __tablename__ = "master_settings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class Reconstruction(Base):
    """One reconstruction attempt: ties together the exact score/arrangement/mix/master versions used."""

    __tablename__ = "reconstructions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(16), default="faithful")
    score_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    arrangement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mix_settings_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    master_settings_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stem_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    mix_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    master_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="rendered")  # rendered | mixed | mastered | stale | final
    notes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class FidelityReport(Base):
    __tablename__ = "fidelity_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    reconstruction_id: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # metrics, alignment map, discrepancies
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    gate: Mapped[int] = mapped_column(Integer, index=True)
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected | invalidated
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    refs: Mapped[dict] = mapped_column(JSON, default=dict)  # exact version ids that were reviewed
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProviderSetting(Base):
    """Server-side provider configuration. Secrets never leave the server (API returns masked values)."""

    __tablename__ = "provider_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
