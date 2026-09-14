"""Hand-listed P4-T02 expectations for E11-E20 and expected entity *types* for E01-E20. Owner A05.

**Written and committed before the P4-T02 campaign was run.** ADR-0012 requires the two providers to
be compared on quality, not only on schema validity: the n=1 smoke comparison already showed both
returning schema-valid output while disagreeing about whether ``JobLab Lakehouse`` is a ``Project``
or a ``Dataset``. Schema validity alone would have scored both 100 %.

Three things are scored against this file (see ``run_benchmark.py``):

1. **entity recall** - share of the hand-listed entities the provider produced at all;
2. **entity-type correctness** - of the hand-listed entities it *did* produce, the share it typed
   with one of the acceptable ``schemas/extraction/episode_extraction.schema.json`` enum values;
3. **relationship recall** - share of the hand-listed pairs present in the call-2
   (``relationship_extraction.schema.json``) output, in either direction.

``EXPECTATIONS`` in :mod:`gate_expectations` holds E01-E10 (written earlier, for the ADR-0002 gate);
this module adds E11-E20 in exactly the same shape and merges them in :data:`BENCHMARK_EXPECTATIONS`.

Acceptable-type sets are deliberately generous: the measurement targets *gross* mistyping (a project
typed as a dataset, a person typed as an organization), not defensible judgement calls such as
``Technology`` vs ``InfrastructureComponent`` for a server daemon. Where the list is genuinely
ambiguous, every defensible value is allowed, so a "wrong type" in the report is a real error.
"""

from __future__ import annotations

from .gate_expectations import EXPECTATIONS, EpisodeExpectation, ExpectedEntity

__all__ = [
    "ACCEPTABLE_TYPES",
    "BENCHMARK_EXPECTATIONS",
    "EXTRA_EXPECTATIONS",
    "acceptable_types",
]


def _e(canonical: str, *aliases: str) -> ExpectedEntity:
    return ExpectedEntity(canonical, aliases)


EXTRA_EXPECTATIONS: dict[str, EpisodeExpectation] = {
    # --- E11 00 Inbox/web-extention/Monitor AI coding agents with Grafana.md -----------------
    "E11": EpisodeExpectation(
        entities=(
            _e("Grafana", "Azure Managed Grafana"),
            _e("GitHub Copilot", "Copilot"),
            _e("Claude Code"),
            _e("OpenClaw"),
            _e("OpenTelemetry", "OTel"),
            _e("OpenTelemetry Collector", "OTel Collector", "Collector"),
            _e("Application Insights", "App Insights"),
            _e("Azure Monitor", "Azure Monitor data source"),
            _e("OTLP", "OTLP endpoint"),
            _e("Log Analytics", "KQL"),
        ),
        relationships=(
            ("Grafana", "Application Insights"),
            ("OpenTelemetry Collector", "Application Insights"),
            ("Claude Code", "OpenTelemetry"),
            ("GitHub Copilot", "OpenTelemetry"),
            ("Grafana", "Azure Monitor"),
        ),
    ),
    # --- E12 03 Resources/GitHub/About and Topics Cheatsheet.md ------------------------------
    "E12": EpisodeExpectation(
        entities=(
            _e("About and Topics Cheatsheet", "About & Topics Cheatsheet"),
            _e("Repository Standard", "GitHub Repository Standard"),
            _e("README Skeleton"),
            _e("GitHub Big Picture Map"),
            _e("GitHub"),
            _e("JobPilot"),
        ),
        relationships=(
            ("About and Topics Cheatsheet", "Repository Standard"),
            ("About and Topics Cheatsheet", "README Skeleton"),
            ("About and Topics Cheatsheet", "GitHub"),
            ("JobPilot", "GitHub"),
        ),
    ),
    # --- E13 03 Resources/GitHub/Profile README Draft.md -------------------------------------
    "E13": EpisodeExpectation(
        entities=(
            _e("Mohammad Behbahani", "Mohammad"),
            _e("Senior AI Engineer"),
            _e("Oploy", "Oploy Platform"),
            _e("JobPilot"),
            _e("joblab-agent-api"),
            _e("mlflow-tracking-server"),
            _e("joblab-analytics-frontend"),
            _e("joblab-data-pipeline"),
            _e("tax-authority-enterprise-rag", "Tax Authority Enterprise RAG"),
            _e("2sHMMREM", "HMM-REM"),
            _e("AWS Bedrock", "Bedrock"),
            _e("MLflow"),
            _e("FastAPI"),
            _e("Next.js", "NextJS"),
            _e("OpenSearch"),
            _e("AWS Lambda", "Lambda"),
            _e("RAG", "retrieval-augmented generation"),
        ),
        relationships=(
            ("Mohammad Behbahani", "JobPilot"),
            ("Mohammad Behbahani", "Oploy"),
            ("joblab-agent-api", "FastAPI"),
            ("joblab-analytics-frontend", "Next.js"),
            ("joblab-agent-api", "AWS Lambda"),
            ("mlflow-tracking-server", "MLflow"),
        ),
    ),
    # --- E14 03 Resources/GitHub/GitHub Big Picture Map.md -----------------------------------
    "E14": EpisodeExpectation(
        entities=(
            _e("GitHub Big Picture Map"),
            _e("Mbehbahani"),
            _e("GitHub"),
            _e("Repository Standard"),
            _e("README Skeleton"),
            _e("About and Topics Cheatsheet"),
            _e("joblab-agent-api", "job-agent-backend"),
            _e("joblab-data-pipeline", "job-market-pipeline"),
            _e("joblab-analytics-frontend", "job-analytics-frontend"),
            _e("joblab-lakehouse"),
            _e("JobPilot"),
            _e("tax-authority-enterprise-rag"),
            _e("RAG-Boilerplate"),
            _e("mlflow-tracking-server"),
            _e("Python"),
            _e("TypeScript"),
        ),
        relationships=(
            ("GitHub Big Picture Map", "GitHub"),
            ("GitHub Big Picture Map", "Repository Standard"),
            ("job-agent-backend", "joblab-agent-api"),
            ("job-market-pipeline", "joblab-data-pipeline"),
            ("Mbehbahani", "GitHub"),
        ),
    ),
    # --- E15 06 Outputs/Career Docs/2026-09-11 - Career Update ... ---------------------------
    "E15": EpisodeExpectation(
        entities=(
            _e("Mohammad"),
            _e("Oploy"),
            _e("Personal Harness", "Harness"),
            _e("Databricks", "Databricks Free Edition"),
            _e("MLflow"),
            _e("Delta", "Delta table", "Delta tables"),
            _e("Unity Catalog"),
            _e("PySpark"),
            _e("Snowflake"),
            _e("Tableau"),
            _e("LinkedIn"),
            _e("knowledge graph", "knowledge graphs"),
            _e("RAG"),
            _e("MCP", "local MCP service"),
        ),
        relationships=(
            ("Mohammad", "Oploy"),
            ("Oploy", "Personal Harness"),
            ("Databricks", "MLflow"),
            ("Personal Harness", "knowledge graph"),
            ("Mohammad", "Databricks"),
        ),
    ),
    # --- E16 06 Outputs/Career Docs/CV - Current.md ------------------------------------------
    "E16": EpisodeExpectation(
        entities=(
            _e("Mohammad Behbahani", "Mohammad"),
            _e("Utrecht University"),
            _e("Digikala", "Digikala.com"),
            _e("Oploy"),
            _e("MLflow"),
            _e("Databricks"),
            _e("FastAPI"),
            _e("Python"),
            _e("Docker"),
            _e("AWS"),
            _e("Personal Harness", "Harness"),
            _e("Hidden Markov Models", "Hidden Markov Model", "HMM"),
            _e("RAG"),
            _e("MCP"),
        ),
        relationships=(
            ("Mohammad Behbahani", "Utrecht University"),
            ("Mohammad Behbahani", "Digikala"),
            ("Mohammad Behbahani", "Oploy"),
            ("Oploy", "FastAPI"),
            ("Mohammad Behbahani", "Personal Harness"),
        ),
    ),
    # --- E17 06 Outputs/YouTube/YouTube Channel - Oploy.md -----------------------------------
    "E17": EpisodeExpectation(
        entities=(
            _e("Oploy"),
            _e("YouTube", "Oploy YouTube", "@oploy.europe"),
            _e("JobPilot"),
            _e("FastAPI"),
            _e("Railway"),
            _e("Vercel"),
            _e("Netlify"),
            _e("PostgreSQL", "Postgres"),
            _e("MLflow"),
            _e("ChatGPT"),
            _e("Gmail Job Tracker", "Gmail"),
            _e("RAG", "Enterprise RAG"),
        ),
        relationships=(
            ("Oploy", "YouTube"),
            ("Oploy", "JobPilot"),
            ("MLflow", "Railway"),
            ("Railway", "PostgreSQL"),
        ),
    ),
    # --- E18 07 Workflows/Context Router.md --------------------------------------------------
    "E18": EpisodeExpectation(
        entities=(
            _e("Context Router"),
            _e("AIOS"),
            _e("me.md", "me"),
            _e("vault-map"),
            _e("skill-map"),
            _e("Context - Content Production"),
            _e("Context - YouTube Channel"),
            _e("Context - Oploy Website"),
            _e("Context - JobLab Product"),
            _e("Personal Harness", "Harness"),
            _e("Wagtail", "Wagtail CMS"),
            _e("oploy.eu"),
            _e("Weekly Review"),
        ),
        relationships=(
            ("Context Router", "AIOS"),
            ("AIOS", "vault-map"),
            ("AIOS", "me.md"),
            ("AIOS", "skill-map"),
            ("Context Router", "Context - JobLab Product"),
        ),
    ),
    # --- E19 AIOS/Maps/vault-map.md ----------------------------------------------------------
    "E19": EpisodeExpectation(
        entities=(
            _e("vault-map"),
            _e("me", "me.md"),
            _e("project-graph"),
            _e("skill-map"),
            _e("00 Inbox", "Inbox"),
            _e("01 Projects", "Projects"),
            _e("02 Areas", "Areas"),
            _e("03 Resources", "Resources"),
            _e("04 Archives", "Archives"),
            _e("05 Templates", "Templates"),
            _e("CV - Current"),
            _e("YouTube Channel - Oploy"),
            _e("Oploy Website - System Map"),
            _e("Repository Standard"),
            _e("README Skeleton"),
            _e("GitHub Big Picture Map"),
        ),
        relationships=(
            ("vault-map", "me"),
            ("vault-map", "project-graph"),
            ("vault-map", "skill-map"),
            ("vault-map", "01 Projects"),
            ("vault-map", "03 Resources"),
        ),
    ),
    # --- E20 AIOS/me.md ----------------------------------------------------------------------
    "E20": EpisodeExpectation(
        entities=(
            _e("Mohammad"),
            _e("Oploy"),
            _e("JobLab"),
            _e("JobPilot"),
            _e("Personal Harness", "Harness"),
            _e("HMM-REM", "HMM/REM", "Hidden Markov"),
            _e("Python"),
            _e("TypeScript"),
            _e("FastAPI"),
            _e("Next.js", "NextJS"),
            _e("SvelteKit"),
            _e("LangChain"),
            _e("Databricks"),
            _e("Delta Lake", "Delta"),
            _e("Unity Catalog"),
            _e("MLflow"),
            _e("AWS"),
            _e("Obsidian"),
            _e("Claude Code"),
            _e("AIOS"),
            _e("vault-map"),
            _e("project-graph"),
            _e("skill-map"),
        ),
        relationships=(
            ("Mohammad", "Oploy"),
            ("Mohammad", "Python"),
            ("JobLab", "JobPilot"),
            ("Mohammad", "Personal Harness"),
            ("AIOS", "vault-map"),
        ),
    ),
}

BENCHMARK_EXPECTATIONS: dict[str, EpisodeExpectation] = {**EXPECTATIONS, **EXTRA_EXPECTATIONS}

#: canonical entity name -> the ``entities[].type`` enum values that are defensible for it.
ACCEPTABLE_TYPES: dict[str, tuple[str, ...]] = {
    # people
    "Mohammad": ("Person",),
    "Mohammad Behbahani": ("Person",),
    "Mbehbahani": ("Person",),
    # organizations
    "GitHub": ("Organization", "Technology", "Application"),
    "Utrecht University": ("Organization",),
    "Digikala": ("Organization",),
    "AWS": ("Organization", "Technology", "InfrastructureComponent"),
    "LinkedIn": ("Organization", "Application", "Technology"),
    "YouTube": ("Organization", "Application", "Technology"),
    "Railway": ("Organization", "Technology", "InfrastructureComponent", "Application"),
    "Vercel": ("Organization", "Technology", "InfrastructureComponent"),
    "Netlify": ("Organization", "Technology", "InfrastructureComponent"),
    "Databricks": ("Technology", "Organization", "Application", "InfrastructureComponent"),
    "Snowflake": ("Technology", "Organization", "InfrastructureComponent"),
    "Grafana": ("Technology", "Application", "Organization", "InfrastructureComponent"),
    # projects / products
    "JobLab DE Lakehouse": ("Project", "SubProject", "Application", "InfrastructureComponent"),
    "JobLab": ("Project", "SubProject", "Application", "Organization"),
    "JobPilot": ("Project", "SubProject", "Application", "Repository"),
    "Oploy": ("Project", "Organization", "Application", "SubProject"),
    "Oploy Landing Page": ("Document", "Application", "SubProject", "Project"),
    "oploy.eu": ("Application", "Project", "InfrastructureComponent", "SubProject"),
    "Personal Harness": ("Project", "Application", "SubProject"),
    "JobLab Data Pipeline": ("Project", "SubProject", "Repository", "Application"),
    "JobLab Agent Backend": ("Project", "SubProject", "Repository", "Application"),
    "JobLab Analytics Frontend": ("Project", "SubProject", "Repository", "Application"),
    "HMM-REM": ("Project", "Experiment", "Concept", "ResearchFinding", "SubProject"),
    "AIOS": ("Project", "Concept", "Application", "SubProject", "Document"),
    # repositories
    "joblab-agent-api": ("Repository", "Project", "Application", "SubProject"),
    "joblab-data-pipeline": ("Repository", "Project", "Application", "SubProject"),
    "joblab-analytics-frontend": ("Repository", "Project", "Application", "SubProject"),
    "joblab-lakehouse": ("Repository", "Project", "Application", "SubProject"),
    "mlflow-tracking-server": ("Repository", "Project", "Application", "SubProject"),
    "tax-authority-enterprise-rag": ("Repository", "Project", "Application", "SubProject"),
    "RAG-Boilerplate": ("Repository", "Project", "Application", "SubProject"),
    "2sHMMREM": ("Repository", "Project", "Experiment", "SubProject"),
    "wagtailio": ("Repository", "Application", "Project", "SubProject"),
    # documents
    "Repository Standard": ("Document",),
    "GitHub Repository Standard": ("Document",),
    "README Skeleton": ("Document",),
    "About and Topics Cheatsheet": ("Document",),
    "GitHub Big Picture Map": ("Document",),
    "Operating Manual": ("Document",),
    "Weekly Review": ("Document", "Task", "Concept"),
    "Context Router": ("Document", "Concept"),
    "Context - Content Production": ("Document",),
    "Context - YouTube Channel": ("Document",),
    "Context - Oploy Website": ("Document",),
    "Context - JobLab Product": ("Document",),
    "vault-map": ("Document", "Concept"),
    "project-graph": ("Document", "Concept"),
    "skill-map": ("Document", "Concept"),
    "me": ("Document", "Concept"),
    "me.md": ("Document", "Concept"),
    "CV - Current": ("Document",),
    "YouTube Channel - Oploy": ("Document",),
    "Oploy Website - System Map": ("Document",),
    "CITATION.cff": ("Document", "Technology", "Concept"),
    "00 Inbox": ("Document", "Concept"),
    "01 Projects": ("Document", "Concept"),
    "02 Areas": ("Document", "Concept"),
    "03 Resources": ("Document", "Concept"),
    "04 Archives": ("Document", "Concept"),
    "05 Templates": ("Document", "Concept"),
    "06 Outputs": ("Document", "Concept"),
    # concepts
    "decision intelligence": ("Concept",),
    "AI engineering": ("Concept",),
    "Senior AI Engineer": ("Concept", "Requirement"),
    "RAG": ("Concept", "Technology"),
    "knowledge graph": ("Concept", "Technology"),
    "Hidden Markov Models": ("Concept", "Technology", "ResearchFinding"),
    "MIT": ("Concept", "Document", "Organization"),
    # technologies / infrastructure
    "Supabase": ("Technology", "InfrastructureComponent", "Application", "Organization"),
    "PySpark": ("Technology",),
    "Tableau Public": ("Technology", "Application", "Organization"),
    "Tableau": ("Technology", "Application", "Organization"),
    "Delta": ("Technology", "Dataset", "InfrastructureComponent"),
    "Delta Lake": ("Technology", "Dataset", "InfrastructureComponent"),
    "Parquet": ("Technology", "Dataset", "Concept"),
    "Unity Catalog": ("Technology", "InfrastructureComponent", "Application"),
    "Bedrock": ("Technology", "Application", "InfrastructureComponent"),
    "AWS Bedrock": ("Technology", "Application", "InfrastructureComponent"),
    "AWS Lambda": ("Technology", "InfrastructureComponent", "Application"),
    "OpenSearch": ("Technology", "InfrastructureComponent", "Application"),
    "FastAPI": ("Technology",),
    "Docker": ("Technology", "InfrastructureComponent"),
    "Python": ("Technology",),
    "TypeScript": ("Technology",),
    "Next.js": ("Technology",),
    "SvelteKit": ("Technology",),
    "Convex": ("Technology", "InfrastructureComponent", "Organization"),
    "Nova": ("Technology",),
    "LangChain": ("Technology",),
    "MLflow": ("Technology", "Application", "InfrastructureComponent"),
    "Django": ("Technology",),
    "Wagtail": ("Technology", "Application"),
    "Wagtail admin": ("Technology", "Application", "InfrastructureComponent"),
    "Wagtail API": ("Technology", "InfrastructureComponent", "Application"),
    "Obsidian": ("Technology", "Application"),
    "Claude Code": ("Technology", "Application"),
    "ChatGPT": ("Technology", "Application"),
    "GitHub Copilot": ("Technology", "Application"),
    "OpenClaw": ("Technology", "Application"),
    "OpenTelemetry": ("Technology", "Concept"),
    "OpenTelemetry Collector": ("Technology", "InfrastructureComponent", "Application"),
    "Application Insights": ("Technology", "Application", "InfrastructureComponent"),
    "Azure Monitor": ("Technology", "Application", "InfrastructureComponent"),
    "OTLP": ("Technology", "Concept"),
    "Log Analytics": ("Technology", "Application", "InfrastructureComponent"),
    "PostgreSQL": ("Technology", "InfrastructureComponent"),
    "MCP": ("Technology", "Concept"),
    "Gmail Job Tracker": ("Application", "Technology", "Project"),
    "Ubuntu Server": ("Technology", "InfrastructureComponent", "Application"),
    "Cockpit": ("Technology", "Application", "InfrastructureComponent"),
    "Portainer": ("Technology", "Application", "InfrastructureComponent"),
    "Cloudflare Tunnel": ("Technology", "InfrastructureComponent", "Application"),
    "Huawei laptop": ("InfrastructureComponent", "Technology"),
    "Windows laptop": ("InfrastructureComponent", "Technology"),
    "ufw": ("Technology", "InfrastructureComponent", "Application"),
    "netplan": ("Technology", "InfrastructureComponent", "Application"),
    "SSH": ("Technology", "InfrastructureComponent"),
    "ssh-keygen": ("Technology", "Application"),
    "sudo": ("Technology", "Concept", "Application"),
    "PowerShell": ("Technology", "Application"),
    "Terraform": ("Technology", "InfrastructureComponent"),
}


def acceptable_types(canonical: str) -> tuple[str, ...] | None:
    """Acceptable enum values for ``canonical``; ``None`` means "not scored for type"."""
    return ACCEPTABLE_TYPES.get(canonical)
