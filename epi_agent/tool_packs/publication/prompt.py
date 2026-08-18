from epi_agent.tool_packs.publication.pubmed import is_pubmed_configured


_PUBLICATION_CORE_PROMPT = """\
Publication knowledge rules:
Use publication-search_study_evidence with one exact study_id for design, eligibility, operational
definition, variable-domain, and historical analysis-pattern questions.
Use publication-open_study_source with the exact source_ref returned by search
when more bounded sections are needed. Publication evidence is independent from participant
database availability and never proves that a field exists in the current
database. Cite supported publication claims using the exact bracket form
[source_id] from tool observations. Do not claim evidence absent from those
observations.
"""

_PUBMED_PROMPT = """\
For current biomedical and epidemiology literature, use
publication-search_pubmed before general web search. It returns citation
metadata only; use publication-open_pubmed_article with one exact PMID before
claiming an abstract supports a conclusion. Cite PubMed findings as
[pubmed:<PMID>] from tool observations. PubMed evidence never proves that a
field exists in the current participant database."""


def build_publication_system_prompt(*, include_pubmed: bool | None = None) -> str:
    if include_pubmed is None:
        include_pubmed = is_pubmed_configured()
    return "\n".join(
        section
        for section in (_PUBLICATION_CORE_PROMPT, _PUBMED_PROMPT if include_pubmed else "")
        if section
    )


PUBLICATION_SYSTEM_PROMPT = build_publication_system_prompt()


__all__ = ["PUBLICATION_SYSTEM_PROMPT", "build_publication_system_prompt"]
