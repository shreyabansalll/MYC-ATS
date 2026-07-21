# training/prelabel.py
# Pre-labels all 393 resumes using Ollama/qwen2.5:7b
# Output: data/prelabeled_393.json
# Features: checkpoint saves every 10 resumes, retry logic, robust JSON parsing

import os, json, time, re, importlib
import sys
try:
    openai = importlib.import_module('openai')
    OpenAI = openai.OpenAI
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "Missing required dependency 'openai'. Install it with 'pip install openai'"
    ) from e
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.extractor import extract_text

client = OpenAI(
    base_url='http://localhost:11434/v1',
    api_key='ollama',
)

MARITIME_EXTRACTION_PROMPT = '''You are a maritime HR expert. Extract ALL entities from this seafarer resume.
Return ONLY valid JSON. No explanation. No markdown. No backticks. Just the raw JSON object.

{
    "name": "full name or null",
    "email": "email or null",
    "phone": "phone or null",
    "rank": "current/target rank e.g. Chief Officer, 3rd Engineer, AB Seaman, or null",
    "department": "DECK or ENGINE or CATERING or ETO or OTHER",
    "certificates": ["list each cert found e.g. CoC, STCW Basic Safety, GMDSS, ENG1, BOSIET"],
    "vessels": [
        {
            "name": "vessel name",
            "type": "vessel type e.g. Chemical Tanker, Bulk Carrier, Container Ship",
            "dwt_gt": "tonnage if mentioned e.g. 45000 DWT or null",
            "flag": "flag state if mentioned or null",
            "from": "MM/YYYY or null",
            "to": "MM/YYYY or null",
            "rank_onboard": "rank held on this vessel"
        }
    ],
    "sea_service_months": 0,
    "manning_agencies": ["list of agency names found"],
    "education": [{"degree": "degree name", "institute": "institute name", "year": "YYYY or null"}],
    "skills": ["list of all skills and competencies mentioned"],
    "summary_present": true,
    "sections_detected": ["list of section headers found in the resume"]
}'''


def clean_json_response(raw: str) -> str:
    """Robustly strip markdown fences and extract JSON from LLM response."""
    # Remove markdown code fences
    raw = raw.strip()
    if '```' in raw:
        # Extract content between first ``` and last ```
        parts = raw.split('```')
        # parts[1] is the content between first pair of backticks
        for part in parts[1:]:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('{'):
                return part
    # If no fences, look for first { to last }
    start = raw.find('{')
    end = raw.rfind('}')
    if start != -1 and end != -1:
        return raw[start:end+1]
    return raw


def prelabel_resume(file_path: str, job_id: str, max_retries: int = 2) -> dict:
    """Extract maritime entities from a single resume file. Retries on failure."""
    ext = file_path.split('.')[-1].lower()
    
    # Extract raw text
    try:
        raw = extract_text(file_path, ext)
        text = raw.get('raw_text', '')
    except Exception as e:
        return {'error': f'EXTRACTION_FAILED: {str(e)}', 'source_file': file_path, 'job_id': job_id}

    if len(text.strip()) < 50:
        return {'error': 'EMPTY_OR_UNREADABLE', 'source_file': file_path, 'job_id': job_id}

    # Attempt LLM extraction with retries
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model='qwen2.5:7b',
                messages=[
                    {'role': 'system', 'content': MARITIME_EXTRACTION_PROMPT},
                    {'role': 'user', 'content': text[:4000]}
                ],
                temperature=0.1,
                timeout=120,  # 2 min timeout per resume
            )

            raw_response = response.choices[0].message.content.strip()
            cleaned = clean_json_response(raw_response)
            result = json.loads(cleaned)
            result['source_file'] = file_path
            result['job_id'] = job_id
            result['raw_text_length'] = len(text)
            return result

        except json.JSONDecodeError as e:
            if attempt < max_retries:
                print(f'    ↻ JSON parse failed (attempt {attempt+1}), retrying...')
                time.sleep(2)
                continue
            return {
                'error': f'JSON_PARSE_FAILED: {str(e)}',
                'raw_response_preview': raw_response[:200] if 'raw_response' in dir() else 'N/A',
                'source_file': file_path,
                'job_id': job_id
            }
        except Exception as e:
            if attempt < max_retries:
                print(f'    ↻ Error (attempt {attempt+1}): {str(e)}, retrying...')
                time.sleep(3)
                continue
            return {'error': str(e), 'source_file': file_path, 'job_id': job_id}


def save_checkpoint(results: list, failed: list, checkpoint_path: str):
    """Save progress so we don't lose work if script is interrupted."""
    checkpoint = {'results': results, 'failed': failed, 'count': len(results)}
    with open(checkpoint_path, 'w') as f:
        json.dump(checkpoint, f, indent=2)


if __name__ == '__main__':
    resume_list_path = 'data/resume_file_list.txt'
    checkpoint_path  = 'data/prelabel_checkpoint.json'
    output_path      = 'data/prelabeled_393.json'
    failed_path      = 'data/prelabeled_failed.json'

    # Load resume list
    with open(resume_list_path) as f:
        resumes = [line.strip() for line in f if line.strip()]
    print(f'Found {len(resumes)} resumes to process')

    # Resume from checkpoint if it exists
    results = []
    failed  = []
    start_index = 0

    if os.path.exists(checkpoint_path):
        print(f'⚡ Checkpoint found — resuming from last save...')
        chk = json.load(open(checkpoint_path))
        results     = chk.get('results', [])
        failed      = chk.get('failed', [])
        start_index = len(results) + len(failed)
        print(f'   Resuming from resume #{start_index + 1}')

    # Process resumes
    for i, path in enumerate(resumes[start_index:], start=start_index):
        filename = os.path.basename(path)
        print(f'[{i+1}/{len(resumes)}] {filename}')

        result = prelabel_resume(path, str(i))

        if 'error' in result:
            print(f'  ✗ Failed: {result["error"]}')
            failed.append(result)
        else:
            name = result.get('name', 'Unknown')
            rank = result.get('rank', 'Unknown rank')
            certs = len(result.get('certificates', []))
            vessels = len(result.get('vessels', []))
            print(f'  ✓ {name} | {rank} | {certs} certs | {vessels} vessels')
            results.append(result)

        # Checkpoint save every 10 resumes
        if (i + 1) % 10 == 0:
            save_checkpoint(results, failed, checkpoint_path)
            print(f'  💾 Checkpoint saved ({len(results)} done, {len(failed)} failed)')

    # Final save
    os.makedirs('data', exist_ok=True)
    json.dump(results, open(output_path, 'w'), indent=2)
    json.dump(failed,  open(failed_path, 'w'), indent=2)

    # Clean up checkpoint
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)

    print(f'\n{"="*50}')
    print(f'✅ Complete: {len(results)} succeeded, {len(failed)} failed')
    print(f'📄 Output:   {output_path}')
    print(f'❌ Failed:   {failed_path}')

    # Quick stats on what was extracted
    ranks = [r.get('rank') for r in results if r.get('rank')]
    depts = [r.get('department') for r in results if r.get('department')]
    from collections import Counter
    print(f'\n📊 Rank distribution (top 10):')
    for rank, count in Counter(ranks).most_common(10):
        print(f'   {rank}: {count}')
    print(f'\n📊 Department distribution:')
    for dept, count in Counter(depts).most_common():
        print(f'   {dept}: {count}')