"""Standalone, ontology-aware IPCC figure knowledge-graph explorer.

The network is a presentation of *existing* RDF triples, not a source of new
scientific claims. Figure--concept links mean caption mentions, and ambiguous
or excluded candidate terms remain separate evidence. Full source captions and
glossary definitions are intentionally not embedded in exported HTML.

The renderer uses native SVG and a small, embedded force layout: no browser
library, CDN, server, or additional Python dependency is required. Published
IPCC images are referenced by URL, never downloaded or redistributed here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, SKOS

from ontology.schema import IPCC, resource_iri

if TYPE_CHECKING:
    from .subgraph import KnowledgeSubgraph


# The view contains RDF schema descriptions as well as instance triples.
# Only the following source-backed INSTANCE predicates are rendered as edges.
# Keep this set aligned with subgraph._RELATIONS; RDF/OWL schema triples are
# displayed as metadata, not misrepresented as scientific relationships.
_RELATIONS: dict[URIRef, tuple[str, str]] = {
    IPCC.mentionsConcept: ("Caption mentions", "caption"),
    IPCC.figureInSection: ("Published in section", "report"),
    IPCC.figureInWorkingGroup: ("Published by working group", "report"),
    IPCC.sectionOfWorkingGroup: ("Section of working group", "report"),
    IPCC.partOfReport: ("Part of report", "report"),
    IPCC.hasImageReference: ("Has image reference", "image"),
    IPCC.hasCaptionMention: ("Has caption evidence", "evidence"),
    IPCC.mentionInFigure: ("Mention appears in figure", "evidence"),
    IPCC.denotesConcept: ("Accepted concept interpretation", "evidence"),
    IPCC.candidateConcept: ("Candidate interpretation", "candidate"),
    IPCC.glossaryWorkingGroup: ("Glossary attribution", "glossary"),
    SKOS.member: ("Collection member", "topic"),
    PROV.wasDerivedFrom: ("Derived from", "evidence"),
}

# Classes are ordered from most specific to least specific. A Chapter is also
# a ReportSection, and a ScientificConcept is also skos:Concept.
_CLASSES: tuple[tuple[URIRef, str], ...] = (
    (IPCC.Chapter, "section"),
    (IPCC.AssessmentReport, "report"),
    (IPCC.WorkingGroup, "working_group"),
    (IPCC.ReportSection, "section"),
    (IPCC.Figure, "figure"),
    (IPCC.ImageReference, "image"),
    (IPCC.ScientificConcept, "concept"),
    (IPCC.CaptionMention, "mention"),
    (SKOS.Collection, "topic"),
)

_MAX_ATTRIBUTES = 48
_MAX_ATTRIBUTE_CHARS = 1200


def _public_url(value: object) -> str | None:
    """Accept navigable public URLs only; never accept active-content schemes."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return value


def _text(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else None


def _short_label(value: URIRef) -> str:
    fragment = str(value).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return unquote(fragment).replace("_", " ")


def _label(graph: Graph, uri: URIRef, kind: str, figures: dict[str, Any]) -> str:
    if kind == "figure":
        hit = figures.get(str(uri))
        if hit is not None:
            return hit.label or hit.title or hit.figure_id
        return _text(graph, uri, DCTERMS.identifier) or _short_label(uri)
    if kind == "concept":
        return _text(graph, uri, SKOS.prefLabel) or _short_label(uri)
    if kind == "mention":
        span = _text(graph, uri, IPCC.matchedText) or "Caption mention"
        status = _text(graph, uri, IPCC.resolution) or ""
        return f"{span} · {status}" if status in {"excluded", "ambiguous"} else span
    if kind == "image":
        index = _text(graph, uri, IPCC.imagePosition)
        return f"Image {int(index) + 1}" if index is not None else "Image reference"
    if kind == "section":
        code = _text(graph, uri, IPCC.sectionCode)
        if code:
            return ("Chapter " + code[8:]) if code.startswith("chapter-") else code.replace("-", " ").title()
    if kind == "working_group":
        return _text(graph, uri, IPCC.workingGroupCode) or "Working group"
    if kind == "report":
        return _text(graph, uri, DCTERMS.identifier) or "Assessment report"
    if kind == "topic":
        return _text(graph, uri, RDFS.label) or "Curated topic"
    return _short_label(uri)


def _safe_attributes(graph: Graph, uri: URIRef, known: set[str]) -> list[dict[str, str]]:
    """Include published literal/provenance metadata, not entire captions."""
    attributes: list[dict[str, str]] = []
    for predicate, value in sorted(graph.predicate_objects(uri), key=lambda item: (str(item[0]), str(item[1]))):
        if predicate == IPCC.captionText:
            continue  # Do not silently republish original full IPCC captions.
        if (predicate in _RELATIONS and isinstance(value, URIRef)
                and str(value) in known):
            continue  # Represent this exact triple as an inspectable graph edge.
        if predicate == RDF.type:
            # Type IRIs are shown under the inspector's ontology metadata.
            continue
        text_value = str(value)
        if len(text_value) > _MAX_ATTRIBUTE_CHARS:
            text_value = text_value[:_MAX_ATTRIBUTE_CHARS] + "…"
        attributes.append({
            "predicate": str(predicate),
            "label": _short_label(predicate),
            "value": text_value,
            "url": _public_url(text_value) if isinstance(value, URIRef) else None,
        })
    return attributes[:_MAX_ATTRIBUTES]


def _visualization_data(view: KnowledgeSubgraph) -> dict[str, Any]:
    """Prepare a faithful network of *published* typed instance resources.

    Every edge in the returned data exists in ``view.graph``. Search-result
    ranking determines which nodes appear initially; evidence and structural
    resources remain locally searchable and expandable in the same network.
    """
    rdf = view.graph
    hit_by_uri = {
        str(resource_iri("figure", item.figure_id)): item for item in view.figures
    }
    node_kinds: dict[str, str] = {}
    for rdf_class, kind in _CLASSES:
        for node in rdf.subjects(RDF.type, rdf_class):
            if isinstance(node, URIRef):
                node_kinds.setdefault(str(node), kind)
    known = set(node_kinds)

    nodes: list[dict[str, Any]] = []
    for uri_str in sorted(known):
        uri = URIRef(uri_str)
        kind = node_kinds[uri_str]
        rdf_types = sorted(str(typ) for typ in rdf.objects(uri, RDF.type) if isinstance(typ, URIRef))
        klass = next((c for c, k in _CLASSES if k == kind and c in rdf.objects(uri, RDF.type)), None)
        # Several ontology classes may map to the same presentation kind.
        if kind == "section" and (uri, RDF.type, IPCC.Chapter) in rdf:
            klass = IPCC.Chapter
        node = {
            "id": uri_str,
            "kind": kind,
            "label": _label(rdf, uri, kind, hit_by_uri),
            "types": rdf_types,
            "typeDescription": _text(rdf, klass, RDFS.comment) if klass is not None else None,
            "attributes": _safe_attributes(rdf, uri, known),
        }
        if kind == "figure":
            hit = hit_by_uri.get(uri_str)
            node["figure"] = {
                "id": _text(rdf, uri, DCTERMS.identifier) or (hit.figure_id if hit else None),
                "title": hit.title if hit is not None else None,
                "wg": hit.working_group if hit is not None else None,
                "section": hit.section if hit is not None else None,
                "source": _public_url(_text(rdf, uri, DCTERMS.source)),
                "captionStatus": _text(rdf, uri, IPCC.captionStatus),
                "matches": ([{"text": span.text, "start": span.start_offset,
                             "end": span.end_offset} for span in hit.matches]
                            if hit is not None else []),
                "images": [],
                "mentions": [],
            }
        elif kind == "concept":
            node["concept"] = {
                "id": _text(rdf, uri, DCTERMS.identifier),
                "aliases": sorted({str(x) for x in rdf.objects(uri, SKOS.altLabel)},
                                  key=str.casefold),
                "source": _public_url(_text(rdf, uri, DCTERMS.source)),
            }
        elif kind == "mention":
            node["mention"] = {
                "text": _text(rdf, uri, IPCC.matchedText),
                "start": _text(rdf, uri, IPCC.startOffset),
                "end": _text(rdf, uri, IPCC.endOffset),
                "resolution": _text(rdf, uri, IPCC.resolution),
                "matchKind": _text(rdf, uri, IPCC.matchKind),
                "qualityRule": _text(rdf, uri, IPCC.qualityRule),
                "selected": next((str(c) for c in rdf.objects(uri, IPCC.denotesConcept)), None),
                "candidates": sorted(str(c) for c in rdf.objects(uri, IPCC.candidateConcept)),
            }
        elif kind == "image":
            node["image"] = {
                "url": _public_url(_text(rdf, uri, IPCC.imageURL)),
                "position": _text(rdf, uri, IPCC.imagePosition),
            }
        elif kind == "topic":
            node["topic"] = {
                "description": _text(rdf, uri, DCTERMS.description),
                "references": sorted(filter(None, (
                    _public_url(str(x)) for x in rdf.objects(uri, DCTERMS.references)
                ))),
            }
        nodes.append(node)

    node_lookup = {n["id"]: n for n in nodes}
    edges: list[dict[str, Any]] = []
    for predicate, (caption, family) in _RELATIONS.items():
        for subject, obj in rdf.subject_objects(predicate):
            if not isinstance(subject, URIRef) or not isinstance(obj, URIRef):
                continue
            a, b = str(subject), str(obj)
            if a not in known or b not in known:
                continue
            edges.append({
                "id": f"{a}|{predicate}|{b}",
                "from": a,
                "to": b,
                "predicate": str(predicate),
                "label": _text(rdf, predicate, RDFS.label) or caption,
                "description": _text(rdf, predicate, RDFS.comment),
                "domain": sorted(str(x) for x in rdf.objects(predicate, RDFS.domain)),
                "range": sorted(str(x) for x in rdf.objects(predicate, RDFS.range)),
                "family": family,
            })
    edges.sort(key=lambda e: (e["family"], e["predicate"], e["from"], e["to"]))

    # Bind image references and *all* recorded caption evidence to the figure
    # inspector, even when these evidence resources start visually collapsed.
    image_refs: dict[str, list[tuple[int, str]]] = defaultdict(list)
    caption_mentions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    accepted_count: Counter[str] = Counter()
    for edge in edges:
        a, b = edge["from"], edge["to"]
        predicate = edge["predicate"]
        if predicate == str(IPCC.hasImageReference):
            image = node_lookup[b].get("image", {})
            url = image.get("url")
            if url:
                image_refs[a].append((int(image.get("position") or 0), url))
        elif predicate == str(IPCC.hasCaptionMention):
            mention = node_lookup[b].get("mention")
            if mention:
                caption_mentions[a].append({"id": b, **mention})
        elif predicate == str(IPCC.mentionsConcept):
            accepted_count[b] += 1

    for node in nodes:
        if node["kind"] == "figure":
            detail = node["figure"]
            detail["images"] = [url for _, url in sorted(image_refs.get(node["id"], []))]
            detail["mentions"] = sorted(
                caption_mentions.get(node["id"], []),
                key=lambda x: (int(x["start"] or 0), int(x["end"] or 0), x["id"]),
            )
        elif node["kind"] == "concept":
            node["concept"]["figuresInView"] = accepted_count[node["id"]]

    initial_ids = [
        str(resource_iri("concept", c.term_id)) for c in view.concepts
    ] + [
        str(resource_iri("figure", f.figure_id)) for f in view.figures
    ]
    visible = list(dict.fromkeys(node for node in initial_ids if node in known))
    seed = str(resource_iri("concept", view.concepts[0].term_id)) if view.match == "concept" and view.concepts else (
        visible[0] if visible else None
    )
    return {
        "query": view.query,
        "match": view.match,
        "totalFigures": view.total_figures,
        "selectedFigures": len(view.figures),
        "initial": visible,
        "seed": seed,
        "nodes": nodes,
        "edges": edges,
        "rdfTriples": len(rdf),
        "semantics": "A figure–concept link means a resolved caption mention, not visual depiction or scientific causality.",
    }


_HTML_DOCUMENT = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>IPCC Knowledge Graph — __IPCC_TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400..800&display=swap" rel="stylesheet">
<style>
/* ---------- Design tokens: clean white theme ---------- */
:root {
 color-scheme:light;
 font-family:'Nunito',ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 font-variant-numeric:tabular-nums;
 --ink:#1a2530; --ink-2:#4a5866; --muted:#7a8794;
 --surface:#ffffff; --raised:#f7f9fb; --canvas:#ffffff;
 --line:#e3e8ed; --line-soft:#eef2f5;
 --accent:#2563a8; --accent-soft:#eef4fb;
 --ok:#1f8a6e; --ok-soft:#eef8f4; --warn:#b26b12; --warn-soft:#fdf6ec;
 --edge:#b4bfca; --edge-caption:#5fb09a; --edge-candidate:#d9a650; --edge-evidence:#a493cf;
 --edge-report:#b4bfca; --edge-image:#6fb8bd; --edge-glossary:#b39ad6; --edge-topic:#cfa27f;
 --label:#1f2b36;
 --t-concept:#effaf6; --t-figure:#f0f6fd; --t-mention:#f6f3fb; --t-image:#eef9f9;
 --t-section:#fdf7ee; --t-wg:#f6f2fb; --t-report:#f4f5f7; --t-topic:#fbf4ef;
 --shadow:0 1px 2px rgba(26,37,48,.04),0 4px 14px rgba(26,37,48,.06);
 --font-mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}

/* ---------- Base & page frame ---------- */
* {box-sizing:border-box}
html,body {margin:0;width:100%;height:100%;color:var(--ink);background:var(--surface)}
body {display:grid;grid-template-rows:auto minmax(0,1fr) auto;height:100vh;height:100dvh;
 font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
button,input,select {font:inherit;color:inherit}
:focus-visible {outline:2px solid var(--accent);outline-offset:2px}
button {cursor:pointer;border:1px solid var(--line);background:var(--surface);
 border-radius:8px;padding:7px 11px;font-size:12.5px;font-weight:550;line-height:1.3;
 transition:background-color .12s,border-color .12s,color .12s}
button:hover {background:var(--raised);border-color:#c9d2db}
button:disabled {opacity:.45;cursor:not-allowed}
.muted {color:var(--muted)}

/* ---------- Header ---------- */
header {position:relative;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
 padding:14px 24px;background:var(--surface);border-bottom:1px solid var(--line)}
.identity {min-width:200px;margin-right:auto}
.identity strong {display:block;font-size:17px;font-weight:800;letter-spacing:-.01em;line-height:1.2}
.identity div {color:var(--muted);font-size:12.5px;margin-top:3px}
.searchwrap {position:relative;flex:1;min-width:180px;max-width:380px}
#search {width:100%;height:38px;border:1px solid var(--line);border-radius:10px;
 background:var(--surface);padding:8px 12px 8px 36px;font-size:13.5px;
 transition:border-color .12s,background-color .12s,box-shadow .12s}
#search::placeholder {color:var(--muted);opacity:1}
#search:hover {border-color:#c9d2db}
#search:focus {outline:none;background:var(--surface);border-color:var(--accent);
 box-shadow:0 0 0 3px var(--accent-soft)}
.searchwrap .glass {position:absolute;top:50%;left:13px;transform:translateY(-50%);
 display:flex;color:var(--muted);pointer-events:none}
#matches {display:none;position:absolute;z-index:12;top:44px;left:0;right:0;background:var(--surface);
 border:1px solid var(--line);box-shadow:var(--shadow);border-radius:10px;max-height:260px;overflow:auto;padding:4px}
#matches button {width:100%;border:0;border-radius:7px;text-align:left;padding:8px 10px;display:block;
 background:transparent;font-size:13px}
#matches button:hover,#matches button:focus-visible {background:var(--accent-soft)}
#matches small {display:block;color:var(--muted);font-weight:400;font-size:11.5px;margin-top:1px}
#matches small::first-letter {text-transform:uppercase}
.filters {display:flex;gap:8px;align-items:center}
.filters label {font-size:12.5px;color:var(--muted)!important}
#relation-filter {height:38px;max-width:190px;border:1px solid var(--line);border-radius:10px;
 background:var(--surface);padding:0 10px;font-size:13px;cursor:pointer}
#relation-filter:hover {border-color:#c9d2db}

/* ---------- Main layout ---------- */
.app {display:grid;grid-template-columns:minmax(0,1fr) 360px;min-height:0;height:auto}

.canvas-wrap {position:relative;min-width:0;min-height:0;overflow:hidden;background:var(--canvas)}
#network {width:100%;height:100%;display:block;touch-action:none;user-select:none;cursor:grab}
#network.panning {cursor:grabbing}
#network:focus-visible {outline-offset:-3px}


/* Edges: each relationship type has its own colour and matching arrowhead */
.graph-edge {stroke:var(--edge);stroke-width:1.3;fill:none;stroke-linecap:round;
 pointer-events:stroke;cursor:pointer;transition:opacity .15s,stroke-width .12s}
.graph-edge[data-family="caption"] {stroke:var(--edge-caption);stroke-width:1.7;marker-end:url(#arrow-caption)}
.graph-edge[data-family="report"] {stroke:var(--edge-report)}
.graph-edge[data-family="image"] {stroke:var(--edge-image);marker-end:url(#arrow-image)}
.graph-edge[data-family="evidence"] {stroke:var(--edge-evidence);stroke-dasharray:2 4;marker-end:url(#arrow-evidence)}
.graph-edge[data-family="candidate"] {stroke:var(--edge-candidate);stroke-dasharray:5 4;marker-end:url(#arrow-candidate)}
.graph-edge[data-family="glossary"] {stroke:var(--edge-glossary);marker-end:url(#arrow-glossary)}
.graph-edge[data-family="topic"] {stroke:var(--edge-topic);marker-end:url(#arrow-topic)}
.graph-edge:hover {stroke:var(--accent);stroke-width:2.6;marker-end:url(#arrow-active)}
.graph-edge:focus-visible {outline:none;stroke:var(--accent);stroke-width:2.8;marker-end:url(#arrow-active)}
.graph-edge.selected {stroke:var(--accent);stroke-width:3;stroke-dasharray:none;marker-end:url(#arrow-active)}

/* Nodes: opaque tinted discs with the kind colour as ring (ring colour comes from the
   existing script, so kinds stay consistent with the inspector). Context nodes are lighter;
   caption evidence uses a dashed ring. */
.graph-node {cursor:grab;outline:none;transition:opacity .15s}
.graph-node.dragging {cursor:grabbing}
.graph-node circle {fill:var(--surface);fill-opacity:1;stroke-width:1.6;
 transition:stroke-width .12s,filter .12s}
.graph-node[aria-label^="concept:"] circle {fill:var(--t-concept);stroke-width:2}
.graph-node[aria-label^="figure:"] circle {fill:var(--t-figure);stroke-width:2}
.graph-node[aria-label^="mention:"] circle {fill:var(--t-mention);stroke-dasharray:4 3}
.graph-node[aria-label^="image:"] circle {fill:var(--t-image)}
.graph-node[aria-label^="section:"] circle {fill:var(--t-section)}
.graph-node[aria-label^="working group:"] circle {fill:var(--t-wg)}
.graph-node[aria-label^="report:"] circle {fill:var(--t-report)}
.graph-node[aria-label^="topic:"] circle {fill:var(--t-topic)}
.graph-node:hover circle {stroke-width:3;filter:drop-shadow(0 2px 8px rgba(26,37,48,.12))}
.graph-node.selected circle {stroke-width:3.5;filter:drop-shadow(0 4px 14px rgba(26,37,48,.18))}
.graph-node:focus-visible circle {stroke-width:4.5;stroke-dasharray:none}
.graph-edge.dimmed,.graph-node.dimmed {opacity:.18}

/* Slightly condensed labels so they fit inside their disc */
.graph-node text {font-family:inherit;fill:var(--label);font-size:10.3px;font-weight:750;letter-spacing:-.01em;
 text-anchor:middle;pointer-events:none;stroke:none;letter-spacing:0}
.graph-node.secondary text {font-size:9.6px;font-weight:650;fill:var(--ink-2)}
.graph-node.selected text {font-weight:800}

/* Canvas overlays */
.metrics {position:absolute;left:16px;top:14px;color:var(--ink-2);font-size:12px;
 background:var(--surface);padding:6px 11px;border:1px solid var(--line);border-radius:999px;
 box-shadow:var(--shadow)}
.canvas-tools {position:absolute;right:16px;bottom:16px;display:flex;flex-direction:column;
 background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.canvas-tools button {width:40px;height:40px;padding:0;display:grid;place-items:center;
 border:0;border-radius:0;background:transparent;color:var(--ink-2)}
.canvas-tools button + button {border-top:1px solid var(--line-soft)}
.canvas-tools button:hover {background:var(--accent-soft);color:var(--accent)}
.canvas-tools button:active {background:var(--accent);color:#fff}
.canvas-tools button:focus-visible {outline-offset:-3px}
#empty {display:none;position:absolute;top:45%;left:50%;transform:translate(-50%,-50%);
 text-align:center;color:var(--ink-2);width:min(320px,90%);line-height:1.55;
 background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;box-shadow:var(--shadow)}

/* ---------- Inspector panel ---------- */
#panel {overflow:auto;background:var(--surface);border-left:1px solid var(--line);
 padding:24px 24px 40px;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#panel h2 {font-size:20px;font-weight:800;letter-spacing:-.015em;line-height:1.25;margin:0 0 8px;overflow-wrap:anywhere}
#panel h3 {font-size:13px;font-weight:800;color:var(--ink);letter-spacing:0;text-transform:none;
 margin:24px 0 10px;padding-top:16px;border-top:1px solid var(--line-soft)}
#panel p {font-size:13.5px;line-height:1.6;overflow-wrap:anywhere;margin:8px 0;color:var(--ink-2);max-width:60ch}
#panel a {display:inline-block;color:var(--accent);font-weight:600;font-size:13px;
 text-decoration:none;overflow-wrap:anywhere;margin:6px 0}
#panel a:hover {text-decoration:underline;text-underline-offset:3px}
.kicker {position:relative;display:block;padding-left:16px;font-size:12.5px;font-weight:800;
 letter-spacing:0;text-transform:none;margin-bottom:6px}
.kicker::first-letter {text-transform:uppercase}
.kicker::before {content:"";position:absolute;left:0;top:50%;width:9px;height:9px;margin-top:-4.5px;
 border-radius:50%;background:currentColor}
.stat {display:inline-block;font-size:12.5px;color:var(--ink-2);background:var(--raised);
 border:1px solid var(--line);border-radius:7px;padding:4px 9px;margin:6px 0}
.chip {display:inline-block;padding:3px 9px;border-radius:999px;background:var(--raised);
 border:1px solid var(--line-soft);color:var(--ink-2);font-size:12px;margin:3px 5px 3px 0}
.hero {display:block;width:100%;height:auto;border-radius:12px;background:#fff;
 border:1px solid var(--line);padding:10px;margin:4px 0 6px}
.pill-button {font-weight:550;margin:3px 5px 3px 0;padding:5px 11px;font-size:12.5px;
 border-radius:999px;background:var(--surface)}
.pill-button:hover {background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
.evidence {border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:10px;
 background:var(--surface);padding:10px 12px;margin:0 0 8px;font-size:12.5px;line-height:1.5;overflow-wrap:anywhere}
.evidence.warn {border-left-color:var(--warn);background:var(--warn-soft)}
.evidence strong {display:block;font-size:14px;font-weight:650;color:var(--ink);margin-bottom:3px}
.evidence small {display:block;color:var(--muted);font-size:11.5px;margin-top:3px}
.evidence .pill-button {margin-top:7px;font-size:12px;padding:4px 10px}
.expand-actions {display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.expand-actions button {padding:7px 11px;font-size:12.5px;background:var(--accent-soft);
 border-color:transparent;color:var(--accent)}
.expand-actions button:hover {background:var(--accent);color:#fff;border-color:var(--accent)}
#panel details {margin-top:24px;border:1px solid var(--line);border-radius:10px;padding:0 14px;background:var(--surface)}
#panel details[open] {padding-bottom:8px}
#panel summary {cursor:pointer;font-size:12.5px;font-weight:600;color:var(--ink-2);padding:10px 0;list-style-position:inside}
#panel summary:hover {color:var(--ink)}
.meta-row {font-size:12px;overflow-wrap:anywhere;padding:7px 0;border-top:1px solid var(--line-soft)}
.meta-row span {display:block;color:var(--muted);font-size:11.5px;margin-bottom:2px}
.meta-row div,.meta-row a {font-family:var(--font-mono);font-size:11.5px!important;color:var(--ink-2);font-weight:400!important}
#panel .source-note {font-size:12px;color:var(--muted);line-height:1.5}
.banner {font-size:13px;background:var(--warn-soft);color:var(--warn)!important;padding:10px 12px;
 border-radius:8px;border:1px solid color-mix(in srgb,var(--warn) 30%,transparent);margin:4px 0 12px}

/* ---------- Footer legend ---------- */
footer {display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:var(--surface);
 border-top:1px solid var(--line);padding:10px 24px;color:var(--ink-2);font-size:12px}
.legend {display:inline-flex;align-items:center;gap:6px}
.dot {width:12px;height:12px;border-radius:50%;flex:none}
.dot.ring {background:var(--f,transparent);border:2px solid var(--c)}
.dot.dash {background:var(--f,transparent);border:2px dashed var(--c)}
.legend-line {width:18px;height:0;border-top:2px solid var(--edge-caption);position:relative}
.legend-line::after {content:"";position:absolute;right:-2px;top:-5px;border:4px solid transparent;border-left:6px solid var(--edge-caption)}
footer .note {margin-left:auto;color:var(--muted)}

/* ---------- Responsive ---------- */
@media (max-width:980px) {.app {grid-template-columns:minmax(0,1fr) 310px}}
@media (max-width:790px) {
 header {gap:10px;padding:14px 12px 12px}
 .identity {min-width:140px;flex:1}
 .identity strong {font-size:15.5px}
 .metrics {font-size:11px;padding:5px 9px}
 .searchwrap {order:3;flex-basis:100%;max-width:none}
 body {height:auto;min-height:100dvh}
 .app {grid-template-columns:minmax(0,1fr);grid-template-rows:62vh auto}
 #panel {border-left:0;border-top:1px solid var(--line);padding:16px;overflow:visible}
 footer {gap:10px;padding:10px 12px}
 footer .note {width:100%;margin:0}
}
@media (prefers-reduced-motion:reduce) {*,*::before,*::after {transition:none!important;scroll-behavior:auto!important}}
</style>
</head>
<body>
<header>
 <div class="identity"><strong>IPCC Figures Knowledge Graph</strong><div id="subtitle">AR6 · Source-backed network</div></div>
 <div class="searchwrap">
  <span class="glass" aria-hidden="true">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="3"
        stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.35-4.35" />
    </svg>
  </span>
  <input id="search" type="search" placeholder="Find a figure or concept in this view" aria-label="Find a node in the exported graph" autocomplete="off">
  <div id="matches" role="listbox" aria-label="Matching graph nodes"></div>
 </div>
 <div class="filters"><label for="relation-filter">Show</label>
  <select id="relation-filter" aria-label="Filter displayed relationships">
   <option value="all">All relationships</option>
   <option value="caption">Caption links</option>
   <option value="report">Report structure</option>
   <option value="evidence">Caption evidence</option>
   <option value="candidate">Candidate matches</option>
   <option value="glossary">Glossary attribution</option>
   <option value="topic">Curated topics</option>
   <option value="image">Image references</option>
  </select></div>
</header>
<main class="app">
 <section class="canvas-wrap" aria-label="Interactive RDF knowledge graph">
  <svg id="network" role="group" aria-label="Draggable, zoomable graph. Select a node or relationship to inspect evidence."
       tabindex="0" xmlns="http://www.w3.org/2000/svg">
   <defs>
    <marker id="arrow" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#b4bfca"></path></marker>
    <marker id="arrow-caption" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#5fb09a"></path></marker>
    <marker id="arrow-candidate" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#d9a650"></path></marker>
    <marker id="arrow-evidence" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#a493cf"></path></marker>
    <marker id="arrow-image" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#6fb8bd"></path></marker>
    <marker id="arrow-glossary" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#b39ad6"></path></marker>
    <marker id="arrow-topic" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#cfa27f"></path></marker>
    <marker id="arrow-active" viewBox="0 0 10 10" markerWidth="9" markerHeight="9" refX="8.5" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,1 L10,5 L0,9 L2.6,5 Z" fill="#2563a8"></path></marker>
   </defs>
   <g id="viewport"><g id="edges"></g><g id="nodes"></g></g>
  </svg>
  <div class="metrics" id="metrics">Loading network…</div>
  <div id="empty" role="status">No graph nodes in this query.<br>Try a different concept or working group.</div>
  <div class="canvas-tools" aria-label="Graph navigation controls">
   <button id="zoom-in" title="Zoom in" aria-label="Zoom in">
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M12 5v14M5 12h14" />
      </svg>
    </button>

    <button id="zoom-out" title="Zoom out" aria-label="Zoom out">
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M5 12h14" />
      </svg>
    </button>

    <button id="fit" title="Fit visible network" aria-label="Fit visible network">
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M8 3H5a2 2 0 0 0-2 2v3" />
        <path d="M21 8V5a2 2 0 0 0-2-2h-3" />
        <path d="M3 16v3a2 2 0 0 0 2 2h3" />
        <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
      </svg>
    </button>

    <button id="reset" title="Reset network and filters" aria-label="Reset network">
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="3"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M3 11a9 9 0 1 1 2.96 6.74" />
        <path d="M3 3v8h8" />
      </svg>
    </button>
  </div>
 </section>
 <aside id="panel" aria-live="polite" aria-label="Node and relationship details"></aside>
</main>
<footer>
 <span class="legend"><i class="dot ring" style="--c:#317cc4;--f:var(--t-figure)"></i>Figure</span>
 <span class="legend"><i class="dot ring" style="--c:#168368;--f:var(--t-concept)"></i>Concept</span>
 <span class="legend"><i class="dot ring" style="--c:#b87928;--f:var(--t-section)"></i>Report context</span>
 <span class="legend"><i class="dot dash" style="--c:#8b6db2;--f:var(--t-mention)"></i>Caption evidence</span>
 <span class="legend"><i class="legend-line"></i>Caption mentions concept</span>
 <span class="note">Links show what captions mention, not what images depict or what causes what.</span>
</footer>
<script id="graph-data" type="application/json">__IPCC_DATA__</script>
<script>
(()=>{
 'use strict';
 const byId=id=>document.getElementById(id);
 const svg=byId('network'), world=byId('viewport'), edgeLayer=byId('edges'), nodeLayer=byId('nodes');
 const panel=byId('panel'), metrics=byId('metrics'), results=byId('matches'), filter=byId('relation-filter');
 const NS='http://www.w3.org/2000/svg';
 const config={maxVisible:135, expandBatch:14, maxTicks:165};
 const palette={concept:'#168368',figure:'#317cc4',mention:'#8b6db2',image:'#29858b',
   section:'#b87928',working_group:'#9671b6',report:'#7b8295',topic:'#ac764d'};
 const radii={concept:33,figure:29,mention:20,image:21,section:25,working_group:24,report:25,topic:25};
 const payload=JSON.parse(byId('graph-data').textContent);
 byId('subtitle').textContent=`AR6 · ${payload.selectedFigures} of ${payload.totalFigures} matching figures`;
 const allNodes=new Map(payload.nodes.map(item=>[item.id,item]));
 const allEdges=new Map(payload.edges.map(item=>[item.id,item]));
 const incident=new Map(payload.nodes.map(item=>[item.id,[]]));
 for (const edge of payload.edges) {
   incident.get(edge.from)?.push(edge);
   incident.get(edge.to)?.push(edge);
 }
 const firstVisible=new Set(payload.initial);
 let visible=new Set(firstVisible), selection=null, selectedEdge=null, activeFilter='all';
 let nodeSVG=new Map(), edgeSVG=new Map(), coords=new Map(), width=700,height=500;
 let scale=1,tx=0,ty=0,frame=0,remaining=0,drag=null,pan=null;
 const prefersReduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

 const element=(tag,text,className)=>{
   const el=document.createElement(tag);
   if(text!==undefined&&text!==null)el.textContent=String(text);
   if(className)el.className=className;
   return el;
 };
 const add=(parent,tag,text,className)=>{
   const child=element(tag,text,className); parent.appendChild(child); return child;
 };
 const svgEl=(tag,attrs={})=>{
   const e=document.createElementNS(NS,tag);
   for(const [key,val] of Object.entries(attrs))e.setAttribute(key,String(val));
   return e;
 };
 const link=(parent,text,url)=>{
   if(!url)return;
   const a=add(parent,'a',text); a.href=url;a.target='_blank';a.rel='noopener noreferrer';
   return a;
 };
 const heading=title=>add(panel,'h3',title);
 const names=id=>allNodes.get(id)?.label ?? id;
 const color=kind=>palette[kind]??'#7b8295';
 const limitText=(text,max=26)=>String(text).length>max?String(text).slice(0,max-1).trimEnd()+'…':String(text);
 const screenPoint=e=>{
   const rect=svg.getBoundingClientRect();
   return {x:(e.clientX-rect.left)*width/Math.max(1,rect.width),
           y:(e.clientY-rect.top)*height/Math.max(1,rect.height)};
 };
 const worldPoint=e=>{const p=screenPoint(e);return {x:(p.x-tx)/scale,y:(p.y-ty)/scale};};
 const applyCamera=()=>world.setAttribute('transform',`translate(${tx} ${ty}) scale(${scale})`);
 const activeEdges=()=>payload.edges.filter(e=>visible.has(e.from)&&visible.has(e.to)&&
   (activeFilter==='all'||e.family===activeFilter));
 const hash=id=>{let h=2166136261;for(let i=0;i<id.length;i++){h=Math.imul(h^id.charCodeAt(i),16777619);}return h>>>0;};
 const place=id=>{
   if(coords.has(id))return;
   const item=allNodes.get(id);const h=hash(id);const theta=(h%3600)*Math.PI/1800;
   const rank={concept:150,figure:260,section:330,working_group:375,report:420,
      topic:325,mention:345,image:310};
   const distance=(rank[item.kind]??250)+(Math.floor(h/4000)%61)-30;
   coords.set(id,{x:Math.cos(theta)*distance,y:Math.sin(theta)*distance,
       vx:0,vy:0,fx:0,fy:0,pinned:false});
 };
 const setSize=()=>{
   const r=svg.getBoundingClientRect();width=Math.max(250,Math.round(r.width));
   height=Math.max(260,Math.round(r.height));
   svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
 };
 function updateMetrics(){
   metrics.textContent=`${visible.size} nodes · ${activeEdges().length} links · ${payload.rdfTriples} RDF triples in view`;
 }
 function draw(){
   nodeSVG.clear(); edgeSVG.clear();nodeLayer.replaceChildren();edgeLayer.replaceChildren();
   for(const id of visible)place(id);
   for(const e of activeEdges()){
     const line=svgEl('line',{class:'graph-edge','data-family':e.family,
          tabindex:'0',role:'button','aria-label':`${names(e.from)} — ${e.label} — ${names(e.to)}`,
          'marker-end':'url(#arrow)'});
     line.addEventListener('click',ev=>{ev.stopPropagation();selectEdge(e.id);});
     line.addEventListener('keydown',ev=>{
       if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();selectEdge(e.id);}
     });
     edgeLayer.appendChild(line);edgeSVG.set(e.id,line);
   }
   for(const id of [...visible].sort((a,b)=>names(a).localeCompare(names(b)))){
     const n=allNodes.get(id);const isMain=n.kind==='concept'||n.kind==='figure';
     const g=svgEl('g',{class:`graph-node${isMain?'':' secondary'}`,tabindex:'0',role:'button',
        'aria-label':`${n.kind.replaceAll('_',' ')}: ${n.label}`});
     const radius=radii[n.kind]??23;
     const circle=svgEl('circle',{r:radius,fill:color(n.kind),'fill-opacity':isMain?'0.15':'0.12',
       stroke:color(n.kind)});
     g.appendChild(circle);
     const label=svgEl('text',{'dominant-baseline':'middle'});
     const short=limitText(n.label,isMain?24:19);
     const words=short.split(/\s+/);const lines=[];let buffer='';
     for(const word of words){
       if(buffer&&(buffer.length+word.length+1>13)){lines.push(buffer);buffer=word;}
       else buffer=buffer?buffer+' '+word:word;
     }
     if(buffer)lines.push(buffer);
     const displayLines=lines.length>2?[lines[0],limitText(lines.slice(1).join(' '),11)]:lines;
     displayLines.forEach((text,i)=>{
       const span=svgEl('tspan',{x:0,dy:i?'12':'0'});
       if(i===0)span.setAttribute('y',String(-(displayLines.length-1)*6));
       span.textContent=text;label.appendChild(span);
     });
     g.appendChild(label);
     g.addEventListener('pointerdown',ev=>{
       if(ev.button!==0)return;
       ev.stopPropagation();const p=screenPoint(ev);
       drag={id,pointer:ev.pointerId,x:p.x,y:p.y,moved:false};
       g.setPointerCapture(ev.pointerId);g.classList.add('dragging');
     });
     g.addEventListener('pointermove',ev=>{
       if(!drag||drag.id!==id||drag.pointer!==ev.pointerId)return;
       const p=worldPoint(ev);const screen=screenPoint(ev);
       if(Math.hypot(screen.x-drag.x,screen.y-drag.y)>4)drag.moved=true;
       if(drag.moved){const v=coords.get(id);v.x=p.x;v.y=p.y;v.vx=0;v.vy=0;v.pinned=true;
         placeElements();}
     });
     const release=ev=>{
       if(!drag||drag.id!==id||drag.pointer!==ev.pointerId)return;
       const moved=drag.moved;drag=null;g.classList.remove('dragging');
       if(g.hasPointerCapture(ev.pointerId))g.releasePointerCapture(ev.pointerId);
       if(!moved)selectNode(id);
     };
     g.addEventListener('pointerup',release);
     g.addEventListener('pointercancel',()=>{drag=null;g.classList.remove('dragging');});
     g.addEventListener('keydown',ev=>{
       if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();selectNode(id);}
     });
     nodeLayer.appendChild(g);nodeSVG.set(id,g);
   }
   updateMetrics();placeElements();highlight();
 }
 function placeElements(){
   for(const [id,g] of nodeSVG){const p=coords.get(id);if(p)g.setAttribute('transform',`translate(${p.x} ${p.y})`);}
   for(const [id,line] of edgeSVG){
     const e=allEdges.get(id);const a=coords.get(e.from),b=coords.get(e.to);
     if(!a||!b)continue;
     const dx=b.x-a.x,dy=b.y-a.y,length=Math.max(0.01,Math.hypot(dx,dy));
     const ra=(radii[allNodes.get(e.from).kind]??23)+3;
     const rb=(radii[allNodes.get(e.to).kind]??23)+7;
     line.setAttribute('x1',a.x+dx*ra/length);line.setAttribute('y1',a.y+dy*ra/length);
     line.setAttribute('x2',b.x-dx*rb/length);line.setAttribute('y2',b.y-dy*rb/length);
   }
 }
 function fit(){
   if(!visible.size)return;
   const points=[...visible].map(id=>coords.get(id));
   const left=Math.min(...points.map(p=>p.x-43));const right=Math.max(...points.map(p=>p.x+43));
   const top=Math.min(...points.map(p=>p.y-43));const bottom=Math.max(...points.map(p=>p.y+43));
   scale=Math.min(1.35,Math.max(.16,Math.min((width-70)/Math.max(100,right-left),
                                           (height-80)/Math.max(100,bottom-top))));
   tx=width/2-((left+right)/2)*scale;
   ty=height/2-((top+bottom)/2)*scale;applyCamera();
 }
 function stepPhysics(){
   frame=0;const ids=[...visible];const connections=activeEdges();
   const iteration=()=>{
     if(!remaining)return;
     remaining--;
     const active=ids.filter(id=>visible.has(id));
     for(const id of active){const p=coords.get(id);p.fx=-p.x*.002;p.fy=-p.y*.002;}
     for(let i=0;i<active.length;i++){
       const ai=active[i],a=coords.get(ai);for(let j=i+1;j<active.length;j++){
         const bj=active[j],b=coords.get(bj);let dx=b.x-a.x,dy=b.y-a.y;
         const d2=Math.max(40,dx*dx+dy*dy),d=Math.sqrt(d2);
         const required=(radii[allNodes.get(ai).kind]??23)+(radii[allNodes.get(bj).kind]??23)+19;
         const force=2900/(d2+550)+Math.max(0,required-d)*.04;
         dx=dx/d*force;dy=dy/d*force;
         a.fx-=dx;a.fy-=dy;b.fx+=dx;b.fy+=dy;
       }
     }
     for(const e of connections){
       const a=coords.get(e.from),b=coords.get(e.to);if(!a||!b)continue;
       const dx=b.x-a.x,dy=b.y-a.y,d=Math.max(1,Math.hypot(dx,dy));
       const desired=e.family==='caption'?200:200;
       const tension=Math.max(-8,Math.min(8,(d-desired)*.012));
       const fx=tension*dx/d,fy=tension*dy/d;
       a.fx+=fx;a.fy+=fy;b.fx-=fx;b.fy-=fy;
     }
     for(const id of active){const p=coords.get(id);if(p.pinned)continue;
       p.vx=Math.max(-10,Math.min(10,(p.vx+p.fx)*.74));
       p.vy=Math.max(-10,Math.min(10,(p.vy+p.fy)*.74));
       p.x+=p.vx;p.y+=p.vy;
     }
     placeElements();
     if(remaining){frame=requestAnimationFrame(iteration);}
     else fit();
   };
   iteration();
 }
 function settle(){
   if(frame)cancelAnimationFrame(frame);frame=0;
   if(!visible.size)return;
   remaining=prefersReduced?32:config.maxTicks;
   stepPhysics();
 }
 function highlight(){
   const related=new Set();if(selection){related.add(selection);
      for(const e of activeEdges())if(e.from===selection||e.to===selection){related.add(e.from);related.add(e.to);}}
   if(selectedEdge){const e=allEdges.get(selectedEdge);if(e){related.add(e.from);related.add(e.to);}}
   for(const [id,g] of nodeSVG){
     g.classList.toggle('selected',id===selection);
     g.classList.toggle('dimmed',related.size>0&&!related.has(id));
   }
   for(const [id,line] of edgeSVG){const e=allEdges.get(id);
     line.classList.toggle('selected',id===selectedEdge);
     line.classList.toggle('dimmed',related.size>0&&!(
         id===selectedEdge||selection&&(e.from===selection||e.to===selection)));
   }
 }
 function focus(id){
   const p=coords.get(id);if(!p)return;
   const target=.9;
   scale=Math.max(scale,target);
   tx=width/2-p.x*scale;ty=height/2-p.y*scale;applyCamera();
 }
 function reveal(id){
   if(!allNodes.has(id))return;
   if(!visible.has(id)){
     if(visible.size>=config.maxVisible){notice('Graph is at its display limit. Reset or use a smaller query.');return;}
     visible.add(id);place(id);draw();settle();
   }
 }
 function selectNode(id){
   if(!allNodes.has(id))return;
   reveal(id);if(!visible.has(id))return;
   selection=id;selectedEdge=null;highlight();focus(id);renderNode(id);
 }
 function selectEdge(id){
   if(!allEdges.has(id))return;
   selectedEdge=id;selection=null;highlight();renderEdge(id);
 }
 function notice(message){const area=byId('notice');if(area){area.textContent=message;return;}
   panel.replaceChildren(add(panel,'p',message,'banner'));
 }
 function subsection(text){return heading(text);}
 function button(text,handler,className='pill-button'){
   const b=add(panel,'button',text,className);b.type='button';b.addEventListener('click',handler);return b;
 }
 function navTo(id,label){
   if(!allNodes.has(id))return;
   button(label??names(id),()=>selectNode(id));
 }
 function technical(node){
   const box=add(panel,'details');const summary=add(box,'summary','RDF identifiers & metadata');
   const content=add(box,'div');
   const row=(label,value,url)=>{
     const div=add(content,'div',null,'meta-row');add(div,'span',label);
     if(url)link(div,value,url);else add(div,'div',value);
   };
   row('Resource IRI',node.id,null);
   for(const type of node.types)row('rdf:type',type,null);
   for(const attr of node.attributes)row(attr.label,attr.value,attr.url);
   if(node.typeDescription)row('Ontology class description',node.typeDescription,null);
   return summary;
 }
 function relatedButtons(id){
   const all=incident.get(id)??[];
   const options=[['Connected figures & concepts','core',e=>{
       const n=allNodes.get(e.from===id?e.to:e.from);
       return n&&['figure','concept'].includes(n.kind);
     }],
     ['Report context','context',e=>['report','section','working_group','topic'].includes(
       allNodes.get(e.from===id?e.to:e.from)?.kind)],
     ['Caption evidence','evidence',e=>allNodes.get(e.from===id?e.to:e.from)?.kind==='mention'],
     ['Image references','images',e=>allNodes.get(e.from===id?e.to:e.from)?.kind==='image'],
     ['All relationships','all',()=>true]];
   const unseen=all.filter(e=>!visible.has(e.from===id?e.to:e.from));
   if(!unseen.length)return;
   subsection('Expand network');
   const actions=add(panel,'div',null,'expand-actions');
   let any=false;
   for(const [text,key,test] of options){
     if(!unseen.some(test))continue;
     any=true;
     const count=unseen.filter(test).length;
     const b=add(actions,'button',`${text} (${count})`);
     b.addEventListener('click',()=>expand(id,key,test));
   }
   if(any)add(panel,'p','Reveals existing RDF neighbors in this exported graph.','source-note');
 }
 function expand(id,_category,test){
   const priority={concept:0,figure:0,section:1,working_group:2,topic:2,report:3,image:4,mention:5};
   const choices=(incident.get(id)??[]).filter(test).map(e=>e.from===id?e.to:e.from)
     .filter(other=>!visible.has(other));
   const unique=[...new Set(choices)].sort((a,b)=>{
     const p=(priority[allNodes.get(a).kind]??9)-(priority[allNodes.get(b).kind]??9);
     return p||names(a).localeCompare(names(b))||a.localeCompare(b);
   });
   const count=Math.min(config.expandBatch,config.maxVisible-visible.size);
   const additions=unique.slice(0,Math.max(0,count));
   if(!additions.length){notice('No additional nodes available in this exported neighborhood.');return;}
   for(const other of additions){visible.add(other);place(other);}
   draw();settle();selection=id;selectedEdge=null;renderNode(id);highlight();
 }
 function showEvidence(item){
   const div=add(panel,'div',null,'evidence'+(['excluded','ambiguous'].includes(item.resolution)?' warn':''));
   add(div,'strong',`“${item.text??''}”`);
   const status=item.resolution??'unknown';
   add(div,'small',`Characters ${item.start}–${item.end} · ${status}`);
   if(item.selected&&allNodes.has(item.selected)){
     const b=add(div,'button',`Accepted: ${names(item.selected)}`,'pill-button');
     b.addEventListener('click',()=>selectNode(item.selected));
   }else if(item.candidates?.length){
     add(div,'small','Candidate only — not an accepted concept assertion.');
     for(const candidate of item.candidates.slice(0,4)){
       const b=add(div,'button',names(candidate),'pill-button');
       b.addEventListener('click',()=>selectNode(candidate));
     }
   }
   if(item.qualityRule)add(div,'small',`Rule: ${item.qualityRule}`);
 }
 function detailsFigure(node){
   const f=node.figure;if(!f)return;
   if(f.title&&f.title!==node.label)add(panel,'p',f.title);
   add(panel,'div',`${f.wg??'IPCC'} · ${f.section??'AR6'} · ${f.captionStatus??'unknown'} caption`,'stat');
   if(f.images.length){
     subsection('Original IPCC figure');
     const img=add(panel,'img',null,'hero');img.alt=`IPCC source image: ${node.label}`;
     img.loading='lazy';img.referrerPolicy='no-referrer';
     img.addEventListener('error',()=>img.replaceWith(element('p',
       'Preview unavailable. Open the official IPCC figure page.','muted')));
     img.src=f.images[0];
     if(f.images.length>1)add(panel,'p',`${f.images.length} image references recorded.`,'source-note');
   }else add(panel,'p','No published image reference was recorded.','muted');
   link(panel,'Open official IPCC figure ↗',f.source);
   const accepted=(incident.get(node.id)??[]).filter(e=>e.predicate.includes('mentionsConcept'))
     .map(e=>e.from===node.id?e.to:e.from);
   if(accepted.length){
     subsection(`Concepts mentioned (${accepted.length})`);
     for(const id of [...new Set(accepted)].sort((a,b)=>names(a).localeCompare(names(b))))navTo(id);
   }
   if(f.matches.length&&payload.match==='caption'){
     subsection('Literal search matches');
     for(const item of f.matches.slice(0,15)){
       add(panel,'div',`“${item.text}” · characters ${item.start}–${item.end}`,'evidence');
     }
     if(f.matches.length>15)add(panel,'p',`+${f.matches.length-15} further matches`,'source-note');
   }
   if(f.mentions.length){
     subsection(`Auditable caption evidence (${f.mentions.length})`);
     const accepted=f.mentions.filter(m=>m.selected);
     const other=f.mentions.filter(m=>!m.selected);
     let shown=0;const showNext=()=>{
       const chunk=[...accepted,...other].slice(shown,shown+12);
       for(const item of chunk)showEvidence(item);
       shown+=chunk.length;
       more.textContent=`Show more evidence (${f.mentions.length-shown} remaining)`;
       more.hidden=shown>=f.mentions.length;
     };
     const more=button('Show caption evidence',()=>showNext());
     showNext();
     if(other.length)add(panel,'p',`${other.length} excluded/ambiguous mentions are candidate evidence only.`,'source-note');
   }else add(panel,'p','No caption evidence recorded for this figure.','source-note');
   add(panel,'p','Full source caption: open the official figure page. This export contains source-backed snippets, not copyrighted full captions.','source-note');
 }
 function detailsConcept(node){
   const c=node.concept;if(!c)return;
   add(panel,'div',`${c.figuresInView} figure connections in this exported RDF neighborhood`,'stat');
   if(c.aliases.length){subsection('Glossary aliases');
     for(const alias of c.aliases)add(panel,'span',alias,'chip');}
   const memberships=(incident.get(node.id)??[]).filter(e=>e.family==='glossary'||e.family==='topic');
   if(memberships.length){subsection('Attribution & curated topics');
     for(const e of memberships)navTo(e.from===node.id?e.to:e.from);}
   link(panel,'Glossary source ↗',c.source);
   const figures=(incident.get(node.id)??[]).filter(e=>e.predicate.endsWith('#mentionsConcept'))
     .map(e=>e.from===node.id?e.to:e.from);
   if(figures.length){subsection(`Connected IPCC figures (${figures.length})`);
     for(const id of figures.slice(0,20))navTo(id);
     if(figures.length>20)add(panel,'p',`+${figures.length-20} more; use graph expansion.`, 'source-note');
   }
   add(panel,'p','Caption connections are observed lexical mentions, not verified visual depiction or causal relationships.','source-note');
 }
 function detailsMention(node){
   const m=node.mention;if(!m)return;
   if(['ambiguous','excluded'].includes(m.resolution)){
     add(panel,'p','This is candidate evidence only. It does not establish an accepted figure–concept link.','banner');
   }
   subsection('Exact caption match');
   showEvidence({...m,id:node.id});
   add(panel,'div',`Match type: ${m.matchKind??'unknown'}`,'stat');
 }
 function detailsImage(node){
   const img=node.image;if(!img)return;
   if(img.url){
     subsection('Published image reference');
     const image=add(panel,'img',null,'hero');image.alt='Original IPCC figure image reference';
     image.referrerPolicy='no-referrer';image.loading='lazy';
     image.addEventListener('error',()=>image.replaceWith(
       element('p','Image preview unavailable; open the source URL.','muted')));
     image.src=img.url;
     link(panel,'Open image source ↗',img.url);
   }else add(panel,'p','The published image URL is missing or invalid.','muted');
 }
 function detailsTopic(node){
   const t=node.topic;if(!t)return;
   if(t.description)add(panel,'p',t.description);
   add(panel,'p','This is an editorial SKOS collection, not a scientific subclass hierarchy.','source-note');
   for(const url of t.references)link(panel,'IPCC report reference ↗',url);
 }
 function renderNode(id){
   const node=allNodes.get(id);if(!node)return;
   panel.replaceChildren();
   const top=add(panel,'div',node.kind.replaceAll('_',' '),'kicker');top.style.color=color(node.kind);
   add(panel,'h2',node.label);
   if(node.kind==='figure')detailsFigure(node);
   else if(node.kind==='concept')detailsConcept(node);
   else if(node.kind==='mention')detailsMention(node);
   else if(node.kind==='image')detailsImage(node);
   else if(node.kind==='topic')detailsTopic(node);
   else if(node.kind==='section')add(panel,'p','A published IPCC section. Expand to navigate associated figures and working group.');
   else if(node.kind==='working_group')add(panel,'p','A working group in the IPCC AR6 publication structure. Glossary attribution is distinct from figure membership.');
   else if(node.kind==='report')add(panel,'p','The IPCC Sixth Assessment Report. Explore its working groups and figures through published RDF links.');
   relatedButtons(id);
   technical(node);
   panel.scrollTop=0;
 }
 function renderEdge(id){
   const e=allEdges.get(id);if(!e)return;
   panel.replaceChildren();
   add(panel,'div','RDF relationship','kicker');add(panel,'h2',e.label);
   add(panel,'p',e.description||'A source-backed RDF instance relationship.');
   subsection('Direction');
   navTo(e.from);add(panel,'p',`↓ ${e.label} ↓`,'stat');navTo(e.to);
   if(e.family==='candidate')add(panel,'p',
     'Candidate interpretation only: this edge is not an accepted concept assertion.','banner');
   if(e.family==='caption'){
     add(panel,'p','This edge means the figure caption contains a resolved glossary mention. It does not imply visual depiction or scientific causality.','source-note');
     const figure=allNodes.get(e.from)?.figure;
     if(figure){
       const supporting=figure.mentions.filter(m=>m.selected===e.to);
       if(supporting.length){subsection(`Supporting caption spans (${supporting.length})`);
         for(const m of supporting.slice(0,10))showEvidence(m);
         if(supporting.length>10)add(panel,'p',`+${supporting.length-10} more mentions.`,'source-note');
       }
     }
   }
   const box=add(panel,'details');add(box,'summary','Exact RDF triple & ontology');
   for(const [label,value] of [['Subject',e.from],['Predicate',e.predicate],['Object',e.to],
      ...e.domain.map(x=>['rdfs:domain',x]),...e.range.map(x=>['rdfs:range',x])]){
     const row=add(box,'div',null,'meta-row');add(row,'span',label);add(row,'div',value);
   }
   panel.scrollTop=0;
 }
 function showWelcome(){
   selection=null;selectedEdge=null;highlight();panel.replaceChildren();
   add(panel,'div','Explore IPCC AR6','kicker');
   add(panel,'h2',payload.query);
   add(panel,'p',`${payload.selectedFigures} selected figures · ${payload.nodes.filter(n=>n.kind==='concept').length} recorded concepts in the exported neighborhood.`);
   add(panel,'p','Select a circle or relationship to inspect its original RDF meaning, figure images and caption evidence.','muted');
   add(panel,'p','Drag nodes, scroll to zoom and use Expand network to reveal the remaining published connections.','muted');
   add(panel,'p','The export is self contained for graph interaction. IPCC image previews and source links still require internet access.','source-note');
 }
 function zoom(amount,point={x:width/2,y:height/2}){
   const next=Math.max(.18,Math.min(3.7,scale*amount));
   const factor=next/scale;tx=point.x+(tx-point.x)*factor;ty=point.y+(ty-point.y)*factor;
   scale=next;applyCamera();
 }
 svg.addEventListener('wheel',ev=>{ev.preventDefault();zoom(Math.exp(-ev.deltaY*.0012),screenPoint(ev));},{passive:false});
 svg.addEventListener('pointerdown',ev=>{
   if(ev.button!==0||ev.target.closest('.graph-node')||ev.target.closest('.graph-edge'))return;
   const p=screenPoint(ev);pan={id:ev.pointerId,x:p.x,y:p.y};
   svg.setPointerCapture(ev.pointerId);svg.classList.add('panning');
 });
 svg.addEventListener('pointermove',ev=>{
   if(!pan||pan.id!==ev.pointerId)return;
   const p=screenPoint(ev);tx+=p.x-pan.x;ty+=p.y-pan.y;
   pan.x=p.x;pan.y=p.y;applyCamera();
 });
 const endPan=ev=>{
   if(pan&&pan.id===ev.pointerId){pan=null;svg.classList.remove('panning');
     if(svg.hasPointerCapture(ev.pointerId))svg.releasePointerCapture(ev.pointerId);}
 };
 svg.addEventListener('pointerup',endPan);svg.addEventListener('pointercancel',endPan);
 svg.addEventListener('click',ev=>{
   if(ev.target===svg||ev.target===world||ev.target===edgeLayer||ev.target===nodeLayer)showWelcome();
 });
 svg.addEventListener('keydown',ev=>{
   if(ev.target!==svg)return;
   if(ev.key==='+')zoom(1.2);else if(ev.key==='-')zoom(1/1.2);
   else if(ev.key==='Escape')showWelcome();else return;
   ev.preventDefault();
 });
 byId('zoom-in').addEventListener('click',()=>zoom(1.25));
 byId('zoom-out').addEventListener('click',()=>zoom(.8));
 byId('fit').addEventListener('click',fit);
 byId('reset').addEventListener('click',()=>{
   if(frame)cancelAnimationFrame(frame);frame=0;remaining=0;
   visible=new Set(firstVisible);coords.clear();filter.value='all';activeFilter='all';
   draw();settle();showWelcome();
 });
 function revealFilteredNeighbors(family){
   if(family==='all')return;
   const budget=Math.min(20,config.maxVisible-visible.size);
   if(budget<=0)return;
   const additions=[];const queued=new Set();
   const offer=id=>{
     if(!visible.has(id)&&!queued.has(id)&&queued.size<budget){
       queued.add(id);additions.push(id);
     }
   };
   if(family==='candidate'){
     // Candidate edges are one hop behind caption mentions. Reveal the actual
     // intervening RDF nodes; never draw a synthetic figure-to-candidate edge.
     for(const e of payload.edges){
       if(queued.size>=budget)break;
       if(e.predicate.endsWith('#hasCaptionMention')&&visible.has(e.from)){
         const links=incident.get(e.to)??[];
         for(const candidate of links.filter(x=>x.family==='candidate')){
           if(queued.size+2>budget)break;
           offer(e.to);offer(candidate.to);
         }
       }
     }
   }else{
     for(const e of payload.edges){
       if(queued.size>=budget)break;
       if(e.family!==family)continue;
       if(visible.has(e.from))offer(e.to);
       else if(visible.has(e.to))offer(e.from);
     }
   }
   for(const id of additions){visible.add(id);place(id);}
 }
 filter.addEventListener('change',()=>{
   activeFilter=filter.value;revealFilteredNeighbors(activeFilter);draw();settle();
   if(selectedEdge&&!edgeSVG.has(selectedEdge)){selectedEdge=null;showWelcome();}
 });
 const search=byId('search');
 function hideResults(){results.replaceChildren();results.style.display='none';}
 function populateResults(){
   const q=search.value.trim().toLocaleLowerCase();results.replaceChildren();
   if(!q){hideResults();return;}
   const found=payload.nodes.filter(n=>n.label.toLocaleLowerCase().includes(q)||
       n.concept?.aliases.some(a=>a.toLocaleLowerCase().includes(q))||
       n.figure?.id?.toLocaleLowerCase().includes(q)).slice(0,9);
   for(const n of found){
     const b=add(results,'button',n.label);b.type='button';
     add(b,'small',n.kind.replaceAll('_',' '));
     b.addEventListener('click',()=>{hideResults();search.value='';selectNode(n.id);});
   }
   results.style.display=found.length?'block':'none';
 }
 search.addEventListener('input',populateResults);
 search.addEventListener('keydown',ev=>{
   if(ev.key==='Escape'){hideResults();search.blur();}
   if(ev.key==='Enter'){
     const q=search.value.trim().toLocaleLowerCase();
     const found=payload.nodes.find(n=>n.label.toLocaleLowerCase().includes(q));
     if(found){ev.preventDefault();hideResults();search.value='';selectNode(found.id);}
   }
 });
 search.addEventListener('blur',()=>window.setTimeout(hideResults,180));
 function init(){
   setSize();draw();fit();settle();showWelcome();
   if(!visible.size)byId('empty').style.display='block';
   if('ResizeObserver' in window){new ResizeObserver(()=>{setSize();fit();}).observe(svg.parentElement);}
   else window.addEventListener('resize',()=>{setSize();fit();});
 }
 try{init();}catch(error){
   console.error('IPCC graph rendering failed:',error);
   metrics.textContent='Network unavailable';
   const b=add(panel,'div','The network could not be initialized. Open the browser developer console for technical details.','banner');
   b.setAttribute('role','alert');
 }
})();
</script>
<noscript><p style="padding:15px">JavaScript is required for interactive network rendering. The RDF data is not affected.</p></noscript>
</body>
</html>'''


def render_html(view: KnowledgeSubgraph) -> str:
    """Render a self-contained, source-backed IPCC knowledge graph document.

    The HTML has no external JavaScript/CSS dependency. External image URLs and
    IPCC source links remain remote references. Only evidence snippets and RDF
    metadata, not full copyrighted captions or definitions, are embedded.
    """
    payload = _visualization_data(view)
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    # Source labels, captions and aliases are untrusted HTML-script text.
    serialized = (serialized.replace("&", "\\u0026").replace("<", "\\u003c")
                  .replace(">", "\\u003e"))
    replacements = {
        "__IPCC_TITLE__": escape(view.query),
        "__IPCC_DATA__": serialized,
    }
    return re.sub(
        r"__IPCC_(?:TITLE|DATA)__",
        lambda match: replacements[match.group()], _HTML_DOCUMENT,
    )


def save_html(view: KnowledgeSubgraph, path: str | Path) -> Path:
    """Atomically save a portable HTML graph without modifying the source RDF."""
    output = Path(path).expanduser()
    if output.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("HTML output must end in .html or .htm")
    document = render_html(view)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent,
        prefix=f".{output.name}.", delete=False,
    ) as stage:
        temporary = Path(stage.name)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def show_subgraph(view: KnowledgeSubgraph, *, height: int = 650) -> None:
    """Display the standalone graph inside a sandboxed Colab/Jupyter iframe."""
    if type(height) is not int or not 350 <= height <= 1600:
        raise ValueError("height must be an integer between 350 and 1600 pixels")
    try:
        from IPython.display import display
    except ImportError as exc:
        raise ImportError(
            "Notebook rendering requires IPython. Use view.save('graph.html') "
            "outside a notebook."
        ) from exc
    document = render_html(view)
    frame = (
        '<iframe title="IPCC knowledge graph" '
        'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
        'referrerpolicy="no-referrer" '
        f'style="width:100%;height:{height}px;border:1px solid #dce5ee;'
        'border-radius:12px" '
        f'srcdoc="{escape(document, quote=True)}"></iframe>'
    )
    display({"text/html": frame}, raw=True)
