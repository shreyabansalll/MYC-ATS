"""Run a compact fictional end-to-end maritime intelligence example."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from services.maritime_intelligence import analyze_maritime_candidate
from services.vacancy_match import extract_vacancy_requirements, match_vacancy

def run_demo():
    resume=('Third Officer\nM/V Synthetic Star\n01/2021 - 12/2022\n'
            'Third Officer | M/V Synthetic Star | 01/2021 - 12/2022\n'
            'GMDSS GOC\nECDIS\nServed on passenger vessels with diesel-electric propulsion.')
    jd=('Third Officer\nRequired:\nGMDSS, ECDIS, 24 months sea service\n'
        'Preferred:\nPassenger vessel experience')
    vacancy=extract_vacancy_requirements(jd)
    vacancy['department']='deck'
    candidate=analyze_maritime_candidate(resume,'third officer',vacancy)
    result={'synthetic_only':True,'vacancy':vacancy,
            'match':match_vacancy(jd,resume,{'rank':'third officer','skills':['GMDSS','ECDIS']}),
            'maritime_intelligence':candidate}
    print(json.dumps(result,indent=2,default=str))
    return result

if __name__=='__main__': run_demo()
