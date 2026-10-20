"""Assemble the IPCC AR6 visual knowledge graph from validated source data.

This is the Step 4 orchestrator. It REBUILDS schema, curated SKOS topic
collections and evidence-backed RDF instance relationships directly from the
current Step 2/3 sources, rather than merging potentially stale Turtle files.

Trust boundaries:
* The Step 3 concepts database must match both the Step 2 corpus and the exact
  glossary vocabulary JSON (SHA-256 checked).
* A figure--concept edge means its *caption mentions* a source-backed glossary
  term. It never implies that the image depicts that concept, nor establishes
  scientific causality, similarity, or a concept hierarchy.
* Ambiguous/excluded mentions are retained as audit evidence, never promoted
  to accepted concept links. Taxonomy categories are editorial SKOS Collections,
  not taxonomic ``is-a`` assertions.
* The RDF export omits complete captions and glossary definitions by default;
  matched text snippets remain in the graph for evidence review. Check IPCC
  reuse terms before publication.

Structural RDF integrity checks run on every build. For formal SHACL validation,
install ``pyshacl`` and use ``--validate-shacl``; without it, the report
explicitly records that SHACL was NOT run. There is no silent fallback.

Run from the repository root (or any directory)::

    python -m ontology.build_ontology
    python -m ontology.build_ontology --validate-shacl

Requires rdflib>=7,<8; pyshacl is optional for formal constraint checking.
Outputs are staged and verified before replacement. As with any two separate
files, publishing Turtle and JSON is not a single atomic transaction.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SH, SKOS

from concepts.vocabulary import load_vocabulary
from ontology.relationship import (
    DEFAULT_CONCEPTS, DEFAULT_CORPUS, WORKING_GROUPS,
    build_relationship_graph,
)
from ontology.schema import IPCC, build_ontology_schema, build_validation_shapes, resource_iri
from ontology.taxonomy import build_taxonomy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOCABULARY = PROJECT_ROOT / "data" / "processed" / "ipcc_ar6_vocabulary.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "ontology" / "ipcc_visual_knowledge_graph.ttl"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "ontology_quality.json"
BUILD_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class OntologyResult:
    """Prepared graph plus independent SHACL shapes and audit-ready report."""

    graph: Graph
    shapes: Graph
    report: dict[str, Any]


def _sha256(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Required input not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_hashes(corpus: Path, concepts: Path, vocabulary: Path) -> dict[str, str]:
    return {
        "corpus_sha256": _sha256(corpus),
        "concepts_sha256": _sha256(concepts),
        "vocabulary_sha256": _sha256(vocabulary),
    }


def _verify_concept_sources(concepts: Path, hashes: dict[str, str], vocabulary: Any) -> None:
    """Check the Step 3 DB was built from the *exact* input snapshots."""
    with closing(sqlite3.connect(concepts.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Concept SQLite integrity check failed")
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("Step 3 concepts.db must use schema version 2; rebuild Step 3.5")
        metadata = dict(db.execute("SELECT key, value FROM metadata"))
        # Trust the actual glossary identities, not a hash string in metadata alone.
        observed = dict(db.execute("SELECT term_id, canonical_label FROM concepts"))
        expected = {term.term_id: term.label for term in vocabulary.terms}
        if observed != expected:
            raise ValueError("Concept identities/labels disagree with the selected vocabulary")
    for key in ("corpus_sha256", "vocabulary_sha256"):
        if metadata.get(key) != hashes[key]:
            raise ValueError(
                f"Step 3 concepts.db {key} does not match the selected input; "
                "rebuild using python -m concepts.build_concepts"
            )


def _verify_graph(graph: Graph, expected: dict[str, int], taxonomy_members: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Cross-check graph edges against evidence, figure provenance and counts.

    These assertions complement, but do NOT replace, a SHACL processor.
    """
    figures = set(graph.subjects(RDF.type, IPCC.Figure))
    concepts = set(graph.subjects(RDF.type, IPCC.ScientificConcept))
    mentions = set(graph.subjects(RDF.type, IPCC.CaptionMention))
    categories = set(graph.subjects(RDF.type, SKOS.Collection))

    for name, observed, required in (
        ("figures", len(figures), expected["figures"]),
        ("concepts", len(concepts), expected["concepts"]),
        ("mentions", len(mentions), expected.get("total_mentions", 0)),
        ("topic collections", len(categories), len(taxonomy_members)),
    ):
        if observed != required:
            raise ValueError(f"RDF {name} count mismatch: {observed} != {required}")

    if set(graph.triples((None, SKOS.broader, None))) or set(graph.triples((None, SKOS.related, None))):
        raise ValueError("Unreviewed scientific hierarchy/relatedness assertions were introduced")

    working_groups = {wg: resource_iri("working-group", wg) for wg in WORKING_GROUPS}
    fig_to_wg: dict[URIRef, str] = {}
    wg_figures: Counter[str] = Counter()
    for fig in figures:
        groups = set(graph.objects(fig, IPCC.figureInWorkingGroup))
        sections = set(graph.objects(fig, IPCC.figureInSection))
        if len(groups) != 1 or len(sections) != 1:
            raise ValueError(f"Figure must have exactly one working group and section: {fig}")
        group = next(iter(groups))
        wg = next((code for code, node in working_groups.items() if group == node), None)
        section = next(iter(sections))
        if wg is None or (section, IPCC.sectionOfWorkingGroup, group) not in graph:
            raise ValueError(f"Figure/section/working-group mismatch: {fig}")
        fig_to_wg[fig] = wg
        wg_figures[wg] += 1

    allowed_pairs: set[tuple[URIRef, URIRef]] = set()
    accepted_mentions = 0
    unresolved_mentions = 0
    for mention in mentions:
        linked_figures = set(graph.objects(mention, IPCC.mentionInFigure))
        if len(linked_figures) != 1 or not linked_figures <= figures:
            raise ValueError(f"Caption mention has no unique known figure: {mention}")
        fig = next(iter(linked_figures))
        if (fig, IPCC.hasCaptionMention, mention) not in graph:
            raise ValueError(f"Missing inverse caption mention edge: {mention}")
        resolution = {str(v) for v in graph.objects(mention, IPCC.resolution)}
        selected = set(graph.objects(mention, IPCC.denotesConcept))
        candidates = set(graph.objects(mention, IPCC.candidateConcept))
        if not candidates or not candidates <= concepts or len(resolution) != 1:
            raise ValueError(f"Invalid mention candidates/resolution: {mention}")
        if resolution <= {"lexical", "explicit_expansion", "chemical_formula"}:
            if len(selected) != 1 or not selected <= candidates:
                raise ValueError(f"Resolved mention missing selected candidate: {mention}")
            allowed_pairs.add((fig, next(iter(selected))))
            accepted_mentions += 1
        elif resolution <= {"ambiguous", "excluded"}:
            if selected:
                raise ValueError(f"Unresolved mention has selected concept: {mention}")
            unresolved_mentions += 1
        else:
            raise ValueError(f"Unknown mention resolution: {mention}")

    actual_pairs = set(graph.subject_objects(IPCC.mentionsConcept))
    if actual_pairs != allowed_pairs or len(actual_pairs) != expected["figure_concept_links"]:
        raise ValueError("Figure-concept links do not match resolved caption evidence")
    if accepted_mentions != expected.get("resolved_mentions", 0):
        raise ValueError("Resolved mention count disagrees with source database")
    if unresolved_mentions != expected.get("unresolved_mentions", 0):
        raise ValueError("Unresolved mention count disagrees with source database")

    for category, concept in graph.subject_objects(SKOS.member):
        if category not in categories or concept not in concepts:
            raise ValueError("Taxonomy membership references an unknown category or glossary concept")

    membership_count = sum(len(ids) for ids in taxonomy_members.values())
    if len(set(graph.subject_objects(SKOS.member))) != membership_count:
        raise ValueError("Category memberships disagree with curated taxonomy")

    by_wg: dict[str, dict[str, int]] = {}
    for wg in WORKING_GROUPS:
        wg_nodes = {fig for fig in figures if fig_to_wg[fig] == wg}
        by_wg[wg] = {
            "figures": wg_figures[wg],
            "figure_concept_links": sum(fig in wg_nodes for fig, _ in allowed_pairs),
            "distinct_linked_concepts": len({concept for fig, concept in allowed_pairs if fig in wg_nodes}),
        }
    return {"by_working_group": by_wg}


def _validate_shacl(graph: Graph, shapes: Graph) -> None:
    """Run real SHACL Core validation when explicitly requested; fail closed."""
    try:
        from pyshacl import validate  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            'Formal SHACL validation needs pyshacl. Install: pip install "pyshacl>=0.30,<1"'
        ) from exc

    conforms, _, description = validate(
        data_graph=graph,
        shacl_graph=shapes,
        inference="rdfs",  # Include subclass semantics for SHACL class targets.
        advanced=False,
        abort_on_first=False,
        meta_shacl=False,
    )
    if not conforms:
        raise ValueError(f"RDF graph failed SHACL validation: {str(description)[:2000]}")


def build_knowledge_graph(
    corpus_path: Path = DEFAULT_CORPUS,
    concepts_path: Path = DEFAULT_CONCEPTS,
    vocabulary_path: Path = DEFAULT_VOCABULARY,
    *,
    include_captions: bool = False,
    validate_shacl: bool = False,
) -> OntologyResult:
    """Return a fresh, cross-checked union graph, shapes, and quality report."""
    corpus_path, concepts_path, vocabulary_path = map(
        Path, (corpus_path, concepts_path, vocabulary_path)
    )
    source_paths = (corpus_path, concepts_path, vocabulary_path)
    if len({p.resolve() for p in source_paths}) != 3:
        raise ValueError("Corpus, concepts DB and vocabulary JSON must be distinct files")
    hashes = _source_hashes(*source_paths)
    vocabulary = load_vocabulary(vocabulary_path)
    _verify_concept_sources(concepts_path, hashes, vocabulary)
    taxonomy = build_taxonomy(vocabulary)
    relationship = build_relationship_graph(
        corpus_path, concepts_path, include_captions=include_captions
    )
    if relationship.summary["concepts"] != len(vocabulary.terms):
        raise ValueError("Concept DB and vocabulary disagree on glossary term count")

    schema = build_ontology_schema()
    shapes = build_validation_shapes()
    graph = Graph()
    for component in (schema, taxonomy.graph, relationship.graph):
        graph += component
    for prefix, uri in (
        ("ipcc", "https://neelkumar01.github.io/IPCC-visual-knowledge-graph/ontology#"),
        ("skos", str(SKOS)),
    ):
        graph.bind(prefix, uri, replace=True)

    integrity = _verify_graph(graph, relationship.summary, dict(taxonomy.members_by_category))
    if validate_shacl:
        _validate_shacl(graph, shapes)
    if _source_hashes(*source_paths) != hashes:
        raise ValueError("An input was modified during ontology construction; rebuild from stable sources")

    report = {
        "report_type": "ontology_build_quality",
        "build_version": BUILD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {**hashes, "corpus_path": str(corpus_path),
                   "concepts_path": str(concepts_path), "vocabulary_path": str(vocabulary_path)},
        "summary": {
            **relationship.summary,
            "rdf_triples": len(graph),
            "schema_triples": len(schema),
            "taxonomy_triples": len(taxonomy.graph),
            "relationship_triples": len(relationship.graph),
            "topic_categories": taxonomy.summary["categories"],
            "categorized_concepts": taxonomy.summary["assigned_concepts"],
            "uncategorized_concepts": taxonomy.summary["unassigned_concepts"],
            "category_memberships": taxonomy.summary["category_memberships"],
        },
        **integrity,
        "validation": {
            "rdf_integrity": "passed",
            "shacl": "passed" if validate_shacl else "not_run",
            "shacl_shapes": len(set(shapes.subjects(RDF.type, SH.NodeShape))),
        },
        "notes": [
            "Caption mentions and project-curated navigation facets do not establish scientific causation or visual depiction.",
            "Ambiguous/excluded mentions retain candidate evidence but do not generate accepted figure-concept links.",
            "Full captions and glossary definitions are omitted unless explicitly opted in; matched text snippets remain.",
            "Scientific extraction accuracy needs human review; RDF structural correctness does not imply scientific validity.",
            "Published IPCC material and glossary-derived content may have reuse restrictions.",
        ],
    }
    return OntologyResult(graph=graph, shapes=shapes, report=report)


def write_ontology(
    corpus_path: Path = DEFAULT_CORPUS,
    concepts_path: Path = DEFAULT_CONCEPTS,
    vocabulary_path: Path = DEFAULT_VOCABULARY,
    output_path: Path = DEFAULT_OUTPUT,
    report_path: Path = DEFAULT_REPORT,
    *,
    include_captions: bool = False,
    validate_shacl: bool = False,
) -> dict[str, Any]:
    """Stage, round-trip check and publish final RDF + report safely.

    A failed build or parse leaves previously published files intact. Because
    output files are replaced separately, this is not a two-file transaction.
    """
    corpus_path, concepts_path, vocabulary_path, output_path, report_path = map(
        Path, (corpus_path, concepts_path, vocabulary_path, output_path, report_path)
    )
    paths = (corpus_path, concepts_path, vocabulary_path, output_path, report_path)
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError("All input and output paths must be different")
    result = build_knowledge_graph(
        corpus_path, concepts_path, vocabulary_path,
        include_captions=include_captions, validate_shacl=validate_shacl,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ontology-build-", dir=output_path.parent) as graph_tmp:
        with tempfile.TemporaryDirectory(prefix=".ontology-report-", dir=report_path.parent) as report_tmp:
            pending_graph = Path(graph_tmp) / "knowledge_graph.ttl"
            pending_report = Path(report_tmp) / "ontology_quality.json"
            result.graph.serialize(destination=pending_graph, format="turtle", encoding="utf-8")
            reloaded = Graph().parse(pending_graph, format="turtle")
            if len(reloaded) != len(result.graph) or set(reloaded) != set(result.graph):
                raise RuntimeError("Serialized ontology graph failed exact RDF round-trip")
            pending_report.write_text(
                json.dumps(result.report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            # Verify the inputs have not changed since the completed checks.
            if _source_hashes(corpus_path, concepts_path, vocabulary_path) != {
                key: result.report["inputs"][key] for key in
                ("corpus_sha256", "concepts_sha256", "vocabulary_sha256")
            }:
                raise ValueError("An input changed before final publication")
            os.replace(pending_graph, output_path)
            os.replace(pending_report, report_path)
    return result.report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the source-backed IPCC AR6 ontology graph and quality report."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--concepts", type=Path, default=DEFAULT_CONCEPTS)
    parser.add_argument("--vocabulary", type=Path, default=DEFAULT_VOCABULARY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--include-captions", action="store_true",
        help="Include full IPCC captions in RDF (check IPCC reuse permissions first).",
    )
    parser.add_argument(
        "--validate-shacl", action="store_true",
        help="Run formal SHACL validation using installed pyshacl (fails if unavailable).",
    )
    args = parser.parse_args()
    report = write_ontology(
        args.corpus, args.concepts, args.vocabulary, args.output, args.report,
        include_captions=args.include_captions, validate_shacl=args.validate_shacl,
    )
    summary = report["summary"]
    print(
        f"Knowledge graph: {summary['figures']} figures; "
        f"{summary['concepts']} glossary concepts; "
        f"{summary['figure_concept_links']} caption-supported links; "
        f"{summary['topic_categories']} curated topic collections; "
        f"{summary['rdf_triples']} RDF triples."
    )
    print(f"Integrity: {report['validation']['rdf_integrity']}; "
          f"SHACL: {report['validation']['shacl']}")
    print(f"Turtle: {args.output}")
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
