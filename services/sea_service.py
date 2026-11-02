"""Sea-service interval parsing and aggregation with overlap-safe totals."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from pipeline.maritime import detect_rank, detect_vessel_types

_DATE = r'(?:20\d{2}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}[/-]\d{4}|[A-Za-z]{3,9}\s+\d{4}|present|current)'
_RANGE = re.compile(rf'(?P<start>{_DATE})\s*(?:-|–|—|to)\s*(?P<end>{_DATE})', re.I)
_VESSEL = re.compile(r'\b(?:M\s*/\s*[TV]|MV|MT|Vessel(?:\s+name)?\s*[:=-])\s*([A-Z][A-Za-z0-9 .()\'-]{1,48})', re.I)
_COMPANY = re.compile(r'\b(?:company|employer|operator)\s*[:=-]\s*([^|;\n]{2,80})', re.I)
_DAY = 86400


def _parse_endpoint(value: str, *, is_end: bool, today: date) -> tuple[date | None, bool, bool]:
    value = value.strip()
    if value.casefold() in {'present','current'}:
        return today, False, True
    for fmt in ('%Y-%m-%d','%b %Y','%B %Y'):
        try:
            dt = datetime.strptime(value, fmt).date()
            partial = fmt != '%Y-%m-%d'
            if partial:
                dt = dt.replace(day=calendar.monthrange(dt.year,dt.month)[1] if is_end else 1)
                if is_end:
                    dt += timedelta(days=1)  # internal intervals are half-open
            elif is_end:
                dt += timedelta(days=1)
            return dt, False, partial
        except ValueError:
            pass
    if re.fullmatch(r'\d{1,2}[/-]\d{4}', value):
        month, year = map(int, re.split(r'[/-]', value))
        if not 1 <= month <= 12: return None, False, True
        dt = date(year, month, calendar.monthrange(year,month)[1] if is_end else 1)
        if is_end: dt += timedelta(days=1)
        return dt, False, True
    if re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{4}', value):
        a,b,year=map(int,re.split(r'[/-]',value))
        ambiguous = a <= 12 and b <= 12
        # Indian resume convention: day/month/year. Do not discard it, but keep the ambiguity flag.
        try:
            dt=date(year,b,a) + (timedelta(days=1) if is_end else timedelta(0))
            return dt, ambiguous, False
        except ValueError:
            return None, True, False
    return None, True, False


def _union_days(intervals: list[tuple[date,date]]) -> int:
    return sum((end-start).days for start,end in _merge(intervals))


def _merge(intervals: list[tuple[date,date]]) -> list[tuple[date,date]]:
    if not intervals: return []
    merged=[]
    for start,end in sorted(intervals):
        if end <= start: continue
        if not merged or start > merged[-1][1]: merged.append([start,end])
        else: merged[-1][1]=max(merged[-1][1],end)
    return [(start,end) for start,end in merged]


def _month_equivalent(days: int) -> float:
    return round(days / 30.436875, 2)


def _service_months(intervals: list[tuple[date,date]]) -> float:
    total=0.0
    for start,end in _merge(intervals):
        if start.day==1 and end.day==1:
            total+=(end.year-start.year)*12+(end.month-start.month)
        else:
            total+=(end-start).days/30.436875
    return round(total,2)


def _safe_evidence(value: str) -> str:
    value=value[:400]
    protected=[]
    def hold(match):
        protected.append(match.group())
        return f'__SERVICE_DATE_{len(protected)-1}__'
    value=_RANGE.sub(hold,value)
    value=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted email]', value)
    value=re.sub(r'(?<!\w)\+?\d[\d\s()./-]{7,}\d(?!\w)',
                 lambda m:'[redacted number]' if sum(c.isdigit() for c in m.group())>=10 else m.group(),value)
    value=re.sub(r'\b(passport|cdc|indos|document|certificate)\s*(?:no\.?\s*:|number\s*:|#|:)\s*[A-Z0-9/-]+', r'\1 [redacted]', value, flags=re.I)
    for index,date_range in enumerate(protected): value=value.replace(f'__SERVICE_DATE_{index}__',date_range)
    return value


def calculate_sea_service(text: str, *, today: date | None = None,
                          verified_records: list[dict] | None = None,
                          vacancy: dict | None = None) -> dict:
    """Parse, deduplicate and aggregate claimed intervals; resume claims stay unverified.

    Verified records must be supplied separately by an authorized verification source;
    wording in resume text cannot upgrade a claim to verified.
    """
    today=today or datetime.utcnow().date()
    lines=(text or '').splitlines()
    records=[]; duplicate_records=[]; seen={}
    for index,line in enumerate(lines):
        for match in _RANGE.finditer(line):
            start, amb_start, partial_start=_parse_endpoint(match.group('start'),is_end=False,today=today)
            end, amb_end, partial_end=_parse_endpoint(match.group('end'),is_end=True,today=today)
            if not start or not end or end <= start: continue
            context='\n'.join(lines[max(0,index-1):min(len(lines),index+2)])
            rank=detect_rank(context); rank=None if rank=='unknown' else rank
            vessel_match=_VESSEL.search(context)
            vessel=vessel_match.group(1).strip(' .,-') if vessel_match else None
            try:
                from services.vessel_intelligence import extract_vessel_intelligence
                vessel_types=sorted({item['normalized'] for item in extract_vessel_intelligence(context)
                                     if item['entity_type']=='category'})
            except Exception:
                vessel_types=detect_vessel_types(context)
            company_match=_COMPANY.search(context)
            company=company_match.group(1).strip() if company_match else None
            department='eto' if rank and ('eto' in rank or 'electrical' in rank) else 'engine' if rank and ('engine' in rank or rank in {'oiler','motorman'}) else 'deck' if rank else None
            key=(start,end,(rank or '').casefold())
            record={'start_date':start.isoformat(),'end_date_exclusive':end.isoformat(),
                    'days':(end-start).days,'rank':rank,'department':department,
                    'vessel':vessel,'vessel_types':vessel_types,'company':company,
                    'evidence':_safe_evidence(context.strip()),
                    'date_precision':'partial_month' if partial_start or partial_end else 'day',
                    'date_ambiguous':amb_start or amb_end,
                    'verification_status':'unverified_resume_claim'}
            record['period_id']=len(records)+1
            if key in seen:
                original=records[seen[key]]
                duplicate_records.append({**record,'duplicate_of':original['period_id'],
                    'duplicate_type':'same_period_rank_possible_duplicate',
                    'metadata_conflict':record.get('vessel')!=original.get('vessel') or record.get('company')!=original.get('company')})
                if record.get('vessel')==original.get('vessel') and record.get('company')==original.get('company'):
                    continue
            else:
                seen[key]=len(records)
            records.append(record)
    groups=defaultdict(list)
    for rec in records:
        groups['all'].append(rec)
        for key in ('rank','department','vessel'):
            if rec.get(key): groups[f'{key}:{rec[key]}'].append(rec)
        for value in rec['vessel_types']:
            groups[f'vessel_type:{value}'].append(rec)
    def aggregate(rs):
        spans=[(date.fromisoformat(x['start_date']),date.fromisoformat(x['end_date_exclusive'])) for x in rs]
        return {'service_days':_union_days(spans),'service_months':_service_months(spans),
                'period_count':len(rs)}
    aggregates={key:aggregate(value) for key,value in groups.items()}
    verified=[]
    for raw in verified_records or []:
        try:
            start=date.fromisoformat(raw['start_date']); end=date.fromisoformat(raw['end_date'])+timedelta(days=1)
            if end>start: verified.append((start,end))
        except (KeyError,TypeError,ValueError): continue
    all_spans=[(date.fromisoformat(x['start_date']),date.fromisoformat(x['end_date_exclusive'])) for x in records]
    total_days=_union_days(all_spans)
    # Subtract the verified overlap from the claimed union to avoid describing
    # verified time as both verified and unverified.
    overlap_segments=[]
    for start,end in all_spans:
        overlap_segments.extend((max(start,vs),min(end,ve)) for vs,ve in verified if max(start,vs)<min(end,ve))
    verified_overlap=_union_days(overlap_segments)
    verified_days=verified_overlap
    unverified_days=max(0,total_days-verified_overlap)
    claimed_months=_service_months(all_spans)
    verified_months=min(claimed_months,_service_months(overlap_segments))
    unverified_months=max(0,round(claimed_months-verified_months,2))
    for record in records:
        start=date.fromisoformat(record['start_date']); end=date.fromisoformat(record['end_date_exclusive'])
        covered=_union_days([(max(start,vs),min(end,ve)) for vs,ve in verified if max(start,vs)<min(end,ve)])
        record['verification_status']=('verified_external_record' if covered==(end-start).days else
                                       'partially_verified' if covered else 'unverified_resume_claim')
    overlaps=[]
    for i,left in enumerate(records):
        ls=date.fromisoformat(left['start_date']); le=date.fromisoformat(left['end_date_exclusive'])
        for right in records[i+1:]:
            rs=date.fromisoformat(right['start_date']); re_=date.fromisoformat(right['end_date_exclusive'])
            days=(min(le,re_)-max(ls,rs)).days
            if days>0:
                overlaps.append({'period_ids':[left['period_id'],right['period_id']],
                                 'overlap_days':days,'same_rank':left.get('rank')==right.get('rank'),
                                 'same_vessel':(left.get('vessel') or '').casefold()==(right.get('vessel') or '').casefold(),
                                 'double_count_prevented':True})
    target=(vacancy or {})
    relevant=[]
    target_rank=(target.get('rank') or '').casefold()
    target_dept=(target.get('department') or '').casefold()
    target_vessels={str(v).casefold() for v in target.get('vessel_types',[])}
    for rec in records:
        if ((target_rank and (rec.get('rank') or '').casefold()==target_rank)
            or (target_dept and (rec.get('department') or '').casefold()==target_dept)
            or (target_vessels & {v.casefold() for v in rec['vessel_types']})):
            relevant.append(rec)
    relevant_spans=[(date.fromisoformat(x['start_date']),date.fromisoformat(x['end_date_exclusive'])) for x in relevant]
    relevant_days=_union_days(relevant_spans)
    return {'periods':records,'duplicates':duplicate_records,'overlaps':overlaps,'aggregates':aggregates,
            'total':{'service_days':total_days,'service_months':claimed_months,
                     'verified_days':verified_days,'verified_months':verified_months,
                     'unverified_days':unverified_days,
                     'unverified_months':unverified_months},
            'vacancy_relevant':{'service_days':relevant_days,'service_months':_service_months(relevant_spans),
                                'matched_period_count':len(relevant)},
            'ambiguity_count':sum(r['date_ambiguous'] for r in records),
            'calculation_basis':'Half-open calendar-day intervals; month-only dates span full calendar months; displayed months = days / 30.436875; overlap merged.'}
