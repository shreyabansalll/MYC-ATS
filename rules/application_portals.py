"""MYC cruise-application portal registry.

URLs are operational navigation aids supplied by MYC, not a claim that an
employer currently uses a particular page or recruitment platform.
"""

from __future__ import annotations


MYC_PRIORITY_COMPANIES = (
    "carnival",
    "disney",
    "norwegian",
    "royal_caribbean",
)

APPLICATION_PORTALS = {
    "carnival": [
        ("Carnival Cruise Line", "https://shipjobs.carnival.com/", True),
        ("Carnival UK", "https://jobs.carnivalcorp.com/business/industry/carnival%2520uk/28021/5", False),
        ("Costa", "https://www.costacruise.com/", False),
        ("Cunard", "https://www.cunardcareers.co.uk/", False),
        ("Holland America Line", "https://www.hollandamerica.com/en/about/our-company/careers", False),
        ("P&O Cruises UK", "https://www.pocruisescareers.co.uk/", False),
        ("P&O Cruises Australia", "https://www.pocruises.com.au/careers", False),
        ("Princess Cruises", "https://www.princess.com/en-int/careers", False),
        ("Princess Cruises officers", "https://princesscruises.pinpointhq.com/other-jobs", False),
        ("Seabourn", "https://www.seabourn.com/en/about/careers", False),
    ],
    "royal_caribbean": [
        ("Royal Caribbean Group", "https://rclctrac.com/", True),
    ],
    "msc": [
        ("MSC Cruises Careers", "https://www.careers.msccruises.com/", True),
    ],
    "disney": [
        ("Disney Cruise Line", "https://jobs.disneycareers.com/search-jobs?ascf=%5B%7B%22key%22:%22custom_fields.DCLTheme%22,%22value%22:%22Shipboard%22%7D%5D", True),
    ],
    "norwegian": [
        ("Norwegian Cruise Line", "https://www.ncl.com/about/careers", True),
    ],
    "viking": [
        ("Viking Crew", "https://www.vikingcrew.com/", True),
    ],
    "virgin": [
        ("Virgin Voyages", "https://www.virginvoyages.com/careers", True),
    ],
    "fred_olsen": [
        ("Fred Olsen Cruise Lines", "https://www.fredolsencruises.com/careers/shipside", True),
    ],
    "columbia": [
        ("Columbia Ship Management", "https://columbia-shipmanagement.com/sea-job-opportunities/", True),
    ],
    "v_ships": [
        ("V.Ships", "https://vgroup.pinpointhq.com/en", True),
    ],
    "cordelia": [
        ("Cordelia Cruises", "https://www.cordeliacruises.com/career", True),
    ],
    "antara": [
        ("Antara Cruises", "https://www.antaracruises.com/careers/", True),
    ],
}

PORTAL_SOURCE = "MYC application portals.docx — operational MYC registry"


def get_application_portals(company: str) -> dict:
    """Return current MYC portal entries for a normalized or alias company."""
    from generic_ats.career_track import resolve_company

    resolved = resolve_company(company)
    entries = APPLICATION_PORTALS.get(resolved, [])
    return {
        "company": resolved or None,
        "priority": bool(resolved in MYC_PRIORITY_COMPANIES),
        "source": PORTAL_SOURCE,
        "portals": [
            {
                "name": name,
                "url": url,
                "myc_primary": primary,
                "provenance": "operational_myc_reference",
            }
            for name, url, primary in entries
        ],
    }
