# Third-party notices: CPU news embedding experiment

Model: intfloat/multilingual-e5-small (MIT per model card), revision 614241f622f53c4eeff9890bdc4f31cfecc418b3. Official ONNX INT8 export and tokenizer; hashes in ignored artifacts/model/manifest.json.
Model source: https://huggingface.co/intfloat/multilingual-e5-small

News sources: https://www.interfax.ru/rss.asp ; https://lenta.ru/rss/news ; https://www.kommersant.ru/RSS/news.xml . Public news excerpts/texts retain their original attribution; no redistribution license is asserted. Corpus is local and excluded from Git.
Company sample: supplied hackathon dataset, local only, not redistributed. Corpus collection/packaging and benchmark code are authored for this experiment. No paid LLM calls.

## Installed distributions

Exact dependency resolution: uv.lock. Licenses below are declarations from installed package metadata, not independent legal verification. Missing declarations are explicitly identified.

- alpha-personalization 0.1.0: not declared in distribution metadata; consult upstream license.
- anyio 4.15.1: MIT. Documentation, https://anyio.readthedocs.io/en/latest/
- attrs 26.1.0: MIT. Documentation, https://www.attrs.org/
- babel 2.18.0: BSD-3-Clause. https://babel.pocoo.org/
- certifi 2026.10.19: MPL-2.0. https://github.com/certifi/python-certifi
- charset-normalizer 3.5.2: MIT. Changelog, https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md
- click 8.5.0: BSD-3-Clause. Changes, https://click.palletsprojects.com/page/changes/
- courlan 1.4.0: Apache-2.0. Homepage, https://github.com/adbar/courlan
- dateparser 1.4.3: BSD-3-Clause. Source, https://github.com/scrapinghub/dateparser
- feedparser 6.0.14: BSD-2-Clause. https://github.com/kurtmckee/feedparser
- feedparser-sgmllib 2.1.0: PSF-2.0. Changelog, https://feedparser-sgmllib.readthedocs.io/en/latest/changelog/
- filelock 4.2.4: MIT. Documentation, https://py-filelock.readthedocs.io
- flatbuffers 25.12.19: Apache 2.0. https://google.github.io/flatbuffers/
- fsspec 2026.9.0: BSD-3-Clause. Changelog, https://filesystem-spec.readthedocs.io/en/latest/changelog.html
- h11 0.16.0: MIT. https://github.com/python-hyper/h11
- hf-xet 1.7.0: Apache-2.0. Documentation, https://huggingface.co/docs/hub/xet/index
- htmldate 1.11.0: Apache-2.0. Homepage, https://htmldate.readthedocs.io
- httpcore 1.0.9: BSD-3-Clause. Documentation, https://www.encode.io/httpcore
- httpcore2 2.16.0: BSD-3-Clause. Changelog, https://github.com/pydantic/httpx2/blob/main/src/httpcore2/CHANGELOG.md
- httpx 0.28.1: BSD-3-Clause. Changelog, https://github.com/encode/httpx/blob/master/CHANGELOG.md
- httpx2 2.16.0: BSD-3-Clause. Changelog, https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md
- huggingface_hub 2.2.0: Apache-2.0. https://github.com/huggingface/huggingface_hub
- idna 3.20: BSD-3-Clause. Changelog, https://github.com/kjd/idna/blob/master/HISTORY.md
- jsonschema 4.25.1: MIT. Homepage, https://github.com/python-jsonschema/jsonschema
- jsonschema-specifications 2025.9.1: MIT. Documentation, https://jsonschema-specifications.readthedocs.io/
- jusText 3.0.2: The BSD 2-Clause License. https://github.com/miso-belica/jusText
- lxml 6.1.3: BSD-3-Clause. https://lxml.de/
- lxml_html_clean 0.4.5: BSD-3-Clause. https://github.com/fedora-python/lxml_html_clean/
- numpy 2.5.4: BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0. homepage, https://numpy.org
- onnxruntime 1.31.0: MIT License. https://onnxruntime.ai
- packaging 26.3: Apache-2.0 OR BSD-2-Clause. Documentation, https://packaging.pypa.io/
- protobuf 7.36.2: 3-Clause BSD License. https://developers.google.com/protocol-buffers/
- python-dateutil 2.9.0.post0: Dual License. https://github.com/dateutil/dateutil
- pytz 2026.5: MIT. http://pythonhosted.org/pytz
- PyYAML 6.0.3: MIT. https://pyyaml.org/
- referencing 0.37.0: MIT. Documentation, https://referencing.readthedocs.io/
- regex 2026.9.29: Apache-2.0 AND CNRI-Python. Homepage, https://github.com/mrabarnett/mrab-regex
- rfc3339-validator 0.1.4: MIT license. https://github.com/naimetti/rfc3339-validator
- rfc3986-validator 0.1.1: MIT license. https://github.com/naimetti/rfc3986-validator
- rpds-py 2026.9.1: MIT. Documentation, https://rpds.readthedocs.io/
- six 1.17.0: MIT. https://github.com/benjaminp/six
- tld 0.13.2: MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later. Bug Tracker, https://github.com/barseghyanartur/tld/issues
- tokenizers 0.23.3: Apache Software License. Homepage, https://github.com/huggingface/tokenizers
- tqdm 4.70.1: MPL-2.0 AND MIT. homepage, https://tqdm.github.io
- trafilatura 2.3.1: Apache-2.0. Homepage, https://trafilatura.readthedocs.io
- truststore 0.10.5: MIT. Documentation, https://truststore.readthedocs.io
- typing_extensions 4.16.0: PSF-2.0. Bug Tracker, https://github.com/python/typing_extensions/issues
- tzlocal 5.4.4: MIT. Source code, https://github.com/regebro/tzlocal
- urllib3 2.8.0: MIT. Changelog, https://github.com/urllib3/urllib3/blob/main/CHANGES.rst

## Additional models for dimension comparison

- `intfloat/multilingual-e5-base` — MIT, revision `d128750597153bb5987e10b1c3493a34e5a4502a`, 768 dimensions. Model card: https://huggingface.co/intfloat/multilingual-e5-base
- `intfloat/multilingual-e5-large` — MIT, revision `3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3`, 1024 dimensions. Model card: https://huggingface.co/intfloat/multilingual-e5-large

Both use the authors' `onnx/model_qint8_avx512_vnni.onnx` plus tokenizer from the same revision. Model cards are preserved locally with downloaded files; exact file SHA256 and byte sizes are in ignored `artifacts/model-768/manifest.json` and `artifacts/model-1024/manifest.json`. Dependencies remain those in the existing uv.lock; no new packages are required.
