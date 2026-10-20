"""OCR PDFs and produce page-grounded draft chunks. No LLM calls.

Requires pdftotext, pdftoppm and Tesseract with rus+eng traineddata.
OCR text is NOT verified bank opinion: every chunk starts pending_review.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ocr_page(task):
    path, number, doc_hash = task
    dest = Path("data/processed/ocr") / doc_hash[:12] / f"page-{number:03d}.txt"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="alpha-ocr-page-") as tmp:
            prefix = str(Path(tmp) / "page")
            subprocess.run(["pdftoppm", "-f", str(number), "-l", str(number),
                            "-r", "180", "-singlefile", "-png", str(path), prefix],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            result = subprocess.run([os.environ.get("TESSERACT_BIN", "tesseract"),
                                     prefix+".png", "stdout", "-l", "rus+eng", "--psm", "3"],
                                    check=True, text=True, capture_output=True)
            dest.write_text(result.stdout)
    return number, dest.read_text()


def chunk_page(text, size=2200, overlap=250):
    start = 0
    while start < len(text):
        end = min(start+size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start+size//2, end)
            if boundary > start:
                end = boundary
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start+1, end-overlap)


def main():
    manifest, chunks = [], []
    catalog = json.loads(Path("data/processed/document_catalog.json").read_text())
    for path in sorted(Path("data/analytics").glob("*.pdf")):
        metadata = catalog[path.name]
        doc_hash = sha(path.read_bytes())
        raw = subprocess.check_output(["pdftotext", str(path), "-"]).decode()
        pages = raw.split("\f")
        if pages[-1] == "":
            pages.pop()
        tasks = [(path, i+1, doc_hash) for i in range(len(pages))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            texts = list(pool.map(ocr_page, tasks))
        for page, text in texts:
            for start, end, content in chunk_page(text):
                chunk_id = f"{doc_hash[:12]}:p{page}:{start}-{end}:{sha(content.encode())[:8]}"
                chunks.append({
                    "chunk_id": chunk_id, "document_id": doc_hash[:12],
                    "source_path": str(path), "document_sha256": doc_hash,
                    "page": page, "char_start": start, "char_end": end,
                    "text": content, "text_sha256": sha(content.encode()),
                    "extraction": "tesseract-rus-eng-180dpi-psm3",
                    "verification_status": "pending_review",
                    "published_at": metadata["published_at"],
                    "date_status": metadata["date_status"],
                    "title": metadata["title"],
                    "topics": metadata["topics"],
                    "historical_only": metadata["historical_only"],
                    "retrieval_eligible": page not in metadata["exclude_pages"],
                    "source_type": "bank_analytics_case_material",
                })
        manifest.append({"path": str(path), "sha256": doc_hash, "pages": len(pages),
                         "embedded_text_chars": len(raw.strip("\f\n ")),
                         "ocr_chars": sum(len(t) for _, t in texts)})
        print(f"{path.name}: {len(pages)} pages, {sum(len(t) for _, t in texts)} OCR chars", flush=True)
    Path("data/processed/rag_chunks.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False)+"\n" for c in chunks))
    Path("data/processed/pdf_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    print(f"Saved {len(chunks)} draft chunks; manual source review required.", flush=True)


if __name__ == "__main__":
    main()
