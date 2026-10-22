## Ontology & Knowledge Graph

The `ontology/` module converts processed **IPCC AR6 figures and glossary concepts** into a structured **RDF knowledge graph**

It connects figures with their report information, image references and scientific concepts identified in **figure captions**

<br>

### Ontology Structure

The ontology defines the types of nodes, their connections, and the information stored in the graph.

| Component | Count | Meaning |
|---|---:|---|
| **Classes** | 8 | Types of nodes (e.g., Figure, ScientificConcept, CaptionMention) |
| **Object Properties** | 11 | Relationships between nodes (e.g., `mentionsConcept`) |
| **Data Properties** | 13 | Node information (e.g., `captionStatus`, `imageURL`) |
| **SHACL Shapes** | 7 | Rules for checking graph structure |
| **Topic Collections** | 8 | Curated scientific topic groups using SKOS |

<br>

### Technologies

| Technology | Purpose |
|---|---|
| **Python + RDFLib** | Build the knowledge graph |
| **RDF** | Represent data as connected triples |
| **OWL + RDFS** | Define classes and relationships |
| **SKOS** | Organize scientific concepts into topic collections |
| **SHACL + PySHACL** | Define and optionally run graph validation rules |
| **PROV-O + DCTERMS** | Represent evidence and source metadata |
| **Turtle (`.ttl`)** | Save the RDF graph |

<br>


### Knowledge Graph Statistics

| Metric | Count |
|---|---:|
| **RDF Triples** | **143,082** |
| IPCC Figures | 1,310 |
| Glossary Concepts | 920 |
| Concepts Linked to Figures | 508 |
| Accepted Figure–Concept Links | 6,178 |
| Image URL References | 1,355 |
| Caption Mentions | 10,398 |

*Based on the saved project quality reports. Image references are URLs, not downloaded images.*

<br>

### Ontology Classes (8)

Classes define the different types of nodes in the knowledge graph.

| Class | Meaning |
|---|---|
| `AssessmentReport` | The IPCC AR6 assessment report |
| `WorkingGroup` | One of the three IPCC working groups (WG1, WG2, WG3) |
| `ReportSection` | A report chapter, summary, annex, or atlas section |
| `Chapter` | A chapter within a report section |
| `Figure` | An individual IPCC figure |
| `ImageReference` | An image URL associated with a figure |
| `ScientificConcept` | A scientific term from the IPCC glossary-derived vocabulary |
| `CaptionMention` | An exact occurrence of a matched term in a figure caption |

<br>

### Object Properties (11)

Object properties define how different nodes are connected.

| Property | Connection |
|---|---|
| `partOfReport` | Working Group → Assessment Report |
| `sectionOfWorkingGroup` | Report Section → Working Group |
| `figureInSection` | Figure → Report Section |
| `figureInWorkingGroup` | Figure → Working Group |
| `hasImageReference` | Figure → Image Reference |
| `hasCaptionMention` | Figure → Caption Mention |
| `mentionInFigure` | Caption Mention → Figure |
| `denotesConcept` | Accepted Caption Mention → Scientific Concept |
| `candidateConcept` | Caption Mention → Possible Scientific Concept |
| `mentionsConcept` | Figure → Accepted Scientific Concept |
| `glossaryWorkingGroup` | Scientific Concept → Working Group listed in the glossary |

<br>

### Data Properties (13)

Data properties store information about individual nodes.

| Property | Meaning |
|---|---|
| `workingGroupCode` | Working group identifier, such as WG1 |
| `sectionCode` | Report section identifier |
| `captionText` | Figure caption text (optional in RDF export) |
| `captionStatus` | Whether a caption is available or missing |
| `captionSHA256` | Digital fingerprint of the caption |
| `imageURL` | URL of a figure image |
| `imagePosition` | Position of an image reference |
| `matchedText` | Exact term found in the caption |
| `startOffset` | Starting character position of the term |
| `endOffset` | Ending character position of the term |
| `matchKind` | How the term was matched |
| `resolution` | Whether the match was accepted, ambiguous, or excluded |
| `qualityRule` | Rule used to assess the match |
