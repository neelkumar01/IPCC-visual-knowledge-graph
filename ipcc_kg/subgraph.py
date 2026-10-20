"""Source-faithful, bounded exploration of the published IPCC AR6 RDF graph.

A figure--concept edge says *caption mentions concept*, never *image depicts
concept*. Ambiguous and excluded candidate matches remain evidence, not
accepted figure--concept edges. This module never invents RDF assertions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Iterable, Literal

from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, SKOS

from ontology.schema import IPCC, resource_iri

from .query import ConceptSummary, FigureHit, MatchMode

if TYPE_CHECKING:
    from .query import IPCCGraph


_MAX_FIGURES = 100
_MAX_RELATED_CONCEPTS = 25
_MAX_EXPANSION_NODES = 250
_MAX_EXPANSION_DEPTH = 3

# Only instance-level RDF links are traversable. RDF/OWL schema nodes, source
# URLs, and literal metadata are never traversed as graph neighborhoods.
_RELATIONS = frozenset({
    IPCC.partOfReport,
    IPCC.sectionOfWorkingGroup,
    IPCC.figureInSection,
    IPCC.figureInWorkingGroup,
    IPCC.hasImageReference,
    IPCC.hasCaptionMention,
    IPCC.mentionInFigure,
    IPCC.denotesConcept,
    IPCC.candidateConcept,
    IPCC.mentionsConcept,
    IPCC.glossaryWorkingGroup,
    SKOS.member,
    PROV.wasDerivedFrom,
})

# In bounded expansion, show substantive figure/concept links before the
# individual evidence mentions. Evidence remains available for inspection.
_RELATION_ORDER = {
    predicate: index for index, predicate in enumerate((
        IPCC.mentionsConcept,
        IPCC.figureInSection,
        IPCC.figureInWorkingGroup,
        IPCC.hasImageReference,
        IPCC.sectionOfWorkingGroup,
        IPCC.partOfReport,
        SKOS.member,
        IPCC.glossaryWorkingGroup,
        IPCC.hasCaptionMention,
        IPCC.denotesConcept,
        IPCC.candidateConcept,
        IPCC.mentionInFigure,
        PROV.wasDerivedFrom,
    ))
}

# Metadata properties are copied verbatim from the published RDF. Full captions
# are intentionally excluded even when the upstream graph opted to publish them.
_METADATA = {
    IPCC.AssessmentReport: (DCTERMS.identifier, DCTERMS.source),
    IPCC.WorkingGroup: (IPCC.workingGroupCode,),
    IPCC.ReportSection: (IPCC.sectionCode,),
    IPCC.Chapter: (IPCC.sectionCode,),
    IPCC.Figure: (
        DCTERMS.identifier, DCTERMS.source, IPCC.captionStatus,
        IPCC.captionSHA256,
    ),
    IPCC.ImageReference: (IPCC.imageURL, IPCC.imagePosition),
    IPCC.ScientificConcept: (
        DCTERMS.identifier, DCTERMS.source, SKOS.prefLabel, SKOS.altLabel,
    ),
    IPCC.CaptionMention: (
        IPCC.matchedText, IPCC.startOffset, IPCC.endOffset,
        IPCC.matchKind, IPCC.resolution, IPCC.qualityRule,
    ),
    SKOS.Collection: (
        RDFS.label, DCTERMS.description, DCTERMS.isPartOf,
        DCTERMS.references,
    ),
}

# For the subgraph export, include only the applicable ontology descriptions,
# not the entire ontology, SHACL shapes, or taxonomy. This enables accurate edge
# explanations without accidentally importing unrelated classes and assertions.
_PREDICATE_METADATA = (
    RDF.type, RDFS.label, RDFS.comment, RDFS.domain, RDFS.range,
)
_CLASS_METADATA = (
    RDF.type, RDFS.label, RDFS.comment, RDFS.subClassOf,
)


def _instance_type(source: Graph, node: URIRef) -> URIRef | None:
    """Return a recognized entity class, never a schema or external resource."""
    for kind in _METADATA:  # Insertion order is deterministic.
        if (node, RDF.type, kind) in source:
            return kind
    return None


def _node_iri(node: URIRef | str) -> URIRef:
    if isinstance(node, URIRef):
        value = str(node)
    elif isinstance(node, str):
        value = node
    else:
        raise TypeError("node must be an RDF URIRef or an absolute resource IRI")
    if not value or value != value.strip() or not value.startswith("https://"):
        raise ValueError("node must be an absolute HTTPS resource IRI")
    return URIRef(value)


def _predicate_set(predicates: Iterable[URIRef] | None) -> frozenset[URIRef]:
    if predicates is None:
        return _RELATIONS
    selected = frozenset(predicates)
    if not all(isinstance(p, URIRef) and p in _RELATIONS for p in selected):
        raise ValueError("predicates must contain only supported RDF instance predicates")
    return selected


def _edges_for(
    source: Graph, node: URIRef, predicates: frozenset[URIRef],
) -> tuple[tuple[URIRef, URIRef, URIRef], ...]:
    """Find incoming and outgoing source triples in stable order."""
    edges: set[tuple[URIRef, URIRef, URIRef]] = set()
    for predicate in predicates:
        for obj in source.objects(node, predicate):
            if isinstance(obj, URIRef) and _instance_type(source, obj) is not None:
                edges.add((node, predicate, obj))
        for subject in source.subjects(predicate, node):
            if isinstance(subject, URIRef) and _instance_type(source, subject) is not None:
                edges.add((subject, predicate, node))
    return tuple(sorted(
        edges, key=lambda triple: (
            _RELATION_ORDER[triple[1]], str(triple[0]), str(triple[2])
        ),
    ))


@dataclass(frozen=True, slots=True)
class KnowledgeSubgraph:
    """Ranked search results and a source-backed RDF neighborhood.

    ``figures`` and ``concepts`` retain the original *search selection*. The RDF
    graph can contain additional evidence, contextual resources and expanded
    neighbors. These do not alter the search ranking or ``total_figures``.

    Original caption text and glossary definitions remain in read-only SQLite;
    only matching snippets and published RDF metadata are serialized here.
    """

    query: str
    match: Literal["concept", "caption"]
    total_figures: int
    figures: tuple[FigureHit, ...]
    concepts: tuple[ConceptSummary, ...]
    graph: Graph
    _source: Graph | None = field(default=None, repr=False, compare=False, kw_only=True)
    _wg_filter: str | None = field(default=None, repr=False, compare=False, kw_only=True)

    @property
    def figure_ids(self) -> tuple[str, ...]:
        return tuple(hit.figure_id for hit in self.figures)

    @property
    def concept_ids(self) -> tuple[str, ...]:
        return tuple(concept.term_id for concept in self.concepts)

    def __len__(self) -> int:
        return len(self.figures)

    def neighbors(
        self, node: URIRef | str, *, predicates: Iterable[URIRef] | None = None,
    ) -> tuple[tuple[URIRef, URIRef, URIRef], ...]:
        """Return real incident RDF relations, including undisplayed neighbors.

        Only resources already present in this view may be expanded, and only
        the instance predicates defined above can be traversed. Returned
        triples are not inferred or added to the view.
        """
        if self._source is None:
            raise RuntimeError("Source graph unavailable for neighborhood exploration")
        uri = _node_iri(node)
        if _instance_type(self._source, uri) is None:
            raise KeyError(f"Unknown IPCC graph resource: {uri}")
        if not _has_resource(self.graph, uri):
            raise ValueError("Start node is not part of this view")
        return _edges_for(self._source, uri, _predicate_set(predicates))

    def expand(
        self, node: URIRef | str, *, depth: int = 1, max_nodes: int = 40,
        predicates: Iterable[URIRef] | None = None,
    ) -> KnowledgeSubgraph:
        """Return a new RDF view with a bounded, source-backed neighborhood.

        Expansion is bidirectional so concepts can reveal more figures and
        figures can reveal image/evidence/report context. ``max_nodes`` limits
        *additional* resources, not already-visible nodes. Any working-group
        filter from the initial search also applies to newly discovered figures.
        The source graph and original search results remain unchanged.
        """
        if type(depth) is not int or not 1 <= depth <= _MAX_EXPANSION_DEPTH:
            raise ValueError(f"depth must be between 1 and {_MAX_EXPANSION_DEPTH}")
        if type(max_nodes) is not int or not 1 <= max_nodes <= _MAX_EXPANSION_NODES:
            raise ValueError(f"max_nodes must be between 1 and {_MAX_EXPANSION_NODES}")

        uri = _node_iri(node)
        relations = _predicate_set(predicates)
        # Validates membership and recognition even for isolated nodes.
        self.neighbors(uri, predicates=relations)
        if self._source is None:  # ``neighbors`` already checks; defensive.
            raise RuntimeError("Source graph unavailable for neighborhood exploration")

        subset = _RDFSubset(self._source, initial=self.graph)
        present = _instance_resources(self._source, subset.graph)
        frontier = (uri,)
        added = 0
        for _ in range(depth):
            following: set[URIRef] = set()
            for center in frontier:
                for triple in _edges_for(self._source, center, relations):
                    subject, _, obj = triple
                    newly_visible = tuple(x for x in (subject, obj) if x not in present)
                    if self._wg_filter and any(
                        (target, RDF.type, IPCC.Figure) in self._source
                        and (target, IPCC.figureInWorkingGroup,
                             resource_iri("working-group", self._wg_filter)) not in self._source
                        for target in newly_visible
                    ):
                        continue
                    if added + len(newly_visible) > max_nodes:
                        continue
                    subset.graph.add(triple)
                    for target in newly_visible:
                        present.add(target)
                        subset.describe(target)
                        following.add(target)
                        added += 1
            if not following:
                break
            frontier = tuple(sorted(following, key=str))

        subset.schema_descriptions()
        return replace(self, graph=subset.graph)

    def show(self, *, height: int = 650) -> None:
        """Display the selected RDF view in Colab or Jupyter."""
        from .visualize import show_subgraph
        show_subgraph(self, height=height)

    def save(self, path: str | Path) -> Path:
        """Export HTML or an RDF serialization (``.json`` is JSON-LD)."""
        output = Path(path).expanduser()
        if output.suffix.lower() in (".html", ".htm"):
            from .visualize import save_html
            return save_html(self, output)
        rdf_format = {
            ".ttl": "turtle", ".nt": "nt", ".rdf": "xml",
            ".xml": "xml", ".jsonld": "json-ld", ".json": "json-ld",
        }.get(output.suffix.lower())
        if rdf_format is None:
            raise ValueError(
                "Unsupported RDF format; use .ttl, .nt, .rdf, .xml, .jsonld or .json"
            )
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.name}.", delete=False,
        ) as stage:
            temporary = Path(stage.name)
        try:
            self.graph.serialize(destination=temporary, format=rdf_format, encoding="utf-8")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output


def _has_resource(graph: Graph, node: URIRef) -> bool:
    # Object-only resources are valid: some selected nodes have no own metadata.
    return next(graph.triples((node, None, None)), None) is not None or next(
        graph.triples((None, None, node)), None
    ) is not None


def _instance_resources(source: Graph, graph: Graph) -> set[URIRef]:
    candidates = set(graph.subjects()) | {
        obj for obj in graph.objects() if isinstance(obj, URIRef)
    }
    return {node for node in candidates if isinstance(node, URIRef)
            and _instance_type(source, node) is not None}


class _RDFSubset:
    """Copy source triples and preserve their exact provenance and semantics."""

    def __init__(self, source: Graph, *, initial: Graph | None = None) -> None:
        self.source = source
        self.graph = Graph()
        for prefix, namespace in source.namespaces():
            self.graph.bind(prefix, namespace)
        if initial is not None:
            self.graph += initial

    def describe(self, uri: URIRef) -> None:
        """Copy recognized RDF types and safe, published node properties."""
        kind = _instance_type(self.source, uri)
        if kind is None:
            raise ValueError(f"Not a recognized instance resource: {uri}")
        for rdf_type in self.source.objects(uri, RDF.type):
            self.graph.add((uri, RDF.type, rdf_type))
        # Chapter is a subtype of ReportSection. Its sectionCode is retained.
        for predicate in _METADATA[kind]:
            for value in self.source.objects(uri, predicate):
                self.graph.add((uri, predicate, value))

    def connect(self, subject: URIRef, predicate: URIRef, obj: URIRef) -> None:
        """Copy an existing instance edge and describe its endpoints."""
        if (subject, predicate, obj) not in self.source:
            raise ValueError(f"Relationship is absent from the source RDF: {predicate}")
        if predicate not in _RELATIONS:
            raise ValueError(f"Relationship cannot be traversed: {predicate}")
        self.graph.add((subject, predicate, obj))
        self.describe(subject)
        self.describe(obj)

    def connect_out(self, subject: URIRef, predicate: URIRef) -> tuple[URIRef, ...]:
        targets = tuple(sorted((o for o in self.source.objects(subject, predicate)
                                if isinstance(o, URIRef)), key=str))
        for target in targets:
            self.connect(subject, predicate, target)
        return targets

    def initial_figures(self, figure_ids: tuple[str, ...]) -> None:
        """Bring in full evidence and structure for matched figure resources."""
        for figure_id in figure_ids:
            figure = resource_iri("figure", figure_id)
            self.describe(figure)
            for group in self.connect_out(figure, IPCC.figureInWorkingGroup):
                self.connect_out(group, IPCC.partOfReport)
            for section in self.connect_out(figure, IPCC.figureInSection):
                self.connect_out(section, IPCC.sectionOfWorkingGroup)
            self.connect_out(figure, IPCC.hasImageReference)

            # Every accepted link is copied from the source, including links
            # to concepts outside the displayed top-N recommendation set.
            accepted = set(self.connect_out(figure, IPCC.mentionsConcept))
            supported: set[URIRef] = set()
            for mention in self.connect_out(figure, IPCC.hasCaptionMention):
                if (mention, IPCC.mentionInFigure, figure) in self.source:
                    self.connect(mention, IPCC.mentionInFigure, figure)
                if (mention, PROV.wasDerivedFrom, figure) in self.source:
                    self.connect(mention, PROV.wasDerivedFrom, figure)
                candidates = self.connect_out(mention, IPCC.candidateConcept)
                selected = self.connect_out(mention, IPCC.denotesConcept)
                if len(selected) > 1 or (selected and selected[0] not in candidates):
                    raise ValueError(f"Invalid caption mention selection: {mention}")
                resolution = self.source.value(mention, IPCC.resolution)
                if selected:
                    if str(resolution) not in {
                        "lexical", "explicit_expansion", "chemical_formula",
                    }:
                        raise ValueError(
                        f"Unaccepted caption mention has a selected concept: {mention}"
                    )
                    supported.add(selected[0])
                elif str(resolution) not in {"excluded", "ambiguous"}:
                    raise ValueError(f"Unresolved caption mention lacks a resolution: {mention}")
            if supported != accepted:
                raise ValueError(
                    f"Caption evidence and accepted RDF concept links disagree for {figure_id}"
                )

    def initial_concepts(self, concept_ids: tuple[str, ...]) -> None:
        for concept_id in concept_ids:
            concept = resource_iri("concept", concept_id)
            self.describe(concept)
            self.connect_out(concept, IPCC.glossaryWorkingGroup)
            for category in sorted(self.source.subjects(SKOS.member, concept), key=str):
                self.connect(category, SKOS.member, concept)

    def schema_descriptions(self) -> None:
        """Expose definitions of predicates/classes actually used in this view."""
        predicates = {p for _, p, _ in self.graph}
        classes = {o for _, _, o in self.graph.triples((None, RDF.type, None))
                   if isinstance(o, URIRef)}
        for predicate in predicates:
            for meta in _PREDICATE_METADATA:
                for value in self.source.objects(predicate, meta):
                    self.graph.add((predicate, meta, value))
        for klass in classes:
            for meta in _CLASS_METADATA:
                for value in self.source.objects(klass, meta):
                    self.graph.add((klass, meta, value))


def build_subgraph(
    kg: IPCCGraph,
    query: str,
    *,
    wg: str | None = None,
    limit: int = 20,
    related_concepts: int = 5,
    match: MatchMode = "auto",
) -> KnowledgeSubgraph:
    """Build a complete-on-demand RDF neighborhood for a figure search.

    The default result displays ranked figures and the seed plus a bounded
    selection of adjacent concepts. All actual RDF mentions (including
    excluded/ambiguous candidates), images, publication structure, glossary
    attribution, and curated topic links remain present for inspection.
    Further nodes can be discovered via ``view.neighbors`` / ``view.expand``.
    """
    if type(limit) is not int or not 1 <= limit <= _MAX_FIGURES:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_FIGURES}")
    if (type(related_concepts) is not int
            or not 0 <= related_concepts <= _MAX_RELATED_CONCEPTS):
        raise ValueError(
            f"related_concepts must be an integer between 0 and {_MAX_RELATED_CONCEPTS}"
        )

    results = kg.search(query, wg=wg, limit=limit, match=match)
    figure_ids = tuple(hit.figure_id for hit in results.items)
    seed = results.concept_id

    coverage = Counter(
        term_id
        for figure_id in figure_ids
        for term_id in kg._by_figure.get(figure_id, ())
        if term_id != seed
    )
    neighbors = sorted(
        coverage,
        key=lambda term_id: (
            -coverage[term_id], kg._concept_labels[term_id].casefold(), term_id
        ),
    )[:related_concepts]
    selected = ((seed,) if seed else ()) + tuple(neighbors)

    subset = _RDFSubset(kg._graph)
    subset.initial_figures(figure_ids)
    subset.initial_concepts(selected)
    subset.schema_descriptions()

    return KnowledgeSubgraph(
        query=results.query,
        match=results.match,
        total_figures=results.total,
        figures=results.items,
        concepts=tuple(
            ConceptSummary(term_id, kg._concept_labels[term_id]) for term_id in selected
        ),
        graph=subset.graph,
        _source=kg._graph,
        _wg_filter=wg.strip().upper() if wg is not None else None,
    )
