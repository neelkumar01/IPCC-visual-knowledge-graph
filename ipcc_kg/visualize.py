"""Interactive, evidence-backed visualization of a published IPCC subgraph.

The visualizer only displays relationships already present in ``KnowledgeSubgraph``.
Caption-mention evidence is displayed in the details panel rather than
inventing edges between concepts. Image references are loaded on selection;
no image pixels are analyzed or bundled into the export.

The HTML uses a pinned vis-network standalone browser bundle. Rendering the
network (and retrieving remote IPCC images) requires internet connectivity,
but the exported HTML retains a readable fallback list of original sources.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from rdflib import URIRef
from rdflib.namespace import DCTERMS, RDFS, SKOS

from ontology.schema import IPCC, resource_iri

if TYPE_CHECKING:
    from .subgraph import KnowledgeSubgraph


_NETWORK_SCRIPT = (
    "https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/"
    "standalone/umd/vis-network.min.js"
)
_MAX_VISIBLE_EVIDENCE = 12


def _public_url(value: object) -> str | None:
    """Allow only absolute HTTP(S) URLs without user-info or control bytes."""
    if (not isinstance(value, str) or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        _ = parsed.port  # Reject malformed ports.
    except ValueError:
        return None
    return value


def _rdf_text(graph, subject: URIRef, predicate: URIRef) -> str | None:
    node = graph.value(subject, predicate)
    return str(node) if node is not None else None


def _evidence(graph, figure: URIRef, selected: set[str]) -> dict[str, dict]:
    """Group verified caption mentions by their *selected* glossary concept."""
    mentions: dict[str, list[dict]] = defaultdict(list)
    for mention in graph.objects(figure, IPCC.hasCaptionMention):
        concept = graph.value(mention, IPCC.denotesConcept)
        if concept is None or str(concept) not in selected:
            continue
        text = _rdf_text(graph, mention, IPCC.matchedText)
        start = _rdf_text(graph, mention, IPCC.startOffset)
        end = _rdf_text(graph, mention, IPCC.endOffset)
        if text is None or start is None or end is None:
            raise ValueError("Selected caption evidence is missing its source span")
        mentions[str(concept)].append({
            "text": text,
            "start": int(start),
            "end": int(end),
            "resolution": _rdf_text(graph, mention, IPCC.resolution),
        })

    result = {}
    for concept, spans in mentions.items():
        spans.sort(key=lambda item: (item["start"], item["end"]))
        result[concept] = {
            "count": len(spans),
            "spans": spans[:_MAX_VISIBLE_EVIDENCE],
        }
    return result


def _visualization_data(view: KnowledgeSubgraph) -> dict:
    """Convert only existing RDF edges to a compact browser graph."""
    rdf = view.graph
    nodes: list[dict] = []
    edges: list[dict] = []
    details: dict[str, dict] = {}
    known_nodes: set[str] = set()
    known_edges: set[tuple[str, str, str]] = set()

    def add_node(uri: URIRef, label: str, kind: str, **extra: object) -> None:
        key = str(uri)
        if key not in known_nodes:
            known_nodes.add(key)
            # Long IPCC figure labels can contain near-caption-length text;
            # retain the full source label in details but keep nodes legible.
            display_label = label if len(label) <= 54 else label[:51].rstrip() + "…"
            nodes.append({"id": key, "label": display_label, "group": kind})
            details[key] = {"kind": kind, "label": label, **extra}

    def add_edge(src: URIRef, predicate: URIRef, dst: URIRef, relation: str) -> None:
        if (src, predicate, dst) not in rdf:
            return
        source, target = str(src), str(dst)
        if source not in known_nodes or target not in known_nodes:
            return
        key = (source, relation, target)
        if key not in known_edges:
            known_edges.add(key)
            edges.append({
                "from": source, "to": target, "relation": relation,
            })

    concepts = {
        str(resource_iri("concept", c.term_id)): c.label for c in view.concepts
    }
    for uri_str, label in concepts.items():
        concept = URIRef(uri_str)
        if not list(rdf.objects(concept, SKOS.prefLabel)):
            raise ValueError("Subgraph is missing a selected concept label")
        add_node(concept, label, "concept", source=_public_url(
            _rdf_text(rdf, concept, DCTERMS.source)))

    figures = {
        str(resource_iri("figure", f.figure_id)): f for f in view.figures
    }
    for uri_str, figure in figures.items():
        figure_uri = URIRef(uri_str)
        image_refs = []
        for image in rdf.objects(figure_uri, IPCC.hasImageReference):
            url = _public_url(_rdf_text(rdf, image, IPCC.imageURL))
            position = _rdf_text(rdf, image, IPCC.imagePosition)
            if url is not None:
                image_refs.append((int(position) if position is not None else 0, url))
        image_refs.sort(key=lambda item: (item[0], item[1]))
        # FigureHit is from the corpus; RDF image references are authoritative.
        label = figure.label or figure.figure_id
        add_node(
            figure_uri, label, "figure", figure_id=figure.figure_id,
            title=figure.title, working_group=figure.working_group,
            section=figure.section,
            source=_public_url(figure.source_url),
            images=[url for _, url in image_refs],
            matched=[{
                "text": m.text, "start": m.start_offset,
                "end": m.end_offset,
            } for m in figure.matches],
            evidence=_evidence(rdf, figure_uri, set(concepts)),
        )

    # Report context is part of the selected RDF, not a visual-only association.
    for figure_uri_str in figures:
        figure_uri = URIRef(figure_uri_str)
        for section in rdf.objects(figure_uri, IPCC.figureInSection):
            add_node(section, _rdf_text(rdf, section, IPCC.sectionCode) or "Section",
                     "section")
            for group in rdf.objects(section, IPCC.sectionOfWorkingGroup):
                add_node(group, _rdf_text(rdf, group, IPCC.workingGroupCode) or "WG",
                         "working_group")
                add_edge(section, IPCC.sectionOfWorkingGroup, group, "section_of_group")
            add_edge(figure_uri, IPCC.figureInSection, section, "figure_in_section")

        for group in rdf.objects(figure_uri, IPCC.figureInWorkingGroup):
            add_node(group, _rdf_text(rdf, group, IPCC.workingGroupCode) or "WG",
                     "working_group")
            # Direct figure-to-WG edges are in RDF, but are redundant when a
            # figure-to-section-to-WG path already exists in the drawing.
            if not any(
                (URIRef(e["from"]) == figure_uri and e["relation"] == "figure_in_section")
                for e in edges
            ):
                add_edge(figure_uri, IPCC.figureInWorkingGroup, group, "figure_in_group")

            for report in rdf.objects(group, IPCC.partOfReport):
                add_node(report, _rdf_text(rdf, report, DCTERMS.identifier) or "AR6",
                         "report")
                add_edge(group, IPCC.partOfReport, report, "part_of_report")

        for concept_uri_str in concepts:
            concept_uri = URIRef(concept_uri_str)
            add_edge(figure_uri, IPCC.mentionsConcept, concept_uri, "caption_mentions")

    # Curated SKOS collections are navigation facets, NOT scientific subclasses.
    for concept_uri_str in concepts:
        concept = URIRef(concept_uri_str)
        for category in rdf.subjects(SKOS.member, concept):
            add_node(category, _rdf_text(rdf, category, RDFS.label) or "Topic",
                     "topic")
            add_edge(category, SKOS.member, concept, "collection_member")

    figure_count = defaultdict(int)
    for edge in edges:
        if edge["relation"] == "caption_mentions":
            figure_count[edge["to"]] += 1
    for uri, total in figure_count.items():
        details[uri]["figures_in_view"] = total

    nodes.sort(key=lambda node: (node["group"], node["label"].casefold(), node["id"]))
    edges.sort(key=lambda edge: (edge["relation"], edge["from"], edge["to"]))
    initial = (
        str(resource_iri("concept", view.concepts[0].term_id))
        if view.match == "concept" and view.concepts else
        str(resource_iri("figure", view.figures[0].figure_id))
        if view.figures else None
    )
    return {
        "query": view.query,
        "match": view.match,
        "total_figures": view.total_figures,
        "shown_figures": len(view.figures),
        "nodes": nodes,
        "edges": edges,
        "details": details,
        "initial": initial,
    }


_HTML_DOCUMENT = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>IPCC knowledge graph — __IPCC_TITLE__</title>
<style>
:root {font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:#1d2939;background:#f8fafc;font-size:14px}
* {box-sizing:border-box}
body {margin:0}
header {display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:13px 18px;border-bottom:1px solid #e2e8f0;background:white}
header h1 {font-size:16px;margin:0 0 3px;font-weight:650}
.subtle {color:#64748b;font-size:12px}
.toolbar {display:flex;align-items:center;gap:8px}
button {background:#fff;color:#334155;cursor:pointer;border:1px solid #cbd5e1;
  border-radius:8px;padding:7px 11px;font:inherit}
button:hover {background:#f1f5f9}
.layout {display:grid;grid-template-columns:minmax(0,1fr) 300px;
  height:calc(100vh - 114px);min-height:420px}
#network {min-width:0;background:#fbfdff}
#details {padding:18px;overflow:auto;background:white;border-left:1px solid #e2e8f0}
#details h2 {margin:0 0 8px;font-size:16px;overflow-wrap:anywhere}
#details h3 {font-size:12px;letter-spacing:.02em;text-transform:uppercase;
  color:#64748b;margin:18px 0 8px}
#details p {line-height:1.5;margin:8px 0;overflow-wrap:anywhere}
#details .meta {font-size:12px;color:#475569;overflow-wrap:anywhere}
#details img {width:100%;max-height:245px;object-fit:contain;background:#f1f5f9;
  border:1px solid #e2e8f0;border-radius:8px}
#details a {color:#0758a6;overflow-wrap:anywhere}
#details .evidence {font-size:12px;border-left:3px solid #b5d4c4;
  margin:8px 0;padding:6px 9px;background:#f5faf7;overflow-wrap:anywhere}
#details .context {font-size:12px;color:#475569;margin:6px 0}
.legend {display:flex;gap:13px;flex-wrap:wrap;align-items:center;
  padding:12px 18px;background:white;border-top:1px solid #e2e8f0;
  color:#475569;font-size:11px}
.legend span {display:flex;align-items:center;gap:5px}
.dot {height:10px;width:10px;border-radius:50%;display:inline-block}
.alert {font-size:12px;color:#9a3412;background:#fff7ed;padding:8px 10px;margin-bottom:12px}
#fallback {padding:14px 18px;background:white;border-top:1px solid #e2e8f0}
#fallback li {margin:6px 0}
#fallback a {color:#0758a6}
#empty {display:none;padding:26px;color:#475569}
@media(max-width:700px) {
  header {align-items:flex-start;flex-direction:column}
  .layout {grid-template-columns:1fr;grid-template-rows:minmax(270px,1fr) 235px;
    height:calc(100vh - 155px);min-height:550px}
  #details {border-left:0;border-top:1px solid #e2e8f0}
}
</style>
<script src="__IPCC_SCRIPT__"></script>
</head>
<body>
<header>
<div><h1>IPCC AR6 · __IPCC_TITLE__</h1>
<div class="subtle" id="summary">Evidence-backed figure–concept relationships</div></div>
<div class="toolbar"><button id="fit" type="button">Fit graph</button></div>
</header>
<div class="layout">
  <div id="network" role="img" aria-label="Interactive IPCC figure knowledge graph"></div>
  <aside id="details" aria-live="polite"><h2>Select a node</h2>
    <p class="subtle">Choose a figure, concept, working group or topic.</p></aside>
</div>
<div id="empty">No connected figures were found for this query.</div>
<div class="legend">
 <span><i class="dot" style="background:#2c87bd"></i>Figure</span>
 <span><i class="dot" style="background:#2a936c"></i>Glossary concept</span>
 <span><i class="dot" style="background:#e3a83c"></i>Report section</span>
 <span><i class="dot" style="background:#8365ba"></i>Working group</span>
 <span><i class="dot" style="background:#b78b4a"></i>Curated topic</span>
 <span>Connections indicate caption mentions or report structure, not visual depiction or causality.</span>
</div>
<section id="fallback">
 <strong>Figure sources (available without interactive rendering)</strong>
 <ul>__IPCC_FALLBACK__</ul>
</section>
<script type="application/json" id="graph-data">__IPCC_DATA__</script>
<script>
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('graph-data').textContent);
  const panel = document.getElementById('details');
  const fallback = document.getElementById('fallback');
  document.getElementById('summary').textContent =
    `${data.shown_figures} of ${data.total_figures} matching figures · ${data.match} search`;

  const el = (tag, text, cssClass) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (cssClass) element.className = cssClass;
    return element;
  };
  const add = (parent, tag, text, cssClass) => {
    const child = el(tag, text, cssClass);
    parent.appendChild(child);
    return child;
  };
  const addLink = (parent, text, url) => {
    if (!url) return;
    const link = add(parent, 'a', text);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  };
  const heading = (text) => add(panel, 'h3', text);

  if (!data.nodes.length) {
    document.getElementById('empty').style.display = 'block';
    panel.replaceChildren(add(document.createDocumentFragment(), 'h2', 'No matching figures'));
    return;
  }
  if (typeof vis === 'undefined') {
    add(panel, 'div', 'Interactive network unavailable. Internet access is required for the visualization library; source links are listed below.', 'alert');
    return;
  }
  fallback.hidden = true;
  const network = new vis.Network(document.getElementById('network'), {
    nodes: new vis.DataSet(data.nodes), edges: new vis.DataSet(data.edges.map(edge => ({
      ...edge, arrows:{to:{enabled:true,scaleFactor:0.35}},
      color: edge.relation === 'caption_mentions' ? '#9ac8b0' :
        edge.relation === 'collection_member' ? '#dfc9a3' : '#ced8e3',
      dashes: edge.relation === 'collection_member',
      width: edge.relation === 'caption_mentions' ? 1.6 : 1,
    }))),
  }, {
    autoResize: true,
    interaction: {hover: true, tooltipDelay: 350, navigationButtons: true},
    nodes: {font:{size:12,face:'system-ui',color:'#213047'},borderWidth:1,
      margin:10,shadow:false},
    groups: {
      figure: {shape:'box',color:{background:'#e3f1fb',border:'#2c87bd'}},
      concept: {shape:'ellipse',color:{background:'#d7f2e4',border:'#2a936c'},
        font:{size:15,bold:{color:'#176c4b'}}},
      section: {shape:'box',color:{background:'#fff2d5',border:'#e3a83c'},
        font:{size:11}},
      working_group: {shape:'ellipse',color:{background:'#eee5fc',border:'#8365ba'}},
      report: {shape:'ellipse',color:{background:'#e8edf4',border:'#778ba5'}},
      topic: {shape:'box',color:{background:'#f6ead8',border:'#b78b4a'},
        font:{size:11}},
    },
    edges: {smooth:{type:'dynamic'},selectionWidth:2},
    physics: {stabilization:{iterations:170},barnesHut:{gravitationalConstant:-6000,
      centralGravity:0.16,springLength:135,springConstant:0.03,damping:0.17}},
  });
  network.once('stabilizationIterationsDone', () => {
    network.setOptions({physics:false});
    network.fit({animation:{duration:400}});
  });
  document.getElementById('fit').addEventListener('click', () =>
    network.fit({animation:{duration:300}}));

  function selectNode(id) {
    if (!data.details[id]) return;
    network.selectNodes([id]);
    network.focus(id, {scale:1.05,animation:{duration:350}});
    renderDetails(id);
  }
  function renderDetails(id) {
    const item = data.details[id];
    if (!item) return;
    panel.replaceChildren();
    add(panel,'h2',item.label);
    add(panel,'p',item.kind.replaceAll('_',' '),'meta');
    if (item.kind === 'figure') {
      if (item.title && item.title !== item.label) add(panel,'p',item.title);
      add(panel,'p',`${item.working_group} · ${item.section}`,'meta');
      heading('IPCC figure');
      if (item.images.length) {
        const image = add(panel,'img');
        image.alt = `Original IPCC figure reference: ${item.label}`;
        image.loading = 'lazy';
        image.referrerPolicy = 'no-referrer';
        image.addEventListener('error', () => image.replaceWith(
          el('p','Image preview unavailable; open the original IPCC source.','subtle')));
        image.src = item.images[0];
        if (item.images.length > 1) add(panel,'p',
          `${item.images.length} published image references; showing the first.`,'meta');
      } else add(panel,'p','No image reference was recorded for this figure.','subtle');
      addLink(panel,'Open original IPCC figure ↗',item.source);
      if (data.match === 'caption' && item.matched.length) {
        heading('Literal caption matches');
        for (const span of item.matched.slice(0,12)) {
          add(panel,'div',`“${span.text}” · characters ${span.start}–${span.end}`,'evidence');
        }
        if (item.matched.length > 12) add(panel,'p',
          `+${item.matched.length - 12} additional matches`,'subtle');
        add(panel,'p','Literal matches are not scientific concept assertions.','subtle');
      }
      const conceptIds = Object.keys(item.evidence);
      if (conceptIds.length) {
        heading('Caption-supported concepts');
        conceptIds.sort((a,b) => (data.details[a]?.label || a).localeCompare(
          data.details[b]?.label || b));
        for (const cid of conceptIds) {
          const evidence = item.evidence[cid];
          const link = add(panel,'button',data.details[cid]?.label || cid);
          link.style.margin = '0 6px 4px 0';
          link.addEventListener('click',()=>selectNode(cid));
          for (const span of evidence.spans) add(panel,'div',
            `“${span.text}” · characters ${span.start}–${span.end} · ${span.resolution}`,
            'evidence');
          if (evidence.count > evidence.spans.length) add(panel,'p',
            `+${evidence.count - evidence.spans.length} further mentions`,'subtle');
        }
      }
      if (!conceptIds.length && !item.matched.length)
        add(panel,'p','No selected caption evidence in this subgraph.','subtle');
    } else if (item.kind === 'concept') {
      add(panel,'p',`${item.figures_in_view || 0} connected figures in this view`,'context');
      add(panel,'p','These connections mean caption mentions, not verified visual depiction.','subtle');
      addLink(panel,'Open glossary source ↗',item.source);
      heading('Connected figures');
      const neighbors = data.edges.filter(edge => edge.relation === 'caption_mentions'
        && edge.to === id).map(edge => edge.from);
      for (const neighbor of neighbors) {
        const btn = add(panel,'button',data.details[neighbor]?.label || neighbor);
        btn.style.display='block';btn.style.margin='0 0 6px';
        btn.addEventListener('click',()=>selectNode(neighbor));
      }
    } else {
      add(panel,'p','This is a published report-structure or curated navigation link, not an inferred scientific relationship.','subtle');
    }
  }
  network.on('click', params => {
    if (params.nodes.length) renderDetails(params.nodes[0]);
  });
  if (data.initial) selectNode(data.initial);
})();
</script>
</body>
</html>'''


def render_html(view: KnowledgeSubgraph) -> str:
    """Create a portable browser document for this RDF subgraph.

    The visualization JavaScript and remote IPCC image previews require an
    internet connection. The HTML embeds no image binaries or full captions.
    """
    payload = _visualization_data(view)
    # Never allow source labels or caption snippets to end an inline JSON script.
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    serialized = (serialized.replace("&", "\\u0026").replace("<", "\\u003c")
                  .replace(">", "\\u003e"))
    items = []
    for figure in view.figures:
        url = _public_url(figure.source_url)
        text = escape(f"{figure.label or figure.figure_id} ({figure.working_group})")
        if url is None:
            items.append(f"<li>{text}</li>")
        else:
            items.append(
                f'<li><a href="{escape(url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{text}</a></li>'
            )
    if not items:
        items = ["<li>No matching IPCC figures.</li>"]
    # Substitute in one pass: source text must never be interpreted as a token.
    replacements = {
        "__IPCC_TITLE__": escape(view.query),
        "__IPCC_SCRIPT__": escape(_NETWORK_SCRIPT, quote=True),
        "__IPCC_FALLBACK__": "".join(items),
        "__IPCC_DATA__": serialized,
    }
    return re.sub(
        r"__IPCC_(?:TITLE|SCRIPT|FALLBACK|DATA)__",
        lambda match: replacements[match.group()], _HTML_DOCUMENT,
    )


def save_html(view: KnowledgeSubgraph, path: str | Path) -> Path:
    """Atomically export interactive HTML without changing RDF artifacts."""
    output = Path(path).expanduser()
    if output.suffix.lower() not in (".html", ".htm"):
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
    """Render inside Jupyter/Colab using an isolated HTML ``iframe``."""
    if type(height) is not int or not 350 <= height <= 1600:
        raise ValueError("height must be an integer between 350 and 1600 pixels")
    try:
        from IPython.display import display
    except ImportError as exc:
        raise ImportError(
            "Notebook display requires IPython. Use view.save('graph.html') "
            "outside Jupyter or Colab."
        ) from exc
    document = render_html(view)
    frame = (
        '<iframe title="IPCC knowledge graph" '
        'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
        'referrerpolicy="no-referrer" '
        f'style="width:100%;height:{height}px;border:1px solid #e2e8f0;'
        'border-radius:10px" '
        f'srcdoc="{escape(document, quote=True)}"></iframe>'
    )
    # Raw MIME display avoids IPython rewriting the iframe document.
    display({"text/html": frame}, raw=True)
