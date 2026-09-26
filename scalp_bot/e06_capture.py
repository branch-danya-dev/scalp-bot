"""Fixed E06 paper capture contract; no dotenv, credentials or live execution."""
import json
from pathlib import Path

from .e01_live import smoke_settings
from .manifest_schema import PUBLIC_CONFIG_FIELDS


DURATIONS = {'smoke': 1800, 'independent': 43200}
MAX_EVENTS = {'smoke': 5_000_000, 'independent': 60_000_000}
PROFILES = {'smoke': 'e06-smoke.json', 'independent': 'e06-independent-12h.json'}
PROTOCOL = {
    'id': 'E06-independent-12h-v1', 'experiment': 'E06',
    'technicalSmokeSeconds': 1800, 'independentSeconds': 43200,
    'baseline': 'sustained hold 8s; E01 off; E06 off',
    'candidate': 'current nearCloses>=2 AND compressedPullbacks>=2:3s; else8s; E01 off',
    'primaryMetric': 'candidate full-lifecycle net minus baseline net after fees/funding',
    'stopRule': 'fixed duration; no optional PnL stopping or retries of invalid captures',
    'blocks': 'six chronological 2h blocks from shared start; trades assigned by entry time',
    'minimumDifferingSetupIdentities': 30,
    'minimumDifferingSymbols': 3, 'minimumDifferingBlocks': 3,
    'minimumNetImprovementEquityFraction': .001,
    'maximumCandidateToBaselineSampledDrawdownRatio': 1.10,
    'zeroBaselineDrawdownRule': 'candidate drawdown must also be zero',
    'assessment': 'insufficient evidence if coverage fails; positive candidate net, net improvement and drawdown limits only admit further validation, never live',
    'requiredBeforeAssessment': 'sealed capture, financial audit, full native paired replay parity, concentration and block analysis',
    'smokeIsHoldout': False, 'independentIsUntouched': True,
    'productionReady': False,
}


def settings(profile, phase):
    if phase not in DURATIONS: raise ValueError('unknown E06 phase')
    config = smoke_settings(profile, max_duration_seconds=DURATIONS[phase])
    if (config.paper_run_duration_seconds != DURATIONS[phase] or
        config.e01_breakout_obstacle_veto or config.e06_conditional_breakout_hold or
        config.breakout_hold_without_retest_seconds != 8 or config.breakout_staged_entries_enabled):
        raise ValueError('E06 profile differs from the fixed experiment')
    return config


def load_profiles(root):
    configs = {phase: settings(json.loads((root/'configs'/name).read_text(encoding='utf-8')),phase)
               for phase,name in PROFILES.items()}
    different = {field for field in PUBLIC_CONFIG_FIELDS
                 if getattr(configs['smoke'],field) != getattr(configs['independent'],field)}
    if different != {'run_label','paper_run_duration_seconds'}:
        raise ValueError('smoke and independent profiles differ beyond label/duration')
    return configs


def verify_smoke(root, config, audit):
    report = audit(root)
    result = json.loads((root/'result.json').read_text(encoding='utf-8'))
    if report['experiment'] != 'E06' or result.get('purpose') != 'technical_smoke':
        raise ValueError('requires an E06 technical smoke')
    actual = result['portfolios']['baseline']['manifest']['config']
    expected = {k:getattr(config,k) for k in PUBLIC_CONFIG_FIELDS}
    if actual != expected: raise ValueError('smoke profile differs from frozen profile')
    if any(p['durationSeconds'] < DURATIONS['smoke']-1 for p in report['portfolios'].values()):
        raise ValueError('smoke was shorter than required')
    return report
