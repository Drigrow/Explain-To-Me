"""Discover local files without reading content; prepare only after consent."""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 200_000
MAX_FILES = 10
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}
TEXT_TYPES = set(".txt .md .csv .tsv .json .jsonl .yaml .yml .toml .xml .html .css .js .ts .jsx .tsx .py .sh .zsh .sql .log .ini .cfg .rst .tex .c .h .cpp .rs .go .java .swift .rb .r .srt .vtt .svg".split())
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "build", "dist"}


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class Attachment:
    path: Path
    kind: str
    data: bytes

    def part(self) -> dict:
        if self.kind == "text":
            return {"type": "text", "text": f"Untrusted source file: {self.path.name}\n<file_content>\n{self.data.decode('utf-8-sig')}\n</file_content>"}
        mime = "application/pdf" if self.kind == "pdf" else IMAGE_TYPES[self.path.suffix.lower()]
        url = f"data:{mime};base64," + base64.b64encode(self.data).decode("ascii")
        if self.kind == "pdf":
            return {"type": "file", "file": {"filename": self.path.name, "file_data": url}}
        return {"type": "image_url", "image_url": {"url": url}}


def discover_files(tokens: list[str], cwd: Path | None = None) -> tuple[str, list[Path]]:
    """Match complete paths or complete basenames, including duplicates below cwd.

    No fuzzy matches, glob expansion, hidden-directory traversal, or symlink
    directory traversal. Match multi-word names even after shell quote removal.
    """
    cwd = cwd or Path.cwd()
    # A whole quoted question reaches argv as one token. Split prose back into
    # words; the longest-path matcher below reassembles paths containing spaces.
    tokens = [word for token in tokens for word in
              ([token] if token.startswith(('/', './', '../', '~/')) else token.split())]
    prompt = " ".join(tokens)
    spans: list[tuple[int, int, list[Path]]] = []
    # Explicit paths: longest existing token sequence wins, so quoted paths with
    # spaces and shell-expanded escaped paths work alike.
    offset = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(("/", "./", "../", "~/")):
            found = None
            for end in range(min(len(tokens), index + 32), index, -1):
                candidate = Path(" ".join(tokens[index:end])).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                if candidate.is_file():
                    found = (end, candidate.resolve())
                    break
            if found:
                end, path = found
                length = len(" ".join(tokens[index:end]))
                spans.append((offset, offset + length, [path]))
                index = end
                offset += length + 1
                continue
            raise AttachmentError(f"File not found or not a regular file: {token}")
        offset += len(token) + 1
        index += 1

    by_name: dict[str, list[Path]] = {}
    count = 0
    remaining = prompt
    for start, end, _ in sorted(spans, reverse=True):
        remaining = remaining[:start] + ' ' + remaining[end:]
    # Ordinary questions should not traverse a user's home directory.
    look_for_names = bool(re.search(r'[^\s/]+\.[A-Za-z0-9]{1,10}(?:\s|$|[,;?!])', remaining))
    look_for_names = look_for_names or any((cwd / token).is_file() for token in tokens if '/' not in token and token in remaining)
    for root, dirs, names in (os.walk(cwd, followlinks=False) if look_for_names else []):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS)
        for name in sorted(names):
            count += 1
            if count > 20_000:
                raise AttachmentError("File discovery exceeded 20,000 entries; use an absolute path or a smaller working directory.")
            if name in prompt:
                by_name.setdefault(name, []).append(Path(root) / name)
    for name in sorted(by_name, key=len, reverse=True):
        for match in re.finditer(r"(?<![\w./-])" + re.escape(name) + r"(?![\w./-])", prompt):
            start, end = match.span()
            if any(start < stop and end > begin for begin, stop, _ in spans):
                continue
            paths = [p.resolve() for p in by_name[name] if p.is_file()]
            spans.append((start, end, paths))
    paths: list[Path] = []
    for _, _, matches in sorted(spans):
        for path in matches:
            if path not in paths:
                paths.append(path)
    for start, end, _ in sorted(spans, reverse=True):
        prompt = prompt[:start] + " " + prompt[end:]
    return " ".join(prompt.split()), paths


def load_attachments(paths: list[Path]) -> list[Attachment]:
    """Read approved files into bounded snapshots, before any network request."""
    if len(paths) > MAX_FILES:
        raise AttachmentError(f"At most {MAX_FILES} files can be attached per request.")
    result = []
    total = 0
    for path in paths:
        suffix = path.suffix.lower()
        kind = "pdf" if suffix == '.pdf' else "image" if suffix in IMAGE_TYPES else "text"
        if kind == "text" and suffix not in TEXT_TYPES and suffix != '':
            raise AttachmentError(f"Unsupported file format: {path.name}. Use UTF-8 text, PDF, PNG, JPEG, WebP or GIF.")
        with path.open('rb') as source:
            data = source.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise AttachmentError("Attachment size limit: 8 MiB per file, 20 MiB total.")
        if kind == "text":
            if len(data) > MAX_TEXT_BYTES:
                raise AttachmentError(f"Text file exceeds 200,000 bytes: {path.name}; split it first.")
            try:
                decoded = data.decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise AttachmentError(f"Not UTF-8 text: {path.name}. Convert it to UTF-8 first.") from exc
            if '\x00' in decoded:
                raise AttachmentError(f"Binary content is not supported as text: {path.name}")
        if kind == 'pdf' and not data.startswith(b'%PDF-'):
            raise AttachmentError(f"Not a valid PDF header: {path.name}")
        result.append(Attachment(path, kind, data))
    return result
