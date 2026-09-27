"""Supplemental bounded FIFO hash chain using the existing capture writer."""
import gzip
import json
from pathlib import Path

from .capture import InputWriter
from .input_journal import DeferredJournalRow, JournalHashChain, detach_json
from .manifest_validation import fingerprint


class ResearchJournal:
    def __init__(self, path, capture_id, manifest):
        self.path = Path(path)
        self.writer = InputWriter(path)
        self.chain = JournalHashChain()
        self.capture_id = capture_id
        self.sequence = 0
        self.closed = False
        self.append("header", dict(manifest=manifest))

    def append(self, kind, body):
        if self.closed:
            raise RuntimeError("research journal closed")
        detached, size = detach_json(body)
        self.sequence += 1
        row = dict(schema="wave2-research-v1", captureId=self.capture_id,
            sequence=self.sequence, kind=kind, body=detached)
        self.writer.record("research_input", body.get("symbol"), DeferredJournalRow(row, self.chain, size+2048))

    def close(self):
        if not self.closed:
            self.append("footer", dict(rows=self.sequence))
            self.closed = True
            self.writer.close()

    def health(self):
        return dict(error=self.writer.error, accepted=self.writer.accepted, written=self.writer.written,
            pendingBytes=self.writer.pending_bytes, complete=self.closed and not self.writer.error)


def read_research(path):
    previous = None
    capture_id = None
    footer = False
    sequence = 0
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)["payload"]
            digest = row.pop("hash")
            sequence += 1
            if (footer or row["sequence"] != sequence or row["previousHash"] != previous
                    or fingerprint(row) != digest or row["schema"] != "wave2-research-v1"):
                raise ValueError("research hash/sequence integrity failure")
            if sequence == 1:
                if row["kind"] != "header":
                    raise ValueError("research header missing")
                capture_id = row["captureId"]
            if row["captureId"] != capture_id:
                raise ValueError("mixed research capture")
            if row["kind"] == "footer":
                if row["body"]["rows"] != sequence-1:
                    raise ValueError("research footer count mismatch")
                footer = True
            previous = digest
            yield row
    if not footer:
        raise ValueError("research footer missing; incomplete capture")
