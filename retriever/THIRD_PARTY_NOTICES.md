# Local runtime dependency inventory

Generated from installed distribution metadata after `uv sync --extra embeddings`; exact resolution is in root `uv.lock`. This is an inventory of declared upstream licenses, not a replacement for their license texts.

| Distribution | Version | Declared license |
|---|---|---|
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| filelock | 4.2.4 | MIT |
| flatbuffers | 25.12.19 | Apache 2.0 |
| fsspec | 2026.9.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.7.0 | Apache-2.0 |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| huggingface_hub | 2.2.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| jsonschema | 4.25.1 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.31.0 | MIT License |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| protobuf | 7.36.2 | 3-Clause BSD License |
| PyYAML | 6.0.3 | MIT |
| referencing | 0.37.0 | MIT |
| rfc3339-validator | 0.1.4 | MIT license |
| rfc3986-validator | 0.1.1 | MIT license |
| rpds-py | 2026.9.1 | MIT |
| six | 1.17.0 | MIT |
| tokenizers | 0.23.3 | License :: OSI Approved :: Apache Software License |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| truststore | 0.10.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

## Model and data

- Model: intfloat/multilingual-e5-small, MIT, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`. Artifact checksums are pinned in `embeddings.py` and recorded in build reports. Upstream: https://huggingface.co/intfloat/multilingual-e5-small.
- Company/payment CSVs and QA transcript: supplied hackathon materials; no new distribution rights are asserted. Exact source hashes are recorded in generated manifests. GROUP_ID and individual-person relations are not newly embedded by this module.
- News: existing local corpus from Interfax, Kommersant and Lenta. Collection URLs/timestamps and HTML/RSS provenance remain in the original experiment artifacts; no new crawling was performed. News content is not covered by the model or library licenses. See the experiment THIRD_PARTY_NOTICES.md.
- Synthetic unit fixtures: repository-authored company and B6 fixtures, explicitly separated from unlabelled real evaluation data.
- Standard-library SQLite, decimal, hashlib and related Python modules: Python/runtime licensing. No external agent framework, paid LLM, GNN library, vector DB or ANN dependency added.
