"""Restore cold replay configuration from the capture prefix, never environment.

This restores the state at capture creation, not a mid-session checkpoint.
Market/bootstrap/scanner/control inputs must still be replayed in order.
"""
from copy import deepcopy
from pathlib import Path

from .config import Settings
from .engine import TradingEngine
from .input_journal import SCHEMA, valid_body
from .manifest_schema import PUBLIC_CONFIG_FIELDS, SECRET_CONFIG_FIELDS
from .manifest_validation import check_manifest, fingerprint
from .offline_segment import SegmentMismatch
from .research_policy import ResearchPolicyRuntime
from .run_manifest import code_provenance, runtime_provenance
from .runtime_clock import ReplayRuntimeClock


class _RecordedSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                  dotenv_settings, file_secret_settings):
        return (init_settings,)


class _OfflineRest:
    async def close(self):
        pass

    def __getattr__(self, name):
        raise SegmentMismatch('network adapter unavailable in offline engine')


class _MemoryRecorder:
    """No file, writer thread, or system-clock observation."""
    def __init__(self, clock):
        self.clock = clock
        self.path = '<offline-memory>'
        self.rows = []

    def record(self, event, symbol, payload):
        self.rows.append(deepcopy(dict(event=event, symbol=symbol, payload=payload)))

    def health(self):
        return {'mode': 'offline_memory', 'pending': 0, 'dropped': 0, 'writerError': None}

    def close(self):
        pass

    def start_background_writer(self):
        raise SegmentMismatch('live recorder unavailable in offline engine')


class OfflineEngine(TradingEngine):
    async def start(self):
        raise SegmentMismatch('live startup forbidden in offline engine')

    def set_running(self, value):
        raise SegmentMismatch('control replay requires a session dispatcher')


def restore_cold_engine(prefix):
    """Consume exactly header/capture-manifest/policy rows; fail closed.

    Source comparison describes files on disk, not loaded bytecode attestation.
    No paths or executable content from the journal are opened or evaluated.
    """
    rows = deepcopy(list(prefix))
    if len(rows) != 3:
        raise SegmentMismatch('expected complete three-row capture prefix')
    previous = None
    for sequence, (row, kind) in enumerate(zip(rows, ('header', 'manifest', 'policy_snapshot')), 1):
        if (row.get('schema') != SCHEMA or row.get('kind') != kind
                or row.get('sequence') != sequence or row.get('previousHash') != previous
                or not valid_body(kind, row.get('symbol'), row.get('body'))
                or row.get('hash') != fingerprint({k: v for k, v in row.items() if k != 'hash'})):
            raise SegmentMismatch('invalid capture prefix')
        previous = row['hash']
    if rows[1]['body']['phase'] != 'capture':
        raise SegmentMismatch('run manifest cannot replace capture manifest')
    manifest = rows[1]['body']['manifest']
    if manifest.get('manifestVersion') not in (3, 4) or check_manifest(manifest)[1]:
        raise SegmentMismatch('incomplete or invalid capture manifest')
    if (manifest['recordSchemaVersion'] != 'jsonl-clock-v2'
            or manifest['executionModelVersion'] != 'paper-v1'):
        raise SegmentMismatch('unsupported execution model')
    current_code = code_provenance(Path(__file__).resolve().parents[1])
    if manifest['code']['sourceSha256'] != current_code['sourceSha256']:
        raise SegmentMismatch('source code differs from recorded capture')
    if manifest['runtimeSha256'] != fingerprint(runtime_provenance()):
        raise SegmentMismatch('runtime differs from recorded capture')
    if set(Settings.model_fields) != PUBLIC_CONFIG_FIELDS | SECRET_CONFIG_FIELDS:
        raise SegmentMismatch('settings contain unclassified fields')
    try:
        config = _RecordedSettings(_env_file=None, **manifest['config'],
                                   bybit_api_key='', bybit_api_secret='')
    except Exception:
        raise SegmentMismatch('invalid recorded configuration') from None
    if fingerprint({k: getattr(config, k) for k in PUBLIC_CONFIG_FIELDS}) != manifest['configSha256']:
        raise SegmentMismatch('configuration changed during validation')
    snapshot = rows[2]['body']
    public = manifest['researchPolicy']
    if (snapshot['mode'] != config.research_policy_mode or snapshot['mode'] != public.get('mode')
            or snapshot['sourceFileSha256'] != public.get('sourceSha256')
            or (snapshot['manifest'] or {}).get('policyFingerprint') != public.get('policyFingerprint')):
        raise SegmentMismatch('policy snapshot binding mismatch')
    policy = ResearchPolicyRuntime(mode=snapshot['mode'], manifest=snapshot['manifest'],
        source_sha256=snapshot['sourceFileSha256'], source_path=public.get('sourcePath'))
    if policy.public() != public:
        raise SegmentMismatch('policy metadata differs from capture')
    clock = ReplayRuntimeClock(wall_seconds=rows[0]['processingWallSeconds'],
                               mono_ns=rows[0]['processingMonoNs'])
    engine = OfflineEngine(config, clock=clock, rest_client=_OfflineRest(),
        recorder=_MemoryRecorder(clock), research_policy=policy, configure_observability=False)
    enabled = set(manifest['strategies']['enabled'])
    if not enabled <= engine.strategies.keys():
        raise SegmentMismatch('unknown recorded strategy')
    engine.strategy_enabled = {key: key in enabled for key in engine.strategies}
    engine.replay_origin = {'manifestId': manifest['manifestId'],
        'manifestSha256': manifest['manifestSha256'], 'configSha256': manifest['configSha256'],
        'sourceSha256': current_code['sourceSha256'], 'runtimeSha256': manifest['runtimeSha256'],
        'policySha256': fingerprint({'public': policy.public(), 'manifest': policy.manifest}),
        'strategiesSha256': fingerprint(engine.strategy_enabled),
        'prefixHash': rows[-1]['hash'], 'nextSequence': 4, 'state': 'cold', 'parityReady': False}
    return engine
