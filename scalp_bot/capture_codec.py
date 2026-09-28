"""Bounded hash/JSON/gzip service with no trading or exchange dependencies."""
import gzip
import multiprocessing
import os
import pickle

import msgspec

from .input_journal import DEFERRED_ROWS, resolve_journal_row


def _chains(rows):
    found = {}
    for row in rows:
        payload = row.get('payload')
        if isinstance(payload, DEFERRED_ROWS):
            found.setdefault(id(payload.chain), payload.chain)
    return list(found.values())


def _worker(connection):
    try:
        connection.send(os.getpid())
        encoder = msgspec.json.Encoder()
        while True:
            # This private pipe receives only our own bounded, detached rows.
            rows = pickle.loads(connection.recv_bytes(64 * 1024**2))
            if rows is None:
                return
            chains = _chains(rows)
            hashed = sum(isinstance(row.get('payload'), DEFERRED_ROWS) for row in rows)
            data = b''.join(encoder.encode(resolve_journal_row(row))+b'\n' for row in rows)
            # Independent gzip members are a standard gzip stream; readers
            # recover exactly the original concatenated JSONL bytes.
            connection.send(dict(data=gzip.compress(data, compresslevel=1, mtime=0),
                chainHashes=[chain.previous_hash for chain in chains],
                encodedBytes=len(data), hashedRows=hashed))
    except (EOFError, BrokenPipeError, OSError):
        return
    except Exception as exc:
        try:
            connection.send(dict(error=type(exc).__name__))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class CaptureCodec:
    """One in-flight batch. Chain receipts are applied before the next send.

    Pickle preserves shared chain identities inside a batch; returned hashes
    update those same parent chains. No unbounded per-capture chain registry or
    independently advancing writer can reorder observations.
    """
    def __init__(self):
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child,), name='capture-codec', daemon=True)
        self.error = None
        try:
            self.process.start()
            child.close()
            if not self.connection.poll(10):
                raise TimeoutError('capture codec startup timeout')
            self.pid = self.connection.recv()
        except BaseException:
            child.close()
            self.abort()
            raise

    def encode(self, rows):
        chains = _chains(rows)
        self.connection.send(rows)
        if not self.connection.poll(10):
            raise TimeoutError('capture codec response timeout')
        result = pickle.loads(self.connection.recv_bytes(65 * 1024**2))
        if result.get('error'):
            self.error = result['error']
            raise RuntimeError('capture codec failed: '+self.error)
        for chain, digest in zip(chains, result['chainHashes'], strict=True):
            chain.previous_hash = digest
        return result

    def close(self):
        try:
            if self.process.is_alive():
                self.connection.send(None)
                self.process.join(2)
        except (BrokenPipeError, EOFError, OSError):
            pass
        finally:
            self.abort()

    def abort(self):
        if self.process.pid is not None and self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
        self.connection.close()
