# Marine ATS — Integration Guide

For MYC's app developer. Covers everything the app needs to call this service: auth, the two request flows, request/response shapes, and error handling.

## Base URL and auth

Every endpoint below except `GET /` and `GET /health` requires an `X-API-Key` header:

```
X-API-Key: <your MYC_API_KEY>
```

Missing or wrong key → `403 Forbidden`.

## Two ways to score a resume

There are two separate flows, and picking the right one matters:

| | `POST /ats/score` | `POST /ats/submit` |
|---|---|---|
| What it does | ATS score + maritime score only | Full pipeline: scores + AI-rewritten, ATS-optimised DOCX |
| Speed | Seconds, one HTTP response | Async — queues a job, poll for completion |
| Depends on the AI provider (Groq) | No | Yes |
| Use when | You just need the score/issues, or want a fast pre-check before the full rewrite | You want the candidate a polished, downloadable DOCX |

If your product currently only needs the score (e.g. to show the candidate their ATS grade before deciding whether to run the full rewrite), use `/ats/score` — it's faster and has no dependency on the AI provider being up.

---

## Fast path: `POST /ats/score`

Synchronous. One request, one response, typically a couple of seconds.

**Request** — `multipart/form-data`:

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | PDF or DOCX, max 5MB |
| `job_description` | No | Improves keyword-match scoring if provided |

```bash
curl -X POST https://<host>/ats/score \
  -H "X-API-Key: $MYC_API_KEY" \
  -F "file=@resume.pdf"
```

**Response** — `200 OK`:

```json
{
  "name": "Srinivas Menon",
  "ats_score": 70,
  "ats_grade": "PASS",
  "maritime_score": 78,
  "maritime_grade": "STRONG",
  "rank_detected": "third officer",
  "sea_months": 24,
  "cert_coverage": "3/3",
  "vessel_types": ["oil tanker", "bulk carrier"],
  "issues": ["FORMAT: LINKEDIN_URL_MISSING (-2pts) [R37]", "..."],
  "recommendations": ["..."],
  "missing_certs": [],
  "keyword_data": { "match_pct": null, "note": "No JD provided — base score assigned" }
}
```

`ats_grade` is one of `EXCELLENT` / `GOOD` / `PASS` / `FAIL`. `maritime_grade` is one of `STRONG` / `ADEQUATE` / `WEAK`.

This endpoint doesn't create a job or persist anything — nothing to poll, nothing to download. If you need the AI-rewritten resume too, use `/ats/submit` below (you'll get the same scores as part of that flow, plus the rewrite).

---

## Full path: `POST /ats/submit` → poll → `GET /ats/result` → `GET /ats/download`

### 1. Submit

**Request** — `multipart/form-data`:

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | PDF or DOCX, max 5MB |
| `job_description` | No | Improves keyword-match scoring |
| `target_rank` | No | Overrides automatic rank detection |
| `target_cruise_line` | No | See "Cruise-line tailoring" below |

```bash
curl -X POST https://<host>/ats/submit \
  -H "X-API-Key: $MYC_API_KEY" \
  -F "file=@resume.pdf"
```

**Response** — `200 OK`:

```json
{ "job_id": "a1b2c3d4-...", "status": "queued" }
```

If the exact same file (by content hash) was already processed successfully within the last 24 hours, you instead get the cached result immediately:

```json
{ "job_id": "<existing job's id>", "status": "done", "cached": true, "message": "Same file processed recently — returning existing result" }
```

### 2. Poll status

```
GET /ats/status/{job_id}
```

```json
{ "status": "processing", "progress": 65, "current_stage": "Generating ATS-clean DOCX" }
```

`status` is one of `queued` / `processing` / `done` / `failed`. Poll every 2-3 seconds. A full run typically takes several seconds to low tens of seconds depending on the AI provider's response time.

### 3. Get the result

```
GET /ats/result/{job_id}
```

Only meaningful once `status == "done"`. Returns before/after scores, issues, recommendations, and the download URL:

```json
{
  "job_id": "a1b2c3d4-...",
  "score_before": 70, "grade_before": "PASS",
  "score_after": 78, "grade_after": "GOOD",
  "improvement": 8,
  "maritime_score_before": 78, "maritime_grade_before": "STRONG",
  "maritime_score_after": 78, "maritime_grade_after": "STRONG",
  "rank_detected": "third officer",
  "sea_months": 24,
  "cert_coverage": "3/3",
  "issues": ["..."],
  "recommendations": ["..."],
  "missing_certs": [],
  "issues_fixed": ["..."],
  "keyword_data": {},
  "download_url": "/ats/download/a1b2c3d4-...",
  "completed_at": "2026-09-19T10:15:00",
  "expires_at": "2026-09-21T10:15:00"
}
```

If called while still processing, you get `202` with `{"detail": "Still processing", "progress": ..., "stage": ...}`. If the job failed, you get `500` with the failure reason in `detail`.

### 4. Download

```
GET /ats/download/{job_id}
```

Returns the optimised `.docx` file. Available for 48 hours after completion (`expires_at` above); after that, `410 Gone`.

---

## Cruise-line tailoring (optional)

If you know which specific cruise line a submission targets, pass `target_cruise_line` on `/ats/submit` (e.g. `"Disney"`, `"Royal Caribbean"`, `"Carnival"`, `"Norwegian"`, `"MSC"`, `"Celebrity"`, `"Princess"`, `"Holland America"`, `"Silversea"`, `"Viking"`, `"Regent"` / `"Oceania"`, `"Costa"` — common abbreviations like `"RCL"`/`"NCL"`/`"DCL"` also work). This layers that line's real hiring keywords and certification emphasis into the rewrite, on top of the normal rank-based optimisation — it does not replace it, and it never invents a certificate or experience the candidate doesn't actually have.

Leave it blank (or omit it) for the normal, rank-only behavior — this is what almost every submission should do, since candidates typically apply to many agencies with one resume. An unrecognized or misspelled line name is not an error — it just falls back to normal behavior.

---

## Errors

| Code | Meaning |
|---|---|
| `403` | Missing/invalid `X-API-Key` |
| `413` | File exceeds 5MB |
| `415` | Not a valid PDF/DOCX (wrong extension or failed content check) |
| `422` | (`/ats/score` only) Could not extract any text from the file — see message for why (corrupt file, password-protected, scanned image with no text layer) |
| `429` | Rate limit — 10 requests/minute per IP on `/ats/submit` and `/ats/score` |
| `503` | Server at capacity (too many resumes processing at once) — retry shortly |
| `507` | Server disk space too low to accept uploads — an ops issue on our end, not yours |

For `/ats/submit`, a bad file that fails during processing (rather than at upload) surfaces as `status: "failed"` on the job, not an HTTP error — check `error_detail` via `/ats/status` or the `detail` field from `/ats/result`.

## Rate limits

10 requests/minute per IP address, shared across `/ats/submit` and `/ats/score`. Design your integration to submit on user action, not to poll-resubmit.
