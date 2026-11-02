"""Operator-invoked, offline DPO/LoRA fine-tuning from prepared preference data."""
from __future__ import annotations
import argparse
import os
from pathlib import Path


def train(model_path: str, train_jsonl: str, valid_jsonl: str, output_dir: str):
    if not Path(model_path).is_dir(): raise FileNotFoundError('model_path must be a local model directory')
    if not Path(train_jsonl).is_file(): raise FileNotFoundError(train_jsonl)
    os.environ['HF_HUB_OFFLINE']='1'
    try:
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except ImportError as exc:
        raise RuntimeError('Install requirements-training.txt in an isolated offline training environment') from exc
    data=load_dataset('json',data_files={'train':train_jsonl,'validation':valid_jsonl},split=None)
    tokenizer=AutoTokenizer.from_pretrained(model_path,local_files_only=True)
    model=AutoModelForCausalLM.from_pretrained(model_path,local_files_only=True)
    args=DPOConfig(output_dir=output_dir,per_device_train_batch_size=1,
                   per_device_eval_batch_size=1,num_train_epochs=1,
                   learning_rate=5e-6,logging_steps=10,save_strategy='epoch',
                   eval_strategy='epoch',report_to='none',remove_unused_columns=False)
    trainer=DPOTrainer(model=model,args=args,train_dataset=data['train'],
                       eval_dataset=data.get('validation'),processing_class=tokenizer,
                       peft_config=LoraConfig(r=8,lora_alpha=16,lora_dropout=0.05,
                                               target_modules='all-linear',task_type='CAUSAL_LM'))
    trainer.train(); trainer.save_model(output_dir); tokenizer.save_pretrained(output_dir)
    return {'output_dir':output_dir,'training_mode':'offline_dpo_lora','trained':True}


def main():
    p=argparse.ArgumentParser(); p.add_argument('--model-path',required=True)
    p.add_argument('--train-jsonl',required=True); p.add_argument('--valid-jsonl',required=True)
    p.add_argument('--output-dir',required=True); args=p.parse_args()
    print(train(args.model_path,args.train_jsonl,args.valid_jsonl,args.output_dir))


if __name__=='__main__': main()
