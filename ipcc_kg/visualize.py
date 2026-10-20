"""Interactive, evidence-backed visualization of a published IPCC subgraph.

The visualizer only displays relationships already present in ``KnowledgeSubgraph``.
Caption-mention evidence is displayed in the details panel rather than
inventing edges between concepts. Image references are loaded on selection;
no image pixels are analyzed or bundled into the export.

The browser graph is rendered using native SVG and requires no network or
JavaScript dependencies. Only remote IPCC image previews require connectivity.
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
        "figure_order": [str(resource_iri("figure", f.figure_id)) for f in view.figures],
        "details": details,
        "initial": initial,
    }


_HTML_DOCUMENT = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>IPCC AR6 — __IPCC_TITLE__</title>
<style>
:root {font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:#203047;background:#fff;font-size:14px}
* {box-sizing:border-box}
body {margin:0}
header {padding:15px 20px 12px;border-bottom:1px solid #e2e8f0}
header h1 {font-size:20px;letter-spacing:-.02em;font-weight:650;margin:0 0 4px}
.subtle {font-size:12px;color:#64748b;line-height:1.5}
.toolbar {display:flex;align-items:center;justify-content:space-between;
  gap:8px;flex-wrap:wrap;padding:8px 20px;border-bottom:1px solid #e2e8f0}
.tabs {display:flex;gap:5px}
button {font:inherit;cursor:pointer;border:1px solid #cbd5e1;
  background:white;color:#24415e;padding:7px 11px;border-radius:8px}
button:hover {background:#f1f5f9}
button:focus-visible,[role="button"]:focus-visible,a:focus-visible {outline:3px solid #2563eb;outline-offset:3px}
.tabs button[aria-selected="true"] {background:#e8f2ff;border-color:#548cd1;color:#13467a;font-weight:650}
.layout {display:grid;grid-template-columns:minmax(0,1fr) 330px;
  height:calc(100vh - 185px);min-height:460px}
#main {min-width:0;min-height:0;overflow:auto;background:#f9fbfe;position:relative}
#network {min-width:980px;width:100%;min-height:390px}
#network svg {display:block;width:980px;max-width:none;height:auto}
#list {padding:16px;max-width:780px}
#list[hidden],#network[hidden] {display:none}
.figure-item {display:block;width:100%;text-align:left;padding:12px 14px;
  margin:0 0 8px;border:1px solid #d7e2ed;border-radius:10px;background:white}
.figure-item strong {display:block;font-size:14px;line-height:1.5}
.figure-item small {display:block;color:#64748b;margin-top:4px}
#details {overflow:auto;padding:18px;border-left:1px solid #e2e8f0;background:#fff}
#details h2 {font-size:18px;overflow-wrap:anywhere;margin:0 0 5px}
#details h3 {font-size:12px;color:#64748b;text-transform:uppercase;
  letter-spacing:.04em;margin:20px 0 9px}
#details p {line-height:1.55;overflow-wrap:anywhere;margin:8px 0}
#details img {display:block;width:100%;max-height:270px;object-fit:contain;
  background:#f7fafc;border:1px solid #e2e8f0;border-radius:8px;margin:10px 0}
#details a {color:#0758a6}
#details .choice {margin:0 5px 6px 0;max-width:100%;text-align:left;overflow-wrap:anywhere}
#details .evidence {background:#f3f8f4;padding:8px 10px;
  border-left:3px solid #80ad94;font-size:12px;overflow-wrap:anywhere;margin:7px 0}
#details .warning {font-size:12px;color:#475569;background:#f1f5f9;
  border-radius:6px;padding:9px;margin:12px 0}
.node {cursor:pointer;outline:none}
.node rect {stroke-width:1.3}
.node.concept rect {fill:#e5f6eb;stroke:#409a6e}
.node.figure rect {fill:#e8f2fc;stroke:#438cc0}
.node text {font:13px system-ui,-apple-system,sans-serif;fill:#1e3349;pointer-events:none}
.node.active rect {stroke:#164a9a;stroke-width:3}
.node.dim {opacity:.75}
.node:focus rect {stroke:#1d4ed8;stroke-width:3}
.edge {fill:none;stroke:#a0bbc7;stroke-width:1.6;opacity:.36;pointer-events:none}
.edge.active {stroke:#21845e;stroke-width:2.6;opacity:.92}
.edge.dim {opacity:.14}
.column-label {font:600 13px system-ui,sans-serif;fill:#657991;letter-spacing:.06em}
.footer {padding:10px 20px;color:#64748b;font-size:12px;
  border-top:1px solid #e2e8f0;line-height:1.5;background:white}
#fallback {padding:10px 20px;background:#fff}
#fallback a {color:#0758a6}
@media(max-width:760px) {
  .layout {display:flex;flex-direction:column;height:auto;min-height:0}
  #main {height:375px;flex:none}
  #details {height:350px;border-left:0;border-top:1px solid #e2e8f0}
  header h1 {font-size:17px}
}
</style>
</head>
<body>
<header>
<h1>__IPCC_TITLE__</h1>
<div class="subtle" id="summary">IPCC AR6 · caption-based figure discovery</div>
</header>
<div class="toolbar">
 <div class="tabs" role="tablist" aria-label="Explore results">
  <button type="button" id="graph-tab" role="tab" aria-selected="true">Graph</button>
  <button type="button" id="list-tab" role="tab" aria-selected="false">Figure list</button>
 </div>
 <span class="subtle">Select any figure or concept to inspect it.</span>
</div>
<div class="layout">
  <main id="main">
    <div id="network" role="region" aria-label="Interactive figure and glossary concept graph"></div>
    <div id="list" hidden></div>
  </main>
  <aside id="details" aria-live="polite"><h2>Figure explorer</h2></aside>
</div>
<div class="footer">Connections mean <b>the figure caption mentions a glossary concept</b> — not that the image visually depicts it or that the concepts are scientifically related.</div>
<section id="fallback" hidden><strong>Original figure sources</strong><ul>__IPCC_FALLBACK__</ul></section>
<script type="application/json" id="graph-data">__IPCC_DATA__</script>
<script>
(() => {
 'use strict';
 const data=JSON.parse(document.getElementById('graph-data').textContent);
 const network=document.getElementById('network');
 const list=document.getElementById('list');
 const panel=document.getElementById('details');
 const graphTab=document.getElementById('graph-tab');
 const listTab=document.getElementById('list-tab');
 document.getElementById('summary').textContent =
   `IPCC AR6 · ${data.shown_figures} of ${data.total_figures} matching figures · ${data.match} search`;
 const S='http://www.w3.org/2000/svg';
 const nodeById=new Map(data.nodes.map(n=>[n.id,n]));
 const concepts=data.nodes.filter(n=>n.group==='concept');
 const ranked=new Map((data.figure_order||[]).map((id,i)=>[id,i]));
 const figures=data.nodes.filter(n=>n.group==='figure')
   .sort((a,b)=>(ranked.get(a.id)??9999)-(ranked.get(b.id)??9999));
 const edges=data.edges.filter(e=>e.relation==='caption_mentions'
   && nodeById.has(e.from) && nodeById.has(e.to));
 const neighbors=new Map();
 for(const edge of edges){
   if(!neighbors.has(edge.from)) neighbors.set(edge.from,new Set());
   if(!neighbors.has(edge.to)) neighbors.set(edge.to,new Set());
   neighbors.get(edge.from).add(edge.to);
   neighbors.get(edge.to).add(edge.from);
 }
 let selected=null;
 let graphSvg=null;
 const elt=(tag,text,cls)=>{
   const e=document.createElement(tag);
   if(text!==undefined && text!==null) e.textContent=String(text);
   if(cls)e.className=cls;
   return e;
 };
 const put=(parent,tag,text,cls)=>{
   const e=elt(tag,text,cls);parent.appendChild(e);return e;
 };
 const link=(parent,label,url)=>{
   if(!url)return;
   const a=put(parent,'a',label);a.href=url;a.target='_blank';
   a.rel='noopener noreferrer';return a;
 };
 const svgElt=(tag,attrs)=>{
   const e=document.createElementNS(S,tag);
   for(const [k,v] of Object.entries(attrs||{})) e.setAttribute(k,String(v));
   return e;
 };
 const short=(text,limit)=>{
   text=String(text||'');
   return text.length>limit?text.slice(0,limit-1).trimEnd()+'…':text;
 };
 function select(id){
   if(!data.details[id])return;
   selected=id;
   for(const g of network.querySelectorAll('.node')){
     const nid=g.getAttribute('data-id');
     g.classList.toggle('active',nid===id);
     g.classList.toggle('dim',nid!==id && !(neighbors.get(id)||new Set()).has(nid));
   }
   for(const line of network.querySelectorAll('.edge')){
     const on=line.getAttribute('data-a')===id||line.getAttribute('data-b')===id;
     line.classList.toggle('active',on);
     line.classList.toggle('dim',!on);
   }
   renderDetails(id);
 }
 function graph(){
   network.replaceChildren();
   const h=Math.max(425,130+figures.length*56,130+concepts.length*76);
   const svg=svgElt('svg',{viewBox:`0 0 980 ${h}`,width:980,height:h,
     role:'group','aria-label':'Glossary concepts connected to IPCC figures by caption mentions'});
   graphSvg=svg;
   const title=svgElt('title'); title.textContent='Figure–concept evidence graph';svg.appendChild(title);
   const labelA=svgElt('text',{x:65,y:32,class:'column-label'});
   labelA.textContent='GLOSSARY CONCEPTS';svg.appendChild(labelA);
   const labelB=svgElt('text',{x:575,y:32,class:'column-label'});
   labelB.textContent='IPCC FIGURES';svg.appendChild(labelB);
   const pos=new Map();
   const place=(items,x,step)=>items.forEach((n,i)=>pos.set(n.id,{x,y:90+i*step}));
   place(figures,745,56);
   // Spread concepts across the figure column to reduce overlapping links.
   concepts.forEach((n,i)=>pos.set(n.id,{x:195,y:concepts.length===1?h/2:90+i*(h-165)/(concepts.length-1)}));
   for(const e of edges){
     const a=pos.get(e.to),b=pos.get(e.from);if(!a||!b)continue;
     const path=svgElt('path',{'data-a':e.from,'data-b':e.to,
       class:'edge',d:`M330 ${a.y} C445 ${a.y} 460 ${b.y} 575 ${b.y}`});
     svg.appendChild(path);
   }
   function draw(n,kind){
     const p=pos.get(n.id);if(!p)return;
     const isConcept=kind==='concept';
     const w=isConcept?270:340;
     const x=isConcept?60:575;
     const g=svgElt('g',{class:`node ${kind}`,'data-id':n.id,role:'button',tabindex:0,
       'aria-label':`${kind}: ${n.label}`});
     const rect=svgElt('rect',{x,y:p.y-19,width:w,height:38,rx:9});g.appendChild(rect);
     const text=svgElt('text',{x:x+13,y:p.y+4});
     text.textContent=short(n.label,isConcept?32:43);g.appendChild(text);
     g.addEventListener('click',()=>select(n.id));
     g.addEventListener('keydown',event=>{
       if(event.key==='Enter'||event.key===' '){event.preventDefault();select(n.id);}
     });
     svg.appendChild(g);
   }
   concepts.forEach(n=>draw(n,'concept'));
   figures.forEach(n=>draw(n,'figure'));
   if(!figures.length){
     const t=svgElt('text',{x:575,y:105,fill:'#64748b'});
     t.textContent='No matching figures in this view.';svg.appendChild(t);
   }
   if(!concepts.length){
     const t=svgElt('text',{x:65,y:105,fill:'#64748b'});
     t.textContent='No glossary links selected.';svg.appendChild(t);
   }
   network.appendChild(svg);
 }
 function renderList(){
   list.replaceChildren();
   if(!figures.length){put(list,'p','No matching figures.');return;}
   for(const n of figures){
     const d=data.details[n.id];
     const btn=put(list,'button',null,'figure-item');
     put(btn,'strong',d.label);
     put(btn,'small',`${d.working_group} · ${d.section}`);
     btn.addEventListener('click',()=>select(n.id));
   }
 }
 function setTab(mode){
   const isGraph=mode==='graph';
   graphTab.setAttribute('aria-selected',String(isGraph));
   listTab.setAttribute('aria-selected',String(!isGraph));
   network.hidden=!isGraph;list.hidden=isGraph;
 }
 graphTab.addEventListener('click',()=>setTab('graph'));
 listTab.addEventListener('click',()=>setTab('list'));
 function detailButton(label,fn){
   const b=put(panel,'button',label,'choice');b.addEventListener('click',fn);return b;
 }
 function renderDetails(id){
   const d=data.details[id];if(!d)return;
   panel.replaceChildren();
   put(panel,'h2',d.label);
   put(panel,'p',d.kind==='figure'?`${d.working_group} · ${d.section}`:'IPCC AR6 glossary concept','subtle');
   if(d.kind==='figure'){
     if(d.images&&d.images.length){
       const img=put(panel,'img');img.alt=`IPCC figure: ${d.label}`;
       img.loading='lazy';img.referrerPolicy='no-referrer';
       img.addEventListener('error',()=>img.replaceWith(elt('p',
         'Image preview unavailable. Open the original IPCC figure.','subtle')));
       img.src=d.images[0];
     }else put(panel,'p','No image URL recorded for this figure.','subtle');
     link(panel,'Open original IPCC figure ↗',d.source);
     if(d.title&&d.title!==d.label)put(panel,'p',d.title);
     if(data.match==='caption'&&d.matched&&d.matched.length){
       put(panel,'h3','Caption search matches');
       for(const m of d.matched.slice(0,8))
         put(panel,'div',`“${m.text}” · ${m.start}–${m.end}`,'evidence');
     }
     const pairs=Object.entries(d.evidence||{}).sort((a,b)=>
       (data.details[a[0]]?.label||'').localeCompare(data.details[b[0]]?.label||''));
     if(pairs.length){
       put(panel,'h3','Found in caption');
       for(const [cid,evidence] of pairs){
         detailButton(data.details[cid]?.label||cid,()=>select(cid));
         for(const span of evidence.spans.slice(0,4))
           put(panel,'div',`“${span.text}” · characters ${span.start}–${span.end}`,'evidence');
         if(evidence.count>4)put(panel,'p',`+${evidence.count-4} further mentions`,'subtle');
       }
     }else if(data.match!=='caption')put(panel,'p','No selected glossary matches.','subtle');
   }else if(d.kind==='concept'){
     const linked=figures.filter(n=>(neighbors.get(id)||new Set()).has(n.id));
     put(panel,'p',`${linked.length} figure${linked.length===1?'':'s'} linked in this view.`);
     link(panel,'Open glossary source ↗',d.source);
     put(panel,'h3','Figures mentioning this concept');
     for(const n of linked)detailButton(data.details[n.id].label,()=>select(n.id));
     if(!linked.length)put(panel,'p','No caption matches in this view.','subtle');
   }
 }
 graph();renderList();
 setTab('graph');
 if(data.initial&&data.details[data.initial])select(data.initial);
 else if(figures.length)select(figures[0].id);
 else if(concepts.length)select(concepts[0].id);
 else put(panel,'p','No matching figures for this search.','subtle');
})();
</script>
</body>
</html>'''

def render_html(view: KnowledgeSubgraph) -> str:
    """Create a portable browser document for this RDF subgraph.

    The visualization works offline. Remote IPCC image previews require
    internet access. The HTML embeds no image binaries or full captions.
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
        "__IPCC_FALLBACK__": "".join(items),
        "__IPCC_DATA__": serialized,
    }
    return re.sub(
        r"__IPCC_(?:TITLE|FALLBACK|DATA)__",
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
