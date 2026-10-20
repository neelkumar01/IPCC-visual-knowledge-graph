"""Curated, non-inferential topic collections for IPCC AR6 glossary concepts.

The existing IPCC-derived vocabulary contains *concept identities* and source
attribution, not an authoritative cross-working-group subject taxonomy. This
module groups a deliberately limited set of manually selected canonical
headwords into browsing facets. It does not classify by string keywords, infer
scientific relationships, or treat a working group's attribution as a topic.

SKOS Collections are used because topical membership is NOT an ``is-a`` or
``skos:broader`` assertion. Categories may overlap; unassigned concepts remain
valid and must not be forced into a category. The choices are editorial seed
assignments by this project, NOT official IPCC taxonomy statements.

Run from the repository root (requires rdflib>=7,<8)::

    python -m ontology.taxonomy
    python -m ontology.taxonomy --vocabulary path/to/vocabulary.json

This module only reads the saved vocabulary and exports Turtle. No network,
caption access, database writes, machine-learning model or hidden synonyms.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCMITYPE, DCTERMS, OWL, RDF, RDFS, SKOS

from concepts.vocabulary import DEFAULT_OUTPUT as DEFAULT_VOCABULARY, Vocabulary, load_vocabulary, normalize_term
from ontology.schema import BASE_IRI, IPCC, ONTOLOGY_IRI, resource_iri


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY = PROJECT_ROOT / "ontology" / "ipcc_topic_taxonomy.ttl"
TAXONOMY_IRI = URIRef(BASE_IRI + "taxonomy")
CATEGORY_BASE = BASE_IRI + "taxonomy/category/"
TAXONOMY_VERSION = "1.0.0"
_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

# These are *navigation* topics, inspired by broad AR6 WG coverage. They are
# not claimed to be an IPCC-issued classification or an exhaustive taxonomy.
_WG1 = URIRef("https://www.ipcc.ch/report/ar6/wg1/")
_WG2 = URIRef("https://www.ipcc.ch/report/ar6/wg2/")
_WG3 = URIRef("https://www.ipcc.ch/report/ar6/wg3/")


@dataclass(frozen=True, slots=True)
class TopicCollection:
    """One editor-curated collection of exact glossary headwords.

    ``headwords`` must refer to canonical vocabulary *labels*, never inferred
    abbreviations. Lookup is exact after the vocabulary's typography-only
    normalization. Missing or duplicated headwords abort the build.
    """

    slug: str
    label: str
    description: str
    headwords: tuple[str, ...]
    references: tuple[URIRef, ...]


# The small, reviewed seed is intentionally auditable rather than pretending
# all 920 glossary concepts can be categorized without expert judgment. New
# terms must be added by explicit editorial review of their *definitions*.
COLLECTIONS: tuple[TopicCollection, ...] = (
    TopicCollection(
        "climate-system", "Climate system and processes",
        "Physical components, processes, state variables and responses of the climate system.",
        (
            "Climate", "Climate system", "Atmosphere", "Ocean", "Cryosphere",
            "Biosphere", "Hydrological cycle", "Water cycle", "Carbon cycle",
            "Permafrost", "Sea ice", "Snow cover", "Glacier", "Albedo",
            "Sea surface temperature", "Atlantic Meridional Overturning Circulation",
            "Climate feedback", "Climate variability", "Climate sensitivity",
            "Equilibrium climate sensitivity", "Transient climate response",
        ),
        (_WG1,),
    ),
    TopicCollection(
        "drivers-and-forcing", "Climate drivers and radiative forcing",
        "Forcing agents, greenhouse gases, and processes that alter the energy balance.",
        (
            "Radiative forcing", "External forcing", "Greenhouse effect",
            "Greenhouse gases", "Carbon dioxide", "Methane", "Nitrous oxide",
            "Black carbon", "Aerosol", "Fossil fuels", "Land-use change",
            "Solar activity",
        ),
        (_WG1, _WG3),
    ),
    TopicCollection(
        "climate-changes", "Climate change and extremes",
        "Changes in climate conditions, physical hazards and extreme events.",
        (
            "Climate change", "Global warming", "Sea level rise",
            "Climate extreme", "Extreme weather event", "Heatwave", "Drought",
            "Flood", "Ocean acidification",
        ),
        (_WG1, _WG2),
    ),
    TopicCollection(
        "impacts-and-risk", "Climate impacts and risk",
        "Consequences, hazards, exposure, vulnerabilities and systems at risk.",
        (
            "Risk", "Disaster risk", "Hazard", "Vulnerability", "Exposure",
            "Impacts", "Food security", "Water security", "Biodiversity",
            "Ecosystem services", "Health", "Heat stress",
        ),
        (_WG2,),
    ),
    TopicCollection(
        "adaptation-and-resilience", "Adaptation and resilience",
        "Responses to climate impacts, adaptive capacity and risk reduction.",
        (
            "Adaptation", "Adaptive capacity", "Adaptation limits",
            "Ecosystem-based adaptation", "Nature-based solutions",
            "Climate resilient development", "Climate resilient development pathways",
            "Transformational adaptation", "Adaptation options", "Resilience",
            "Early warning systems", "Disaster risk reduction",
            "Disaster risk management", "Climate services", "Capacity building",
        ),
        (_WG2,),
    ),
    TopicCollection(
        "mitigation-and-emissions", "Mitigation and emissions",
        "Emission-reduction pathways, energy options and carbon removal approaches.",
        (
            "Mitigation", "Carbon dioxide removal", "Net zero CO2 emissions",
            "Net zero greenhouse gas emissions", "Renewable energy", "Energy efficiency",
            "Carbon dioxide capture and storage", "Direct air capture",
            "Bioenergy with carbon dioxide capture and storage",
            "Decarbonisation", "Agriculture, Forestry and Other Land Use",
            "Bioenergy", "Energy system", "Mitigation pathways",
            "Emission pathways", "Remaining carbon budget",
        ),
        (_WG3,),
    ),
    TopicCollection(
        "policy-and-development", "Policy, governance and development",
        "Climate governance, justice, finance and sustainable-development topics.",
        (
            "Climate governance", "Governance", "Sustainable Development Goals",
            "Sustainable development", "Equity", "Climate finance",
            "Conference of the Parties", "Climate justice",
            "Development pathways", "Sustainable development pathways",
            "Capacity building",
        ),
        (_WG2, _WG3),
    ),
    TopicCollection(
        "methods-and-evidence", "Models, methods and evidence",
        "Scientific models, scenarios, assessment methods and uncertainty terminology.",
        (
            "Earth system model", "Regional climate model", "Climate model",
            "General circulation model", "Integrated assessment model", "Models",
            "Ensemble", "Scenario", "Representative Concentration Pathways",
            "Confidence", "Uncertainty", "Likelihood", "Detection", "Attribution",
            "Detection and attribution", "Climate projection", "Emulators",
            "Paleoclimate", "Lifecycle assessment", "Global warming potential",
        ),
        (_WG1, _WG2, _WG3),
    ),
)


@dataclass(frozen=True, slots=True)
class TaxonomyResult:
    """Graph and coverage information; assignments do not imply scientific truth."""

    graph: Graph
    members_by_category: Mapping[str, tuple[str, ...]]
    unassigned_term_ids: tuple[str, ...]
    vocabulary_size: int

    @property
    def summary(self) -> dict[str, object]:
        assigned = {tid for ids in self.members_by_category.values() for tid in ids}
        return {
            "taxonomy_version": TAXONOMY_VERSION,
            "categories": len(self.members_by_category),
            "vocabulary_terms": self.vocabulary_size,
            "assigned_concepts": len(assigned),
            "unassigned_concepts": len(self.unassigned_term_ids),
            "category_memberships": sum(map(len, self.members_by_category.values())),
            "members_by_category": {k: len(v) for k, v in self.members_by_category.items()},
        }


def category_iri(slug: str) -> URIRef:
    """Stable category IRI; categories are SKOS Collections, not OWL classes."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError(f"Invalid taxonomy category slug: {slug!r}")
    return URIRef(CATEGORY_BASE + slug)


def build_taxonomy(
    vocabulary: Vocabulary, *, collections: tuple[TopicCollection, ...] = COLLECTIONS
) -> TaxonomyResult:
    """Build curated SKOS topical membership from the *exact* glossary.

    Strict by design: abort on unknown/duplicate headwords or category keys,
    rather than silently losing entries if the source glossary changes. A
    canonical headword is used only to find its existing stable glossary ID.
    No lexical or inferred assignments are made for the remaining concepts.
    """
    if not isinstance(vocabulary, Vocabulary):
        raise TypeError("vocabulary must be a Vocabulary")
    if not collections:
        raise ValueError("At least one topic collection is required")

    by_label = {normalize_term(t.label): t for t in vocabulary.terms}
    graph = Graph()
    graph.bind("ipcc", IPCC)
    graph.bind("skos", SKOS)
    graph.bind("dcterms", DCTERMS)
    graph.bind("owl", OWL)
    graph.bind("rdfs", RDFS)
    graph.bind("taxonomy", URIRef(CATEGORY_BASE))

    graph.add((TAXONOMY_IRI, RDF.type, DCMITYPE.Collection))
    graph.add((TAXONOMY_IRI, DCTERMS.title, Literal("IPCC AR6 figure graph: curated topic collections", lang="en")))
    graph.add((TAXONOMY_IRI, DCTERMS.description, Literal(
        "Project-curated, non-exhaustive navigation facets for selected IPCC-glossary-derived concepts; "
        "NOT an official IPCC taxonomy, verified concept hierarchy or classification of figures.", lang="en")))
    graph.add((TAXONOMY_IRI, OWL.versionInfo, Literal(TAXONOMY_VERSION)))
    graph.add((TAXONOMY_IRI, DCTERMS.relation, ONTOLOGY_IRI))

    assignments: dict[str, tuple[str, ...]] = {}
    seen_slugs: set[str] = set()
    used_ids: set[str] = set()
    for group in collections:
        category = category_iri(group.slug)
        if group.slug in seen_slugs:
            raise ValueError(f"Repeated taxonomy category: {group.slug!r}")
        seen_slugs.add(group.slug)
        if not group.label.strip() or not group.description.strip() or not group.headwords:
            raise ValueError(f"Incomplete taxonomy category: {group.slug!r}")
        graph.add((category, RDF.type, SKOS.Collection))
        graph.add((category, RDFS.label, Literal(group.label, lang="en")))
        graph.add((category, DCTERMS.description, Literal(group.description, lang="en")))
        graph.add((category, DCTERMS.isPartOf, TAXONOMY_IRI))
        for url in group.references:
            if not isinstance(url, URIRef) or not str(url).startswith("https://www.ipcc.ch/"):
                raise ValueError(f"Invalid thematic reference for {group.slug!r}")
            # The report motivates the broad category, *not* the membership.
            graph.add((category, DCTERMS.references, url))

        ids: set[str] = set()
        seen_labels: set[str] = set()
        for name in group.headwords:
            key = normalize_term(name)
            if not key or key in seen_labels:
                raise ValueError(f"Blank or repeated headword in {group.slug!r}: {name!r}")
            seen_labels.add(key)
            term = by_label.get(key)
            if term is None:
                raise ValueError(f"Unknown canonical glossary headword in {group.slug!r}: {name!r}")
            # If a source changes to a similar alias or different headword, do
            # not silently reinterpret the curator's original assignment.
            if name != term.label:
                raise ValueError(f"Non-identical canonical headword in {group.slug!r}: {name!r} (source: {term.label!r})")
            ids.add(term.term_id)
            used_ids.add(term.term_id)
            concept = resource_iri("concept", term.term_id)
            graph.add((category, SKOS.member, concept))
            graph.add((concept, RDF.type, IPCC.ScientificConcept))
            graph.add((concept, RDF.type, SKOS.Concept))
            graph.add((concept, DCTERMS.identifier, Literal(term.term_id)))
            graph.add((concept, SKOS.prefLabel, Literal(term.label, lang="en")))
        assignments[group.slug] = tuple(sorted(ids))

    unassigned = tuple(sorted(t.term_id for t in vocabulary.terms if t.term_id not in used_ids))
    return TaxonomyResult(graph, assignments, unassigned, len(vocabulary.terms))


def write_taxonomy(
    vocabulary_path: Path = DEFAULT_VOCABULARY,
    output_path: Path = DEFAULT_TAXONOMY,
) -> dict[str, object]:
    """Export category membership to a staged Turtle file and return summary.

    The vocabulary is read-only. All assignments are validated before the new
    artifact replaces an existing export; this is one-file atomic publication.
    """
    vocabulary_path, output_path = Path(vocabulary_path), Path(output_path)
    if vocabulary_path.resolve() == output_path.resolve():
        raise ValueError("Vocabulary input and taxonomy output must be different files")
    result = build_taxonomy(load_vocabulary(vocabulary_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".ttl", prefix=".taxonomy-",
        dir=output_path.parent, delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        result.graph.serialize(destination=temporary, format="turtle", encoding="utf-8")
        # Round-trip structural sanity before exposing the file.
        parsed = Graph().parse(temporary, format="turtle")
        if set(parsed) != set(result.graph):
            raise RuntimeError("Taxonomy Turtle round-trip changed its RDF statements")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return result.summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build curated IPCC AR6 SKOS topic collections")
    parser.add_argument("--vocabulary", type=Path, default=DEFAULT_VOCABULARY)
    parser.add_argument("--output", type=Path, default=DEFAULT_TAXONOMY)
    args = parser.parse_args()
    summary = write_taxonomy(args.vocabulary, args.output)
    print(
        f"Taxonomy: {summary['categories']} categories; "
        f"{summary['assigned_concepts']}/{summary['vocabulary_terms']} concepts assigned; "
        f"{summary['unassigned_concepts']} deliberately unassigned; "
        f"{summary['category_memberships']} category memberships."
    )
    print(f"Turtle: {args.output}")


if __name__ == "__main__":
    main()
