# rules/cruise_line_keywords.py
# Cruise-line-specific hiring language and certification emphasis,
# layered ON TOP of the existing rank-based MARITIME_JDS system in
# services/ai_client.py -- data, not logic, same convention as
# cert_rules.py/aliases.py. Sourced from real MYC research ("Key words
# Officers.pdf") into what each cruise line's own postings/ATS actually
# emphasize.
#
# This does NOT replace rank-based matching -- MARITIME_JDS is permanent
# by design (see CLAUDE.md's "AI rewriting" section: seafarers submit
# one resume to many agencies, there is no per-candidate JD to collect).
# target_cruise_line is an OPTIONAL layer used only when the specific
# target line is actually known (e.g. an MYC recruiter tailoring one
# submission to one line's application), parallel to how target_rank is
# already optional. Everything here is vocabulary/emphasis guidance for
# phrasing the candidate's OWN real experience -- never a source of new
# facts. build_rewrite_prompt() must keep Rule 0 (grounding) and Rule 10
# (never invent certs/experience) in force regardless of which cruise
# line is targeted.

UNIVERSAL_CRUISE_KEYWORDS = {
    'certifications': [
        'STCW Basic Safety Training (BST)', 'STCW II/1 (OICNW)',
        'Certificate of Competency (COC)', 'GMDSS/GOC',
        'ENG1 / Medical Fitness Certificate', "Seafarer's Discharge Book (CDC)",
        'Valid Passport (min. 12 months)', 'Proficiency in Survival Craft (PSC)',
        'Advanced Fire Fighting (AFF)', 'Medical First Aid (MFA)',
        'Passenger Ship Safety (STCW V/2)', 'Crowd Management',
        'Crisis Management & Human Behaviour',
    ],
    'keywords': [
        'Bridge watchkeeping', 'ECDIS operations', 'Passage planning', 'COLREGs',
        'SOLAS compliance', 'MARPOL compliance', 'ISM Code',
        'Safety Management System (SMS)', 'MLC 2006', 'Port State Control (PSC)',
        'Muster drill coordination', 'Guest safety briefings',
        'Emergency preparedness', 'BRM — Bridge Resource Management', 'ISPS Code',
        'Voyage data recorder (VDR)', 'AIS / ARPA / RADAR operations',
        'English language proficiency (C1+)', 'Multicultural crew management',
        'Passenger vessel experience',
    ],
}

CRUISE_LINE_KEYWORDS = {
    'disney': {
        'display_name': 'Disney Cruise Line',
        'keywords': [
            'Cast member mindset', 'Guest experience excellence',
            'Disney Standard / service culture', 'Family-friendly environment',
            'Enchanting guest interactions', 'Safety & magic balance',
            'US Public Health (USPH) compliance', 'Environmentality practices',
            'Assembly Plan duties', 'Ship Assembly Plan',
        ],
        'certifications': [
            'BRM (Bridge Resource Management)', 'ECDIS type-specific approval',
            'ENG1 medical certificate', 'Passenger familiarization (STCW V/2)',
            'Crowd & crisis management', 'Disney Traditions (onboarding program)',
            'Safety & emergency procedure cert',
        ],
        'tone': [
            'Guest-first attitude', 'Positive energy & enthusiasm',
            'Child-safe environment awareness', 'Inclusive team culture',
            'Commitment to 2-3 contracts minimum',
        ],
    },
    'rcl': {
        'display_name': 'Royal Caribbean Group',
        'keywords': [
            'Safety excellence culture', 'Continuous improvement', 'Vessel compliance',
            'Guest satisfaction scores', 'Large vessel operations (Oasis / Icon class)',
            'Dynamic positioning (DP)', 'Environmental stewardship',
            'Schengen / C1-D visa readiness',
        ],
        'certifications': [
            'STCW II/1 or II/2 (OOW or Master)', 'BRM certificate',
            'ECDIS type-specific', 'ENG1 medical', 'Security Awareness / SSO',
            'DP Basic / DP Unlimited (advantageous)',
        ],
        'tone': ['Accountability & safety ownership', 'Diversity & inclusion',
                  'Collaboration across departments'],
    },
    'carnival': {
        'display_name': 'Carnival Cruise Line',
        'keywords': [
            'OOW (Officer of the Watch)', 'PMS Safety management',
            'Safety drill assessor role', 'Potable water systems compliance',
            'USPHS standards', 'Ship certificate upkeep',
            'Training Drill & Assessment program', 'Staff Captain reporting',
        ],
        'certifications': [
            'STCW II/1 OICNW', 'Seafarers Medical Fitness Exam',
            'Security Awareness cert', 'ECDIS type-specific', 'BRM',
            '2-4 years watchkeeping experience',
        ],
        'tone': ['Fun & safety culture (Carnival values)', 'Continuous improvement mindset',
                  'Guest interaction readiness'],
    },
    'norwegian': {
        'display_name': 'Norwegian Cruise Line Holdings',
        'keywords': [
            'Freestyle cruising operations', 'Large fleet vessel handling',
            'Safety management oversight', 'Environmental compliance — NOx/SOx',
            'Flag state compliance (Bahamas / Panama)', 'DP operations',
        ],
        'certifications': [
            'STCW II/1 OICNW', 'ECDIS type-specific', 'BRM', 'ENG1 medical',
            'GMDSS / GOC', 'Passenger safety certificates (STCW V/2)',
        ],
        'tone': [],
    },
    'msc': {
        'display_name': 'Mediterranean Shipping Company',
        'keywords': [
            'Italian maritime heritage', 'Multilingual communication',
            'Mediterranean route experience', 'Panama / Malta flag compliance',
            'Environmental sustainability (MSC Foundation)',
            'LNG-ready vessel awareness',
        ],
        'certifications': [
            'STCW II/1 OICNW', 'ECDIS type-specific (NAVTOR / JRC)', 'BRM',
            'ENG1 / Seafarer medical', 'ISPS Ship Security Officer',
        ],
        'tone': ['Italian or French language (advantageous)'],
    },
    'celebrity': {
        'display_name': 'Celebrity Cruises',
        'keywords': [
            'Modern luxury operations', 'Premium guest experience',
            'Edge-class vessel operations', 'Environmental innovation',
            'Destination experience coordination',
        ],
        'certifications': ['STCW II/1 or II/2', 'BRM', 'ECDIS type-specific',
                            'ENG1', 'Security Awareness'],
        'tone': ['Luxury service standards', 'Guest-centric leadership',
                  'Innovation & continuous improvement'],
    },
    'princess': {
        'display_name': 'Princess Cruises',
        'keywords': [
            'Cadetship / training record book (TRB)',
            'Princess Medallion Class technology', 'Guest safety leadership',
            'Carnival Corporation fleet standards', 'Alaska / world voyage experience',
        ],
        'certifications': ['STCW II/1 OICNW', 'BRM', 'ECDIS type-specific',
                            'ENG1 medical', 'GMDSS GOC', 'Passenger familiarization'],
        'tone': [],
    },
    'holland_america': {
        'display_name': 'Holland America Line',
        'keywords': [
            'Classic premium service delivery', 'World voyage / grand voyage operations',
            'Older demographic guest awareness', 'Dutch maritime tradition',
            'Explorations Central (destination enrichment)',
        ],
        'certifications': ['STCW II/1 OICNW', 'BRM', 'ECDIS type-specific', 'ENG1 medical'],
        'tone': ['Dutch language (advantageous)'],
    },
    'silversea': {
        'display_name': 'Silversea Cruises',
        'keywords': [
            'Ultra-luxury all-inclusive service', 'Expedition voyage navigation',
            'Polar / Arctic route operations', 'Small ship maneuvering',
            'High net worth guest management',
        ],
        'certifications': [
            'STCW II/1 or higher', 'BRM advanced', 'ECDIS type-specific',
            'ENG1 medical', 'Polar Code / PWOM certificate',
            'DP operator certificate (advantageous)',
        ],
        'tone': [],
    },
    'viking': {
        'display_name': 'Viking Cruises',
        'keywords': [
            'Cultural enrichment cruising', 'Small ship (930 guest capacity) handling',
            'Norwegian maritime standards', 'Exploratory itinerary navigation',
            'Adults-only environment awareness',
        ],
        'certifications': ['STCW II/1 OICNW', 'BRM', 'ECDIS type-specific',
                            'ENG1 medical', 'Polar Code awareness (expeditions)'],
        'tone': [],
    },
    'regent_oceania': {
        'display_name': 'Regent Seven Seas / Oceania Cruises',
        'keywords': [
            'All-inclusive luxury operations', 'Culinary-focused itinerary support',
            'Small luxury fleet handling', 'Personalized guest service model',
        ],
        'certifications': ['STCW II/1 or higher', 'BRM', 'ECDIS type-specific',
                            'ENG1 medical', 'Security Awareness / SSO'],
        'tone': [],
    },
    'costa': {
        'display_name': 'Costa Crociere',
        'keywords': [
            'Italian/European guest culture', 'Mediterranean & Asia itineraries',
            'Carnival Corporation standards', 'Multilingual guest communication',
        ],
        'certifications': ['STCW II/1 OICNW', 'ECDIS type-specific', 'BRM', 'ENG1 medical'],
        'tone': ['Italian language (strongly advantageous)'],
    },
}

# How a caller (MYC's app, or an internal recruiter) might reasonably
# spell a cruise line's name, mapped to its canonical CRUISE_LINE_KEYWORDS
# key. Deliberately permissive (lowercase, common abbreviations) since
# this is form-field input, not a constrained dropdown on every client.
CRUISE_LINE_ALIASES = {
    'disney': 'disney', 'disney cruise line': 'disney', 'disney cruise lines': 'disney', 'dcl': 'disney',
    'rcl': 'rcl', 'royal caribbean': 'rcl', 'royal caribbean international': 'rcl',
    'royal caribbean group': 'rcl', 'rci': 'rcl',
    'carnival': 'carnival', 'carnival cruise line': 'carnival', 'carnival cruise lines': 'carnival',
    'norwegian': 'norwegian', 'norwegian cruise line': 'norwegian', 'norwegian cruise lines': 'norwegian', 'ncl': 'norwegian',
    'msc': 'msc', 'msc cruises': 'msc', 'mediterranean shipping company': 'msc',
    'celebrity': 'celebrity', 'celebrity cruises': 'celebrity',
    'princess': 'princess', 'princess cruises': 'princess',
    'holland america': 'holland_america', 'holland america line': 'holland_america',
    'holland america lines': 'holland_america', 'hal': 'holland_america',
    'silversea': 'silversea', 'silversea cruises': 'silversea',
    'viking': 'viking', 'viking cruises': 'viking', 'viking ocean cruises': 'viking',
    'regent': 'regent_oceania', 'regent seven seas': 'regent_oceania',
    'regent seven seas cruises': 'regent_oceania', 'oceania': 'regent_oceania',
    'oceania cruises': 'regent_oceania',
    'costa': 'costa', 'costa cruises': 'costa', 'costa crociere': 'costa',
}


def resolve_cruise_line(name: str) -> str:
    """
    Normalizes free-text cruise line input to its canonical
    CRUISE_LINE_KEYWORDS key. Returns '' for empty/unrecognized input --
    deliberately never raises, so an unrecognized or misspelled line
    name degrades to "no cruise-line layer applied" (plain rank-based
    behavior) rather than erroring the whole rewrite over an optional
    field.
    """
    if not name or not name.strip():
        return ''
    return CRUISE_LINE_ALIASES.get(name.strip().lower(), '')
