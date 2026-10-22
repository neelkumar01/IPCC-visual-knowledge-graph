<h3 align="center">IPCC Visual Knowledge Graph</h1>

<p align="center">Connecting IPCC climate figures with scientific concepts through a structured, evidence backed knowledge graph</p>

<p align="center"><img height="500" alt="image" src="https://github.com/user-attachments/assets/6cdd657a-a839-4dd6-be98-1afd08200a09" />
</p>


<br>

The **IPCC Visual Knowledge Graph** is a project that organizes figures from the **IPCC Sixth Assessment Report (AR6)** across all three Working Group 

It uses figure captions and an IPCC glossary derived vocabulary to identify scientific concepts and connect them to their source figures in a **machine readable RDF knowledge graph**

The project focuses on **caption based concept matching** not visual image analysis or AI generated scientific relationships

<br>

### Project Overview

The **IPCC Visual Knowledge Graph** aims to make scientific figures from the **IPCC Sixth Assessment Report (AR6)** easier to explore through their associated climate science concepts

The project collects figure captions, metadata and image URLs from all three IPCC Working Groups. It then identifies scientific terms in the captions using an IPCC glossary derived vocabulary and connects figures, concepts and report information in a structured **RDF knowledge graph**

This enables users to explore which figures mention specific scientific concepts while preserving links to the original IPCC sources

<br>

### Key Features

- **Automated Figure Collection**

Collects figure captions, metadata and image URLs from all three IPCC AR6 Working Groups

- **Structured Figure Corpus**

Cleans, validates, and stores collected figure information in a SQLite database

- **Glossary Based Concept Extraction**

Identifies scientific terms in captions using an IPCC AR6 glossary-derived vocabulary

- **Evidence Backed Knowledge Graph**

Connects figures, scientific concepts, report sections, and working groups through RDF relationships

- **Standardized Semantic Structure**

Uses RDF, OWL, RDFS and SKOS to represent and organize graph information

- **Data Quality & Traceability**

Preserves original source references and generates validation reports to support reproducibility

<br>

### References

- IPCC Sixth Assessment Report (AR6) — https://www.ipcc.ch/assessment-report/ar6/

- IPCC AR6 Glossary — https://apps.ipcc.ch/glossary/searchlatest.php

- TIB Hannover ClimateKG Data Bench — Glossary-derived HTML source

- RDF 1.1 — https://www.w3.org/TR/rdf11-concepts/

- OWL 2 — https://www.w3.org/TR/owl2-overview/

- SKOS — https://www.w3.org/TR/skos-reference/

- SHACL — https://www.w3.org/TR/shacl/
