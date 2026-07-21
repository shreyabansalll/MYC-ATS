# rules/aliases.py
# Rank-detection keywords and certificate-name aliases. Externalized from
# pipeline/parser.py (RANK_KEYWORDS) and pipeline/maritime.py
# (CERT_ALIASES) — data, not logic.

RANK_KEYWORDS = {
    'master':           ['master mariner', 'dg approved master', 'master fg', 'captain', 'master '],
    'chief officer':    ['chief officer', 'c/o ', 'chief mate', 'first officer', 'first mate', '1st officer'],
    'second officer':   ['second officer', '2nd officer', '2/o '],
    'third officer':    ['third officer', '3rd officer', '3/o '],
    'chief engineer':   ['chief engineer', 'c/e ', 'chief eng'],
    'second engineer':  ['second engineer', '2nd engineer', '2/e '],
    'third engineer':   ['third engineer', '3rd engineer', '3/e '],
    'fourth engineer':  ['fourth engineer', '4th engineer', '4/e ', 'third assistant engineer', 'fifth engineer'],
    'eto':              ['eto', 'electro technical', 'electrical officer'],
    'oiler':            ['oiler ', 'motorman', 'wiper '],
    'ab seaman':        ['able seaman', 'ab seaman', ' a.b. seaman', 'able bodied seaman', 'ordinary seaman'],
    'bosun':            ['bosun', 'boatswain'],
    'deck cadet':       ['deck cadet', 'nautical cadet'],
    'engine cadet':     ['engine cadet', 'engineering cadet', 'trainee marine engineer', 'junior engineer', 'junior marine engineer'],
    'jwko':             ['junior watchkeeping officer', 'jwko', 'j.w.k.o'],
    'deck officer':     ['deck officer', 'navigating officer', 'watch officer'],
}

CERT_ALIASES = {
    'ENG1': [
        'eng1', 'eng 1', 'ml5', 'ml-5', 'medical certificate',
        'seafarer medical', 'medical fitness', 'indos medical',
        'fitness certificate', 'cdc medical', 'medical exam',
        'cdc',
        'indos', 'indos no', 'indos no.', 'indos:', 'indos number',
        'cdc no', 'cdc no.', 'cdc number', 'discharge book',
        'cdc mum', 'cdc chh', 'cdc kol',
    ],
    'GMDSS': [
        'gmdss', 'goc', 'roc', 'general operator certificate',
        'restricted operator certificate', 'gmdss/goc', 'gmdss/roc',
        'long range certificate', 'lrc',
    ],
    'STCW': [
        'stcw', 'basic safety training', 'bst', 'stcw 95', 'stcw 2010',
        'stcw basic', 'elementary first aid', 'fire prevention',
        'personal survival', 'pscrb', 'proficiency survival craft',
    ],
    'CoC': [
        'coc', 'certificate of competency', 'competency certificate',
        'mmd certificate', 'dg shipping', 'watchkeeping',
        'ooow', 'oow', 'eoow', 'meo class',
    ],
    'BOSIET': ['bosiet', 'huet', 'opito', 'offshore survival'],
}
