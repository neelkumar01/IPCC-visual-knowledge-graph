"""Core RDF/OWL vocabulary and SHACL shapes for the IPCC AR6 figure graph.

Defines *what* figures, glossary concepts, report sections, and evidence
mentions mean. Does not load source databases, create individual resources,
infer a climate taxonomy, or assert scientific relations from co-occurrence.

The ontology uses OWL/RDFS for vocabulary semantics and SKOS/PROV-O/DCTERMS
for interoperability. A separate SHACL graph expresses data constraints:
OWL domain/range are inference rules, not input validation rules.

Run from the repository root:
    python -m ontology.schema

Requires: rdflib>=7,<8. The namespace below is intended for a future
project-owned GitHub Pages site; publish/redirect it before claiming the
ontology IRIs are dereferenceable. Never reuse these IRIs for unrelated data.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.parse import quote

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS, SH, SKOS, XSD


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_IRI = "https://neelkumar01.github.io/IPCC-visual-knowledge-graph/"
ONTOLOGY_IRI = URIRef(BASE_IRI + "ontology")
IPCC = Namespace(BASE_IRI + "ontology#")
RESOURCE = Namespace(BASE_IRI + "resource/")

DEFAULT_ONTOLOGY = PROJECT_ROOT / "ontology" / "ipcc_visual_ontology.ttl"
DEFAULT_SHAPES = PROJECT_ROOT / "ontology" / "ipcc_visual_shapes.ttl"

# A figure is not necessarily part of a numbered chapter: the raw corpus also
# includes summaries for policymakers, technical summaries, annexes and atlas.
_CLASSES = (
    ("AssessmentReport", "Assessment report", "An assessment report, such as IPCC AR6.", None),
    ("WorkingGroup", "Working group", "An IPCC assessment working group (WG1, WG2 or WG3).", None),
    ("ReportSection", "Report section", "A chapter, summary, annex or atlas section containing figures.", None),
    ("Chapter", "Chapter", "A numbered or cross-cutting report chapter.", IPCC.ReportSection),
    ("Figure", "Figure", "A figure identified on an IPCC report figure page; its image may be unavailable.", PROV.Entity),
    ("ImageReference", "Image reference", "A published image URL reference, not a verified or downloaded binary.", None),
    ("ScientificConcept", "Scientific concept", "A glossary concept identity; a term match is not a validated scientific assertion.", SKOS.Concept),
    ("CaptionMention", "Caption mention", "A specific text span in a figure caption with auditable extraction status.", PROV.Entity),
)

# Domain/range tell RDF reasoners how to classify connected resources. They do
# not enforce cardinality or prove the scientific correctness of a statement.
_OBJECT_PROPERTIES = (
    ("partOfReport", "part of report", "Working group membership in an assessment report.", IPCC.WorkingGroup, IPCC.AssessmentReport),
    ("sectionOfWorkingGroup", "section of working group", "Working group publishing this section.", IPCC.ReportSection, IPCC.WorkingGroup),
    ("figureInSection", "figure in section", "Section containing this figure.", IPCC.Figure, IPCC.ReportSection),
    ("figureInWorkingGroup", "figure in working group", "Working group publishing this figure.", IPCC.Figure, IPCC.WorkingGroup),
    ("hasImageReference", "has image reference", "Image URL listed for this figure, without implying download success.", IPCC.Figure, IPCC.ImageReference),
    ("hasCaptionMention", "has caption mention", "Observed lexical mention in the figure caption, including excluded or ambiguous mentions.", IPCC.Figure, IPCC.CaptionMention),
    ("mentionInFigure", "mention in figure", "Figure whose caption contains this exact mention.", IPCC.CaptionMention, IPCC.Figure),
    ("denotesConcept", "denotes concept", "Selected glossary concept for a non-excluded caption mention.", IPCC.CaptionMention, IPCC.ScientificConcept),
    ("candidateConcept", "candidate concept", "Possible glossary interpretation, including unresolved alternatives.", IPCC.CaptionMention, IPCC.ScientificConcept),
    ("mentionsConcept", "mentions concept", "Convenience link: a resolved, non-excluded concept is mentioned in the figure caption, not necessarily depicted in its image.", IPCC.Figure, IPCC.ScientificConcept),
    ("glossaryWorkingGroup", "glossary working group", "Working-group attribution recorded in the glossary source, not evidence that a figure uses the term.", IPCC.ScientificConcept, IPCC.WorkingGroup),
)

_DATA_PROPERTIES = (
    ("workingGroupCode", "working group code", "WG1, WG2 or WG3.", IPCC.WorkingGroup, XSD.string),
    ("sectionCode", "section code", "Original section identifier from the figure corpus.", IPCC.ReportSection, XSD.string),
    ("captionText", "caption text", "Optional original caption; publication may be subject to source reuse policy.", IPCC.Figure, XSD.string),
    ("captionStatus", "caption status", "Whether the official source supplied caption text: available or missing.", IPCC.Figure, XSD.string),
    ("captionSHA256", "caption SHA-256", "Digest of the exact original caption text when available.", IPCC.Figure, XSD.string),
    ("imageURL", "image URL", "Published candidate image URL, without reachability assertions.", IPCC.ImageReference, XSD.anyURI),
    ("imagePosition", "image position", "Zero-based order of this image reference in the original figure.", IPCC.ImageReference, XSD.nonNegativeInteger),
    ("matchedText", "matched text", "Exact substring of the original caption for this mention.", IPCC.CaptionMention, XSD.string),
    ("startOffset", "start offset", "Zero-based Python character offset, inclusive, in the original caption.", IPCC.CaptionMention, XSD.nonNegativeInteger),
    ("endOffset", "end offset", "Zero-based Python character offset, exclusive, in the original caption.", IPCC.CaptionMention, XSD.nonNegativeInteger),
    ("matchKind", "match kind", "Lexical match type: label, alias, or mixed.", IPCC.CaptionMention, XSD.string),
    ("resolution", "resolution", "Extractor resolution: lexical, explicit_expansion, chemical_formula, ambiguous, or excluded.", IPCC.CaptionMention, XSD.string),
    ("qualityRule", "quality rule", "Identifier of a recorded extraction or exclusion rule, if applied.", IPCC.CaptionMention, XSD.string),
)

_RESOLVED = ("lexical", "explicit_expansion", "chemical_formula")
_UNRESOLVED = ("ambiguous", "excluded")


def _prefixes(graph: Graph) -> Graph:
    for prefix, namespace in (
        ("ipcc", IPCC), ("resource", RESOURCE), ("rdf", RDF), ("rdfs", RDFS),
        ("owl", OWL), ("skos", SKOS), ("prov", PROV), ("dcterms", DCTERMS),
        ("sh", SH), ("xsd", XSD),
    ):
        graph.bind(prefix, namespace)
    return graph


def resource_iri(kind: str, identifier: str) -> URIRef:
    """Mint a deterministic, URL-safe resource IRI for a later graph builder.

    Use section identifiers that include their WG, e.g. ``WG1:chapter-1``;
    use figure IDs from the corpus and glossary term IDs without rewriting them.
    The function creates an identifier only; it does not assert graph triples.
    """
    permitted = {
        "report", "working-group", "section", "figure", "image-reference",
        "concept", "caption-mention",
    }
    if kind not in permitted:
        raise ValueError(f"Unknown resource kind: {kind!r}")
    if not isinstance(identifier, str) or not identifier.strip() or identifier != identifier.strip():
        raise ValueError("Resource identifier must be a nonempty, unpadded string")
    return URIRef(f"{RESOURCE}{kind}/{quote(identifier, safe='-._~')}")


def build_ontology_schema() -> Graph:
    """Return the reusable TBox (classes/properties), without instance data."""
    graph = _prefixes(Graph())
    graph.add((ONTOLOGY_IRI, RDF.type, OWL.Ontology))
    graph.add((ONTOLOGY_IRI, DCTERMS.title, Literal("IPCC AR6 Visual Knowledge Graph Ontology", lang="en")))
    graph.add((ONTOLOGY_IRI, DCTERMS.description, Literal(
        "Schema for source-traceable IPCC AR6 figures, glossary concepts, and caption evidence; "
        "scientific topic and causal relationships are intentionally not inferred.", lang="en")))
    graph.add((ONTOLOGY_IRI, OWL.versionInfo, Literal("0.1.0")))
    graph.add((ONTOLOGY_IRI, RDFS.seeAlso, URIRef("https://www.ipcc.ch/assessment-report/ar6/")))

    for name, label, description, parent in _CLASSES:
        subject = IPCC[name]
        graph.add((subject, RDF.type, OWL.Class))
        graph.add((subject, RDFS.label, Literal(label, lang="en")))
        graph.add((subject, RDFS.comment, Literal(description, lang="en")))
        if parent is not None:
            graph.add((subject, RDFS.subClassOf, parent))

    for name, label, description, domain, range_ in _OBJECT_PROPERTIES:
        subject = IPCC[name]
        graph.add((subject, RDF.type, OWL.ObjectProperty))
        graph.add((subject, RDFS.label, Literal(label, lang="en")))
        graph.add((subject, RDFS.comment, Literal(description, lang="en")))
        graph.add((subject, RDFS.domain, domain))
        graph.add((subject, RDFS.range, range_))

    graph.add((IPCC.hasCaptionMention, OWL.inverseOf, IPCC.mentionInFigure))

    for name, label, description, domain, datatype in _DATA_PROPERTIES:
        subject = IPCC[name]
        graph.add((subject, RDF.type, OWL.DatatypeProperty))
        graph.add((subject, RDFS.label, Literal(label, lang="en")))
        graph.add((subject, RDFS.comment, Literal(description, lang="en")))
        graph.add((subject, RDFS.domain, domain))
        graph.add((subject, RDFS.range, datatype))

    # Useful external SKOS vocabulary is reused, not duplicated or automatically
    # populated. In particular, skos:broader / skos:related require curated facts.
    return graph


def _stable_node(*parts: object) -> BNode:
    """Use stable blank-node IDs so regenerated Turtle avoids arbitrary churn."""
    seed = "|".join(str(part) for part in parts)
    return BNode("n" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24])


def _rdf_list(graph: Graph, values: tuple[URIRef | Literal | BNode, ...]) -> BNode:
    """Build a deterministic RDF collection (without rdflib's random IDs)."""
    if not values:
        raise ValueError("SHACL enumeration must not be empty")
    seed = "|".join(str(v) for v in values)
    cells = [_stable_node("list", seed, index) for index in range(len(values))]
    for index, value in enumerate(values):
        graph.add((cells[index], RDF.first, value))
        graph.add((cells[index], RDF.rest, cells[index + 1] if index + 1 < len(values) else RDF.nil))
    return cells[0]


def _shape_property(
    graph: Graph, shape: URIRef | BNode, path: URIRef, *,
    required: bool = False, single: bool = False,
    datatype: URIRef | None = None, value_class: URIRef | None = None,
    iri: bool = False, choices: tuple[str, ...] = (), pattern: str | None = None,
) -> None:
    prop = _stable_node("property", shape, path)
    graph.add((shape, SH.property, prop))
    graph.add((prop, SH.path, path))
    if required:
        graph.add((prop, SH.minCount, Literal(1)))
    if single:
        graph.add((prop, SH.maxCount, Literal(1)))
    if datatype is not None:
        graph.add((prop, SH.datatype, datatype))
    if value_class is not None:
        graph.add((prop, SH["class"], value_class))
        graph.add((prop, SH.nodeKind, SH.IRI))
    if iri:
        graph.add((prop, SH.nodeKind, SH.IRI))
    if choices:
        graph.add((prop, SH['in'], _rdf_list(graph, tuple(Literal(x) for x in choices))))
    if pattern is not None:
        graph.add((prop, SH.pattern, Literal(pattern)))


def build_validation_shapes() -> Graph:
    """Return SHACL Core constraints; missing captions and images stay valid.

    These shapes validate evidence structure, not scientific truth. Offset/end
    span consistency against a caption, HTTP reachability and relationship
    scientific accuracy need checks in later data/evaluation modules.
    """
    graph = _prefixes(Graph())
    definitions = (
        ("AssessmentReportShape", IPCC.AssessmentReport),
        ("WorkingGroupShape", IPCC.WorkingGroup),
        ("ReportSectionShape", IPCC.ReportSection),
        ("FigureShape", IPCC.Figure),
        ("ImageReferenceShape", IPCC.ImageReference),
        ("ScientificConceptShape", IPCC.ScientificConcept),
        ("CaptionMentionShape", IPCC.CaptionMention),
    )
    for name, klass in definitions:
        shape = IPCC[name]
        graph.add((shape, RDF.type, SH.NodeShape))
        graph.add((shape, SH.targetClass, klass))

    _shape_property(graph, IPCC.AssessmentReportShape, DCTERMS.identifier, required=True, single=True, datatype=XSD.string)
    _shape_property(graph, IPCC.WorkingGroupShape, IPCC.workingGroupCode, required=True, single=True, datatype=XSD.string, choices=("WG1", "WG2", "WG3"))
    _shape_property(graph, IPCC.WorkingGroupShape, IPCC.partOfReport, required=True, single=True, value_class=IPCC.AssessmentReport)
    _shape_property(graph, IPCC.ReportSectionShape, IPCC.sectionCode, required=True, single=True, datatype=XSD.string)
    _shape_property(graph, IPCC.ReportSectionShape, IPCC.sectionOfWorkingGroup, required=True, single=True, value_class=IPCC.WorkingGroup)

    fig = IPCC.FigureShape
    _shape_property(graph, fig, DCTERMS.identifier, required=True, single=True, datatype=XSD.string)
    _shape_property(graph, fig, DCTERMS.source, required=True, single=True, iri=True)
    _shape_property(graph, fig, IPCC.figureInSection, required=True, single=True, value_class=IPCC.ReportSection)
    _shape_property(graph, fig, IPCC.figureInWorkingGroup, required=True, single=True, value_class=IPCC.WorkingGroup)
    _shape_property(graph, fig, IPCC.captionStatus, required=True, single=True, datatype=XSD.string, choices=("available", "missing"))
    _shape_property(graph, fig, IPCC.captionText, single=True, datatype=XSD.string)
    _shape_property(graph, fig, IPCC.captionSHA256, single=True, datatype=XSD.string, pattern="^[0-9a-f]{64}$")
    _shape_property(graph, fig, IPCC.hasImageReference, value_class=IPCC.ImageReference)
    _shape_property(graph, fig, IPCC.hasCaptionMention, value_class=IPCC.CaptionMention)
    _shape_property(graph, fig, IPCC.mentionsConcept, value_class=IPCC.ScientificConcept)

    _shape_property(graph, IPCC.ImageReferenceShape, IPCC.imageURL, required=True, single=True, datatype=XSD.anyURI)
    _shape_property(graph, IPCC.ImageReferenceShape, IPCC.imagePosition, required=True, single=True, datatype=XSD.nonNegativeInteger)

    concept = IPCC.ScientificConceptShape
    _shape_property(graph, concept, DCTERMS.identifier, required=True, single=True, datatype=XSD.string)
    # Prefer English language tags but accept untagged original glossary labels;
    # both are legal RDF literals and multilingual labels can be added later.
    _shape_property(graph, concept, SKOS.prefLabel, required=True)
    _shape_property(graph, concept, IPCC.glossaryWorkingGroup, value_class=IPCC.WorkingGroup)

    mention = IPCC.CaptionMentionShape
    _shape_property(graph, mention, IPCC.mentionInFigure, required=True, single=True, value_class=IPCC.Figure)
    _shape_property(graph, mention, IPCC.matchedText, required=True, single=True, datatype=XSD.string)
    _shape_property(graph, mention, IPCC.startOffset, required=True, single=True, datatype=XSD.nonNegativeInteger)
    _shape_property(graph, mention, IPCC.endOffset, required=True, single=True, datatype=XSD.nonNegativeInteger)
    _shape_property(graph, mention, IPCC.matchKind, required=True, single=True, datatype=XSD.string, choices=("label", "alias", "mixed"))
    _shape_property(graph, mention, IPCC.resolution, required=True, single=True, datatype=XSD.string, choices=_RESOLVED + _UNRESOLVED)
    _shape_property(graph, mention, IPCC.qualityRule, single=True, datatype=XSD.string)
    _shape_property(graph, mention, IPCC.denotesConcept, single=True, value_class=IPCC.ScientificConcept)
    _shape_property(graph, mention, IPCC.candidateConcept, value_class=IPCC.ScientificConcept)

    # Structurally enforce that unresolved mentions do not produce an accepted
    # concept link; resolved mentions must identify precisely one concept.
    resolved = _stable_node("resolved-mention-branch")
    unresolved = _stable_node("unresolved-mention-branch")
    _shape_property(graph, resolved, IPCC.resolution, required=True, single=True, choices=_RESOLVED)
    _shape_property(graph, resolved, IPCC.denotesConcept, required=True, single=True, value_class=IPCC.ScientificConcept)
    _shape_property(graph, unresolved, IPCC.resolution, required=True, single=True, choices=_UNRESOLVED)
    denied = _stable_node("unresolved-mention-no-concept")
    graph.add((unresolved, SH.property, denied))
    graph.add((denied, SH.path, IPCC.denotesConcept))
    graph.add((denied, SH.maxCount, Literal(0)))
    graph.add((mention, SH['or'], _rdf_list(graph, (resolved, unresolved))))

    return graph


def write_schema(
    ontology_path: Path = DEFAULT_ONTOLOGY,
    shapes_path: Path = DEFAULT_SHAPES,
) -> tuple[Path, Path]:
    """Serialize independent, deterministic Turtle files; never export records."""
    ontology_path, shapes_path = Path(ontology_path), Path(shapes_path)
    if ontology_path.resolve() == shapes_path.resolve():
        raise ValueError("Ontology and shapes outputs must be different paths")
    for path in (ontology_path, shapes_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    build_ontology_schema().serialize(destination=ontology_path, format="turtle")
    build_validation_shapes().serialize(destination=shapes_path, format="turtle")
    return ontology_path, shapes_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the IPCC figure ontology and SHACL shapes")
    parser.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY)
    parser.add_argument("--shapes", type=Path, default=DEFAULT_SHAPES)
    args = parser.parse_args()
    ontology, shapes = write_schema(args.ontology, args.shapes)
    print(f"Ontology: {ontology}")
    print(f"SHACL: {shapes}")


if __name__ == "__main__":
    main()
