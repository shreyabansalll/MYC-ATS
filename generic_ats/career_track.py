"""career_track → maritime overlay + JD template mapping.

When career_track is absent or explicitly generic, Layer 2 (maritime) stays
off. Known maritime ranks/tracks (and existing MYC fields target_rank /
target_cruise_line) turn it on. Unrecognized track names stay generic —
we do not invent a maritime overlay for an unrelated industry.
"""

MARITIME_GENERIC = "generic"
MARITIME_NONE = frozenset({"", "generic", "none", "general", "other"})

# Values a caller can send as career_track that mean "run the maritime overlay".
MARITIME_TRACKS = {
    "maritime",
    "seafarer",
    "seafaring",
    "cruise",
    "cruise_line",
    "merchant_navy",
    "deck",
    "engine",
    "eto",
    "deck_officer",
    "chief_officer",
    "second_officer",
    "third_officer",
    "master",
    "chief_engineer",
    "second_engineer",
    "third_engineer",
    "fourth_engineer",
    "engineer",
    "able_seaman",
    "oiler",
    "deck_cadet",
    "engine_cadet",
    "electrical_cadet",
    "rating",
    "bosun",
}

# Rank-style career_track values (spaces or underscores) → MARITIME_JDS key.
# Kept here so generic scoring can pick a JD without importing the rewriter.
TRACK_TO_JD = {
    "maritime": "deck_officer",
    "seafarer": "deck_officer",
    "seafaring": "deck_officer",
    "cruise": "deck_officer",
    "cruise_line": "deck_officer",
    "merchant_navy": "deck_officer",
    "deck": "deck_officer",
    "deck_officer": "deck_officer",
    "master": "deck_officer",
    "chief_officer": "chief_officer",
    "chief officer": "chief_officer",
    "second_officer": "second_officer",
    "second officer": "second_officer",
    "third_officer": "deck_officer",
    "third officer": "deck_officer",
    "engine": "engineer",
    "engineer": "engineer",
    "chief_engineer": "engineer",
    "chief engineer": "engineer",
    "second_engineer": "engineer",
    "second engineer": "engineer",
    "third_engineer": "engineer",
    "third engineer": "engineer",
    "fourth_engineer": "fourth_engineer",
    "fourth engineer": "fourth_engineer",
    "eto": "eto",
    "electro_technical_officer": "eto",
    "electro technical officer": "eto",
    "able_seaman": "able_seaman",
    "able seaman": "able_seaman",
    "ab seaman": "able_seaman",
    "bosun": "able_seaman",
    "oiler": "oiler",
    "deck_cadet": "deck_cadet",
    "deck cadet": "deck_cadet",
    "engine_cadet": "rating",
    "engine cadet": "rating",
    "electrical_cadet": "eto",
    "electrical cadet": "eto",
    "rating": "rating",
}

COMPANY_KEYWORDS = {
    'carnival': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'OOW', 'PMS safety management', 'Safety drill assessor role',
            'Potable water systems compliance', 'USPHS standards',
            'Ship certificate upkeep', 'Training Drill & Assessment program',
            'Staff Captain reporting', 'Continuous improvement',
        ],
        'engine': [
            'Azipod propulsion systems', 'HFO / MDO / LO purifiers',
            'Bilge and sludge handling', 'Quick-closing valve operation',
            'Emergency shutdown procedures', 'Potable / grey / black water systems',
            'Staff Chief Engineer reporting', 'USPHS sanitation compliance',
        ],
        'eto': [
            'Electrical plant operation and maintenance', 'HV switchboard and distribution boards',
            'Azipod electrical drive systems', 'IAS / SCADA monitoring',
            'Navigation electronics', 'IT network infrastructure maintenance',
            'CCTV and security electronics', 'Fire alarm and detection system upkeep',
            'PMS software (electrical module)',
        ],
    },
    'royal_caribbean': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Safety excellence culture', 'Continuous improvement', 'Vessel compliance',
            'Guest satisfaction scores', 'Large vessel operations', 'Oasis class',
            'Icon class', 'Dynamic positioning', 'Environmental stewardship',
            'Schengen visa readiness', 'C1-D visa readiness', 'Diversity and inclusion',
        ],
        'engine': [
            'Main and auxiliary engine operation', 'Propulsion plant management',
            'Large vessel systems', 'Hotel services engineering',
            'Technical space maintenance', 'Safety and regulatory compliance',
            'Schengen visa readiness', 'C1-D visa readiness',
            'Environmental stewardship', 'Zero-incident mindset',
        ],
        'eto': [
            '11kV', '6.6kV', 'Main electrical plant', 'Electric propulsion (Azipod)',
            'Integrated automation and control', 'Frequency converter / VFD maintenance',
            'Shipboard IT and communications networks', 'GMDSS equipment maintenance',
            'Navigation electronics upkeep', 'Electrical survey readiness',
        ],
    },
    'disney': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Cast member mindset', 'Guest experience excellence',
            'Disney Standard / service culture', 'Family-friendly environment',
            'Enchanting guest interactions', 'Safety and magic balance',
            'US Public Health compliance', 'Environmentality practices',
            'Assembly Plan duties', 'Ship Assembly Plan',
        ],
        'engine': [
            'Hotel services engineering', 'HVAC and plumbing',
            'Main propulsion plant oversight', 'Technical space and machinery maintenance',
            'US Public Health sanitation', 'Environmentality practices',
            'Assembly Plan duties', 'Guest and crew safety environment',
            'Cast member mindset', 'Behind-the-scenes excellence',
        ],
        'eto': [
            'Entertainment and AV systems maintenance', 'Theatrical lighting',
            'Stage electronics', 'Guest cabin electrical systems',
            'HV and LV distribution maintenance', 'US Public Health sanitation',
            'Environmentality', 'IT and network infrastructure',
            'Fire detection and alarm systems', 'Cast member mindset',
        ],
    },
    'ncl': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Freestyle cruising operations', 'Large fleet vessel handling',
            'Safety management oversight', 'NOx / SOx environmental compliance',
            'Flag state compliance', 'Bahamas flag', 'Panama flag', 'DP operations',
        ],
        'engine': [
            'Diesel-electric propulsion', 'LNG-aware systems', 'NOx / SOx emissions monitoring',
            'Scrubber (EGCS) system operation', 'Ballast water treatment systems',
            'Flag state compliance', 'Bahamas flag',
        ],
        'eto': [
            'HV and LV electrical plant maintenance', 'Diesel generator sets',
            'Electric propulsion systems', 'IAS / SCADA automation',
            'Switchboard operations', 'Navigation electronics maintenance',
            'GMDSS equipment upkeep', 'Shipboard LAN / VSAT',
            'LNG electrical systems awareness', 'Scrubber / EGCS electrical controls',
        ],
    },
    'msc': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Italian maritime heritage', 'Multilingual communication',
            'Mediterranean route experience', 'Panama flag compliance',
            'Malta flag compliance', 'Environmental sustainability',
            'LNG-ready vessel awareness',
        ],
        'engine': [
            'Diesel-electric plant operation', 'LNG dual-fuel engine systems',
            'Azipod / pod propulsion', 'Malta flag compliance',
            'Panama flag compliance', 'Emissions sustainability',
            'Multilingual crew coordination',
        ],
        'eto': [
            'HV and LV electrical plant maintenance', 'Diesel generator sets',
            'Electric propulsion systems', 'IAS / SCADA automation',
            'Switchboard operations', 'Navigation electronics maintenance',
            'GMDSS equipment upkeep', 'Shipboard LAN / VSAT',
            'LNG electrical systems awareness',
        ],
    },
    'celebrity': {
        'source': 'Key words Officers.pdf — MYC reference',
        'deck': [
            'Modern luxury operations', 'Premium guest experience',
            'Edge-class vessel operations', 'Environmental innovation',
            'Destination experience coordination', 'Luxury service standards',
        ],
        'engine': [],
        'eto': [],
    },
    'princess': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Cadetship / training record book', 'Princess Medallion Class technology',
            'Guest safety leadership', 'Carnival Corporation fleet standards',
            'Alaska voyage experience', 'World voyage experience',
        ],
        'engine': [
            'Carnival Corporation standards compliance', 'Planned Maintenance System',
            'Engine department safety drills', 'Class and flag state survey readiness',
            'Hotel services plant', 'HVAC and refrigeration',
            'Diesel engine and gas turbine operations',
        ],
        'eto': [
            'HV and LV electrical plant maintenance', 'Diesel generator sets',
            'Electric propulsion systems', 'IAS / SCADA automation',
            'Switchboard operations', 'Navigation electronics maintenance',
            'GMDSS equipment upkeep', 'Shipboard LAN / VSAT',
        ],
    },
    'hal': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Classic premium service delivery', 'World voyage operations',
            'Grand voyage operations', 'Older demographic guest awareness',
            'Dutch maritime tradition', 'Explorations Central',
        ],
        'engine': [
            'Carnival Corporation standards compliance', 'Planned Maintenance System',
            'Engine department safety drills', 'Class and flag state survey readiness',
            'Hotel services plant', 'HVAC and refrigeration',
        ],
        'eto': [
            'HV and LV electrical plant maintenance', 'Diesel generator sets',
            'Electric propulsion systems', 'IAS / SCADA automation',
            'Switchboard operations', 'Navigation electronics maintenance',
            'GMDSS equipment upkeep',
        ],
    },
    'silversea': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Ultra-luxury all-inclusive service', 'Expedition voyage navigation',
            'Polar / Arctic route operations', 'Small ship maneuvering',
            'High net worth guest management', 'Polar Code', 'PWOM',
        ],
        'engine': [
            'Small ship engineering', 'Expedition vessel systems',
            'Polar / Arctic machinery operation', 'High-end HVAC and hotel plant',
            'Low-noise / vibration-sensitive machinery',
            'Multi-system competency for reduced crew',
        ],
        'eto': [
            'Small ship full electrical responsibility',
            'High-end hotel AV and entertainment electronics',
            'Low-noise / EMC-sensitive systems', 'Expedition vessel electrical systems',
            'Polar Code electrical awareness', 'Multi-role competency',
        ],
    },
    'viking': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Cultural enrichment cruising', 'Small ship handling',
            'Norwegian maritime standards', 'Exploratory itinerary navigation',
            'Adults-only environment awareness',
        ],
        'engine': [
            'Small ship engineering', 'Expedition vessel systems',
            'Polar / Arctic machinery operation', 'High-end HVAC and hotel plant',
            'Low-noise / vibration-sensitive machinery',
            'Multi-system competency for reduced crew',
        ],
        'eto': [
            'Small ship full electrical responsibility',
            'High-end hotel AV and entertainment electronics',
            'Low-noise / EMC-sensitive systems', 'Expedition vessel electrical systems',
            'Multi-role competency',
        ],
    },
    'regent_oceania': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'All-inclusive luxury operations', 'Culinary-focused itinerary support',
            'Small luxury fleet handling', 'Personalized guest service model',
        ],
        'engine': [
            'Small ship engineering', 'Expedition vessel systems',
            'High-end HVAC and hotel plant', 'Low-noise / vibration-sensitive machinery',
            'Multi-system competency for reduced crew',
        ],
        'eto': [
            'Small ship full electrical responsibility',
            'High-end hotel AV and entertainment electronics',
            'Low-noise / EMC-sensitive systems', 'Multi-role competency',
        ],
    },
    'costa': {
        'source': 'Key words Officers.pdf and Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [
            'Italian / European guest culture', 'Mediterranean and Asia itineraries',
            'Carnival Corporation standards', 'Multilingual guest communication',
        ],
        'engine': [
            'LNG / dual-fuel engine operation', 'Scrubber (EGCS) operation',
            'European environmental regulations', 'NOx / SOx emissions monitoring',
            'Ballast water management', 'Italian maritime standards',
        ],
        'eto': [
            'HV and LV electrical plant maintenance', 'Diesel generator sets',
            'Electric propulsion systems', 'IAS / SCADA automation',
            'Switchboard operations', 'Navigation electronics maintenance',
            'GMDSS equipment upkeep', 'LNG electrical systems awareness',
        ],
    },
    'aida': {
        'source': 'Keyword Engineers and ETO (1).pdf — MYC reference',
        'deck': [],
        'engine': [
            'LNG / dual-fuel engine operation', 'Scrubber (EGCS) operation',
            'European environmental regulations', 'NOx / SOx emissions monitoring',
            'Ballast water management', 'German maritime standards',
        ],
        'eto': [],
    },
    'virgin': {
        'source': 'ATS SOFTWARES.docx — no company keyword list supplied',
        'deck': [],
        'engine': [],
        'eto': [],
    },
}

# Vendor names and ecosystems below are MYC-supplied operational assumptions,
# not independently verified descriptions of an employer's current configuration.
COMPANY_ATS_PLATFORM = {
    'carnival': 'Workday / SuccessFactors ecosystem',
    'royal_caribbean': 'Workday',
    'ncl': 'Taleo / Workday integrations',
    'msc': 'SuccessFactors / Workday',
    'disney': 'Taleo / Disney enterprise ATS',
    'virgin': 'Greenhouse / modern ATS stack',
    'princess': 'Carnival ecosystem ATS',
    'hal': 'Carnival ecosystem ATS',
    'costa': 'SAP SuccessFactors',
    'aida': 'SAP ecosystem',
    'celebrity': 'Workday',
    'silversea': 'Not specified in ATS SOFTWARES.docx',
    'viking': 'Not specified in ATS SOFTWARES.docx',
    'regent_oceania': 'Not specified in ATS SOFTWARES.docx',
}
COMPANY_ATS_PLATFORM_SOURCE = 'ATS SOFTWARES.docx — MYC-supplied operational assumption'

COMPANY_ALIASES = {
    'carnival': 'carnival',
    'carnival cruise line': 'carnival',
    'carnival corporation': 'carnival',
    'royal caribbean': 'royal_caribbean',
    'royal caribbean group': 'royal_caribbean',
    'royal caribbean international': 'royal_caribbean',
    'rcl': 'royal_caribbean',
    'rci': 'royal_caribbean',
    'disney': 'disney',
    'disney cruise line': 'disney',
    'dcl': 'disney',
    'norwegian': 'ncl',
    'norwegian cruise line': 'ncl',
    'norwegian cruise line holdings': 'ncl',
    'ncl': 'ncl',
    'msc': 'msc',
    'msc cruises': 'msc',
    'mediterranean shipping company': 'msc',
    'celebrity': 'celebrity',
    'celebrity cruises': 'celebrity',
    'princess': 'princess',
    'princess cruises': 'princess',
    'holland america': 'hal',
    'holland america line': 'hal',
    'hal': 'hal',
    'silversea': 'silversea',
    'viking': 'viking',
    'regent': 'regent_oceania',
    'regent seven seas': 'regent_oceania',
    'oceania': 'regent_oceania',
    'costa': 'costa',
    'costa cruises': 'costa',
    'aida': 'aida',
    'virgin': 'virgin',
    'virgin voyages': 'virgin',
}

ATS_RANK_KEYWORDS = {
    'chief_engineer': [
        'Engine Department Management', 'Diesel Electric Propulsion', 'Main Engine Operations',
        'Auxiliary Engine Operations', 'Power Management System', 'Fuel Optimization',
        'Planned Maintenance System', 'Preventive Maintenance', 'Engine Automation',
        'High Voltage Systems', 'HVAC Systems', 'Refrigeration Systems', 'Boiler Operations',
        'Sewage Treatment Plant', 'Fresh Water Generator', 'Incinerator Operations',
        'Waste Management Systems', 'Dry Dock Supervision', 'Class Surveys',
        'Energy Efficiency Management', 'Passenger Vessel Experience',
        'Hotel Technical Operations', 'Guest Comfort Systems', 'USPH Compliance',
        'Environmental Compliance', 'Multinational Crew Management',
        'Cruise Vessel Operations', 'Safety Management System',
        'MAN B&W', 'Wartsila', 'ABB', 'Azipod', 'Kongsberg', 'Siemens Automation',
        'Main Diesel Engine', 'Auxiliary Diesel Generator', 'Fuel Oil System',
        'Lube Oil System', 'Purifier', 'Separator', 'Air Compressor',
        'Heat Exchanger', 'Cooling Water System', 'Turbocharger',
        'Ballast Water Management System', 'Oily Water Separator',
        'Oil Discharge Monitoring Equipment', 'Freshwater Generator',
        'Engine Room Resource Management', 'Failure Analysis',
        'Budget Control', 'Inventory Control', 'Spare Parts Management',
        'Machinery Safety Systems', 'Integrated Propulsion Plant',
        'Marine Engine System Analysis',
    ],
    'chief_officer': [
        'Navigation and Operations', 'Cargo Operations', 'Stability Calculations',
        'Voyage Planning', 'ECDIS', 'Bridge Team Management', 'Mooring Operations',
        'Tender Operations', 'Dynamic Positioning', 'GMDSS', 'ISPS Compliance',
        'Lifeboat Operations', 'Emergency Drills', 'Passenger Safety',
        'Crowd Management', 'Guest Safety', 'Port Operations',
        'Cruise Terminal Coordination', 'Security Operations',
        'International Crew Supervision', 'SOLAS', 'MARPOL', 'Risk Assessment',
        'Permit to Work', 'Safety Management System',
    ],
    'second_officer': [
        'Voyage Planning', 'Passage Planning', 'ECDIS', 'ARPA', 'Radar Navigation',
        'GMDSS', 'Chart Corrections', 'Navigational Watchkeeping',
        'Collision Regulations', 'Bridge Resource Management', 'Weather Routing',
        'Passenger Vessel Navigation', 'Safety Drills', 'Crowd Management',
        'Tender Operations', 'Emergency Response', 'Bridge Procedures',
        'International Waters Experience', 'Safety Compliance',
    ],
    'third_officer': [
        'Third Officer', '3rd Officer', 'Junior Navigating Officer',
        'Bridge Watchkeeping', 'ECDIS Familiarization', 'Radar Navigation', 'GMDSS',
        'Passage Planning', 'Chart Corrections', 'Mooring Operations',
        'Lifeboat Operations', 'Safety Drills', 'Passenger Safety',
        'Crowd Management', 'Emergency Procedures', 'Guest Safety Awareness',
    ],
    'second_engineer': [
        'Engine Room Management', 'Main Engine Maintenance', 'Auxiliary Machinery',
        'Fuel Purifiers', 'Boiler Operations', 'Fresh Water Generator',
        'Sewage Treatment Plant', 'Engine Automation', 'PMS',
        'Preventive Maintenance', 'Engine Room Watchkeeping',
        'Hotel Technical Support', 'HVAC Operations', 'Refrigeration Plant',
        'Guest Area Maintenance', 'Environmental Systems',
        'Engine Room Watch', 'Machinery Operation', 'Machinery Maintenance',
        'Main Engine Overhaul', 'Auxiliary Engine Overhaul', 'Turbocharger Maintenance',
        'Air Compressor Maintenance', 'Pump Maintenance', 'Separator Maintenance',
        'Cooling System', 'Fuel Oil Treatment', 'Lube Oil Treatment',
        'Steam System', 'HT / LT Cooling Water', 'Engine Room Inventory',
        'Engine Log and Record Keeping', 'Machinery Condition Monitoring',
        'Dry Dock', 'Class and Flag State Inspections',
    ],
    'third_engineer': [
        'Third Engineer', '3rd Engineer', 'Auxiliary Engine Operations',
        'Fuel Systems', 'Pump Maintenance', 'Purifier Maintenance',
        'Boiler Watchkeeping', 'Lubrication Systems', 'Mechanical Troubleshooting',
        'PMS', 'Safety Procedures', 'Hotel Maintenance Support', 'HVAC Systems',
        'Environmental Compliance',
        'Air Compressor', 'Heat Exchanger', 'Bilge Pump', 'Ballast Pump',
        'Fuel Oil Purifier', 'Lube Oil Purifier', 'Separator',
        'Fresh Water Generator', 'Refrigeration Systems', 'Boiler Operation',
        'Engine Room Machinery Maintenance', 'Planned Maintenance System',
        'Machinery Log Keeping', 'Equipment Calibration',
        'Marpol Equipment', 'Waste Management',
    ],
    'fourth_engineer': [
        'Fourth Engineer', '4th Engineer', 'Junior Engineer Officer',
        'Engine Room Watchkeeping', 'Pump Operations', 'Valve Overhaul',
        'Auxiliary Machinery', 'Fuel Oil Systems', 'Lube Oil Systems',
        'Safety Procedures', 'Workshop Skills', 'Mechanical Maintenance',
        'Guest Services Support', 'Hotel Operations Assistance',
    ],
    'junior_engineer': [
        'Junior Engineer', 'Engine Junior Officer', 'Engine Room Operations',
        'Planned Maintenance', 'Auxiliary Machinery', 'Mechanical Troubleshooting',
        'Workshop Maintenance', 'Pump Maintenance', 'Valve Maintenance',
        'Fuel Systems', 'Safety Procedures', 'Hotel Maintenance Assistance',
        'Passenger Vessel Familiarization',
    ],
    'engine_cadet': [
        'Engine Cadet', 'Marine Engineering Cadet', 'Engine Room Familiarization',
        'Watchkeeping Assistance', 'Planned Maintenance Exposure', 'Workshop Training',
        'Pump Operations', 'Auxiliary Machinery', 'Safety Familiarization',
        'STCW Basic Safety Training', 'Passenger Vessel Training', 'Safety Drills',
        'Multicultural Environment',
    ],
    'deck_cadet': [
        'Deck Cadet', 'Nautical Cadet', 'Bridge Familiarization', 'Navigation Watch',
        'Voyage Planning', 'ECDIS Familiarization', 'Chart Corrections',
        'Mooring Operations', 'Bridge Procedures', 'Safety Drills',
        'GMDSS Familiarization', 'Passenger Safety', 'Crowd Management',
        'Tender Operations', 'Guest Interaction Awareness',
    ],
    'eto': [
        'Electro Technical Officer', 'ETO', 'Electro Technical Engineer',
        'High Voltage Systems', 'Electrical Maintenance', 'PLC Troubleshooting',
        'Automation Systems', 'Power Management System', 'Alarm Monitoring Systems',
        'Generator Synchronization', 'Variable Frequency Drives',
        'Integrated Automation Systems', 'Switchboard Operations',
        'Diesel Electric Propulsion', 'Hotel Electrical Operations',
        'Passenger Comfort Systems', 'Entertainment Systems', 'HVAC Electrical Systems',
        'Fire Detection Systems', 'Communication Systems',
        'ABB Systems', 'Siemens Automation', 'Kongsberg Automation',
        'Low Voltage Systems', '440V Systems', '6.6kV Systems', '11kV Systems',
        'HV Switching', 'HV Protection', 'Insulation Resistance Testing',
        'Megger Testing', 'Relay Settings', 'Generator Synchronization',
        'Automatic Voltage Regulator', 'AVR', 'Busbar and Bus Tie Operation',
        'Shore Power Connection', 'Shaft Generator', 'Frequency Converter',
        'Soft Starter', 'Motor Control Center', 'MCC',
        'UPS Systems', 'Battery Systems', 'Emergency Power Systems',
        'SCADA Systems', 'Programmable Logic Controller',
        'Navigation Systems', 'GMDSS Maintenance', 'VDR Maintenance',
        'Radar / Gyro / ECDIS Maintenance', 'Azipod Control Systems',
        'Thruster Control Systems', 'Dynamic Positioning Systems',
        'Shipboard IT and Communications', 'Satellite Communications',
        'CCTV Systems', 'PA System', 'Galley Electrical Systems',
        'Fire Alarm and Detection Systems', 'Emergency Lighting Systems',
        'Electrical Fault Diagnosis', 'Class Surveys and Electrical Surveys',
        'High Voltage Permit to Work', 'Electrical Safety Procedures',
        'IEC Standards', 'Energy Metering',
    ],
    'electrical_cadet': [
        'Electrical Cadet', 'Electro Technical Cadet', 'Electrical Maintenance',
        'Switchboard Familiarization', 'Power Distribution', 'Automation Exposure',
        'Alarm Systems', 'Generator Systems', 'Electrical Troubleshooting',
        'High Voltage Awareness', 'Passenger Vessel Familiarization',
        'Hotel Electrical Systems', 'Safety Procedures',
    ],
}
ATS_RANK_KEYWORDS_SOURCE = 'ATS SOFTWARES.docx — MYC rank-wise ATS keyword reference'


def resolve_company(name: str) -> str:
    return COMPANY_ALIASES.get((name or '').strip().casefold(), '')


def department_for_rank(rank: str) -> str:
    normalized = (rank or '').casefold().replace('_', ' ').strip()
    if any(term in normalized for term in ('eto', 'electro technical', 'electrical')):
        return 'eto'
    if any(term in normalized for term in (
        'engine', 'engineer', 'oiler', 'motorman', 'fitter', 'mechanic', 'wiper',
    )):
        return 'engine'
    if any(term in normalized for term in (
        'master', 'officer', 'deck', 'bosun', 'seaman', 'boatswain', 'jwko',
    )):
        return 'deck'
    return ''


def company_ats_platform(company: str) -> dict:
    resolved = resolve_company(company)
    assumption = COMPANY_ATS_PLATFORM.get(resolved)
    if not assumption:
        return {
            'company': resolved or None,
            'platform': 'unknown',
            'assumption': None,
            'source': COMPANY_ATS_PLATFORM_SOURCE,
        }
    known_platforms = ('workday', 'successfactors', 'taleo', 'greenhouse')
    candidates = [
        platform for platform in known_platforms
        if platform in assumption.casefold()
    ]
    platform = candidates[0] if len(candidates) == 1 else 'unknown'
    return {
        'company': resolved,
        'platform': platform,
        'assumption': assumption,
        'source': COMPANY_ATS_PLATFORM_SOURCE,
    }


def score_company_keywords(company: str, department: str, resume_text: str) -> dict:
    from config import COMPANY_KEYWORD_WEIGHT_MULTIPLIER
    from generic_ats.text_match import keyword_in_text

    resolved = resolve_company(company)
    keyword_set = COMPANY_KEYWORDS.get(resolved, {})
    keywords = keyword_set.get(department, [])
    matches = [
        keyword for keyword in keywords
        if keyword_in_text(keyword, resume_text or '')
    ]
    score = (
        min(100.0, round(100 * len(matches) * COMPANY_KEYWORD_WEIGHT_MULTIPLIER / len(keywords), 1))
        if keywords else 0.0
    )
    return {
        'company': resolved or None,
        'department': department or None,
        'matches': matches,
        'score': score,
        'weight_multiplier': COMPANY_KEYWORD_WEIGHT_MULTIPLIER,
        'source': keyword_set.get('source') if keyword_set else None,
    }


def score_rank_keywords(rank: str, resume_text: str) -> dict:
    from generic_ats.text_match import keyword_in_text

    normalized = (rank or '').strip().casefold().replace('-', ' ').replace('_', ' ')
    rank_key = normalized.replace(' ', '_')
    aliases = {
        'chief mate': 'chief_officer',
        'staff captain': 'chief_officer',
        '2nd officer': 'second_officer',
        '3rd officer': 'third_officer',
        'chief engineer': 'chief_engineer',
        'first engineer': 'second_engineer',
        '1st engineer': 'second_engineer',
        '2nd engineer': 'second_engineer',
        'first assistant engineer': 'second_engineer',
        'senior 1st engineer': 'second_engineer',
        'hotel engineer': 'second_engineer',
        '3rd engineer': 'third_engineer',
        '4th engineer': 'fourth_engineer',
        'marine engineer': 'second_engineer',
        'chief eto': 'eto',
        'first eto': 'eto',
        'second eto': 'eto',
        'third eto': 'eto',
        'electro technical officer': 'eto',
        'electro technical engineer': 'eto',
    }
    rank_key = aliases.get(normalized, rank_key)
    keywords = ATS_RANK_KEYWORDS.get(rank_key, [])
    matches = [
        keyword for keyword in keywords
        if keyword_in_text(keyword, resume_text or '')
    ]
    return {
        'rank': rank_key if keywords else None,
        'matches': matches,
        'score': round(100 * len(matches) / len(keywords), 1) if keywords else None,
        'source': ATS_RANK_KEYWORDS_SOURCE if keywords else None,
    }


def _norm(value: str) -> str:
    return (value or "").strip().lower().replace("-", "_")


def maritime_overlay_active(
    career_track: str = "",
    target_rank: str = "",
    target_cruise_line: str = "",
    detected_rank: str = "",
) -> bool:
    """True when Layer 2 (maritime scoring + maritime JD tailoring) should run.

    Explicit generic/none → off.
    target_rank or target_cruise_line → on (existing MYC form fields).
    Known maritime career_track → on.
    Absent career_track: on only if parser already detected a maritime rank,
    so existing seafarer uploads keep maritime scores without a new field.
    """
    if target_rank.strip() or target_cruise_line.strip():
        return True
    track = _norm(career_track)
    if track in MARITIME_NONE:
        if not track and detected_rank and detected_rank.lower() not in ("", "unknown"):
            return True
        return False
    if track in MARITIME_TRACKS or track.replace("_", " ") in TRACK_TO_JD:
        return True
    if track.replace("_", " ") in {
        "chief officer", "second officer", "third officer", "chief engineer",
        "second engineer", "third engineer", "fourth engineer", "able seaman",
        "deck cadet", "engine cadet", "electrical cadet",
    }:
        return True
    return False


def resolve_target_jd(
    career_track: str,
    job_description: str,
    rank: str = "",
    maritime_jds: dict | None = None,
    rank_to_jd: dict | None = None,
) -> str:
    """JD text fed into Resume Matcher's tailoring engine.

    Caller-supplied job_description always wins. Otherwise map career_track
    / rank onto the maritime JD templates. Generic path uses a professional
    baseline (keywords.GENERIC_PROFESSIONAL_JD) when nothing else applies.
    """
    if (job_description or "").strip():
        return job_description.strip()

    from generic_ats.keywords import GENERIC_PROFESSIONAL_JD

    if maritime_jds is None:
        try:
            from services.ai_client import MARITIME_JDS, RANK_TO_JD
            maritime_jds = MARITIME_JDS
            rank_to_jd = RANK_TO_JD
        except Exception:
            maritime_jds = {}
            rank_to_jd = {}
    rank_to_jd = rank_to_jd or {}

    track = _norm(career_track).replace("_", " ")
    track_key = TRACK_TO_JD.get(_norm(career_track)) or TRACK_TO_JD.get(track)
    if track_key and track_key in maritime_jds:
        return maritime_jds[track_key].strip()

    rank_l = (rank or "").strip().lower()
    jd_key = rank_to_jd.get(rank_l)
    if jd_key and jd_key in maritime_jds:
        return maritime_jds[jd_key].strip()

    return GENERIC_PROFESSIONAL_JD.strip()
