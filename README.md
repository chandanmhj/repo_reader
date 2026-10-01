# Repo Reader

Paste a GitHub repo, get an architecture diagram, a description, and a technical explanation. ₹1 per analysis via Razorpay.

## Supabase (SQL editor)
```sql
create table payments (payment_id text primary key, used boolean default false, created_at timestamptz default now());
create table analyses (repo text primary key, result jsonb, created_at timestamptz default now());
alter table payments enable row level security;
alter table analyses enable row level security;
```
Use the service_role key in the backend only (it bypasses RLS). Never put it in the frontend.

## Run locally
- Backend: `cd backend && pip install -r requirements.txt && cp .env.example .env` (fill it, export the vars), then `uvicorn main:app --reload`
- Frontend: `cd frontend && cp .env.example .env && npm install && npm run dev`
- Keep `FREE_MODE=1` and `VITE_FREE_MODE=1` until Razorpay is set up (use test keys first).

## Deploy
- **Render (backend):** root dir `backend`, build `pip install -r requirements.txt`, start `uvicorn main:app --host 0.0.0.0 --port $PORT`. Add the env vars, set `FREE_MODE=0` and `ALLOWED_ORIGINS` to your Netlify/GoDaddy URL.
- **Netlify (frontend):** base dir `frontend`, build `npm run build`, publish `dist`. Env: `VITE_API_URL` = Render URL, `VITE_FREE_MODE=0`.
- **GoDaddy:** add a CNAME for your subdomain pointing to your Netlify site, then add the domain in Netlify.
- Razorpay approval usually needs terms, privacy, and refund pages on the live site.

## Notes
- If Groq rejects `response_format` for your model, change `GROQ_MODEL` (e.g. a Llama model).
- The backend downloads the whole repo as a zip and reads every code file (Python, JS/TS, Java, Kotlin, Go). Limits: 40 MB download, 3000 code files, files over 200 KB and test/build/vendor folders skipped.
- Set `GITHUB_TOKEN` in production: without it GitHub allows only about 60 API requests per hour per IP.