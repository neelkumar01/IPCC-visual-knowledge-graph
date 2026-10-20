"""Evidence-backed RDF subgraphs for exploring IPCC AR6 figures.

Selections are *views of the published knowledge graph*, not new assertions.
Figure–concept edges represent accepted caption mentions, never visual
recognition, scientific causality or semantic relationships between concepts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Literal

from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, SKOS

from ontology.schema import IPCC, resource_iri

from .query import ConceptSummary, FigureHit, MatchMode

if TYPE_CHECKING:
    from .query import IPCCGraph


_MAX_FIGURES = 100
_MAX_RELATED_CONCEPTS = 25

_FIGURE_PROPERTIES = (
    RDF.type, DCTERMS.identifier, DCTERMS.source,
    IPCC.captionStatus, IPCC.captionSHA256,
)
_CONCEPT_PROPERTIES = (
    RDF.type, DCTERMS.identifier, DCTERMS.source,
    SKOS.prefLabel, SKOS.altLabel,
)
_MENTION_PROPERTIES = (
    RDF.type, IPCC.matchedText, IPCC.startOffset, IPCC.endOffset,
    IPCC.matchKind, IPCC.resolution, IPCC.qualityRule,
)
_SECTION_PROPERTIES = (RDF.type, IPCC.sectionCode)
_GROUP_PROPERTIES = (RDF.type, IPCC.workingGroupCode)
_REPORT_PROPERTIES = (RDF.type, DCTERMS.identifier, DCTERMS.source)
_IMAGE_PROPERTIES = (RDF.type, IPCC.imageURL, IPCC.imagePosition)
_CATEGORY_PROPERTIES = (RDF.type, RDFS.label, DCTERMS.description)


@dataclass(frozen=True, slots=True)
class KnowledgeSubgraph:
    """A focused RDF graph and the corresponding ranked search results.

    ``graph`` contains only triples copied from the published ontology graph.
    ``figures`` preserves query ordering and, for caption searches, the literal
    matching spans that are not RDF concept assertions. The graph deliberately
    omits full captions and glossary definitions; inspect those via
    ``kg.figure()`` and ``kg.concept()`` when reuse is appropriate.
    """

    query: str
    match: Literal["concept", "caption"]
    total_figures: int
    figures: tuple[FigureHit, ...]
    concepts: tuple[ConceptSummary, ...]
    graph: Graph

    @property
    def figure_ids(self) -> tuple[str, ...]:
        return tuple(figure.figure_id for figure in self.figures)

    @property
    def concept_ids(self) -> tuple[str, ...]:
        return tuple(concept.term_id for concept in self.concepts)

    def __len__(self) -> int:
        return len(self.figures)

    def show(self, *, height: int = 650) -> None:
        """Show this subgraph interactively in Jupyter or Google Colab."""
        from .visualize import show_subgraph
        show_subgraph(self, height=height)

    def save(self, path: str | Path) -> Path:
        """Export as interactive HTML or as an RDF serialization.

        ``.json`` means JSON-LD; it does not invent a flattened graph schema.
        """
        output = Path(path).expanduser()
        if output.suffix.lower() in (".html", ".htm"):
            from .visualize import save_html
            return save_html(self, output)
        format_by_suffix = {
            ".ttl": "turtle",
            ".nt": "nt",
            ".rdf": "xml",
            ".xml": "xml",
            ".jsonld": "json-ld",
            ".json": "json-ld",
        }
        rdf_format = format_by_suffix.get(output.suffix.lower())
        if rdf_format is None:
            raise ValueError(
                "Unsupported RDF format; use .ttl, .nt, .rdf, .xml, .jsonld or .json"
            )
        # Do not damage a previous export if serialization fails halfway.
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.name}.", delete=False
        ) as stage:
            temporary = Path(stage.name)
        try:
            self.graph.serialize(
                destination=temporary, format=rdf_format, encoding="utf-8"
            )
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output


class _RDFSubset:
    """Copy only published triples relevant to a selected set of resources."""

    def __init__(self, source: Graph) -> None:
        self.source = source
        self.graph = Graph()
        for prefix, namespace in source.namespaces():
            self.graph.bind(prefix, namespace)

    def properties(self, subject: URIRef, predicates: tuple[URIRef, ...]) -> None:
        for predicate in predicates:
            for obj in self.source.objects(subject, predicate):
                self.graph.add((subject, predicate, obj))

    def linked(self, subject: URIRef, predicate: URIRef) -> tuple[URIRef, ...]:
        targets = tuple(sorted(self.source.objects(subject, predicate), key=str))
        for target in targets:
            self.graph.add((subject, predicate, target))
        return targets

    def figures(self, figure_ids: tuple[str, ...]) -> None:
        for figure_id in figure_ids:
            figure = resource_iri("figure", figure_id)
            self.properties(figure, _FIGURE_PROPERTIES)

            for group in self.linked(figure, IPCC.figureInWorkingGroup):
                self.properties(group, _GROUP_PROPERTIES)
                for report in self.linked(group, IPCC.partOfReport):
                    self.properties(report, _REPORT_PROPERTIES)

            for section in self.linked(figure, IPCC.figureInSection):
                self.properties(section, _SECTION_PROPERTIES)
                for group in self.linked(section, IPCC.sectionOfWorkingGroup):
                    self.properties(group, _GROUP_PROPERTIES)

            for image in self.linked(figure, IPCC.hasImageReference):
                self.properties(image, _IMAGE_PROPERTIES)

    def concepts(self, term_ids: tuple[str, ...]) -> None:
        for term_id in term_ids:
            concept = resource_iri("concept", term_id)
            self.properties(concept, _CONCEPT_PROPERTIES)
            for category in sorted(self.source.subjects(SKOS.member, concept), key=str):
                self.graph.add((category, SKOS.member, concept))
                self.properties(category, _CATEGORY_PROPERTIES)

    def evidence(self, figure_ids: tuple[str, ...], term_ids: tuple[str, ...]) -> None:
        selected = {resource_iri("concept", term_id) for term_id in term_ids}
        expected: set[tuple[URIRef, URIRef]] = set()
        supported: set[tuple[URIRef, URIRef]] = set()
        for figure_id in figure_ids:
            figure = resource_iri("figure", figure_id)
            for concept in sorted(self.source.objects(figure, IPCC.mentionsConcept), key=str):
                if concept in selected:
                    self.graph.add((figure, IPCC.mentionsConcept, concept))
                    expected.add((figure, concept))

            for mention in sorted(self.source.objects(figure, IPCC.hasCaptionMention), key=str):
                concept = self.source.value(mention, IPCC.denotesConcept)
                if concept not in selected:
                    continue  # Never promote excluded/ambiguous evidence.
                if (figure, IPCC.mentionsConcept, concept) not in self.graph:
                    raise ValueError(f"Inconsistent caption evidence for figure {figure_id}")
                self.graph.add((figure, IPCC.hasCaptionMention, mention))
                if (mention, IPCC.mentionInFigure, figure) in self.source:
                    self.graph.add((mention, IPCC.mentionInFigure, figure))
                if (mention, PROV.wasDerivedFrom, figure) in self.source:
                    self.graph.add((mention, PROV.wasDerivedFrom, figure))
                self.graph.add((mention, IPCC.denotesConcept, concept))
                self.properties(mention, _MENTION_PROPERTIES)
                supported.add((figure, concept))

        if supported != expected:
            raise ValueError("Published graph has accepted links without matching caption evidence")


def build_subgraph(
    kg: IPCCGraph,
    query: str,
    *,
    wg: str | None = None,
    limit: int = 20,
    related_concepts: int = 5,
    match: MatchMode = "auto",
) -> KnowledgeSubgraph:
    """Select query-matched figures and caption-supported concept neighbors.

    ``related_concepts`` counts *additional* concept nodes beyond an exact
    glossary query. They are ranked by how many selected figures mention them;
    no direct concept-to-concept relationship is asserted. Caption-only queries
    have no seed concept, so the limit applies to all concept nodes.

    Every output RDF triple must already exist in ``kg``'s published graph.
    Unmatched queries yield an empty subgraph, not manufactured associations.
    """
    if type(limit) is not int or not 1 <= limit <= _MAX_FIGURES:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_FIGURES}")
    if (type(related_concepts) is not int
            or not 0 <= related_concepts <= _MAX_RELATED_CONCEPTS):
        raise ValueError(
            f"related_concepts must be an integer between 0 and {_MAX_RELATED_CONCEPTS}"
        )

    results = kg.search(query, wg=wg, limit=limit, match=match)
    figures = results.items
    figure_ids = tuple(hit.figure_id for hit in figures)
    seed = results.concept_id

    # Count *figure coverage*, not textual mention frequency or scientific
    # relatedness. The accepted-link index was verified against RDF at load.
    frequency = Counter(
        term_id
        for figure_id in figure_ids
        for term_id in kg._by_figure.get(figure_id, ())
        if term_id != seed
    )
    neighbors = sorted(
        frequency, key=lambda term_id: (
            -frequency[term_id], kg._concept_labels[term_id].casefold(), term_id
        )
    )[:related_concepts]
    # A valid glossary concept is still explorable when it has no figure hits.
    term_ids = ((seed,) if seed else ()) + tuple(neighbors)

    subset = _RDFSubset(kg._graph)
    subset.figures(figure_ids)
    subset.concepts(term_ids)
    subset.evidence(figure_ids, term_ids)

    return KnowledgeSubgraph(
        query=results.query,
        match=results.match,
        total_figures=results.total,
        figures=figures,
        concepts=tuple(
            ConceptSummary(term_id, kg._concept_labels[term_id])
            for term_id in term_ids
        ),
        graph=subset.graph,
    )
