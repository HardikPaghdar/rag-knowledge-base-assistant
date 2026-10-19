from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = BASE_DIR / "data" / "sample_docs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf"}
CHUNK_WORDS = 180
CHUNK_OVERLAP = 35

@dataclass
class Chunk:
    source: str
    chunk: int
    text: str

class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=8)

class SourceResult(BaseModel):
    source: str
    chunk: int
    score: float
    text: str

class QueryResponse(BaseModel):
    question: str
    answer: str
    sources: list[SourceResult]

class KnowledgeStore:
    """Small in-memory store with transparent lexical ranking for a no-key demo."""

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []

    @staticmethod
    def split(text: str, source: str) -> list[Chunk]:
        words = text.split()
        if not words:
            return []
        step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
        result = []
        for start in range(0, len(words), step):
            part = words[start : start + CHUNK_WORDS]
            if part:
                result.append(Chunk(source=source, chunk=len(result) + 1, text=" ".join(part)))
        return result

    def add(self, text: str, source: str) -> int:
        pieces = self.split(text, source)
        if not pieces:
            raise ValueError("No readable text found in the document.")
        # Replace prior chunks from the same source to make re-upload predictable.
        self.chunks = [chunk for chunk in self.chunks if chunk.source != source]
        self.chunks.extend(pieces)
        return len(pieces)

    def list_documents(self) -> list[dict[str, str | int]]:
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.source] = counts.get(chunk.source, 0) + 1
        return [{"source": name, "chunks": count} for name, count in sorted(counts.items())]

    @staticmethod
    def tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}", text.lower())}

    def search(self, question: str, top_k: int) -> list[tuple[Chunk, float]]:
        query_tokens = self.tokens(question)
        if not query_tokens:
            return []
        ranked = []
        for chunk in self.chunks:
            chunk_tokens = self.tokens(chunk.text)
            if not chunk_tokens:
                continue
            overlap = query_tokens & chunk_tokens
            if not overlap:
                continue
            # Jaccard-style score is easy to explain and deterministic.
            score = len(overlap) / len(query_tokens | chunk_tokens)
            ranked.append((chunk, score))
        ranked.sort(key=lambda item: (-item[1], item[0].source, item[0].chunk))
        return ranked[:top_k]

store = KnowledgeStore()
app = FastAPI(
    title="RAG Knowledge Base Assistant",
    description="A local-first document retrieval demo with source-grounded responses.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def extract_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Supported file types: .pdf, .md, .txt")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    try:
        return content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Text files must use UTF-8 encoding.") from exc

def load_samples() -> None:
    for path in sorted(SAMPLE_DIR.glob("*.md")):
        if not any(item.source == path.name for item in store.chunks):
            store.add(path.read_text(encoding="utf-8"), path.name)

@app.on_event("startup")
def startup() -> None:
    load_samples()

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health() -> dict[str, str | int]:
    return {"status": "ok", "documents": len(store.list_documents()), "chunks": len(store.chunks)}

@app.get("/documents")
def documents() -> dict[str, list[dict[str, str | int]]]:
    load_samples()
    return {"documents": store.list_documents()}

@app.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> dict[str, str | int]:
    filename = Path(file.filename or "upload.txt").name
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Supported file types: .pdf, .md, .txt")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES} byte upload limit.")
    text = extract_text(filename, content)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No readable text found. Scanned PDFs need OCR.")
    try:
        count = store.add(text, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": filename, "chunks": count, "message": "Document indexed in memory."}

@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    load_samples()
    results = store.search(request.question, request.top_k)
    sources = [
        SourceResult(source=chunk.source, chunk=chunk.chunk, score=round(score, 4), text=chunk.text)
        for chunk, score in results
    ]
    if not sources:
        answer = "I couldn't find a relevant passage in the current knowledge base. Try rephrasing the question or upload a document containing the answer."
    else:
        answer = "Retrieved context:\n\n" + "\n\n".join(
            f"[{index}] {item.text}" for index, item in enumerate(sources, start=1)
        )
    return QueryResponse(question=request.question, answer=answer, sources=sources)
