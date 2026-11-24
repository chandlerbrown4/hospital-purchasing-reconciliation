"""Replay the frozen purchasing engine and grade every invoice in the locked batch."""
import argparse
import csv
import io
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "6f561052eb096886b6b24f22a9efb00de3869666"
BATCH = "a2a89f7c43a5a8661fb8b7b059c249f4eb5fcb88"
FIXTURE = Path("evaluation/held-out-v1")
AMOUNTS = ("price_delta_cents", "unbacked_eaches", "quantity_exposure_cents")
ROUTES = ("clean", "exception", "review")
EXCEPTIONS = {"price_variance", "insufficient_receipts", "over_ordered_quantity"}
REVIEWS = {
    "uncertain_receipts", "ambiguous_po_line", "no_po_line", "sku_mismatch",
    "before_po_date", "fractional_expected_cent", "fractional_exposure_cent",
    "id_conflict", "revision_conflict", "invalid_revision", "invalid",
}
START = "<!-- held-out-v1:start -->"
END = "<!-- held-out-v1:end -->"


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def by_event(rows):
    indexed = {}
    for row in rows:
        key = row["event_id"]
        if not key or key in indexed:
            raise ValueError(f"Expected a unique invoice event ID in this fixture: {key!r}")
        indexed[key] = row
    return indexed


def unpack(revision, path, destination):
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", revision, str(path)], cwd=ROOT,
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(destination)


def run_frozen(folder):
    # The engine workspace receives only its frozen package, feeds and two mappings.
    engine_dir = folder / "engine"
    benchmark_dir = folder / "benchmark"
    unpack(ENGINE, "purchasing", engine_dir)
    unpack(BATCH, FIXTURE, benchmark_dir)
    fixture = benchmark_dir / FIXTURE
    inputs = engine_dir / "inputs"
    inputs.mkdir()
    for name in ("batch", "units.csv", "vendors.csv"):
        (fixture / name).rename(inputs / name)
    expected = read_csv(fixture / "expected.csv")
    invoice_ids = by_event(read_csv(inputs / "batch/invoices.csv")).keys()
    if by_event(expected).keys() != invoice_ids:
        raise ValueError("Locked answer key must cover every input invoice exactly once")
    subprocess.run(
        [sys.executable, "-E", "-s", "-m", "purchasing", "inputs/batch",
         "--mappings", "inputs", "--output", "out"],
        cwd=engine_dir, check=True, stdout=subprocess.DEVNULL,
    )
    with sqlite3.connect(engine_dir / "out/reconciliation.sqlite") as database:
        database.row_factory = sqlite3.Row
        actual = database.execute("""
            SELECT audit.event_id, audit.decision AS engine_decision,
                   line.delta_cents AS price_delta_cents, line.unbacked_eaches,
                   line.unbacked_po_cents AS quantity_exposure_cents,
                   line.actual_cents AS billed_cents, line.audit_id AS active,
                   audit.po_event_id, audit.receipt_event_ids,
                   audit.uncertain_receipt_event_ids, audit.reason
            FROM row_audit AS audit
            LEFT JOIN invoice_lines AS line ON audit.audit_id = line.audit_id
            WHERE audit.kind = 'invoice'
        """).fetchall()
    return score(expected, actual)


def route(row):
    if row is None:
        return "missing"
    status = row["engine_decision"]
    if status in REVIEWS:
        return "review"
    if row["active"] is None:
        return "missing"
    if status == "matched":
        return "clean"
    if status in EXCEPTIONS:
        return "exception"
    raise ValueError(f"Unmapped active engine decision: {status!r}")


def score(expected, actual):
    observed = by_event(actual)
    truth = by_event(expected)
    results = []
    for event_id in dict.fromkeys([*truth, *observed]):
        answer = truth.get(event_id, {})
        row = dict(observed.get(event_id, {}))
        predicted = route(row or None)
        wanted = answer.get("decision", "")
        record = dict(event_id=event_id, source=answer.get("source", ""),
                      expected=wanted, actual=predicted, engine_decision=row.get("engine_decision", ""))
        amounts_match = True
        for name in AMOUNTS:
            wanted_amount = answer.get(name) or None
            if wanted_amount is not None:
                wanted_amount = int(wanted_amount)
            observed_amount = row.get(name)
            record["expected_" + name] = wanted_amount
            record["actual_" + name] = observed_amount
            amounts_match = amounts_match and wanted_amount == observed_amount
        if not wanted:
            result = "unexpected output"
        elif predicted == "missing":
            result = "missing decision"
        elif predicted == "clean" and wanted != "clean":
            result = "false clean"
        elif predicted != wanted:
            result = "wrong route"
        elif not amounts_match:
            result = "wrong amounts"
        else:
            result = "exact"
        record.update(result=result, billed_cents=row.get("billed_cents"),
                      po_event_id=row.get("po_event_id", ""), receipt_event_ids=row.get("receipt_event_ids", ""),
                      uncertain_receipt_event_ids=row.get("uncertain_receipt_event_ids", ""),
                      engine_reason=row.get("reason", ""), expected_evidence=answer.get("evidence", ""))
        results.append(record)
    return results


def render_csv(rows):
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue()


def render_summary(rows):
    paired = [r for r in rows if r["expected"]]
    cells = Counter((r["expected"], r["actual"]) for r in paired)
    results = Counter(r["result"] for r in rows)
    clean_total = sum(r["actual"] == "clean" for r in rows)
    review_total = sum(r["actual"] == "review" for r in rows)
    external = [r for r in paired if r["source"] != "Control"]
    controls = [r for r in paired if r["source"] == "Control"]
    lines = [
        f'**{results["exact"]} of {len(paired)} invoice lines matched the answer key** on both routing and amounts.',
        f'Of {clean_total} lines accepted as clean, **{results["false clean"]} were false accepts**.'
        if results["false clean"] != 1 else
        f'Of {clean_total} lines accepted as clean, **1 was a false accept**.',
        f'The pipeline sent {review_total} to review. The answer key called for review on '
        f'{cells["review", "review"]} of those lines.',
        "",
        "| Expected decision | Accepted as clean | Flagged a discrepancy | Sent to review | Missing |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for wanted in ROUTES:
        counts = " | ".join(str(cells[wanted, actual]) for actual in (*ROUTES, "missing"))
        lines.append(f"| {wanted.capitalize()} | {counts} |")
    lines.extend([
        "",
        f'Exact results by origin were {sum(r["result"] == "exact" for r in external)}/{len(external)} '
        f'for externally sourced cases and {sum(r["result"] == "exact" for r in controls)}/{len(controls)} '
        'for local controls.',
        f'Unexpected output lines: {results["unexpected output"]}. '
        f'Correctly routed lines with wrong amounts: {results["wrong amounts"]}.',
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update scorecard and README from the pinned run")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        rows = run_frozen(Path(tmp))
    readme = ROOT / "README.md"
    before = readme.read_text()
    if before.count(START) != 1 or before.count(END) != 1:
        raise SystemExit("Expected one held-out scorecard section in the README")
    prefix, section = before.split(START)
    _, suffix = section.split(END)
    summary = render_summary(rows)
    artifacts = {ROOT / FIXTURE / "results.csv": render_csv(rows),
                 readme: f"{prefix}{START}\n{summary}\n{END}{suffix}"}
    # Check the inspectable fixture even though the engine reads only the pinned copy.
    subprocess.run(["git", "diff", "--exit-code", BATCH, "--", str(FIXTURE / "batch"),
                    str(FIXTURE / "vendors.csv"), str(FIXTURE / "units.csv"),
                    str(FIXTURE / "expected.csv"), str(FIXTURE / "PROTOCOL.md")],
                   cwd=ROOT, check=True)
    for path, content in artifacts.items():
        if args.write:
            path.write_text(content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"Scorecard differs: {path.relative_to(ROOT)}")
    print(summary)


if __name__ == "__main__":
    main()
