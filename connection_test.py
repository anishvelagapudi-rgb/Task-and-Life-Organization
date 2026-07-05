#!/usr/bin/env python3
"""
Connection engine test suite — mirrors rag_test.py's harness style.
Run:     python connection_test.py
Results: connection_test_results.txt

Tests the connection engine (services/connections/) in isolation. Per
CONNECTION_ENGINE_DESIGN.md, this module never imports services/rag/retriever.py or
services/rag/injector.py — only store.py's/embedder.py's public functions — and this
test file exercises it standalone, the same way rag_test.py exercises the RAG
pipeline standalone.
"""

from dotenv import load_dotenv
load_dotenv()

import os, sys, sqlite3, textwrap
from datetime import datetime
from pathlib import Path

OUTPUT_FILE = "connection_test_results.txt"
lines = []

def w(text=""):
    print(text)
    lines.append(text)

def dump():
    with open(OUTPUT_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


# ── result tracker ────────────────────────────────────────────────────────────

results = []

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

VAULT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vault")

JOURNAL_NOTE = textwrap.dedent("""\
    ---
    title: Feeling overwhelmed this week
    ---

    # Deadline anxiety

    I keep putting off the CS101 project because I do not know where to start and
    the deadline keeps looming closer. Every time I open the assignment I freeze up
    and end up doing something else instead. I know I need to just start somewhere
    small.
""")

PROJECT_NOTE = textwrap.dedent("""\
    ---
    title: CS101 Final Project
    ---

    # Requirements

    Build a small web app with a database backend. Due at the end of the semester.
    Grading rubric covers functionality, code quality, and a short writeup.

    # Status

    Have not started implementation yet. Need to pick a stack and set up the repo.
""")

_TEST_FILES: list[str] = []

def write_test_note(folder, filename, content):
    path = os.path.join(VAULT_ROOT, folder, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    _TEST_FILES.append(os.path.abspath(path))
    return os.path.abspath(path)

def cleanup(db):
    from services.rag.store import delete_by_source
    removed = 0
    for path in _TEST_FILES:
        folder = Path(path).relative_to(VAULT_ROOT).parts[0]
        try:
            delete_by_source(folder, path)
        except Exception:
            pass
        if os.path.exists(path):
            os.remove(path)
            removed += 1
    try:
        db.execute("DELETE FROM note_connections WHERE source_path LIKE '%_conn_test%'")
        db.commit()
    except Exception:
        pass
    w(f"\n  Removed {removed} test file(s) from vault/ChromaDB, and their note_connections rows.")

def get_db():
    db = sqlite3.connect("dev.db")
    db.row_factory = sqlite3.Row
    return db


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT TESTS (no chat/generation API calls; embedding calls happen in the
# discover_connections tests, categorized as "component" the same way rag_test.py
# already treats its own retrieve()-based tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_passes_filter_pure_function():
    from services.connections.engine import _passes_filter, MIN_DISTANCE, MAX_DISTANCE
    cases = [
        # (distance, source_col, target_col, expected)
        (0.28, "journal", "projects", True),        # in-band, cross-folder
        (0.05, "journal", "projects", False),        # too close (near-duplicate)
        (0.60, "journal", "projects", False),        # too far (noise)
        (0.28, "journal", "journal", False),          # same folder — never "non-obvious"
        (MIN_DISTANCE, "journal", "projects", True),  # boundary inclusive
        (MAX_DISTANCE, "journal", "projects", True),  # boundary inclusive
    ]
    wrong = [(d, sc, tc, exp) for d, sc, tc, exp in cases if _passes_filter(d, sc, tc) != exp]
    passed = len(wrong) == 0
    record(
        name="_passes_filter — non-obvious heuristic as a pure function",
        metric="6 cases: distance band inclusion/exclusion + same-folder exclusion",
        result=f"{len(cases) - len(wrong)}/{len(cases)} correct" + (f"  |  Wrong: {wrong}" if wrong else ""),
        passed=passed,
        explanation=(
            "Non-obvious = moderate cross-folder overlap: not near-duplicate, not noise, "
            "never same-folder. " + ("All cases correct." if passed else f"{len(wrong)} misclassified.")
        ),
    )

def test_note_connections_sqlite_roundtrip():
    from services.connections.engine import Connection, save_connections, get_saved_connections
    db = get_db()
    conns = [
        Connection(source_path="journal/_conn_test_a.md", target_path="projects/_conn_test_b.md",
                   summary="test summary", score=0.25),
    ]
    save_connections(db, conns, source_collection="journal")
    read_back = get_saved_connections(db, "journal/_conn_test_a.md")
    passed = (
        len(read_back) == 1
        and read_back[0].target_path == "projects/_conn_test_b.md"
        and read_back[0].summary == "test summary"
        and abs(read_back[0].score - 0.25) < 1e-9
    )
    # upsert semantics: saving again for the same pair should not duplicate
    save_connections(db, conns, source_collection="journal")
    read_back_2 = get_saved_connections(db, "journal/_conn_test_a.md")
    no_dup = len(read_back_2) == 1
    db.execute("DELETE FROM note_connections WHERE source_path = 'journal/_conn_test_a.md'")
    db.commit()
    db.close()
    record(
        name="note_connections — SQLite save/read round-trip, upsert not duplicate",
        metric="save_connections() then get_saved_connections() returns the same data; re-saving doesn't duplicate",
        result=f"Read back: {read_back}  |  No duplicate after re-save: {no_dup}",
        passed=passed and no_dup,
        explanation=(
            "Connections are cached in their own SQLite table (not a new ChromaDB collection — "
            "see CONNECTION_ENGINE_DESIGN.md). " + ("Round-trip correct, upsert working." if passed and no_dup
               else "Round-trip or upsert semantics broken.")
        ),
    )

def test_discover_connections_finds_cross_folder_link():
    from services.rag.indexer import index_file
    from services.connections.engine import discover_connections

    jp = write_test_note("journal", "_conn_test_stress.md", JOURNAL_NOTE)
    pp = write_test_note("projects", "_conn_test_deadline.md", PROJECT_NOTE)
    index_file(jp)
    index_file(pp)

    conns = discover_connections("journal/_conn_test_stress.md", k=5)
    targets = [c.target_path for c in conns]
    hit = any("_conn_test_deadline" in t for t in targets)
    same_folder_leak = any(t.startswith("journal/") for t in targets)
    passed = hit and not same_folder_leak
    record(
        name="discover_connections — finds a real cross-folder connection",
        metric="A deliberately-related note in a different folder (journal vs projects) should surface; same-folder notes never should",
        result=f"Targets found: {targets}",
        passed=passed,
        explanation=(
            "Two notes about the same underlying CS101 deadline stress, written in different "
            "vault folders, should be surfaced as a non-obvious connection. "
            + (f"Found it (targets: {targets})." if passed
               else f"Missing or leaked same-folder result — targets: {targets}.")
        ),
    )

def test_discover_connections_excludes_unrelated():
    from services.rag.indexer import index_file
    from services.connections.engine import discover_connections

    # Deliberately a longer, topically-rich note rather than a one-line list — short,
    # generic text (verified empirically) doesn't embed distinctively enough to test
    # the distance-band filter meaningfully; it lands in the same moderate-distance
    # range as plausible connections regardless of actual relatedness. A real note
    # with substantive, specific vocabulary is what the filter is actually meant to
    # separate from genuinely related content.
    unrelated = textwrap.dedent("""\
        ---
        title: Sourdough starter notes
        ---

        # Feeding schedule

        My sourdough starter lives on the kitchen counter and gets fed a 1:1:1 ratio of
        starter, flour, and water every morning around 8am. It roughly doubles in size
        within four to six hours when the kitchen is warm.

        # Troubleshooting

        If the starter smells like nail polish remover it has gone hungry too long —
        feed it and discard more than usual next time. A layer of dark liquid on top
        (hooch) means the same thing. Keep the jar loosely covered, never airtight.
    """)
    up = write_test_note("reference", "_conn_test_unrelated.md", unrelated)
    index_file(up)

    conns = discover_connections("journal/_conn_test_stress.md", k=10)
    targets = [c.target_path for c in conns]
    leaked = any("_conn_test_unrelated" in t for t in targets)
    passed = not leaked
    record(
        name="discover_connections — unrelated note does not appear",
        metric="A substantive, topically-unrelated note (sourdough troubleshooting) should not be "
               "surfaced as a connection to a deadline-anxiety journal entry",
        result=f"Targets found: {targets}",
        passed=passed,
        explanation=(
            "The distance-band filter should keep genuinely unrelated content out. "
            + ("Correct — sourdough note absent." if passed else f"Unrelated note leaked into results: {targets}")
        ),
    )

def test_delete_connections_for_cleanup():
    from services.connections.engine import Connection, save_connections, get_saved_connections, delete_connections_for
    db = get_db()
    conns = [Connection(source_path="journal/_conn_test_a.md", target_path="projects/_conn_test_b.md",
                         summary="s", score=0.2)]
    save_connections(db, conns, source_collection="journal")
    before = get_saved_connections(db, "journal/_conn_test_a.md")
    delete_connections_for(db, "journal/_conn_test_a.md")
    after = get_saved_connections(db, "journal/_conn_test_a.md")
    db.close()
    passed = len(before) == 1 and len(after) == 0
    record(
        name="delete_connections_for — cleans up rows for a deleted/moved note",
        metric="Rows present before delete, absent after — mirrors vault index cleanup on file delete",
        result=f"Before: {len(before)} row(s)  |  After: {len(after)} row(s)",
        passed=passed,
        explanation=(
            "When a vault file is deleted or moved, its note_connections rows (as source "
            "or target) should be cleaned up so stale connections aren't surfaced. "
            + ("Cleanup confirmed." if passed else "Cleanup did not work as expected.")
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (Gemini chat/generation API calls)
# ═════════════════════════════════════════════════════════════════════════════

def test_find_connections_tool():
    from services.ai.service import AIService
    from services.ai.gemini_provider import GeminiProvider
    from services.rag.indexer import index_file

    jp = write_test_note("journal", "_conn_test_stress.md", JOURNAL_NOTE)
    pp = write_test_note("projects", "_conn_test_deadline.md", PROJECT_NOTE)
    index_file(jp)
    index_file(pp)

    svc = AIService(GeminiProvider())
    reply, sources = svc.chat(get_db(), [{"role": "user", "content":
        "Find non-obvious connections for the vault note journal/_conn_test_stress.md"}])
    mentions_target = any("_conn_test_deadline" in (s.get("source") or "") for s in sources) or \
                       "_conn_test_deadline" in reply
    record(
        name="find_connections AI tool — active connection discovery via chat",
        metric="Asking the AI to find connections for the journal note should surface the projects note",
        result=f"Sources: {sources}\nFull reply:\n{reply}",
        passed=mentions_target,
        explanation=(
            "Asking the AI to find connections should trigger the find_connections tool, which "
            "calls discover_connections() and surfaces the cross-folder projects note either in "
            "the reply text or the returned sources list. "
            + ("Tool surfaced the expected note." if mentions_target
               else "Expected note not found in reply or sources — tool may not have fired.")
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

COMPONENT_TESTS = [
    test_passes_filter_pure_function,
    test_note_connections_sqlite_roundtrip,
    test_discover_connections_finds_cross_folder_link,
    test_discover_connections_excludes_unrelated,
    test_delete_connections_for_cleanup,
]

INTEGRATION_TESTS = [
    test_find_connections_tool,
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
    w(f"CONNECTION ENGINE TEST RESULTS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    w("=" * 72)

    w("\n\n══ COMPONENT TESTS (no AI chat/generation calls) ══════════════════════")
    for fn in COMPONENT_TESTS:
        run(fn)

    w("\n\n══ INTEGRATION TESTS (Gemini API calls) ════════════════════════════════")
    for fn in INTEGRATION_TESTS:
        run(fn)

    w("\n\n── Cleanup ───────────────────────────────────────────────────────────────")
    try:
        cleanup(get_db())
    except Exception as e:
        w(f"  Cleanup error: {e}")

    report_all()
    dump()
    w(f"\nFull report written to {OUTPUT_FILE}")

    failed = sum(1 for r in results if not r["passed"])
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
