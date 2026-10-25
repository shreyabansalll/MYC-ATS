# rules/aliases.py
# Rank-detection keywords and certificate-name aliases. Externalized from
# pipeline/parser.py (RANK_KEYWORDS) and pipeline/maritime.py
# (CERT_ALIASES) — data, not logic.

RANK_KEYWORDS = {
    # Bare 'master ' removed — confirmed false-positive on real generated
    # output: it matched "Master of Business Administration" (an MBA on
    # an Education line), misclassifying a hospitality-industry candidate
    # with zero sea service as rank='master', the most senior deck
    # officer rank. Bare 'captain' removed for the same reason — confirmed
    # on a real 269-resume batch run: it matched "Restaurant Captain" (a
    # hospitality job title, cert_coverage 0/4), misclassifying that
    # candidate as rank='master' too. The remaining phrases are specific
    # enough not to match a degree title or an unrelated "Captain" title.
    'master':           ['master mariner', 'dg approved master', 'master fg',
                          "ship's captain", 'ship captain', 'vessel captain', 'sea captain'],
    'chief officer':    ['chief officer', 'c/o ', 'chief mate', 'first officer', 'first mate', '1st officer',
                          'staff captain'],
    'second officer':   ['second officer', '2nd officer', '2/o '],
    'third officer':    ['third officer', '3rd officer', '3/o '],
    'chief engineer':   ['chief engineer', 'c/e ', 'chief eng'],
    # 'staff captain' / '1st engineer' / 'hotel engineer' -- confirmed via
    # real MYC-supplied rank documentation, not guessed: cruise-line rank
    # ladders use different titles for the SAME organizational tier the
    # existing cargo/tanker-oriented rank names already cover. MYC's own
    # rank list confirms the equivalence explicitly ("Staff Captain (HOD
    # of Deck Department)" -- the deck department head, same role as
    # Chief Officer; the engineer tier directly below Chief Engineer is
    # "1st Engineer / Senior 1st Engineer / Hotel Engineer" on a cruise
    # ship, with their own note "4th Engineer (no 4th engineer in cruise
    # ships)" confirming the ladder is the same LENGTH, just shifted by
    # one name -- cruise "1st/2nd/3rd Engineer" occupies the same three
    # tiers cargo/tanker resumes call "2nd/3rd/4th Engineer"). Only
    # additions for strings that were previously entirely unrecognized
    # (fell through to the generic fallback) -- deliberately NOT
    # remapping "2nd Engineer"/"3rd Engineer" themselves, since those
    # strings already have an established meaning in this system and
    # reinterpreting them is a bigger, real scoring-affecting call this
    # addition doesn't make unilaterally. "First Assistant Engineer" is
    # independently confirmed as the same tier's US-convention title in
    # MYC's separate ATS-keywords document.
    'second engineer':  ['second engineer', '2nd engineer', '2/e ',
                          '1st engineer', 'senior 1st engineer', 'hotel engineer', 'first assistant engineer'],
    'third engineer':   ['third engineer', '3rd engineer', '3/e '],
    'fourth engineer':  ['fourth engineer', '4th engineer', '4/e ', 'third assistant engineer', 'fifth engineer'],
    'eto':              ['eto', 'electro technical', 'electrical officer'],
    'oiler':            ['oiler ', 'motorman', 'wiper '],
    'ab seaman':        ['able seaman', 'ab seaman', ' a.b. seaman', 'able bodied seaman', 'ordinary seaman'],
    'bosun':            ['bosun', 'boatswain'],
    'deck cadet':       ['deck cadet', 'nautical cadet'],
    'engine cadet':     ['engine cadet', 'engineering cadet', 'trainee marine engineer', 'junior engineer', 'junior marine engineer'],
    # New tier, not a remap -- 'deck cadet'/'engine cadet' already exist
    # as their own rank with a 0-month sea-service floor for exactly this
    # reason (a cadet is pre-CoC, near-zero sea service by definition);
    # there was no equivalent entry for the ETO track at all before this,
    # so an Electrical/ETO cadet's resume fell through to the generic
    # unrecognized-rank fallback (['STCW','ENG1'], 6-month minimum) --
    # wrong for a cadet the same way it would be wrong for a deck/engine
    # cadet. 'etr iii/7' is the STCW competency code for this track,
    # confirmed in MYC's rank documentation.
    'electrical cadet': ['electrical cadet', 'electro technical cadet', 'electro-technical cadet', 'etr iii/7'],
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
        'mmd certificate', 'dg shipping',
        'ooow', 'oow', 'eoow', 'meo class',
    ],
    'BOSIET': ['bosiet', 'huet', 'opito', 'offshore survival'],
}
