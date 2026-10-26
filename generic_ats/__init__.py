"""Layer 1 generic ATS engine adapted from srbhr/Resume-Matcher.

Only the relevant scoring, parse-check, prompt, and writing-quality logic is
adapted here. This package has no runtime dependency on the upstream app or
its FastAPI, frontend, database, queue, or Playwright components. See LICENSE
for the upstream Apache-2.0 license.
"""

from generic_ats.layer import score_generic
from generic_ats.career_track import maritime_overlay_active, resolve_target_jd

__all__ = ['score_generic', 'maritime_overlay_active', 'resolve_target_jd']
