"""Validate opted-in feedback and prepare disjoint offline datasets."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from training.feedback_schema import FeedbackRecord, RankingFeedbackRecord

_EMAIL=re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
_PHONE=re.compile(r'(?<!\w)\+?\d[\d ()./-]{7,}\d(?!\w)')
_ID=re.compile(r'\b(passport|cdc|indos|document|certificate)\s*(?:no\.?\s*:|number\s*:|#|:)\s*[A-Z0-9/-]+',re.I)


def redact_feedback_text(value: str) -> str:
    value=_EMAIL.sub('[redacted email]',value)
    value=_PHONE.sub(lambda m:'[redacted number]' if sum(x.isdigit() for x in m.group())>=10 else m.group(),value)
    return _ID.sub(r'\1 [redacted]',value)


def _redact_values(value):
    if isinstance(value,str): return redact_feedback_text(value)
    if isinstance(value,list): return [_redact_values(item) for item in value]
    if isinstance(value,dict): return {key:_redact_values(item) for key,item in value.items()}
    return value


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _partition_by_group(rows: list[dict], group_key: str, validation_fraction: float) -> tuple[list[dict],list[dict]]:
    groups=sorted({row[group_key] for row in rows})
    if len(groups)<2: return rows,[]
    n=max(1,min(len(groups)-1,round(len(groups)*validation_fraction)))
    held=set(groups[-n:])
    return ([row for row in rows if row[group_key] not in held],
            [row for row in rows if row[group_key] in held])


def prepare_datasets(feedback_rows: list[dict], ranking_rows: list[dict], out_dir: str,
                     validation_fraction: float=0.2) -> dict:
    if not 0.05<=validation_fraction<=0.5: raise ValueError('validation_fraction must be in [0.05, 0.5]')
    feedback=[FeedbackRecord.model_validate(row) for row in feedback_rows]
    rankings=[RankingFeedbackRecord.model_validate(row) for row in ranking_rows]
    # These are intentionally independent; no record is copied across tasks.
    rewrites=[{'prompt':redact_feedback_text(str(x.correction.get('prompt',''))),
               'chosen':redact_feedback_text(x.preferred or ''),
               'rejected':redact_feedback_text(x.rejected or ''),
               'source_group':x.split_group,'consent_record_ref':x.consent_record_ref}
              for x in feedback if x.kind.value=='rewrite_preference']
    corrections=[_redact_values(x.model_dump(mode='json')) for x in feedback if x.kind.value!='rewrite_preference']
    rewrite_train,rewrite_valid=_partition_by_group(rewrites,'source_group',validation_fraction)
    rank_dicts=[x.model_dump(mode='json') for x in rankings]
    # Union vacancy and candidate references to keep related ranking examples in one split.
    parent={}
    def find(x):
        parent.setdefault(x,x)
        if parent[x]!=x: parent[x]=find(parent[x])
        return parent[x]
    def union(a,b):
        ra,rb=find(a),find(b)
        if ra!=rb: parent[rb]=ra
    for row in rank_dicts: union('v:'+row['vacancy_ref'],'c:'+row['candidate_ref'])
    for row in rank_dicts: row['_component']=find('v:'+row['vacancy_ref'])
    rank_train,rank_valid=_partition_by_group(rank_dicts,'_component',validation_fraction)
    for row in rank_train+rank_valid: row.pop('_component',None)
    root=Path(out_dir); root.mkdir(parents=True,exist_ok=True,mode=0o700); os.chmod(root,0o700)
    outputs={
        'rewrite_train.jsonl':rewrite_train,'rewrite_valid.jsonl':rewrite_valid,
        'ranking_train.jsonl':rank_train,'ranking_valid.jsonl':rank_valid,
        'corrections_review.jsonl':corrections,
    }
    for name,rows in outputs.items():
        target=root/name
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:
            for row in rows: f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return {'rewrite_train':len(rewrite_train),'rewrite_valid':len(rewrite_valid),
            'ranking_train':len(rank_train),'ranking_valid':len(rank_valid),
            'corrections_for_review':len(corrections),'outputs':list(outputs),
            'note':'Offline, consent-referenced, PII-redacted data preparation only; no model was trained.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--feedback-jsonl',required=True)
    parser.add_argument('--ranking-jsonl',required=True)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--validation-fraction',type=float,default=0.2)
    args=parser.parse_args()
    report=prepare_datasets(_read_jsonl(Path(args.feedback_jsonl)),_read_jsonl(Path(args.ranking_jsonl)),
                            args.output_dir,args.validation_fraction)
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
