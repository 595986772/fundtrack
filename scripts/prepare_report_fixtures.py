"""Download the fixed public reports on demand; verify hashes before parsing."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile

from pypdf import PdfReader

from app.services.fund_report_source import FundReportSource


def prepare():
    directory = Path(__file__).resolve().parents[1] / "data/evals/fund_reports_v1"
    target = directory / "documents.json"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    source = FundReportSource()
    documents = []
    # A partial download never becomes a valid documents.json fixture.
    with tempfile.TemporaryDirectory(dir=directory, prefix="download-") as temporary:
        staged = Path(temporary)
        for item in manifest["documents"]:
            relative = Path(item["pdf_path"])
            if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".pdf":
                raise ValueError("Invalid fixture path")
            existing = directory / relative
            content = existing.read_bytes() if existing.exists() else source.download(item["source_url"])
            if hashlib.sha256(content).hexdigest() != item["content_hash"]:
                raise ValueError(f"Fixture hash mismatch: {relative}")
            reader = PdfReader(BytesIO(content))
            pages = [{"page": index + 1, "text": page.extract_text() or ""}
                     for index, page in enumerate(reader.pages)]
            documents.append({**item, "pages": pages})
            staged_pdf = staged / relative
            staged_pdf.parent.mkdir(parents=True, exist_ok=True)
            staged_pdf.write_bytes(content)
            print(f"Verified {item['fund_code']} {item['period']}: {len(pages)} pages")
        for item in documents:
            destination = directory / item["pdf_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staged / item["pdf_path"]).replace(destination)
        manifest["documents"] = documents
        output = staged / "documents.json"
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.replace(target)


if __name__ == "__main__":
    prepare()
