"""Small derived vocabulary relationships; source registries remain canonical."""
from rules.aliases import CERT_ALIASES, RANK_KEYWORDS
from rules.cert_rules import CERT_REQUIREMENTS
from generic_ats.career_track import ATS_RANK_KEYWORDS

MARITIME_ONTOLOGY = {
    'ranks': {rank: {'aliases': tuple(aliases),
                     'department': 'engine' if 'engine' in rank or rank in ('eto', 'electrical cadet') else 'deck',
                     'keywords': tuple(ATS_RANK_KEYWORDS.get(rank, ())) }
              for rank, aliases in RANK_KEYWORDS.items()},
    'certificates': {name: {'aliases': tuple(aliases),
                            'required_for': tuple(rank for rank, certs in CERT_REQUIREMENTS.items() if name in certs)}
                     for name, aliases in CERT_ALIASES.items()},
    'provenance': 'Derived from rules/aliases.py, rules/cert_rules.py, and generic_ats/career_track.py; not a regulatory authority.',
}
