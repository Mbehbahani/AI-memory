"""Hand-listed C3 expectations for the ADR-0002 gate (P4-T04). Owner A05.

**Written and committed before the gate was run.** Plan section O scores C3 as ">= 60 % of expected
entities and >= 50 % of expected relationships (hand-listed per episode)", which is only auditable if
the list exists independently of the output. Each entry was read off the episode text in
``episodes.py`` by hand; ``aliases`` hold the other surface forms that count as the same entity, so
scoring is not defeated by ``Databricks`` vs ``Databricks Free Edition``.

Scoring rules (deliberately lenient towards Graphiti, stated so the numbers can be re-checked):

* an expected entity counts as found when any produced node name, normalized (lowercased,
  non-alphanumerics collapsed), equals or contains one of its ``names``;
* an expected relationship counts as found when *any* edge exists between the two expected entities
  in **either** direction, regardless of the edge's name - Graphiti invents its own relation
  vocabulary and does not use ``schemas/ontology.yaml`` predicates, so requiring our predicate names
  would fail the criterion for the wrong reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["EXPECTATIONS", "EpisodeExpectation", "ExpectedEntity", "normalize"]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(value: str) -> str:
    return _NON_ALNUM.sub(" ", value.lower()).strip()


@dataclass(frozen=True, slots=True)
class ExpectedEntity:
    """One entity a competent reader would extract, plus the surface forms that count as it."""

    canonical: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(normalize(n) for n in (self.canonical, *self.aliases))


@dataclass(frozen=True, slots=True)
class EpisodeExpectation:
    entities: tuple[ExpectedEntity, ...]
    relationships: tuple[tuple[str, str], ...] = field(default=())


def _e(canonical: str, *aliases: str) -> ExpectedEntity:
    return ExpectedEntity(canonical, aliases)


EXPECTATIONS: dict[str, EpisodeExpectation] = {
    # --- E01 01 Projects/JobLab Product/JobLab DE Lakehouse.md -------------------------------
    "E01": EpisodeExpectation(
        entities=(
            _e("JobLab DE Lakehouse", "joblab-lakehouse", "JobLab DE", "JobLab Lakehouse"),
            _e("Supabase"),
            _e("PySpark", "Spark"),
            _e("Databricks", "Databricks Free Edition"),
            _e("Snowflake"),
            _e("Tableau Public", "Tableau"),
            _e("Delta", "Delta table", "Delta Lake"),
            _e("Parquet"),
        ),
        relationships=(
            ("JobLab DE Lakehouse", "Databricks"),
            ("JobLab DE Lakehouse", "Snowflake"),
            ("JobLab DE Lakehouse", "PySpark"),
            ("JobLab DE Lakehouse", "Tableau Public"),
            ("JobLab DE Lakehouse", "Supabase"),
        ),
    ),
    # --- E02 01 Projects/Oploy Website/Oploy Landing Page.md ---------------------------------
    "E02": EpisodeExpectation(
        entities=(
            _e("Oploy"),
            _e("Oploy Landing Page", "landing page", "homepage"),
            _e("Mohammad"),
            _e("decision intelligence"),
            _e("AI engineering", "applied AI", "AI systems"),
        ),
        relationships=(
            ("Oploy Landing Page", "Oploy"),
            ("Mohammad", "AI engineering"),
            ("Oploy", "decision intelligence"),
        ),
    ),
    # --- E03 02 Areas/Career Development/Career Target - AI Engineering.md -------------------
    "E03": EpisodeExpectation(
        entities=(
            _e("Mohammad"),
            _e("Senior AI Engineer", "Senior AI Engineering", "AI Engineer"),
            _e("Bedrock", "AWS Bedrock", "Bedrock Converse API"),
            _e("JobLab", "JobPilot"),
            _e("Oploy"),
            _e("FastAPI"),
            _e("Docker"),
            _e("AWS"),
            _e("Python"),
            _e("Digikala"),
            _e("Utrecht University"),
            _e("RAG", "retrieval augmented generation"),
        ),
        relationships=(
            ("Mohammad", "Senior AI Engineer"),
            ("Mohammad", "Digikala"),
            ("Mohammad", "Utrecht University"),
            ("JobLab", "Bedrock"),
            ("Mohammad", "Python"),
        ),
    ),
    # --- E04 02 Areas/Home Infrastructure/Ubuntu Server - Admin Dashboards.md ----------------
    "E04": EpisodeExpectation(
        entities=(
            _e("Ubuntu Server", "Ubuntu Server 24.04", "Ubuntu"),
            _e("Cockpit"),
            _e("Portainer", "Portainer CE"),
            _e("Docker"),
            _e("Cloudflare Tunnel", "Cloudflare"),
            _e("Railway"),
            _e("oploy.eu", "oploy-app", "oploy"),
            _e("Wagtail", "oploy-test"),
            _e("Huawei laptop", "Huawei"),
            _e("ufw"),
        ),
        relationships=(
            ("Ubuntu Server", "Cockpit"),
            ("Ubuntu Server", "Portainer"),
            ("Portainer", "Docker"),
            ("oploy.eu", "Railway"),
            ("oploy.eu", "Cloudflare Tunnel"),
        ),
    ),
    # --- E05 02 Areas/Oploy Business/Oploy Website - System Map.md ---------------------------
    "E05": EpisodeExpectation(
        entities=(
            _e("oploy.eu", "oploy"),
            _e("Wagtail", "Wagtail CMS"),
            _e("Django"),
            _e("wagtailio"),
            _e("Wagtail admin", "admin"),
            _e("Wagtail API", "API v2"),
        ),
        relationships=(
            ("oploy.eu", "Wagtail"),
            ("oploy.eu", "Django"),
            ("wagtailio", "Django"),
            ("wagtailio", "oploy.eu"),
        ),
    ),
    # --- E06 03 Resources/GitHub/Repository Standard.md --------------------------------------
    "E06": EpisodeExpectation(
        entities=(
            _e("GitHub"),
            _e("Mbehbahani", "M.Behbahani"),
            _e("GitHub Repository Standard", "Repository Standard"),
            _e("README Skeleton"),
            _e("About and Topics Cheatsheet"),
            _e("MIT", "MIT license"),
            _e("CITATION.cff", "CITATION"),
        ),
        relationships=(
            ("GitHub Repository Standard", "GitHub"),
            ("GitHub Repository Standard", "README Skeleton"),
            ("GitHub Repository Standard", "About and Topics Cheatsheet"),
            ("Mbehbahani", "GitHub"),
        ),
    ),
    # --- E07 03 Resources/Ubuntu Server/Ubuntu Server Setup and Usage Guide.md ---------------
    "E07": EpisodeExpectation(
        entities=(
            _e("Ubuntu Server", "Ubuntu Server 24.04", "Ubuntu"),
            _e("netplan"),
            _e("SSH", "openssh-server", "OpenSSH"),
            _e("PowerShell"),
            _e("Huawei laptop", "Huawei"),
            _e("Windows laptop", "Windows"),
            _e("ssh-keygen", "ed25519"),
            _e("sudo", "sudoers"),
        ),
        relationships=(
            ("Ubuntu Server", "netplan"),
            ("Ubuntu Server", "SSH"),
            ("Windows laptop", "Ubuntu Server"),
            ("Ubuntu Server", "Huawei laptop"),
        ),
    ),
    # --- E08 07 Workflows/Context - JobLab Product.md ----------------------------------------
    "E08": EpisodeExpectation(
        entities=(
            _e("JobLab"),
            _e("JobPilot"),
            _e("SvelteKit"),
            _e("Convex"),
            _e("FastAPI"),
            _e("Supabase"),
            _e("Next.js", "NextJS", "Next"),
            _e("Databricks"),
            _e("Snowflake"),
            _e("Tableau Public", "Tableau"),
            _e("PySpark"),
            _e("Nova"),
        ),
        relationships=(
            ("JobPilot", "SvelteKit"),
            ("JobPilot", "Convex"),
            ("JobPilot", "JobLab"),
            ("JobLab", "FastAPI"),
            ("JobLab", "Supabase"),
        ),
    ),
    # --- E09 07 Workflows/Operating Manual.md ------------------------------------------------
    "E09": EpisodeExpectation(
        entities=(
            _e("Operating Manual"),
            _e("Weekly Review"),
            _e("Context Router"),
            _e("vault-map", "Vault Map"),
            _e("Claude Code"),
            _e("00 Inbox", "Inbox"),
            _e("01 Projects", "Projects"),
            _e("06 Outputs", "Outputs"),
        ),
        relationships=(
            ("Operating Manual", "Weekly Review"),
            ("Claude Code", "Context Router"),
            ("Claude Code", "vault-map"),
            ("01 Projects", "06 Outputs"),
        ),
    ),
    # --- E10 AIOS/Maps/project-graph.md ------------------------------------------------------
    "E10": EpisodeExpectation(
        entities=(
            _e("Mohammad"),
            _e("JobLab Data Pipeline", "JobLabScraperAPI"),
            _e("JobLab Agent Backend", "lambda_backend"),
            _e("JobLab Analytics Frontend", "job-analytics-frontend2"),
            _e("Terraform"),
            _e("Bedrock", "AWS Bedrock"),
            _e("MLflow"),
            _e("Next.js", "NextJS"),
            _e("Supabase"),
            _e("Oploy"),
            _e("HMM-REM", "PhD"),
            _e("project-graph"),
        ),
        relationships=(
            ("JobLab Agent Backend", "Bedrock"),
            ("JobLab Agent Backend", "MLflow"),
            ("JobLab Data Pipeline", "Terraform"),
            ("JobLab Analytics Frontend", "Next.js"),
            ("JobLab Agent Backend", "JobLab Data Pipeline"),
        ),
    ),
}

# The A/B supersession fixture is scored by C4, not C3, so it has no entity expectation here.
