"""Explicit, bounded format smoke test on public archives; never part of runtime.

Downloads one predefined day, without API keys or subscriptions. Nothing is
trained. Data remains in the requested new output directory; publish report.json
only, not raw or normalized vendor data. Not invoked by ordinary pytest.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .alignment import align_archives
from .importer import import_archive, sha256_file
from .sources import ArchiveSpec, PROVIDERS


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Fail explicitly rather than forwarding to an unexpected source.


def download_sample(spec, path, *, max_bytes=128*1024**2, timeout_seconds=120):
    if spec.has_arrival_time and not spec.day.endswith("-01"):
        raise ValueError("no authenticated/paid downloads in the sample checker")
    started = time.monotonic()
    request = Request(spec.url, headers={"User-Agent": "scalp-bot-archive-format-check/1"})
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP status {response.status}")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("archive exceeds sample download byte budget")
        with path.open("xb") as sink:
            received = 0
            while True:
                block = response.read(min(1024*1024, max_bytes-received+1))
                if not block:
                    break
                received += len(block)
                if received > max_bytes or time.monotonic()-started > timeout_seconds:
                    raise ValueError("sample download budget exceeded; not a complete archive")
                sink.write(block)
        if declared and received != int(declared):
            raise ValueError("HTTP content length mismatch")
    return {"downloaded_at": datetime.now(timezone.utc).isoformat(), "bytes": received,
            "sha256": sha256_file(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"purpose": "format_smoke_only", "training_performed": False,
              "sampling_policy": "NEARUSDT 2024-01-01 chosen for schema checks, not PnL",
              "sources": []}
    for provider in PROVIDERS:
        source = ArchiveSpec(provider,"NEARUSDT","2024-01-01","format_smoke_only")
        raw = args.output / (provider+".csv.gz")
        result = {"source": source.public()}
        try:
            result["download"] = download_sample(source, raw)
            manifest = import_archive(raw,source,args.output/provider)
            result.update(status="complete_import", counts=manifest["counts"],
                          quality_status=manifest["quality_status"], raw_sha256=manifest["raw_sha256"],
                          normalized_sha256=manifest["outputs"]["events.jsonl.gz"],
                          warnings=manifest["warnings"], training_ready=False)
        except Exception as exc:
            result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        report["sources"].append(result)
        (args.output/"report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        print(provider,result["status"],flush=True)
    if all(item["status"] == "complete_import" for item in report["sources"]):
        try:
            report["alignment"] = align_archives(args.output/"tardis-trades", args.output/"tardis-l2",
                                                  args.output/"aligned")
        except Exception as exc:
            report["alignment"] = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
    (args.output/"report.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return int(any(item["status"] == "failed" for item in report["sources"])
               or report.get("alignment", {}).get("status") != "complete")


if __name__ == "__main__":
    raise SystemExit(main())
