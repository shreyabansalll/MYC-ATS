# rules/cert_rules.py
# Rank -> required certificates and rank -> minimum sea service (months).
# Externalized from pipeline/maritime_scorer.py — this is a lookup-table
# problem (~20 ranks, ~5 cert types), not logic, so it lives as data here.
# Plain Python dicts for now; move to YAML/JSON only if these start being
# edited by non-engineers.

CERT_REQUIREMENTS = {
    'master':           ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'chief officer':    ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'second officer':   ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    '2nd officer':      ['CoC', 'STCW', 'GMDSS', 'ENG1'],
    'third officer':    ['CoC', 'STCW', 'ENG1'],
    '3rd officer':      ['CoC', 'STCW', 'ENG1'],
    'deck cadet':       ['STCW', 'ENG1'],
    'engine cadet':     ['STCW', 'ENG1'],
    'chief engineer':   ['CoC', 'STCW', 'ENG1'],
    'second engineer':  ['CoC', 'STCW', 'ENG1'],
    '2nd engineer':     ['CoC', 'STCW', 'ENG1'],
    'third engineer':   ['CoC', 'STCW', 'ENG1'],
    '3rd engineer':     ['CoC', 'STCW', 'ENG1'],
    'fourth engineer':  ['CoC', 'STCW', 'ENG1'],
    'eto':              ['CoC', 'STCW', 'ENG1'],
    'electro technical officer': ['CoC', 'STCW', 'ENG1'],
    'bosun':            ['STCW', 'ENG1'],
    'ab seaman':        ['STCW', 'ENG1'],
    'able seaman':      ['STCW', 'ENG1'],
    'motorman':         ['STCW', 'ENG1'],
    'fitter':           ['STCW', 'ENG1'],
    'oiler':            ['STCW', 'ENG1'],
    'jwko':             ['STCW', 'ENG1'],
}

SEA_SERVICE_MIN = {
    'master':           36,
    'chief officer':    24,
    'second officer':   12,
    '2nd officer':      12,
    'third officer':    6,
    '3rd officer':      6,
    'deck cadet':       0,
    'engine cadet':     0,
    'chief engineer':   24,
    'second engineer':  12,
    '2nd engineer':     12,
    'third engineer':   6,
    '3rd engineer':     6,
    'fourth engineer':  6,
    'eto':              12,
    'electro technical officer': 12,
    'bosun':            24,
    'ab seaman':        12,
    'able seaman':      12,
    'motorman':         12,
    'fitter':           12,
    'oiler':            12,
    'jwko':             6,
}
