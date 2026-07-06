#!/usr/bin/env python3
"""
RAG pipeline test suite — comprehensive report.
Run:     python rag_test.py
Results: rag_test_results.txt

Each test records: what was measured, the raw result, and a pass/fail verdict
with an explanation so you can judge whether to tune something.
"""

from dotenv import load_dotenv
load_dotenv()

import os, sys, time, textwrap
import psycopg2
from datetime import datetime
from pathlib import Path

OUTPUT_FILE = "rag_test_results.txt"
lines = []

def w(text=""):
    print(text)
    lines.append(text)

def dump():
    with open(OUTPUT_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


# ── result tracker ────────────────────────────────────────────────────────────

results = []   # list of dicts: {name, metric, result, passed, explanation}

def record(name, metric, result, passed, explanation):
    results.append(dict(name=name, metric=metric, result=result,
                        passed=passed, explanation=explanation))

def report_all():
    w("\n" + "═" * 72)
    w("DETAILED RESULTS")
    w("═" * 72)
    for i, r in enumerate(results, 1):
        verdict = "PASS" if r["passed"] else "FAIL"
        marker  = "✓" if r["passed"] else "✗"
        w(f"\n{marker} [{verdict}]  #{i:02d}  {r['name']}")
        w(f"  Metric      : {r['metric']}")
        w(f"  Result      : {r['result']}")
        w(f"  Explanation : {r['explanation']}")
    total  = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    w("\n" + "═" * 72)
    w(f"SUMMARY: {passed}/{total} passed   {failed} failed")
    w("═" * 72)


# ── fixtures ──────────────────────────────────────────────────────────────────

QCD_NOTE = textwrap.dedent("""\
    ---
    type: class_note
    title: Quantum Chromodynamics Notes
    tags: [physics, QCD, quarks]
    ai_generated: false
    reviewed: true
    ---

    # Quarks and Gluons

    Quantum chromodynamics (QCD) is the theory of the strong nuclear force.
    Quarks carry color charge: red, green, or blue.
    Gluons are the force carriers of the strong interaction.

    # Color Confinement

    Color confinement means quarks cannot exist in isolation — only
    color-neutral combinations form hadrons such as protons and neutrons.
""")

PERSON_NOTE = textwrap.dedent("""\
    ---
    type: person
    title: Dr. Valentina Rossi
    tags: [professor, advisor]
    ai_generated: false
    reviewed: true
    ---

    # Dr. Valentina Rossi

    Dr. Rossi is my thesis advisor in the materials science department.
    Office: Engineering Hall 412. Office hours: Tuesday 3-5pm.
    Research focus: superconducting thin films and magnetron sputtering.
""")

DELETE_NOTE = textwrap.dedent("""\
    ---
    type: reference
    title: Python Decorators Reference
    tags: [python, programming]
    ai_generated: false
    reviewed: true
    ---

    # Python Decorators

    Decorators are a way to modify or wrap functions using a callable.
    They are applied with the @ syntax above a function definition.

    # functools.wraps

    functools.wraps preserves the original function's metadata when wrapping,
    including __name__ and __doc__. Always use it when writing decorator factories.
""")

_TEST_FILES: list[str] = []

def write_test_note(folder, filename, content):
    from services.vault import storage
    key = f"{folder}/{filename}"
    storage.upload(key, content.encode("utf-8"), content_type="text/markdown")
    _TEST_FILES.append(key)
    return key

def _qcd_key():
    """The QCD note is reused across several component tests within one run —
    write it once, on first use."""
    from services.vault import storage
    key = "classes/_test_qcd.md"
    if not storage.exists(key):
        write_test_note("classes", "_test_qcd.md", QCD_NOTE)
    return key

def cleanup():
    from services.rag.store import delete_by_source
    from services.vault import storage
    removed = 0
    for key in _TEST_FILES:
        folder = key.split("/", 1)[0]
        try:
            delete_by_source(folder, key)
        except Exception:
            pass
        try:
            storage.delete(key)
            removed += 1
        except Exception:
            pass
    w(f"\n  Removed {removed} test file(s) from vault Storage and the vector store.")

def get_db():
    # Reuse db.py's _PGConnection wrapper (gives .execute() with ?->%s rewriting
    # and RealDictCursor rows) directly, bypassing get_db()'s Flask g-context
    # dependency — this script runs with no Flask app/request context.
    from db import _PGConnection
    return _PGConnection(psycopg2.connect(os.environ["DATABASE_URL"]))


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT TESTS
# ═════════════════════════════════════════════════════════════════════════════

def test_chunker_heading_split():
    from services.rag.chunker import chunk_bytes
    key = write_test_note("classes", "_test_qcd.md", QCD_NOTE)
    chunks = chunk_bytes(QCD_NOTE.encode("utf-8"), key, "classes")
    headings = [c.heading for c in chunks if c.heading]
    passed = len(chunks) >= 2
    record(
        name="Chunker — splits by headings",
        metric="Number of chunks produced from a 2-heading note",
        result=f"{len(chunks)} chunk(s), headings: {headings}",
        passed=passed,
        explanation=(
            "A note with 2 H1 headings should produce at least 2 chunks. "
            + ("Correct." if passed else f"Only {len(chunks)} chunk(s) produced — heading splitter may be broken.")
        ),
    )

def test_chunker_frontmatter():
    from services.rag.chunker import chunk_bytes
    key = _qcd_key()
    chunks = chunk_bytes(QCD_NOTE.encode("utf-8"), key, "classes")
    c = chunks[0]
    tags_ok = "physics" in c.tags
    ai_ok   = not c.ai_generated
    rev_ok  = c.reviewed
    passed  = tags_ok and ai_ok and rev_ok
    record(
        name="Chunker — frontmatter parsed correctly",
        metric="tags, ai_generated, reviewed fields on first chunk",
        result=f"tags={c.tags}  ai_generated={c.ai_generated}  reviewed={c.reviewed}",
        passed=passed,
        explanation=(
            "Frontmatter YAML should be parsed and attached to each chunk's metadata. "
            + ("All fields correct." if passed
               else f"Mismatch — tags_ok={tags_ok}, ai_ok={ai_ok}, rev_ok={rev_ok}.")
        ),
    )

def test_chunker_size_limit():
    from services.rag.chunker import chunk_bytes, CHUNK_MAX_CHARS
    key = _qcd_key()
    chunks = chunk_bytes(QCD_NOTE.encode("utf-8"), key, "classes")
    oversized = [len(c.text) for c in chunks if len(c.text) > CHUNK_MAX_CHARS + 100]
    passed = len(oversized) == 0
    record(
        name="Chunker — no chunk exceeds size limit",
        metric=f"Max allowed: {CHUNK_MAX_CHARS} chars. Oversized chunks found:",
        result=f"{len(oversized)} oversized chunk(s). Sizes: {[len(c.text) for c in chunks]}",
        passed=passed,
        explanation=(
            "Long sections should be split by paragraph to stay under the token budget. "
            + ("All chunks within limit." if passed
               else f"{len(oversized)} chunk(s) over limit — paragraph splitter may be broken.")
        ),
    )

def test_indexer_dynamic_collection():
    from services.rag.indexer import _collection_for
    cases = [
        ("inbox",      "inbox/x.md"),
        ("school",     "school/x.md"),
        ("custom-new", "custom-new/x.md"),
    ]
    wrong = [(name, _collection_for(key)) for name, key in cases if _collection_for(key) != name]
    passed = len(wrong) == 0
    record(
        name="Indexer — dynamic collection mapping",
        metric="Any vault subfolder should map to itself as a collection name",
        result=f"All OK" if passed else f"Wrong mappings: {wrong}",
        passed=passed,
        explanation=(
            "No static allowlist — any new section the user creates should auto-route. "
            + ("Correct for inbox, school, and a custom folder." if passed
               else f"These folders mapped incorrectly: {wrong}")
        ),
    )

def test_indexer_runs():
    from services.rag.indexer import index_file
    key = _qcd_key()
    error = None
    try:
        index_file(key)
    except Exception as e:
        error = str(e)
    passed = error is None
    record(
        name="Indexer — index_file() completes without error",
        metric="index_file() called on a valid .md file",
        result=f"Success" if passed else f"Exception: {error}",
        passed=passed,
        explanation=(
            "index_file() should chunk the file, embed it, and write to the vector store. "
            + ("Completed cleanly." if passed else f"Crashed with: {error}")
        ),
    )

def test_indexer_idempotency():
    from services.rag.indexer import index_file
    from services.rag.store import count_by_source
    key = _qcd_key()
    index_file(key)
    count_first  = count_by_source("classes", key)
    index_file(key)
    count_second = count_by_source("classes", key)
    passed = count_first > 0 and count_first == count_second
    record(
        name="Indexer — re-indexing same file is idempotent (no duplicates)",
        metric="Chunk count for source_path after 1st index == count after 2nd index",
        result=f"After 1st index: {count_first} chunk(s)  |  After 2nd index: {count_second} chunk(s)",
        passed=passed,
        explanation=(
            "Every vault write path reindexes explicitly after writing, so indexing must be safe to call repeatedly. "
            "upsert() with stable IDs (md5(path)_N) should overwrite, not accumulate. "
            + ("Chunk count stable — upsert working correctly." if passed
               else f"Count changed ({count_first} → {count_second}) — duplicates are being created.")
        ),
    )

def test_indexer_delete():
    from services.rag.indexer import index_file
    from services.rag.retriever import retrieve
    from services.rag.store import delete_by_source
    path = write_test_note("classes", "_test_delete.md", DELETE_NOTE)
    index_file(path)
    before = retrieve("python decorators functools wraps", k=10)
    found_before = any("_test_delete" in r.source_path for r in before)
    delete_by_source("classes", path)
    after = retrieve("python decorators functools wraps", k=10)
    found_after = any("_test_delete" in r.source_path for r in after)
    passed = found_before and not found_after
    record(
        name="Indexer — delete_by_source removes chunks from retrieval",
        metric="Note retrievable before deletion, absent after deletion",
        result=f"Found before: {found_before}  |  Found after: {found_after}",
        passed=passed,
        explanation=(
            "Deleting a vault file should remove its chunks from the vector store so stale content "
            "is never returned. "
            + ("Deletion confirmed — note no longer retrievable." if passed
               else f"Issue — found_before={found_before}, found_after={found_after}. "
                    "delete_by_source may not be filtering by source_path correctly.")
        ),
    )

def test_retrieval_relevant():
    from services.rag.retriever import retrieve
    results = retrieve("quarks and gluons strong nuclear force", k=5)
    sources = [Path(r.source_path).name for r in results]
    hit = any("_test_qcd" in s for s in sources)
    dist = results[0].distance if results else None
    passed = hit
    record(
        name="Retrieval — relevant query surfaces correct note",
        metric="Does '_test_qcd.md' appear in top-5 results for a matching query?",
        result=f"Top sources: {sources[:3]}  |  top distance: {dist:.4f}" if dist else "No results",
        passed=passed,
        explanation=(
            "A query about QCD/quarks should retrieve the QCD test note. "
            + (f"Found it at distance {dist:.4f} (lower=more similar, cosine)." if passed
               else "QCD note not found — embedding or indexing may have failed.")
        ),
    )

def test_retrieval_irrelevant():
    from services.rag.retriever import retrieve
    results = retrieve("personal habits and morning routines", k=3)
    sources = [Path(r.source_path).name for r in results]
    hit = any("_test_qcd" in s for s in sources)
    passed = not hit
    record(
        name="Retrieval — irrelevant query does not surface QCD note at top",
        metric="Is '_test_qcd.md' absent from top-3 results for an unrelated query?",
        result=f"Top sources: {sources}",
        passed=passed,
        explanation=(
            "A note about quarks should not rank highly for a query about morning routines. "
            + ("Correct — QCD note absent from top-3." if passed
               else "QCD note appeared for an unrelated query — embeddings may be noisy.")
        ),
    )

def test_retrieval_keyword_boost():
    from services.rag.retriever import retrieve
    results = retrieve("color confinement hadrons quarks", k=5)
    top = results[0] if results else None
    passed = top is not None and "_test_qcd" in top.source_path
    record(
        name="Retrieval — keyword hybrid re-ranking",
        metric="Does a query with exact keywords from the note rank it #1?",
        result=f"Top result: {Path(top.source_path).name if top else 'none'}  dist={top.distance:.4f}" if top else "No results",
        passed=passed,
        explanation=(
            "Keyword overlap should boost the cosine distance score, moving the exact-match "
            "note to position #1. "
            + ("Keyword boost working." if passed
               else "Expected QCD note at #1 but got something else — keyword re-ranker may be off.")
        ),
    )

def test_retrieval_cross_collection():
    from services.rag.indexer import index_file
    from services.rag.retriever import retrieve
    path = write_test_note("people", "_test_person.md", PERSON_NOTE)
    index_file(path)
    time.sleep(0.3)
    results = retrieve("thesis advisor office hours magnetron", k=5)
    sources = [Path(r.source_path).name for r in results]
    hit = any("_test_person" in s for s in sources)
    passed = hit
    record(
        name="Retrieval — cross-collection (person note found)",
        metric="Does a query about an advisor surface the people/ collection note?",
        result=f"Top sources: {sources[:3]}",
        passed=passed,
        explanation=(
            "Retrieval searches all vector store collections by default, not just 'classes'. "
            + ("People note surfaced correctly." if passed
               else "Person note not found — cross-collection search may not be working.")
        ),
    )

def test_retrieval_scoped():
    from services.rag.retriever import retrieve
    results = retrieve("quarks gluons", k=5, collections=["classes"])
    wrong_col = [r.collection for r in results if r.collection != "classes"]
    passed = len(wrong_col) == 0
    record(
        name="Retrieval — scoped to single collection",
        metric="When collections=['classes'] is passed, do all results come from 'classes'?",
        result=f"Collections in results: {list({r.collection for r in results})}",
        passed=passed,
        explanation=(
            "Passing collections=['classes'] should restrict search to that collection only. "
            + ("All results from 'classes'." if passed
               else f"Got results from wrong collections: {wrong_col}")
        ),
    )

def test_injector_structure():
    from services.rag.retriever import retrieve
    from services.rag.injector import build_context
    results = retrieve("quarks color charge", k=3)
    ctx = build_context(results)
    has_header  = "VAULT CONTEXT" in ctx
    has_numbers = "[1]" in ctx
    has_source  = "classes/" in ctx
    passed = has_header and has_numbers and has_source
    record(
        name="Injector — context block structure",
        metric="Does build_context() produce a header, numbered citations, and source paths?",
        result=f"has_header={has_header}  has_numbers={has_numbers}  has_source={has_source}",
        passed=passed,
        explanation=(
            "The injected block must be readable by the AI: titled, numbered, and sourced. "
            + ("All structural elements present." if passed
               else "Missing elements — AI won't know where citations come from.")
        ),
    )

def test_injector_empty():
    from services.rag.injector import build_context
    result = build_context([])
    passed = result == ""
    record(
        name="Injector — empty input produces empty string",
        metric="build_context([]) == ''",
        result=repr(result),
        passed=passed,
        explanation=(
            "When no chunks are retrieved, nothing should be injected into the system prompt. "
            + ("Correct." if passed else f"Got non-empty output: {repr(result[:60])}")
        ),
    )

def test_skip_rag_heuristic():
    from services.ai.service import _should_skip_rag
    cases = [
        ("mark task abc as done",            True),
        ("mark that task as active",         True),
        ("delete task abc-123",              True),
        ("delete project marketing",         True),
        ("update task 5's priority to high", True),
        ("create a task to buy milk",        True),
        ("create a new project called X",    True),
        ("archive task 99",                  True),
        ("what do I know about eigenvalues", False),
        ("what should I work on next?",      False),
        ("show me my notes on Dr. Rossi",    False),
        ("how do I reduce fear on tasks?",   False),
        ("search the vault for QCD",         False),
        ("tell me about my goals",           False),
        ("summarize my journal entries",     False),
        ("what tasks do I have today?",      False),
    ]
    wrong = [(msg, exp, _should_skip_rag(msg)) for msg, exp in cases
             if _should_skip_rag(msg) != exp]
    passed = len(wrong) == 0
    record(
        name="Skip-RAG heuristic — 16 classification cases",
        metric="How many messages are correctly classified as skip=True/False?",
        result=(f"{len(cases)-len(wrong)}/{len(cases)} correct"
                + (f"  |  Wrong: {[(m, f'expected={e} got={g}') for m,e,g in wrong]}" if wrong else "")),
        passed=passed,
        explanation=(
            "Pure task mutations should skip vault retrieval to save tokens. "
            "Knowledge questions must not be skipped. "
            + ("All 16 cases classified correctly." if passed
               else f"{len(wrong)} misclassified — regex patterns need adjustment.")
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS  (Gemini API — costs ~$0.01-0.05 total)
# ═════════════════════════════════════════════════════════════════════════════

def test_passive_rag():
    from services.ai.service import AIService
    from services.ai.gemini_provider import GeminiProvider
    svc = AIService(GeminiProvider())
    reply, sources = svc.chat(get_db(), [{"role": "user",
        "content": "what do I know about quantum chromodynamics and quarks?"}])
    terms = ["quark", "qcd", "gluon", "confinement", "chromodynamics"]
    hits  = [t for t in terms if t in reply.lower()]
    # Sources must be surfaced separately, not cited inline in the answer text itself.
    has_source_chip = any("classes/" in (s.get("source") or "") for s in sources)
    inline_citation  = any(x in reply for x in ["_test_qcd", "classes/"])
    passed = len(hits) >= 2 and has_source_chip and not inline_citation
    record(
        name="Passive RAG — AI answers using vault content, cites via separate sources list",
        metric="Reply has ≥2 QCD terms, sources list has a classes/ path, reply text has no inline path citation",
        result=(f"Terms found: {hits}\nSources: {sources}\n"
                f"Inline citation present (should be False): {inline_citation}\nFull reply:\n{reply}"),
        passed=passed,
        explanation=(
            "The QCD note should be retrieved and used, with its source surfaced in the "
            "separate `sources` list returned alongside the reply — not named inline in the "
            "answer text. "
            + ("Correct on all three counts." if passed
               else f"Failed — terms_ok={len(hits) >= 2}, source_chip_ok={has_source_chip}, "
                    f"no_inline_citation={not inline_citation}.")
        ),
    )

def test_gk_fallback():
    from services.ai.service import AIService
    from services.ai.gemini_provider import GeminiProvider
    svc = AIService(GeminiProvider())
    reply, sources = svc.chat(get_db(), [{"role": "user",
        "content": "what is the boiling point of tungsten?"}])
    has_gk = "(GK)" in reply
    record(
        name="(GK) fallback — AI labels general knowledge answers",
        metric="Does the reply contain the literal string '(GK)'?",
        result=f"(GK) present: {has_gk}\nSources: {sources}\nFull reply:\n{reply}",
        passed=has_gk,
        explanation=(
            "When answering from general knowledge (nothing in vault), the AI must append '(GK)'. "
            + ("Correct — (GK) marker present." if has_gk
               else "Missing (GK) — the system prompt instruction may need stronger wording "
                    "or the model is ignoring it. Inspect the reply above.")
        ),
    )

def test_search_vault_tool():
    from services.ai.service import AIService
    from services.ai.gemini_provider import GeminiProvider
    svc = AIService(GeminiProvider())
    reply, sources = svc.chat(get_db(), [{"role": "user",
        "content": "search my notes for anything about color confinement and quarks"}])
    terms = ["confinement", "quark", "qcd", "gluon", "chromodynamics"]
    hits  = [t for t in terms if t in reply.lower()]
    passed = len(hits) >= 1
    record(
        name="search_vault tool — active retrieval returns vault content",
        metric="Does the AI reply (via search_vault tool) contain terms from the QCD note?",
        result=f"Terms found: {hits}\nSources: {sources}\nFull reply:\n{reply}",
        passed=passed,
        explanation=(
            "Asking the AI to 'search my notes' should trigger the search_vault tool call, "
            "which queries the vector store and returns chunks. "
            + (f"Tool returned vault content — terms: {hits}." if passed
               else "No QCD terms in reply — tool may not have been called, "
                    "or ChromaDB returned empty results.")
        ),
    )

def test_create_note_tool():
    from services.ai.service import AIService
    from services.ai.gemini_provider import GeminiProvider
    from services.vault import storage
    before = set(storage.list_keys("ai_generated"))

    svc = AIService(GeminiProvider())
    reply, sources = svc.chat(get_db(), [{"role": "user",
        "content": ("Save a short note about RAG pipelines retrieving context for LLMs. "
                    "Title: 'RAG Pipeline Overview'. Filename: rag-pipeline-overview")}])

    time.sleep(1)
    after     = set(storage.list_keys("ai_generated"))
    new_files = after - before

    if new_files:
        _TEST_FILES.extend(new_files)

    file_content = ""
    if new_files:
        file_content = storage.download(next(iter(new_files))).decode("utf-8")

    has_file       = bool(new_files)
    has_reviewed   = "reviewed: false" in file_content.lower() or "reviewed: False" in file_content
    has_ai_flag    = "ai_generated: true" in file_content.lower() or "ai_generated: True" in file_content
    passed = has_file and has_reviewed and has_ai_flag

    record(
        name="create_note tool — AI writes a file to vault/ai_generated/",
        metric="File created + has reviewed:false + has ai_generated:true in frontmatter",
        result=(f"New files: {new_files}\n"
                f"reviewed:false present: {has_reviewed}\n"
                f"ai_generated:true present: {has_ai_flag}\n"
                f"AI reply:\n{reply}"),
        passed=passed,
        explanation=(
            "The AI should call create_note, which writes a .md file with safety frontmatter. "
            + (f"File created correctly with proper flags." if passed
               else f"Issue — file_created={has_file}, reviewed_false={has_reviewed}, ai_flag={has_ai_flag}. "
                    "Tool may not have been called, or frontmatter is missing.")
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

COMPONENT_TESTS = [
    test_chunker_heading_split,
    test_chunker_frontmatter,
    test_chunker_size_limit,
    test_indexer_dynamic_collection,
    test_indexer_runs,
    test_indexer_idempotency,
    test_indexer_delete,
    test_retrieval_relevant,
    test_retrieval_irrelevant,
    test_retrieval_keyword_boost,
    test_retrieval_cross_collection,
    test_retrieval_scoped,
    test_injector_structure,
    test_injector_empty,
    test_skip_rag_heuristic,
]

INTEGRATION_TESTS = [
    test_passive_rag,
    test_gk_fallback,
    test_search_vault_tool,
    test_create_note_tool,
]

def run(fn):
    w(f"\n  Running: {fn.__name__} …")
    try:
        fn()
    except Exception as e:
        record(
            name=fn.__name__,
            metric="Test execution",
            result=f"Exception: {e}",
            passed=False,
            explanation=f"Test crashed before it could evaluate anything. Error: {e}",
        )

def main():
    w(f"RAG TEST RESULTS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    w("=" * 72)

    w("\n\n══ COMPONENT TESTS (no AI calls) ══════════════════════════════════════")
    for fn in COMPONENT_TESTS:
        run(fn)

    w("\n\n══ INTEGRATION TESTS (Gemini API calls) ════════════════════════════════")
    for fn in INTEGRATION_TESTS:
        run(fn)

    w("\n\n── Cleanup ───────────────────────────────────────────────────────────────")
    try:
        cleanup()
    except Exception as e:
        w(f"  Cleanup error: {e}")

    report_all()
    dump()
    w(f"\nFull report written to {OUTPUT_FILE}")

    failed = sum(1 for r in results if not r["passed"])
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
