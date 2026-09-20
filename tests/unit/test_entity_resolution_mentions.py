"""P8-T06 (A08): locating an extracted entity inside the chunks that actually quote it.

``entity_mentions.chunk_id`` is the only path from a retrieved chunk to a graph seed
(``retrieval.md`` section 4 step 1). Tier 2 left it NULL for every row, so graph expansion could
never be seeded. :mod:`aimemory.knowledge.mentions` fills it deterministically by searching the
episode's chunks for the surface form - which is only trustworthy if the matcher is exact about
three things, all asserted here:

* a match must be a **whole token**, never a substring of a longer word (``R`` in ``README``);
* an entity that appears in several chunks must produce **several rows**, because chunking overlaps
  and a hit on any of those chunks has to seed the same entity;
* an entity the model *inferred* rather than quoted must produce **no site and a reason**, never a
  guessed chunk - a fabricated mention would poison both the ``entity_linked`` boost and ``explain``.

No database and no LLM: the locator is pure text over :class:`ChunkRef` values.
"""

from __future__ import annotations

import uuid

import pytest
from aimemory.common.ids import normalize_name
from aimemory.domain.enums import EntityType
from aimemory.knowledge.entity_resolution.aliases import AliasHit, AliasIndex
from aimemory.knowledge.mentions import (
    AliasForms,
    ChunkRef,
    MatchMethod,
    MentionLocator,
    NotLocated,
    _compile,
)

DOCUMENT = (
    "# JobLab Lakehouse\n"  # 0
    "The JobLab DE lakehouse runs on Databricks.\n"
    "Sparkline dashboards and the README are unrelated.\n"
    "We keep PySpark jobs in the repo; Databricks bills by DBU.\n"
)


def _chunks(text: str, *, size: int, overlap: int) -> list[ChunkRef]:
    """Overlapping chunks over one document, exactly as the real chunker's offsets behave."""
    refs: list[ChunkRef] = []
    start, ordinal = 0, 0
    while start < len(text):
        end = min(start + size, len(text))
        refs.append(
            ChunkRef(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"chunk:{ordinal}"),
                ordinal=ordinal,
                char_start=start,
                char_end=end,
                text=text[start:end],
                heading_path=("JobLab Lakehouse",),
            )
        )
        if end == len(text):
            break
        start = end - overlap
        ordinal += 1
    return refs


@pytest.fixture()
def locator() -> MentionLocator:
    return MentionLocator(_chunks(DOCUMENT, size=60, overlap=20))


# ---- the matcher ---------------------------------------------------------------------------


def test_a_name_is_matched_as_a_whole_token_and_not_inside_a_longer_word() -> None:
    """``Spark`` inside ``Sparkline`` is the classic false positive; it would link the wrong node."""
    spark = _compile("Spark")
    assert spark is not None
    assert spark.search("Sparkline dashboards") is None
    assert spark.search("we run Spark jobs") is not None
    assert _compile("R") is None  # below MIN_FORM_LENGTH: no pattern is built at all


def test_matching_is_case_insensitive_and_survives_a_line_break_inside_the_name() -> None:
    """Markdown wraps; ``JobLab\\nDE`` is the same mention as ``JobLab DE``."""
    pattern = _compile("JobLab DE")
    assert pattern is not None
    assert pattern.search("the joblab de lakehouse") is not None
    assert pattern.search("the JobLab\nDE lakehouse") is not None


def test_punctuation_bearing_names_still_match() -> None:
    assert _compile("joblab-lakehouse").search("repo joblab-lakehouse, cloned") is not None
    assert _compile("lambda_backend").search("lambda_backendX") is None


# ---- locating ------------------------------------------------------------------------------


def test_every_chunk_that_quotes_the_entity_gets_a_site(locator: MentionLocator) -> None:
    """Chunks overlap and documents repeat their subject: one mention, several rows, on purpose."""
    located = locator.locate("Databricks")

    assert located.located
    assert len(located.sites) >= 2, "Databricks is quoted twice; both chunks must seed the entity"
    assert len({site.chunk_id for site in located.sites}) == len(located.sites)


def test_the_recorded_offsets_point_at_the_surface_form_in_the_document(
    locator: MentionLocator,
) -> None:
    """Document-absolute offsets, so ``explain`` can quote the evidence without a second search."""
    located = locator.locate("PySpark")

    assert located.located
    for site in located.sites:
        assert DOCUMENT[site.char_start : site.char_end].lower() == "pyspark"


def test_a_located_mention_inherits_the_chunk_heading_path(locator: MentionLocator) -> None:
    """``heading_path`` is a chunk-scoped extra (data-model.md section 6), not an episode property."""
    located = locator.locate("Databricks")
    assert all(site.heading_path == ("JobLab Lakehouse",) for site in located.sites)


def test_an_entity_the_text_never_spells_out_is_reported_unlocated_not_guessed(
    locator: MentionLocator,
) -> None:
    """The model infers entities from context; inventing a chunk for one would fabricate evidence."""
    located = locator.locate("Snowflake")

    assert not located.located
    assert located.sites == ()
    assert located.reason == NotLocated.NOT_QUOTED


def test_an_episode_without_chunks_says_so_rather_than_claiming_the_entity_is_absent() -> None:
    """A manual/MCP episode has no file and no chunks; that is a different fact from 'not quoted'."""
    located = MentionLocator([]).locate("Databricks")

    assert not located.located
    assert located.reason == NotLocated.NO_CHUNKS


def test_the_fan_out_per_mention_is_bounded_and_the_truncation_is_reported() -> None:
    """A very common name in a long document must not dominate ``entity_mentions``."""
    text = "Databricks. " * 40
    locator = MentionLocator(_chunks(text, size=12, overlap=0), max_sites=3)

    located = locator.locate("Databricks")

    assert len(located.sites) == 3
    assert located.truncated is True


def test_a_single_character_name_is_never_searched_for() -> None:
    located = MentionLocator(_chunks(DOCUMENT, size=60, overlap=20)).locate("R")

    assert not located.located
    assert located.reason == NotLocated.TOO_SHORT


# ---- spellings -----------------------------------------------------------------------------


@pytest.fixture()
def alias_forms() -> AliasForms:
    """The two alias tables, in miniature: one project with three spellings, one technology."""
    hit = AliasHit("JobLab Lakehouse (DE)", EntityType.PROJECT, project_id="joblab-lakehouse-de")
    tech = AliasHit("PySpark", EntityType.TECHNOLOGY)
    return AliasForms.build(
        AliasIndex(
            {
                "joblab lakehouse (de)": hit,
                "joblab de": hit,
                "joblab-lakehouse-de": hit,
                "pyspark": tech,
                "spark python api": tech,
            }
        )
    )


def test_an_alias_spelling_locates_a_mention_the_canonical_name_would_miss(
    alias_forms: AliasForms,
) -> None:
    """The registry name is ``JobLab Lakehouse (DE)``; the document only ever writes ``JobLab DE``."""
    locator = MentionLocator(_chunks(DOCUMENT, size=60, overlap=20), alias_forms=alias_forms)

    located = locator.locate(
        "JobLab Lakehouse (DE)",
        canonical_name="JobLab Lakehouse (DE)",
        project_id="joblab-lakehouse-de",
    )

    assert located.located
    assert located.sites[0].matched_form == "joblab de"
    assert located.sites[0].method == MatchMethod.ALIAS_TABLE


def test_spellings_are_tried_most_specific_first_and_de_duplicated(
    alias_forms: AliasForms,
) -> None:
    """Surface form, then canonical, then entity aliases, then the alias tables - once each."""
    locator = MentionLocator([], alias_forms=alias_forms)

    forms = locator.candidate_forms(
        "JobLab Lakehouse (DE)",
        canonical_name="joblab lakehouse (de)",
        aliases=["JobLab Lakehouse"],
        project_id="joblab-lakehouse-de",
    )
    methods = [method for _, method in forms]
    spellings = [normalize_name(form) for form, _ in forms]

    assert forms[0] == ("JobLab Lakehouse (DE)", MatchMethod.SURFACE)
    assert spellings.count("joblab lakehouse (de)") == 1, "canonical == surface: added once"
    assert ("JobLab Lakehouse", MatchMethod.ENTITY_ALIAS) in forms
    assert "joblab de" in spellings, "the alias table contributes the spelling the document uses"
    assert methods.index(MatchMethod.ENTITY_ALIAS) < methods.index(MatchMethod.ALIAS_TABLE)


def test_the_most_specific_spelling_that_matches_wins_and_is_reported(
    alias_forms: AliasForms,
) -> None:
    """Which spelling was found is recorded, so a surprising link can be explained afterwards."""
    locator = MentionLocator(_chunks(DOCUMENT, size=60, overlap=20), alias_forms=alias_forms)

    located = locator.locate("PySpark", canonical_name="PySpark")

    assert located.sites[0].method == MatchMethod.SURFACE
    assert located.sites[0].matched_form == "PySpark"


def test_an_entity_does_not_inherit_the_aliases_of_the_project_it_belongs_to(
    alias_forms: AliasForms,
) -> None:
    """MEASURED regression: this attached "Production AI Systems" to the words "JobLab DE".

    ``entities.project_id`` says which project owns the entity, not which entity it is. Using it as
    an alias lookup key made every entity of a project locatable wherever the project was named,
    which records a mention in a chunk that never names the entity - a fabricated link.
    """
    locator = MentionLocator(_chunks(DOCUMENT, size=60, overlap=20), alias_forms=alias_forms)

    located = locator.locate(
        "Production AI Systems",
        canonical_name="Production AI Systems",
        project_id="joblab-lakehouse-de",
    )

    assert not located.located
    assert located.reason == NotLocated.NOT_QUOTED
