# Scientific Concept Extraction

Scientific concepts are extracted from **IPCC AR6 figure captions** using a vocabulary derived from the **IPCC AR6 Glossary**.

The process follows five steps, from collecting scientific terms to creating reliable figure–concept links. It uses **rule-based text matching**, without machine learning or image recognition.

<br>

### Step 1 — Building the Scientific Vocabulary

The starting point is a [structured HTML export of the IPCC AR6 Glossary](https://github.com/TIBHannover/ClimateKG-Data-Bench), provided by TIB Hannover.

From this glossary, we collect:

- **Scientific terms** — such as *Climate change* and *Carbon dioxide*.
- **Definitions** — the meaning of each term.
- **Aliases** — alternative names and explicitly listed abbreviations.
- **Working Group references** — where each term appears in the glossary.

This produces a structured vocabulary of **920 scientific concepts**, each with a unique identifier.

The vocabulary comes from a third-party export of the IPCC glossary, not a direct download from the official IPCC website.

<br>

### Step 2 — Finding Concepts in Figure Captions

We search the collected IPCC figure captions for terms and aliases from this vocabulary.

The matching process:

1. Reads each available figure caption.
2. Searches for complete glossary terms and known aliases.
3. Handles differences in capitalization, spacing, and certain character formats.
4. Records the **exact matched text and its position** in the original caption.

For example, if a caption contains *“carbon dioxide emissions”*, the term **Carbon dioxide** can be identified as a glossary concept.

Only vocabulary-backed terms are matched. The system does not invent new concepts.

<br>

### Step 3 — Linking Matches to Standard Concepts

Different written forms may refer to the same glossary concept.

Each match is therefore connected to its **standard glossary name and unique concept ID**.

For example, `CO₂` and `carbon dioxide` can be linked to the concept **Carbon dioxide** when supported by the vocabulary and matching rules.

If an abbreviation could refer to multiple concepts, the system only resolves it when the caption provides clear evidence, such as an adjacent full-name expansion.

Otherwise, the match remains **ambiguous** instead of being guessed.

<br>

### Step 4 — Checking Match Quality

Additional rules help prevent incorrect figure–concept links.

| Quality Check | What It Does |
|---|---|
| **Complete-term matching** | Avoids matching parts of unrelated words |
| **Abbreviation handling** | Uses explicit caption evidence to resolve ambiguous abbreviations |
| **Chemical notation** | Distinguishes `CO2` from `CO2-eq` |
| **Source filtering** | Excludes misleading matches in caption `Source:` references |
| **Evidence preservation** | Keeps the original matched text, position, and decision |

Matches are marked as **accepted, ambiguous, or excluded**. Only accepted matches are used to create figure–concept links.

<br>

### Step 5 — Creating Figure–Concept Links

Finally, accepted matches are connected to their source figures.

A figure may mention several concepts, and the same concept may appear multiple times in one caption. Repeated mentions are preserved as evidence, but each **figure–concept pair is counted only once**.

The resulting dataset contains:

| Result | Count |
|---|---:|
| Scientific Concepts in Vocabulary | 920 |
| IPCC Figures Processed | 1,310 |
| Figures with Accepted Concepts | 1,215 |
| Distinct Concepts Identified | 508 |
| Caption Mentions Found | 10,398 |
| Accepted Mentions | 10,259 |
| Excluded Mentions | 139 |
| **Unique Figure–Concept Links** | **6,178** |

*Results are based on the generated concept quality report dated 20 October 2026.*

<br>

**Scope:** These links show which scientific concepts are **mentioned in figure captions**. They do not claim that the concepts were visually detected in the images or that a causal relationship exists. Human-reviewed precision and recall have not yet been established.

<br>

### Concept Database (`concepts.db`)

The `data/processed/concepts.db` SQLite database stores scientific concepts, caption matches, and figure–concept relationships.

It contains **8 tables and 1 view**.

| Table / View | Purpose |
|---|---|
| `concepts` | Stores scientific concept IDs, names, and definitions. |
| `concept_aliases` | Stores alternative names and abbreviations for concepts. |
| `concept_working_groups` | Stores the IPCC Working Groups associated with each concept. |
| `concept_source_series` | Stores the original glossary source information. |
| `figures` | Stores figure IDs and source information used during concept matching. |
| `mentions` | Stores matched caption text, character positions, and match status. |
| `mention_candidates` | Stores possible scientific concepts for each caption mention. |
| `metadata` | Stores source information and checksums for tracking data versions. |
| `figure_concepts` *(view)* | Shows unique accepted figure–concept relationships. |

**Database statistics:**
- **920** scientific concepts
- **10,398** caption mentions
- **10,259** accepted mentions
- **6,178** unique accepted figure–concept relationships

*Note: `figure_concepts` is a database view generated from accepted mentions, not a separate stored table.*
