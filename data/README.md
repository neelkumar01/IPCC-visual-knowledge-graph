## Dataset & Storage

The `data/` folder stores figure information collected from the **IPCC Sixth Assessment Report (AR6)** and the processed data used to build the knowledge graph.

It contains **3 raw JSON files, 1 vocabulary JSON file, and 2 SQLite databases**.

<br>

### Raw Data (`raw/`)

These JSON files contain collected figure records from the three IPCC Working Groups.

| File | Working Group | Figures |
|---|---|---:|
| `wg1.json` | Physical Science Basis | 510 |
| `wg2.json` | Impacts, Adaptation and Vulnerability | 453 |
| `wg3.json` | Mitigation of Climate Change | 347 |
| **Total** | | **1,310** |

Each figure record includes information such as its **figure ID, title, caption, report section, image URL references, source URL, and collection metadata**.

<br>

### Processed Data (`processed/`)

| File | Purpose |
|---|---|
| `corpus.db` | Stores cleaned figure information and image references |
| `concepts.db` | Stores scientific concepts, caption matches, and figure–concept links |
| `ipcc_ar6_vocabulary.json` | Stores IPCC glossary-derived scientific terms, definitions, aliases, and source information |

#### `corpus.db` — Figure Database

A SQLite database containing **2 tables**.

| Table | Contents |
|---|---|
| `figures` | Figure IDs, working groups, sections, captions, source URLs, and data availability status |
| `figure_images` | Image URLs, their positions, and the figures they belong to |

**Stored data:** 1,310 figures and 1,355 image URL references.

#### `concepts.db` — Concept Database

A SQLite database containing **8 tables and 1 view**.

| Table / View | Contents |
|---|---|
| `concepts` | Scientific terms, unique IDs, and definitions |
| `concept_aliases` | Alternative names for concepts |
| `concept_working_groups` | Working groups associated with glossary terms |
| `concept_source_series` | Original glossary source-series information |
| `figures` | Figure IDs and source information used for concept matching |
| `mentions` | Exact matched caption text, positions, and match status |
| `mention_candidates` | Possible concepts associated with each mention |
| `metadata` | Source information and checksums for tracking data versions |
| `figure_concepts` *(view)* | Unique accepted figure–concept pairs |

**Stored data:** 920 concepts, 10,398 caption mentions, and 6,178 accepted figure–concept links.

*The `figure_concepts` view is calculated from accepted mentions; it is not a separate stored table.*

#### `ipcc_ar6_vocabulary.json` — Scientific Vocabulary

Contains **920 scientific concepts** derived from an IPCC AR6 glossary export.

| Field | Meaning |
|---|---|
| `term_id` | Unique concept identifier |
| `label` | Main scientific term |
| `definition` | Meaning of the term |
| `aliases` | Alternative names or abbreviations |
| `working_groups` | Associated IPCC Working Groups |
| `source_series` | Original glossary source series |

The file also records vocabulary metadata, source references, and ambiguous spellings.

<br>

### Dataset Statistics

| Metric | Count |
|---|---:|
| **Total Figures** | **1,310** |
| Available Captions | 1,297 |
| Missing Captions | 13 |
| Image URL References | 1,355 |
| Scientific Concepts | 920 |
| Distinct Concepts Matched | 508 |
| Total Caption Mentions | 10,398 |
| Accepted Caption Mentions | 10,259 |
| **Unique Figure–Concept Links** | **6,178** |

*Statistics are based on the generated corpus and concept quality reports dated 20 October 2026.*

<br>

**Data flow:** `Raw JSON → corpus.db → concepts.db → RDF Knowledge Graph`

**Note:** Image URLs are references, not downloaded images. Concept matches come from figure captions, not visual image analysis.
