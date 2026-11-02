"""Offline LambdaMART trainer for consented recruiter vacancy rankings."""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path


def _load(path: str):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _matrix(rows):
    keys=sorted({key for row in rows for key in row['features']})
    if not keys: raise ValueError('ranking dataset has no feature columns')
    X=[[float(row['features'].get(key,0.0)) for key in keys] for row in rows]
    y=[int(row['relevance']) for row in rows]
    return keys,X,y


def train(train_jsonl: str, valid_jsonl: str, output_path: str):
    train_rows=_load(train_jsonl); valid_rows=_load(valid_jsonl)
    if not train_rows or not valid_rows: raise ValueError('train and validation datasets must both be non-empty')
    train_keys,X,y=_matrix(train_rows); valid_keys,V,vy=_matrix(valid_rows)
    if train_keys!=valid_keys: raise ValueError('train/validation feature schemas differ')
    try: import lightgbm as lgb
    except ImportError as exc: raise RuntimeError('Install requirements-training.txt in an isolated training environment') from exc
    def grouped(rows,features,labels):
        order=sorted(range(len(rows)),key=lambda i:(rows[i]['vacancy_ref'],rows[i]['candidate_ref']))
        groups=[]; last=None; count=0
        for i in order:
            q=rows[i]['vacancy_ref']
            if last is not None and q!=last: groups.append(count); count=0
            count+=1; last=q
        if count: groups.append(count)
        return [features[i] for i in order],[labels[i] for i in order],groups
    X,y,groups=grouped(train_rows,X,y); V,vy,valid_groups=grouped(valid_rows,V,vy)
    model=lgb.LGBMRanker(objective='lambdarank',metric='ndcg',n_estimators=200,
                         learning_rate=0.05,num_leaves=15,random_state=42)
    model.fit(X,y,group=groups,eval_set=[(V,vy)],eval_group=[valid_groups],
              eval_at=(3,5),callbacks=[lgb.early_stopping(20,verbose=False)])
    model.booster_.save_model(output_path)
    return {'model_path':output_path,'algorithm':'LightGBM LambdaMART','feature_names':train_keys,
            'training_queries':len(groups),'validation_queries':len(valid_groups),'trained':True}


def main():
    p=argparse.ArgumentParser();p.add_argument('--train-jsonl',required=True)
    p.add_argument('--valid-jsonl',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();print(json.dumps(train(args.train_jsonl,args.valid_jsonl,args.output),indent=2))


if __name__=='__main__':main()
