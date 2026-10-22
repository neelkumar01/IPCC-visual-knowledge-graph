# IPCC Figure Corpus

The `corpus/` module prepares the collected **IPCC AR6 figure data** for scientific concept extraction and knowledge graph construction.

It takes raw figure records from the three IPCC Working Groups, validates their information, removes unwanted image references, and stores the cleaned data in a structured **SQLite database**.

<br>

### Step 1 — Loading the Raw Figure Data

The process starts with three JSON datasets collected from IPCC AR6 report pages.

| Source | Figures |
|---|---:|
| **WG1** — Physical Science Basis | 510 |
| **WG2** — Impacts, Adaptation and Vulnerability | 453 |
| **WG3** — Mitigation of Climate Change | 347 |
| **Total** | **1,310** |

Each record contains available information such as its figure ID, title, caption, chapter or section, image URLs, and original IPCC source page.

<br>

### Step 2 — Validating the Records

Every figure record is checked against a predefined data structure using **Pydantic**.

The validation checks:

- **Required fields** — important identifiers and source information are present.
- **Data types** — fields contain the expected types of values.
- **Working Group** — the figure belongs to WG1, WG2, or WG3.
- **URLs** — source and image references have valid HTTP(S) formats.
- **Collection timestamps** — dates follow the expected format and include timezone information.

Invalid records are reported instead of silently accepted.

<br>

### Step 3 — Cleaning the Figure Data

After validation, the records are cleaned using a small set of consistent rules.

| Cleaning Rule | What It Does |
|---|---|
| **Decorative image removal** | Removes known IPCC website footer images that are not scientific figures |
| **Duplicate image removal** | Keeps each image URL only once per figure |
| **Duplicate figure detection** | Identifies repeated figure records |
| **Missing caption detection** | Marks figures without captions |
| **Missing image detection** | Marks figures without remaining image references |

The original scientific captions are **not rewritten or automatically generated**. Missing information is recorded rather than invented.

<br>

### Step 4 — Building the Structured Corpus

The cleaned records are stored in a **SQLite database**, with two connected parts:

| Data | What Is Stored |
|---|---|
| **Figures** | Figure identity, Working Group, report section, caption, source information, and availability status |
| **Figure Images** | Image URLs and their positions within each figure |

This makes the figure data easier to search, validate, and reuse in later processing steps.

The original JSON datasets remain unchanged.

<br>

### Step 5 — Checking the Final Dataset

A quality report summarizes the processed figures and any missing or removed information.

| Metric | Count |
|---|---:|
| **Total Cleaned Figures** | **1,310** |
| Available Captions | 1,297 |
| Missing Captions | 13 |
| Figures Without Image References | 2 |
| Image URL References | 1,355 |
| Decorative Image URLs Removed | 3,930 |
| Duplicate Figure Records | 0 |
| Invalid Records | 0 |

*Results are based on the generated corpus quality report dated 20 October 2026.*

<br>

### Final Output

The process produces a **cleaned and validated figure corpus**, which serves as the input for:

- **Concept extraction** — identifying scientific glossary terms in figure captions.
- **Knowledge graph construction** — connecting figures, report information, images, and scientific concepts.

**Scope:** This module processes previously collected figure records. It does not scrape new IPCC pages, download images, verify whether image URLs are accessible, or infer missing scientific information.
