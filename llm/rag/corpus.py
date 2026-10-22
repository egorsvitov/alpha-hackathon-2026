"""Pinned local corpus with eager provenance checks and an in-memory lexical index."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from .query import digest, load_config, tokenize


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def parse_date(value):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").date()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("publication timestamp requires timezone")
    return parsed


class Corpus:
    def __init__(self, path, *, config=None, base_dir=None, catalog_path=None, ocr_dir=None):
        self.path = Path(path).resolve()
        self.config = config or load_config()
        self.base_dir = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parents[2]
        self.ocr_dir = Path(ocr_dir) if ocr_dir else self.path.parent / "ocr"
        self.chunks = []
        self.by_id = {}
        self.provenance = {}
        self.available = self.path.is_file()
        self.corpus_version = None
        if not self.available:
            return
        catalog_path = Path(catalog_path) if catalog_path else self.path.parent / "document_catalog.json"
        catalog = json.loads(catalog_path.read_text()) if catalog_path.is_file() else {}
        manifest_path = self.path.parent / "pdf_manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
        manifest_by_hash = {entry["sha256"]: entry for entry in manifest}
        checked_pdf = {}
        pages = {}
        for line_number, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                def unique_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate JSON key")
                        result[key] = value
                    return result
                chunk = json.loads(line, object_pairs_hook=unique_keys,
                                   parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
                self._validate(chunk)
                if chunk["chunk_id"] in self.by_id:
                    raise ValueError("duplicate chunk_id")
                metadata = catalog.get(Path(chunk["source_path"]).name)
                if metadata:
                    # Catalog is authoritative for corrected metadata and exclusions.
                    for key in ("published_at", "date_status", "title", "topics", "historical_only"):
                        chunk[key] = metadata[key]
                    chunk["retrieval_eligible"] = chunk["retrieval_eligible"] and chunk["page"] not in metadata.get("exclude_pages", [])
                    self._validate(chunk)
                pdf = Path(chunk["source_path"])
                pdf = pdf if pdf.is_absolute() else self.base_dir / pdf
                if pdf not in checked_pdf:
                    checked_pdf[pdf] = hashlib.sha256(pdf.read_bytes()).hexdigest() if pdf.is_file() else None
                pdf_hash = checked_pdf[pdf]
                if pdf_hash is not None and pdf_hash != chunk["document_sha256"]:
                    raise ValueError("document_sha256 mismatch")
                source_metadata = manifest_by_hash.get(chunk["document_sha256"])
                if source_metadata and chunk["page"] > source_metadata["pages"]:
                    raise ValueError("page exceeds PDF manifest bounds")
                page_path = self.ocr_dir / chunk["document_id"] / f"page-{chunk['page']:03d}.txt"
                if page_path not in pages:
                    pages[page_path] = page_path.read_text() if page_path.is_file() else None
                page_text = pages[page_path]
                if page_text is not None and page_text[chunk["char_start"]:chunk["char_end"]] != chunk["text"]:
                    raise ValueError("OCR page boundaries mismatch")
                self.provenance[chunk["chunk_id"]] = {
                    "document_hash": "checked" if pdf_hash else "unavailable",
                    "ocr_boundaries": "checked" if page_text is not None else "unavailable"}
                self.chunks.append(chunk)
                self.by_id[chunk["chunk_id"]] = chunk
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid corpus line {line_number}: {exc}") from exc
        self.corpus_version = digest({"chunks": sorted(self.chunks, key=lambda c: c["chunk_id"]),
                                      "catalog": catalog, "pdf_manifest": manifest, "index_config": self.config,
                                      "provenance": self.provenance})
        self.bags = {c["chunk_id"]: Counter(tokenize(c["text"], self.config["mapping"])) for c in self.chunks}
        self.df = Counter(token for bag in self.bags.values() for token in bag)
        self.avg_length = sum(sum(b.values()) for b in self.bags.values()) / max(1, len(self.bags))

    @staticmethod
    def _validate(c):
        for key in ("chunk_id", "document_id", "source_path", "text", "title", "date_status"):
            if not isinstance(c[key], str) or not c[key]:
                raise ValueError(f"invalid {key}")
        for key in ("document_sha256", "text_sha256"):
            if not isinstance(c[key], str) or not re.fullmatch(r"[0-9a-f]{64}", c[key]):
                raise ValueError(f"invalid {key}")
        if text_hash(c["text"]) != c["text_sha256"]:
            raise ValueError("text_sha256 mismatch")
        legacy_id = re.fullmatch(r"([0-9a-f]{12}):p(\d+):(\d+)-(\d+):([0-9a-f]{8})", c["chunk_id"])
        if legacy_id and legacy_id.groups() != (c["document_sha256"][:12], str(c["page"]),
                                                 str(c["char_start"]), str(c["char_end"]), c["text_sha256"][:8]):
            raise ValueError("chunk_id does not identify its content and boundaries")
        for key in ("page", "char_start", "char_end"):
            if type(c[key]) is not int:
                raise ValueError(f"invalid {key}")
        if c["page"] < 1 or c["char_start"] < 0 or c["char_end"] - c["char_start"] != len(c["text"]):
            raise ValueError("invalid Unicode codepoint boundaries")
        for key in ("historical_only", "retrieval_eligible"):
            if type(c[key]) is not bool:
                raise ValueError(f"invalid {key}")
        if not isinstance(c["topics"], list) or any(not isinstance(t, str) for t in c["topics"]):
            raise ValueError("invalid topics")
        if c["published_at"] is not None:
            parse_date(c["published_at"])
        if c["verification_status"] not in ("pending_review", "synthetic", "visually_checked_excerpt"):
            raise ValueError("invalid verification_status")
        if c["verification_status"] == "synthetic" and c.get("source_kind", c.get("source_type")) != "synthetic":
            raise ValueError("synthetic verification requires synthetic source_kind")
        if c["verification_status"] == "visually_checked_excerpt":
            record = c.get("manual_verification", {})
            expected = {"document_sha256": c["document_sha256"], "page": c["page"],
                        "char_start": c["char_start"], "char_end": c["char_end"], "text_sha256": c["text_sha256"]}
            if any(record.get(k) != v for k, v in expected.items()) or not all(record.get(k) for k in ("reviewer", "method", "reviewed_at")):
                raise ValueError("visually checked excerpt requires exact manual verification record")
            parse_date(record["reviewed_at"])

    @property
    def version(self):
        return self.corpus_version

    def get_chunk(self, chunk_id):
        return self.by_id[chunk_id]

    def verify_hit(self, hit):
        chunk = self.get_chunk(hit["chunk_id"])
        for key in ("document_id", "document_sha256", "page", "source_path", "title", "published_at", "date_status"):
            if hit[key] != chunk[key]:
                raise ValueError(f"evidence {key} mismatch")
        start, end = hit["char_start"], hit["char_end"]
        if not chunk["char_start"] <= start < end <= chunk["char_end"]:
            raise ValueError("evidence boundaries outside chunk")
        if hit["quote"] != chunk["text"][start-chunk["char_start"]:end-chunk["char_start"]] or text_hash(hit["quote"]) != hit["text_sha256"]:
            raise ValueError("evidence quote/hash mismatch")
        expected_id = "bank:" + digest([chunk["document_sha256"], chunk["chunk_id"], start, end, hit["text_sha256"]])
        if hit["evidence_id"] != expected_id:
            raise ValueError("evidence_id mismatch")
        synthetic = chunk.get("source_kind") == "synthetic" or chunk.get("source_type") == "synthetic"
        if hit["is_synthetic"] is not synthetic:
            raise ValueError("evidence synthetic provenance mismatch")
        expected_verification = "synthetic" if synthetic else chunk["verification_status"]
        if hit["verification_status"] != expected_verification:
            raise ValueError("evidence verification_status mismatch")
        return True
