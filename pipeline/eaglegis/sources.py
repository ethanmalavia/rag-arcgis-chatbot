from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PdfAsset:
    path: str
    filename: str
    data: bytes


def iter_local_pdfs(pdf_dir: Path) -> list[PdfAsset]:
    assets: list[PdfAsset] = []
    for path in sorted(pdf_dir.rglob("*.pdf")):
        assets.append(PdfAsset(str(path), path.name, path.read_bytes()))
    return assets


def iter_git_pdfs(ref: str, pdf_dir: str = "pdfs") -> list[PdfAsset]:
    listing = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", ref, pdf_dir],
        text=True,
    )
    paths = [p for p in listing.splitlines() if p.lower().endswith(".pdf")]
    assets: list[PdfAsset] = []
    for path in sorted(paths):
        data = subprocess.check_output(["git", "show", f"{ref}:{path}"])
        assets.append(PdfAsset(path, Path(path).name, data))
    return assets


def read_git_text(ref: str, path: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", f"{ref}:{path}"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return None

