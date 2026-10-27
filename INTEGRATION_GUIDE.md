# Marine ATS — Integration Guide

For MYC's app developer. Covers everything the app needs to call this service: auth, the two request flows, request/response shapes, and error handling.

## Base URL and auth

Protected client endpoints require an `X-API-Key` header matching `MYC_API_KEY`. `/ats/admin/metrics` uses a separate `MYC_ADMIN_KEY`; `/` and `/health` are unauthenticated.

```
X-API-Key: <your MYC_API_KEY>
```

Missing or wrong key → `403 Forbidden`. Production startup requires a distinct admin key; the service does not fall back to the client key for metrics.

For internal testing, the service serves the interactive dashboard at `GET /dashboard`. It reads `MYC_API_KEY` from the server environment and never sends it to the browser. Local access needs no extra setup. To share the dashboard through a temporary tunnel, configure a distinct `DASHBOARD_ACCESS_KEY`; each trusted tester enters that key once and receives an eight-hour HttpOnly session. The proxy is limited to scoring, submissions, job status/results, and downloads. Never share `MYC_API_KEY` or expose the dashboard without the separate access key.

Example team test with ngrok:

```bash
# .env: set DASHBOARD_ACCESS_KEY to a separate random value (e.g. openssl rand -hex 32)
venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000 --proxy-headers
ngrok http 8000
```

Share the ngrok HTTPS URL with trusted testers; they open `<ngrok-url>/dashboard` and enter the dashboard access key. The page uses same-origin proxy routes, so there is no `BASE_URL` to edit in the HTML. Stop the tunnel after testing. Do not use this temporary setup for production hosting.

For production, `deploy.sh` and `marine-ats.service` bind Uvicorn to `127.0.0.1`. A Caddy site example is provided in [`deploy/Caddyfile.example`](./deploy/Caddyfile.example); replace `ats.example.com` with the service's public DNS name, install Caddy from its official package repository, and merge the site block into `/etc/caddy/Caddyfile`. Caddy obtains and renews certificates automatically when DNS points to the host and inbound ports 80/443 are reachable. Validate and reload with `sudo caddy validate --config /etc/caddy/Caddyfile` and `sudo systemctl reload caddy`. Keep port 8000 inaccessible from public networks.

Before enabling public access, allow inbound SSH (including any non-default SSH port), TCP 80, and TCP 443 in both the host and provider firewalls; deny inbound TCP 8000. For UFW, first inspect `sudo ufw status numbered`, ensure the correct SSH allow rule exists, then add the web rules and a top-priority deny for 8000 (`sudo ufw allow 80/tcp`, `sudo ufw allow 443/tcp`, `sudo ufw insert 1 deny 8000/tcp`). Do not enable or modify a remote firewall until SSH access and a recovery path are confirmed. These are host-operator steps; this repository does not change live firewall rules or verify a deployed host.

## Two ways to score a resume

There are two separate flows, and picking the right one matters:

| | `POST /ats/score` | `POST /ats/submit` |
|---|---|---|
| What it does | ATS score + maritime score only | Full pipeline: scores + AI-rewritten, ATS-optimised DOCX |
| Speed | Seconds, one HTTP response | Async — queues a job, poll for completion |
| Depends on the AI provider (Claude) | No | Yes |
| Use when | You just need the score/issues, or want a fast pre-check before the full rewrite | You want the candidate a polished, downloadable DOCX |

If your product currently only needs the score (e.g. to show the candidate their ATS grade before deciding whether to run the full rewrite), use `/ats/score` — it's faster and has no dependency on the AI provider being up.

### Scoring and evaluation notes

- Production resume rewriting uses Anthropic Claude (`claude-haiku-5-5`). A separate legacy offline pre-labeling script can call local Ollama if manually invoked, but it is not used by the live service or deployment. `/ats/score` is deterministic and does not call an LLM; `/ats/submit` attempts the rewrite after its initial scoring stages.
- The generic ATS score is always-on. Maritime scoring is an optional overlay, enabled by a detected seafarer rank or explicit maritime targeting.
- The previously recorded two-layer batch was scores-only: 269 resumes scored with 0 pipeline errors; generic ATS mean 57.0, median 58, range 24–75, with 162 FAIL, 106 PASS, and 1 GOOD; maritime scoring ran for 254 resumes with a mean of 76.0. It was not a rewrite-improvement evaluation, and it has not been rerun after the PDF parser dependency update.
- Older results (ATS 63.4→69.1 and maritime 54.1→70.7, with 56→2 resumes at zero detected sea months) came from the legacy single-layer scorer. They do not measure the current two-layer generic scorer and must not be used as evidence of its rewrite gains.

---

## Fast path: `POST /ats/score`

Synchronous. One request, one response, typically a couple of seconds.

**Request** — `multipart/form-data`:

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | PDF or DOCX, max 5MB; the multipart request body is bounded to the file limit plus 256KB overhead |
| `job_description` | No | Target JD; otherwise a general or rank-based baseline is used |
| `career_track` | No | Use `generic` to disable maritime scoring; a maritime track enables it |
| `target_rank` | No | Overrides automatic rank detection and enables the maritime layer |
| `target_cruise_line` | No | Optional maritime tailoring target |

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
  "overlay_active": true,
  "rank_detected": "third officer",
  "sea_months": 24,
  "cert_coverage": "3/3",
  "vessel_types": ["oil tanker", "bulk carrier"],
  "issues": ["GENERIC_ATS: SECTION_HEADINGS WARN (content/medium)", "..."],
  "recommendations": ["..."],
  "missing_certs": [],
  "keyword_data": { "engine": "resume_matcher", "role": "third officer", "missing_keywords": [] }
}
```

`ats_grade` is one of `EXCELLENT` / `GOOD` / `PASS` / `FAIL`. Maritime score fields are `null` when the overlay is inactive. `maritime_grade`, when present, is one of `STRONG` / `ADEQUATE` / `WEAK`. Pass `career_track=generic` to force generic-only scoring, or use `target_rank` / `target_cruise_line` to explicitly activate the maritime overlay.

This endpoint doesn't create a job or persist anything — nothing to poll, nothing to download. If you need the AI-rewritten resume too, use `/ats/submit` below (you'll get the same scores as part of that flow, plus the rewrite).

---

## Full path: `POST /ats/submit` → poll → `GET /ats/result` → `GET /ats/download`

### 1. Submit

**Request** — `multipart/form-data`:

| Field | Required | Notes |
|---|---|---|
| `file` | Yes | PDF or DOCX, max 5MB; the multipart request body is bounded to the file limit plus 256KB overhead |
| `job_description` | No | Target JD; otherwise a general or rank-based baseline is used |
| `target_rank` | No | Overrides automatic rank detection and enables maritime scoring |
| `target_cruise_line` | No | See "Cruise-line tailoring" below; enables maritime scoring |
| `career_track` | No | Use `generic` for generic-only scoring or a maritime track for both layers |

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

**Important — HTTP 200 does not guarantee an AI rewrite.** The submit response only confirms the job was queued. If `ANTHROPIC_API_KEY` is missing or invalid, the service preserves the original resume content without AI rewriting. Once the job completes, `GET /ats/result/{job_id}` returns `"ai_rewrite": false` and a `note` explaining why. Check `ai_rewrite` before telling the user their resume was rewritten.

### 2. Poll status

```
GET /ats/status/{job_id}
```

```json
{ "status": "processing", "progress": 65, "current_stage": "Generating ATS-clean DOCX" }
```

`status` is one of `queued` / `processing` / `done` / `failed`. Poll every 2-3 seconds. A full run typically takes several seconds to low tens of seconds depending on the AI provider's response time.

### Rewrite quality

When polling returns `status: "done"`, fetch `GET /ats/result/{job_id}` and check `rewrite_quality` in that completed result. (`/ats/status/{job_id}` reports progress; `rewrite_quality` is included in `/ats/result/{job_id}`.)

- `"full"` means the rewrite improved the score by 15 or more points.
- `"partial"` means the score was equal to or higher than the original but improved by fewer than 15 points; the best available version is returned.
- `"none"` means Claude failed or the best rewrite scored below the original; source-derived content is returned instead, and `ai_rewrite` is false. When displaying the resume score, show `score_before` rather than `score_after`: the fallback DOCX is rescored, but formatting and extraction can make that score differ from the uploaded resume. Recommend manual review.

For `"partial"`, show **“Resume score maintained or modestly improved; manual review recommended”** instead of claiming a full rewrite.

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
  "ai_rewrite": true,
  "note": null,
  "rewrite_quality": "partial",
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

The maritime before/after fields are `null` when the maritime overlay did not run. The generic ATS score always runs.

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
| `413` | File exceeds 5MB or the multipart request exceeds the bounded body size |
| `415` | Not a valid PDF/DOCX (wrong extension or failed content check) |
| `422` | (`/ats/score` only) Could not extract any text from the file — see message for why (corrupt file, password-protected, scanned image with no text layer) |
| `429` | Rate limit — 10 requests/minute per IP on `/ats/submit` and `/ats/score` |
| `503` | Server at capacity (too many resumes processing at once) — retry shortly |
| `507` | Server disk space too low to accept uploads — an ops issue on our end, not yours |

For `/ats/submit`, a bad file that fails during processing (rather than at upload) surfaces as `status: "failed"` on the job, not an HTTP error — check `error_detail` via `/ats/status` or the `detail` field from `/ats/result`.

## Rate limits

Both `/ats/score` and `/ats/submit` are rate limited to 10 requests per minute per IP address. If multiple users or team members rapidly hit the same endpoint, they may receive `429 Too Many Requests`. Handle `429` by showing a retry message, not a generic error. Do not retry automatically; let the user retry manually. Design your integration to submit on user action, not to poll-resubmit.
