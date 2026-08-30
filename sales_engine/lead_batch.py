from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TARGETS = HERE / "lead_batch_targets.json"


def load_targets(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("targets", [])
    if not isinstance(data, list):
        raise SystemExit("targets file must contain a list or {'targets': [...]} object")

    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        area = str(item.get("area") or "").strip()
        category = str(item.get("category") or "").strip()
        if not area or not category:
            continue
        out.append(item)
    return out


def parse_pipeline_result(stdout: str) -> dict | None:
    marker = "=== PIPELINE RESULT ==="
    pos = stdout.rfind(marker)
    if pos < 0:
        return None
    tail = stdout[pos + len(marker):].strip()
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        return None


def run_target(index: int, total: int, item: dict, batch_id: str, db: Path | None) -> dict:
    area = str(item["area"]).strip()
    category = str(item["category"]).strip()
    max_results = int(item.get("max_results") or 30)
    campaign_id = f"{batch_id}-{index:02d}"

    cmd = [
        sys.executable,
        str(HERE / "lead_pipeline.py"),
        "--area", area,
        "--category", category,
        "--max-results", str(max_results),
        "--campaign-id", campaign_id,
    ]
    if db is not None:
        cmd.extend(["--db", str(db.resolve())])

    for query in item.get("queries") or []:
        q = str(query).strip()
        if q:
            cmd.extend(["--query", q])

    print("\n" + "#" * 72)
    print(f"BATCH {index}/{total}: {area} / {category}")
    print(f"campaign: {campaign_id}")
    print("#" * 72, flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    rc = proc.wait()
    stdout = "".join(lines)

    result = parse_pipeline_result(stdout) or {}
    result.update({
        "area": area,
        "category": category,
        "campaign_id": result.get("campaign_id") or campaign_id,
        "return_code": rc,
    })
    if rc != 0:
        result["error"] = "pipeline command failed"
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Run Nexccess Lead Pipeline for multiple area/category targets.")
    p.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    p.add_argument("--db", type=Path)
    args = p.parse_args()

    targets_path = args.targets.resolve()
    if not targets_path.exists():
        raise SystemExit(f"targets file not found: {targets_path}")

    targets = load_targets(targets_path)
    if not targets:
        raise SystemExit("No enabled targets found.")

    batch_id = f"WEB-DISCOVERY-BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    print("=" * 72)
    print("Nexccess Lead Batch")
    print(f"batch   : {batch_id}")
    print(f"targets : {len(targets)}")
    print(f"config  : {targets_path}")
    print("=" * 72, flush=True)

    results: list[dict] = []
    for idx, item in enumerate(targets, 1):
        results.append(run_target(idx, len(targets), item, batch_id, args.db))

    totals = {
        "targets": len(results),
        "success": sum(1 for r in results if r.get("return_code") == 0),
        "failed": sum(1 for r in results if r.get("return_code") != 0),
        "new_leads": sum(int(r.get("new_leads") or 0) for r in results),
        "go": sum(int(r.get("go") or 0) for r in results),
        "hold": sum(int(r.get("hold") or 0) for r in results),
        "close": sum(int(r.get("close") or 0) for r in results),
        "sales_queue_added": sum(int(r.get("sales_queue_added") or 0) for r in results),
    }

    report = {
        "batch_id": batch_id,
        "totals": totals,
        "results": results,
    }
    print("\n=== BATCH RESULT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if totals["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
