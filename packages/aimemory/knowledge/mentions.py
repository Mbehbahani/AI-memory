r"""Locating an extracted entity mention inside the chunks it was quoted in (A08, P8-T06).

``entity_mentions.chunk_id`` is what turns a search hit into a graph seed: stage 4 of retrieval
(``retrieval.md`` section 4 step 1, :func:`aimemory.retrieval.expansion.seed_entity_ids`) resolves a
fused chunk hit to entity ids *only* through this column. Tier 2 wrote it as ``NULL`` for every row,
because :class:`~aimemory.domain.extraction.ExtractedEntity` carries no character offsets - the model
returns names, not spans - so graph expansion could never be seeded from a chunk.

This module closes that gap deterministically, with no second LLM call and no re-extraction:
``chunks.text`` is exactly ``episode.body[char_start:char_end]`` (MEASURED: 2837 of 2838 chunks in
the live corpus), so a surface form can be searched for in the chunk text directly and its
document-absolute offsets recovered as ``chunk.char_start + match.start()``.

Three decisions, all deliberate:

* **One row per chunk that actually contains the form, not one row per mention.** Chunking overlaps
  (MEASURED: 1367 adjacent chunk pairs in the live corpus overlap) and a document repeats its
  subject; the grain of ``entity_mentions`` is "where an entity was seen" (data-model.md section 5),
  so every containing chunk gets a row. Keeping only the first occurrence would mean a hit on any
  other chunk of the same document still seeds nothing, which is the bug this module exists to fix.
  :data:`DEFAULT_MAX_SITES` bounds the fan-out per mention so one very common name in a 119-chunk
  document cannot dominate the table.
* **An unlocatable mention keeps ``chunk_id IS NULL``.** Models infer entities the text never spells
  out (a paragraph about "the lakehouse" summarised as ``JobLab Lakehouse (DE)``); attaching such a
  mention to an arbitrary chunk would fabricate evidence and poison both the ``entity_linked`` boost
  and ``explain``. NULL means *not located* - the reason is counted in
  :class:`MentionBackfillReport` and logged as ``mention.not_located`` - and is never a guess.
* **Matching is case-insensitive, whitespace-flexible and boundary-anchored.** ``\s+`` between the
  tokens of a form, so a name wrapped across a line break still matches; ``(?<!\w)`` / ``(?!\w)``
  guards, so ``R`` does not match inside ``README`` and ``Spark`` does not match inside ``Sparkle``.
  Spellings are tried most specific first - the surface form the model returned, then the entity's
  canonical name, then ``entities.aliases``, then ``config/technology-aliases.yaml`` and
  ``project_aliases`` - and the first spelling that matches anywhere wins. The matched form and the
  method are reported, so "which spelling was found" is not guesswork either.

Consumers: :class:`aimemory.knowledge.persist.KnowledgeWriter` (forward path, every new episode) and
:func:`backfill_mention_chunks` (the rows written before this module existed; re-extracting instead
would mean sending vault text to a hosted model again for nothing).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.ids import new_id, normalize_name
from ..common.logging import get_logger
from ..domain.enums import EntityType
from .entity_resolution.aliases import AliasIndex, load_alias_index

__all__ = [
    "DEFAULT_MAX_SITES",
    "MIN_FORM_LENGTH",
    "AliasForms",
    "ChunkRef",
    "LocatedMention",
    "MatchMethod",
    "MentionBackfillReport",
    "MentionLocator",
    "MentionSite",
    "NotLocated",
    "backfill_mention_chunks",
    "load_alias_forms",
    "load_chunk_refs",
]

logger = get_logger(__name__)

#: Maximum number of chunks one mention may be attached to. A bound, not a target: a name like "AI"
#: in a 119-chunk document would otherwise write 119 rows for a single extracted entity. The run
#: report counts how often the bound was actually hit, so the choice stays MEASURED.
DEFAULT_MAX_SITES = 12

#: Shorter spellings are not searched for: a one-character alias matches wherever a word boundary
#: allows and buys nothing. An entity whose only spelling is one character stays unlocated.
MIN_FORM_LENGTH = 2

_WORD = re.compile(r"\w", re.UNICODE)


class NotLocated:
    """Why a mention has no chunk. Plain strings so a run report can count them."""

    NO_CHUNKS = "no_chunks"  # the episode has no version, or the version was never chunked
    NOT_QUOTED = "not_quoted"  # no known spelling of the entity appears in any chunk
    TOO_SHORT = "too_short"  # every candidate spelling is shorter than MIN_FORM_LENGTH


class MatchMethod:
    """Which spelling located the mention."""

    SURFACE = "surface"  # the name the extractor returned
    CANONICAL = "canonical"  # entities.canonical_name
    ENTITY_ALIAS = "entity_alias"  # entities.aliases
    ALIAS_TABLE = "alias_table"  # technology-aliases.yaml / project_aliases


@dataclass(frozen=True, slots=True)
class ChunkRef:
    """The chunk columns locating needs, plus the heading path a located mention inherits."""

    id: UUID
    ordinal: int
    char_start: int
    char_end: int
    text: str
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MentionSite:
    """One chunk a mention was found in, with **document-absolute** offsets."""

    chunk_id: UUID
    char_start: int
    char_end: int
    matched_form: str
    method: str
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocatedMention:
    """The outcome for one extracted entity: the chunks it was quoted in, or why it was not."""

    surface_form: str
    sites: tuple[MentionSite, ...] = ()
    reason: str | None = None
    truncated: bool = False

    @property
    def located(self) -> bool:
        return bool(self.sites)


# --------------------------------------------------------------------------------------------------
# Alias spellings
# --------------------------------------------------------------------------------------------------


class AliasForms:
    """Canonical entity -> every spelling the deterministic alias tables know for it.

    :class:`~aimemory.knowledge.entity_resolution.aliases.AliasIndex` maps *spelling -> canonical*,
    which is what resolution needs; locating needs the inverse, because the text may spell an entity
    any way the tables allow (``JobLab DE`` for ``JobLab Lakehouse (DE)``).
    """

    def __init__(
        self, groups: Mapping[str, tuple[str, ...]], by_spelling: Mapping[str, str]
    ) -> None:
        self._groups = dict(groups)
        self._by_spelling = dict(by_spelling)

    @classmethod
    def build(cls, index: AliasIndex) -> AliasForms:
        groups: dict[str, set[str]] = {}
        by_spelling: dict[str, str] = {}
        for spelling, hit in index.entries.items():
            key = cls._key(hit.entity_type, hit.canonical_name, hit.project_id)
            groups.setdefault(key, set()).update({spelling, normalize_name(hit.canonical_name)})
            by_spelling[spelling] = key
            if hit.project_id:
                by_spelling.setdefault(normalize_name(hit.project_id), key)
        return cls({key: tuple(sorted(values)) for key, values in groups.items()}, by_spelling)

    @staticmethod
    def _key(
        entity_type: EntityType | str | None, canonical_name: str, project_id: str | None
    ) -> str:
        if project_id:
            return f"project:{project_id}"
        kind = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type or "")
        return f"{kind}:{normalize_name(canonical_name)}"

    def forms_for(self, *names: str) -> tuple[str, ...]:
        """Every known spelling of whichever of ``names`` the alias tables recognise."""
        out: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = self._by_spelling.get(normalize_name(name or ""))
            if key is None:
                continue
            for spelling in self._groups.get(key, ()):
                if spelling not in seen:
                    seen.add(spelling)
                    out.append(spelling)
        return tuple(out)

    def __len__(self) -> int:
        return len(self._groups)


def load_alias_forms(session: Session | None = None) -> AliasForms:
    """``technology-aliases.yaml`` plus, when a session is given, the database's own project rows.

    The overlays are the ones entity resolution applies (``projects``, then ``project_aliases``), so
    a mention is searched for under exactly the spellings that would have resolved to that entity.
    """
    index = load_alias_index()
    if session is not None:
        try:
            projects = {
                str(row[0]): str(row[1])
                for row in session.execute(text("SELECT id, name FROM projects")).all()
            }
            aliases = {
                str(row[0]): str(row[1])
                for row in session.execute(
                    text("SELECT normalized_alias, project_id FROM project_aliases")
                ).all()
            }
        except Exception as exc:  # noqa: BLE001 - locating must never fail a write
            logger.warning("mention.alias_overlay_failed", error=f"{type(exc).__name__}: {exc}")
        else:
            index = index.with_projects(projects).with_project_aliases(aliases)
    return AliasForms.build(index)


# --------------------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------------------


def _compile(form: str) -> re.Pattern[str] | None:
    """Case-insensitive, whitespace-flexible, boundary-anchored pattern for one spelling."""
    stripped = (form or "").strip()
    if len(stripped) < MIN_FORM_LENGTH:
        return None
    tokens = [re.escape(token) for token in stripped.split()]
    if not tokens:
        return None
    body = r"\s+".join(tokens)
    prefix = r"(?<!\w)" if _WORD.match(stripped[0]) else ""
    suffix = r"(?!\w)" if _WORD.match(stripped[-1]) else ""
    try:
        return re.compile(prefix + body + suffix, re.IGNORECASE)
    except re.error:  # pragma: no cover - re.escape makes a bad pattern unreachable
        return None


class MentionLocator:
    """Locates mentions inside one episode's chunks. Built once per episode, reused per entity."""

    def __init__(
        self,
        chunks: Sequence[ChunkRef],
        *,
        alias_forms: AliasForms | None = None,
        max_sites: int = DEFAULT_MAX_SITES,
    ) -> None:
        self._chunks = sorted(chunks, key=lambda c: c.ordinal)
        self._alias_forms = alias_forms
        self._max_sites = max(1, max_sites)
        self._cache: dict[str, re.Pattern[str] | None] = {}

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def candidate_forms(
        self,
        surface_form: str,
        *,
        canonical_name: str | None = None,
        aliases: Iterable[str] = (),
        project_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """``[(spelling, method)]``, most specific first, de-duplicated on the normalized spelling."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(value: str | None, method: str) -> None:
            key = normalize_name(value or "")
            if not key or key in seen:
                return
            seen.add(key)
            out.append(((value or "").strip(), method))

        add(surface_form, MatchMethod.SURFACE)
        add(canonical_name, MatchMethod.CANONICAL)
        for alias in aliases:
            add(alias, MatchMethod.ENTITY_ALIAS)
        if self._alias_forms is not None:
            # By *name* only. ``entities.project_id`` is the project an entity belongs to, not the
            # entity itself, so looking spellings up by it lets every entity inherit its project's
            # aliases. MEASURED on the live corpus: that attached "Production AI Systems" to the
            # words "Oploy Website" - a fabricated mention in a chunk that never named the entity.
            # A Project entity still finds its own group through its canonical name or slug, both of
            # which ``project_aliases`` contains.
            for spelling in self._alias_forms.forms_for(surface_form, canonical_name or ""):
                add(spelling, MatchMethod.ALIAS_TABLE)
        return out

    def locate(
        self,
        surface_form: str,
        *,
        canonical_name: str | None = None,
        aliases: Iterable[str] = (),
        project_id: str | None = None,
    ) -> LocatedMention:
        """Every chunk (up to the cap) containing the first spelling that matches anywhere."""
        if not self._chunks:
            return LocatedMention(surface_form, reason=NotLocated.NO_CHUNKS)

        forms = self.candidate_forms(
            surface_form, canonical_name=canonical_name, aliases=aliases, project_id=project_id
        )
        searched = False
        for form, method in forms:
            pattern = self._pattern(form)
            if pattern is None:
                continue
            searched = True
            sites: list[MentionSite] = []
            for chunk in self._chunks:
                match = pattern.search(chunk.text)
                if match is None:
                    continue
                sites.append(
                    MentionSite(
                        chunk_id=chunk.id,
                        char_start=min(chunk.char_start + match.start(), chunk.char_end),
                        char_end=min(chunk.char_start + match.end(), chunk.char_end),
                        matched_form=form,
                        method=method,
                        heading_path=chunk.heading_path,
                    )
                )
            if sites:
                return LocatedMention(
                    surface_form,
                    sites=tuple(sites[: self._max_sites]),
                    truncated=len(sites) > self._max_sites,
                )
        return LocatedMention(
            surface_form, reason=NotLocated.NOT_QUOTED if searched else NotLocated.TOO_SHORT
        )

    def _pattern(self, form: str) -> re.Pattern[str] | None:
        if form not in self._cache:
            self._cache[form] = _compile(form)
        return self._cache[form]


# --------------------------------------------------------------------------------------------------
# Loading chunks
# --------------------------------------------------------------------------------------------------

_CHUNKS_SQL = """
    SELECT c.id, c.ordinal, c.char_start, c.char_end, c.text, c.heading_path
      FROM chunks c
     WHERE c.version_id = CAST(:version_id AS uuid)
     ORDER BY c.ordinal
"""


def load_chunk_refs(
    session: Session, version_id: UUID | str | None, *, section_path: Sequence[str] = ()
) -> list[ChunkRef]:
    """The chunks of one source version, optionally narrowed to an episode's section.

    An episode whose ``section_path`` is set covers one section of a document, so only chunks whose
    ``heading_path`` starts with that section can hold its mentions. V0.1 writes document-level
    episodes (``section_path = {}``), which takes the whole version; the filter is here so a
    section-level episode cannot silently borrow another section's chunks.

    **A section path that matches no chunk is not a section path.** MEASURED on the live corpus: a
    ``document_change`` episode carries ``section_path = ['__change__']`` purely to coexist with the
    document episode under ``uq_episodes_version_section``
    (:mod:`aimemory.sources.pipeline`), while its body is the *whole* document. Narrowing on that
    sentinel returned zero chunks and reported every entity of a changed document as unlocatable, so
    a filter that excludes everything is discarded - and logged - rather than obeyed.
    """
    if version_id is None:
        return []
    rows = session.execute(text(_CHUNKS_SQL), {"version_id": str(version_id)}).mappings().all()
    wanted = tuple(section_path or ())
    refs: list[ChunkRef] = []
    for row in rows:
        heading = tuple(row["heading_path"] or ())
        if wanted and heading[: len(wanted)] != wanted:
            continue
        refs.append(_chunk_ref(row))
    if wanted and not refs and rows:
        logger.info(
            "mention.section_path_ignored",
            version_id=str(version_id),
            section_path=list(wanted),
            chunks=len(rows),
        )
        refs = [_chunk_ref(row) for row in rows]
    return refs


def _chunk_ref(row: Mapping[str, Any]) -> ChunkRef:
    return ChunkRef(
        id=UUID(str(row["id"])),
        ordinal=int(row["ordinal"]),
        char_start=int(row["char_start"]),
        char_end=int(row["char_end"]),
        text=row["text"] or "",
        heading_path=tuple(row["heading_path"] or ()),
    )


# --------------------------------------------------------------------------------------------------
# Reconciling entity_mentions against the chunks
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class MentionBackfillReport:
    """MEASURED result of one reconcile run. Every field is counted, nothing is estimated."""

    groups: int = 0  # distinct (entity, episode, surface form) triples examined
    located: int = 0  # groups that matched at least one chunk
    not_located: int = 0  # groups that matched nothing: they keep one NULL-chunk row
    unchanged: int = 0  # groups whose rows already matched the computed set exactly
    rows_updated: int = 0
    rows_inserted: int = 0
    rows_deleted: int = 0
    sites: int = 0  # total (mention, chunk) pairs the corpus should hold
    truncated: int = 0  # groups that hit the max-sites bound
    by_reason: dict[str, int] = field(default_factory=dict)
    by_method: dict[str, int] = field(default_factory=dict)
    sites_per_mention: dict[int, int] = field(default_factory=dict)
    unlocated_examples: list[dict[str, Any]] = field(default_factory=list)
    located_examples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "groups": self.groups,
            "located": self.located,
            "not_located": self.not_located,
            "unchanged": self.unchanged,
            "rows_updated": self.rows_updated,
            "rows_inserted": self.rows_inserted,
            "rows_deleted": self.rows_deleted,
            "sites": self.sites,
            "truncated": self.truncated,
            "by_reason": dict(sorted(self.by_reason.items())),
            "by_method": dict(sorted(self.by_method.items())),
            "sites_per_mention": dict(sorted(self.sites_per_mention.items())),
            "unlocated_examples": self.unlocated_examples,
            "located_examples": self.located_examples,
        }


#: One row per (entity, episode, surface form), with its rows and their chunks as parallel arrays
#: (same ``ORDER BY``), plus everything locating needs. Grouping by the triple - not by row - is what
#: makes the reconcile idempotent: the triple is the natural key of "this episode named this entity",
#: and the chunks under it are derived data that may be recomputed at any time.
_GROUPS_SQL = """
    SELECT m.entity_id,
           m.episode_id,
           m.surface_form,
           array_agg(m.id ORDER BY m.id) AS row_ids,
           array_agg(CAST(m.chunk_id AS text) ORDER BY m.id) AS row_chunks,
           e.canonical_name,
           e.aliases,
           e.project_id AS entity_project_id,
           ep.version_id,
           ep.section_path
      FROM entity_mentions m
      JOIN entities e ON e.id = m.entity_id
      JOIN episodes ep ON ep.id = m.episode_id
     WHERE CAST(:episode_ids AS uuid[]) IS NULL
        OR m.episode_id = ANY(CAST(:episode_ids AS uuid[]))
     GROUP BY m.entity_id, m.episode_id, m.surface_form, e.canonical_name, e.aliases,
              e.project_id, ep.version_id, ep.section_path
     ORDER BY ep.version_id, m.entity_id, m.surface_form
"""

_UPDATE_SQL = """
    UPDATE entity_mentions
       SET chunk_id = CAST(:chunk_id AS uuid),
           char_start = :char_start,
           char_end = :char_end,
           heading_path = CAST(:heading_path AS text[])
     WHERE id = CAST(:id AS uuid)
"""

_CLEAR_SQL = """
    UPDATE entity_mentions
       SET chunk_id = NULL, char_start = NULL, char_end = NULL, heading_path = '{}'
     WHERE id = CAST(:id AS uuid)
"""

_DELETE_SQL = "DELETE FROM entity_mentions WHERE id = CAST(:id AS uuid)"

#: A new row copies the template's whole ``[PROV]`` stamp - same episode, same source, same model -
#: and differs only in the chunk-scoped extras of data-model.md section 6.
_INSERT_SQL = """
    INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form, char_start,
                                 char_end, confidence, source_id, source_uri, source_hash,
                                 source_version, project_id, device_id, observed_at, valid_from,
                                 valid_to, extraction_model_id, embedding_model_id,
                                 ingestion_run_id, heading_path)
    SELECT CAST(:new_id AS uuid), t.entity_id, t.episode_id, CAST(:chunk_id AS uuid),
           t.surface_form, :char_start, :char_end, t.confidence, t.source_id, t.source_uri,
           t.source_hash, t.source_version, t.project_id, t.device_id, t.observed_at, t.valid_from,
           t.valid_to, t.extraction_model_id, t.embedding_model_id, t.ingestion_run_id,
           CAST(:heading_path AS text[])
      FROM entity_mentions t
     WHERE t.id = CAST(:template_id AS uuid)
"""


def backfill_mention_chunks(
    session: Session,
    *,
    episode_ids: Sequence[UUID | str] | None = None,
    max_sites: int = DEFAULT_MAX_SITES,
    dry_run: bool = False,
    examples: int = 10,
) -> MentionBackfillReport:
    """Reconcile every ``entity_mentions`` row against the chunks that actually quote it.

    For each ``(entity, episode, surface form)`` triple the locator computes the chunk set, and the
    rows are made to match it: an existing row is updated, a spare row is reused before a new one is
    inserted, and a row pointing at a chunk the text does not support is deleted. A triple that
    cannot be located anywhere keeps **exactly one** row with ``chunk_id IS NULL`` - the episode did
    name the entity, and that assertion is never thrown away.

    Reconciling rather than only filling NULLs is deliberate: the alias tables, the chunker and this
    matcher all change, and a run must then be able to *remove* a link it should not have made.
    Because the computation is a pure function of the text, a second run is a no-op (``unchanged``).

    ``episode_ids`` narrows the run to the episodes one ingestion run wrote, which is what the Tier 2
    runner needs; the default is the whole corpus, which is what an operator repairing history needs.

    The caller owns the transaction; nothing here commits.
    """
    report = MentionBackfillReport()
    alias_forms = load_alias_forms(session)
    scope = [str(e) for e in episode_ids] if episode_ids is not None else None
    rows = session.execute(text(_GROUPS_SQL), {"episode_ids": scope}).mappings().all()

    locators: dict[str, MentionLocator] = {}
    for row in rows:
        report.groups += 1
        version_id = row["version_id"]
        section_path = tuple(row["section_path"] or ())
        cache_key = f"{version_id}|{'/'.join(section_path)}"
        locator = locators.get(cache_key)
        if locator is None:
            locator = MentionLocator(
                load_chunk_refs(session, version_id, section_path=section_path),
                alias_forms=alias_forms,
                max_sites=max_sites,
            )
            locators[cache_key] = locator

        located = locator.locate(
            row["surface_form"],
            canonical_name=row["canonical_name"],
            aliases=list(row["aliases"] or []),
            project_id=row["entity_project_id"],
        )
        _count_outcome(report, row, located, locator, examples=examples)
        if not dry_run:
            _apply_group(session, report, row, located)

    logger.info(
        "mention.reconciled", **{k: v for k, v in report.as_dict().items() if isinstance(v, int)}
    )
    return report


def _count_outcome(
    report: MentionBackfillReport,
    row: Mapping[str, Any],
    located: LocatedMention,
    locator: MentionLocator,
    *,
    examples: int,
) -> None:
    """Everything the run report says about one triple, before anything is written."""
    if not located.located:
        report.not_located += 1
        reason = located.reason or NotLocated.NOT_QUOTED
        report.by_reason[reason] = report.by_reason.get(reason, 0) + 1
        logger.info(
            "mention.not_located",
            surface_form=row["surface_form"],
            entity_id=str(row["entity_id"]),
            episode_id=str(row["episode_id"]),
            reason=reason,
            chunks_searched=locator.chunk_count,
        )
        if len(report.unlocated_examples) < examples:
            report.unlocated_examples.append(
                {
                    "surface_form": row["surface_form"],
                    "canonical_name": row["canonical_name"],
                    "episode_id": str(row["episode_id"]),
                    "reason": reason,
                    "chunks_searched": locator.chunk_count,
                }
            )
        return

    report.located += 1
    report.truncated += int(located.truncated)
    report.sites += len(located.sites)
    method = located.sites[0].method
    report.by_method[method] = report.by_method.get(method, 0) + 1
    count = len(located.sites)
    report.sites_per_mention[count] = report.sites_per_mention.get(count, 0) + 1
    if len(report.located_examples) < examples:
        report.located_examples.append(
            {
                "surface_form": row["surface_form"],
                "episode_id": str(row["episode_id"]),
                "chunks": count,
                "matched_form": located.sites[0].matched_form,
                "method": method,
                "char_start": located.sites[0].char_start,
            }
        )


def _apply_group(
    session: Session,
    report: MentionBackfillReport,
    row: Mapping[str, Any],
    located: LocatedMention,
) -> None:
    """Make the rows of one triple equal the computed chunk set. Update, reuse, insert, delete."""
    row_ids = [str(rid) for rid in row["row_ids"]]
    row_chunks = [str(c) if c else None for c in row["row_chunks"]]
    existing: dict[str, list[str]] = {}
    spare: list[str] = []
    for rid, cid in zip(row_ids, row_chunks, strict=True):
        if cid is None:
            spare.append(rid)
        else:
            existing.setdefault(cid, []).append(rid)

    desired = {str(site.chunk_id): site for site in located.sites}

    if not desired:
        # Keep one row as the record that this episode named this entity; drop the rest.
        keeper = spare[0] if spare else row_ids[0]
        if keeper not in spare:
            session.execute(text(_CLEAR_SQL), {"id": keeper})
            report.rows_updated += 1
        for rid in row_ids:
            if rid != keeper:
                session.execute(text(_DELETE_SQL), {"id": rid})
                report.rows_deleted += 1
        report.unchanged += int(len(row_ids) == 1 and keeper in spare)
        return

    keep: set[str] = set()
    for cid, rids in existing.items():
        if cid in desired:
            keep.add(rids[0])
            spare.extend(rids[1:])
        else:
            spare.extend(rids)

    touched = 0
    for cid, site in desired.items():
        if cid in existing:
            continue
        payload = {
            "chunk_id": cid,
            "char_start": site.char_start,
            "char_end": site.char_end,
            "heading_path": list(site.heading_path),
        }
        if spare:
            session.execute(text(_UPDATE_SQL), {**payload, "id": spare.pop(0)})
            report.rows_updated += 1
        else:
            session.execute(
                text(_INSERT_SQL),
                {**payload, "new_id": str(new_id()), "template_id": row_ids[0]},
            )
            report.rows_inserted += 1
        touched += 1

    for rid in spare:
        session.execute(text(_DELETE_SQL), {"id": rid})
        report.rows_deleted += 1
        touched += 1
    report.unchanged += int(touched == 0)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    """``python -m aimemory.knowledge.mentions [--apply] [--max-sites N]``; prints MEASURED JSON."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Backfill entity_mentions.chunk_id.")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--max-sites", type=int, default=DEFAULT_MAX_SITES)
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args(argv)

    from ..persistence.db import Database

    db = Database()
    try:
        with db.session() as session:
            report = backfill_mention_chunks(
                session,
                max_sites=args.max_sites,
                dry_run=not args.apply,
                examples=args.examples,
            )
            if not args.apply:
                session.rollback()
    finally:
        db.dispose()
    print(json.dumps({"applied": args.apply, **report.as_dict()}, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
