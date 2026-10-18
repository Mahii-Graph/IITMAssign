# Ledger Agent (JSON POST endpoint)

This service accepts:
```json
{"question":"What was the revenue in North in March 2026 in USD?"}
```
and responds with:
```json
{"answer":"Total revenue from ..."}
```

## 1. Configure environment variables

Set these in Render → your Web Service → Environment:

- `DATA_EXPORT_URL`: copy the full value of `$base.links.export` from PowerShell.
- `RATES_URL`: copy the full value of `$base.links.rates` from PowerShell.

Keep these URLs private because they contain your challenge access parameters.

## 2. Deploy on Render

Create a **Web Service** from this folder/repository.

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Runtime: Python

The public endpoint to submit is the service root URL, for example `https://your-service.onrender.com/` (not `/mcp`).

## 3. Test

Health:
```powershell
Invoke-RestMethod -Uri "https://YOUR-SERVICE.onrender.com/" -Method Get
```

POST:
```powershell
$body = @{ question = "What was the revenue in North in March 2026 in USD?" } | ConvertTo-Json
Invoke-RestMethod -Uri "https://YOUR-SERVICE.onrender.com/" -Method Post -ContentType "application/json" -Body $body
```

## Logic included

- Fetches newline-delimited JSON from the export URL and FX rates from the rates URL.
- Deduplicates order IDs, retaining the row with latest `updated_at`.
- Treats business timestamps as Asia/Kolkata dates.
- Counts only `paid` orders as revenue; separately handles `refunded` and `void`.
- Converts using `usd_per_unit` rates returned by the rates endpoint.

This is a deterministic starter agent. If the challenge includes more specific question types or a customer directory endpoint, extend `answer_question()` accordingly.
