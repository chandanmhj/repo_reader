import os, io, re, json, hmac, hashlib, asyncio, posixpath, zipfile
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
E = os.getenv
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in E("ALLOWED_ORIGINS", "http://localhost:5173").split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(httpx.HTTPError)
async def upstream_error(request, exc):
    msg = str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        msg = f"{exc.request.url.host} returned {exc.response.status_code}: {exc.response.text[:200]}"
    return JSONResponse(status_code=502, content={"detail": msg or type(exc).__name__})


FREE = E("FREE_MODE") == "1"  # skips payment, for local testing
SB_URL, SB_KEY = E("SUPABASE_URL", ""), E("SUPABASE_KEY", "")
RZP = (E("RAZORPAY_KEY_ID", ""), E("RAZORPAY_KEY_SECRET", ""))
GH = {"Authorization": f"Bearer {E('GITHUB_TOKEN')}"} if E("GITHUB_TOKEN") else {}
CODE = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go")
LANG = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".java": "Java", ".kt": "Kotlin", ".go": "Go"}
SKIP = {"node_modules", "dist", "build", ".git", "venv", ".venv", "__pycache__", "tests", "test", "__tests__",
        "vendor", ".next", "target", "coverage", "site-packages"}
CFG = {"package.json", "requirements.txt", "pyproject.toml", "Dockerfile"}
MAX_ZIP, MAX_TEXT, MAX_CODE = 40_000_000, 15_000_000, 3000
EXTS = ("", ".js", ".jsx", ".ts", ".tsx", "/index.js", "/index.jsx", "/index.ts", "/index.tsx")
SERVICES = {
    "Supabase": r"supabase", "Razorpay": r"razorpay", "Groq API": r"groq", "OpenAI API": r"openai",
    "Anthropic API": r"anthropic", "Stripe": r"stripe", "MongoDB": r"mongodb|pymongo|mongoose",
    "PostgreSQL": r"psycopg|asyncpg|postgres|sqlalchemy", "Redis": r"redis", "Firebase": r"firebase",
    "AWS": r"boto3|aws-sdk", "SQLite": r"sqlite",
}
ROUTE_RE = re.compile(r"""(?:@\w+|\b(?:app|router))\.(?:get|post|put|delete|patch|route)\(\s*['"](/[\w\-/{}<>:.]*)['"]""")


async def sb(method, path, extra=None, **kw):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Prefer": "return=representation", **(extra or {})}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{SB_URL}/rest/v1/{path}", headers=h, **kw)
    r.raise_for_status()
    return r.json() if r.text else []


def read_repo(data):
    """Reads EVERY code file in the repo zip (no sampling), plus configs, README and schema files."""
    z = zipfile.ZipFile(io.BytesIO(data))
    paths, code, cand, txt, readme, total = [], [], [], {}, None, 0
    dec = lambda i: z.read(i).decode("utf-8", "ignore")
    for i in z.infolist():
        if i.is_dir() or "/" not in i.filename:
            continue
        p = i.filename.split("/", 1)[1]
        if not p or SKIP & set(p.split("/")):
            continue
        paths.append(p)
        if p.endswith(CODE):
            if not p.endswith(".min.js") and i.file_size <= 200_000 and total < MAX_TEXT and len(code) < MAX_CODE:
                txt[p] = dec(i)
                total += len(txt[p])
                code.append(p)
        elif p.split("/")[-1] in CFG and i.file_size < 200_000:
            cand.append((p, i))
        elif p.lower() == "readme.md":
            txt[p] = dec(i)[:6000]
            readme = p
        elif p.endswith((".sql", ".prisma")) and i.file_size < 100_000:
            txt[p] = dec(i)
    cfg = []
    for p, i in sorted(cand, key=lambda x: x[0].count("/"))[:4]:
        txt[p] = dec(i)
        cfg.append(p)
    return paths, sorted(code), cfg, readme, txt


async def gather(o, r):
    async with httpx.AsyncClient(timeout=60, headers=GH, follow_redirects=True) as c:
        async with c.stream("GET", f"https://api.github.com/repos/{o}/{r}/zipball") as resp:
            if resp.status_code == 404:
                raise HTTPException(404, "Repo not found, or it is private")
            if resp.status_code >= 400:
                await resp.aread()
                resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_ZIP:
                    raise HTTPException(413, "This repo is too large to analyze (over 40 MB).")
    return await asyncio.to_thread(read_repo, bytes(buf))


def make_groups(code, maxg=16):
    g = {}
    for p in code:
        g.setdefault(p.split("/")[0] if "/" in p else "(root)", []).append(p)
    for _ in range(40):
        big = max(g, key=lambda k: len(g[k]))
        n = len(g[big])
        if big == "(root)" or len(g) >= maxg or n < 2 or (len(g) >= 8 and n < 0.3 * len(code)):
            break
        depth = big.count("/") + 1
        kids = {}
        for p in g[big]:
            parts = p.split("/")
            kids.setdefault("/".join(parts[: depth + 1]) if len(parts) > depth + 1 else big, []).append(p)
        if len(kids) == 1 and big in kids or len(g) - 1 + len(kids) > maxg + 2:
            break
        del g[big]
        g.update(kids)
    return g


def graph(code, txt):
    g = {p: [p] for p in code} if len(code) <= 20 else make_groups(code)
    pg = {p: k for k, fs in g.items() for p in fs}
    mods, godirs = {}, {}
    for p in code:
        parts = os.path.splitext(p)[0].split("/")
        if p.endswith((".py", ".java", ".kt")):
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                mods.setdefault(".".join(parts[i:]), p)
            if not p.endswith(".py"):  # Java/Kotlin wildcard imports point at a package folder
                for i in range(len(parts) - 1):
                    mods.setdefault(".".join(parts[i:-1]), p)
        elif p.endswith(".go"):
            godirs.setdefault(posixpath.dirname(p), p)
    edges, routes, svc = {}, {}, {}

    def add(a, b, kind="import"):
        if a != b:
            edges.setdefault((a, b), kind)

    def py_lookup(name):
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            t = mods.get(".".join(parts[:i]))
            if t:
                return t

    for p, src in txt.items():  # API routes defined in the repo
        if p in pg:
            for m in ROUTE_RE.finditer(src):
                r = re.split(r"[{:<]", m[1])[0].rstrip("/")
                if len(r) > 1:
                    routes.setdefault(r, pg[p])

    for p, src in txt.items():
        if p not in pg:
            continue
        a, low = pg[p], src.lower()
        for name, pat in SERVICES.items():
            if re.search(pat, low):
                svc.setdefault(name, set()).add(a)
        if p.endswith(".py"):
            for m in re.finditer(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b\s*([\w, ]*)", src, re.M):
                for t in [py_lookup(m[1])] + [mods.get(f"{m[1]}.{n.strip()}") for n in m[2].split(",") if n.strip()]:
                    if t:
                        add(a, pg[t])
            for m in re.finditer(r"^\s*import\s+([A-Za-z_][\w.]*)", src, re.M):
                t = py_lookup(m[1])
                if t:
                    add(a, pg[t])
            for m in re.finditer(r"^\s*from\s+(\.+)([\w.]*)\s+import\b\s*([\w, ]*)", src, re.M):
                base = posixpath.dirname(p)
                for _ in range(len(m[1]) - 1):
                    base = posixpath.dirname(base)
                rel = posixpath.normpath(posixpath.join(base, m[2].replace(".", "/"))) if (base or m[2]) else ""
                rel = "" if rel == "." else rel
                cands = ([rel + ".py", rel + "/__init__.py"] if rel else []) + [
                    posixpath.join(rel, n.strip() + ".py") for n in m[3].split(",") if n.strip()]
                for t in cands:
                    if t in pg:
                        add(a, pg[t])
        elif p.endswith((".java", ".kt")):
            for m in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;?\s*$", src, re.M):
                t = py_lookup(m[1])
                if t:
                    add(a, pg[t])
        elif p.endswith(".go"):
            specs = re.findall(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', src, re.M)
            for blk in re.findall(r"^\s*import\s*\((.*?)\)", src, re.M | re.S):
                specs += re.findall(r'"([^"]+)"', blk)
            for sp in specs:
                parts = sp.split("/")
                t = next((godirs["/".join(parts[i:])] for i in range(len(parts)) if "/".join(parts[i:]) in godirs), None)
                if t:
                    add(a, pg[t])
        else:
            for m in re.finditer(r"""(?:from\s*|import\s*\(?\s*|require\(\s*)['"]([^'"]+)['"]""", src):
                s, bases = m[1], []
                if s.startswith("."):
                    bases = [posixpath.normpath(posixpath.join(posixpath.dirname(p), s))]
                elif s.startswith(("@/", "~/")):
                    d = posixpath.dirname(p)
                    while True:
                        bases.append(posixpath.join(d, "src", s[2:]))
                        if not d:
                            break
                        d = posixpath.dirname(d)
                for base in bases:
                    t = next((base + e for e in EXTS if base + e in pg), None)
                    if t:
                        add(a, pg[t])
                        break
            for r, bg in routes.items():  # frontend calling a backend route
                if bg != a and re.search(r"""['"`][^'"`\n]*""" + re.escape(r) + r"""(?:[/?'"`]|\$\{)""", src):
                    add(a, bg, "http")
    svc = dict(sorted(svc.items(), key=lambda x: -len(x[1]))[:6])
    return g, [(a, b, k) for (a, b), k in list(edges.items())[:50]], svc


def stack(cfg, txt):
    out = []
    for p in cfg:
        s, n = txt.get(p, ""), p.split("/")[-1]
        if n == "package.json":
            try:
                j = json.loads(s)
                out += list({**j.get("dependencies", {}), **j.get("devDependencies", {})})[:10]
            except Exception:
                pass
        elif n == "requirements.txt":
            out += [re.split(r"[=<>\[ ;]", l)[0] for l in s.splitlines() if l.strip() and not l.startswith("#")][:10]
        elif n == "Dockerfile":
            out.append("Docker")
    return list(dict.fromkeys(out))[:14]


ENTRY = {"main.py", "app.py", "server.py", "manage.py", "wsgi.py", "asgi.py", "index.js", "index.ts", "server.js",
         "server.ts", "main.js", "main.ts", "main.jsx", "main.tsx", "index.jsx", "index.tsx", "app.js", "main.go",
         "Main.java", "Application.java", "Main.kt"}
SYM = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:(?:public|private|protected|static|final|abstract)\s+)*(?:async\s+)?"
                 r"(?:def|class|function|interface|enum|struct|func|fun)\s+(\w+)"
                 r"|^\s*func\s*\([^)]*\)\s*(\w+)"
                 r"|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function|\w+\s*=>)", re.M)
MODEL_RE = re.compile(r"class\s+(\w+)\s*\([^)]*(?:Base|Model|SQLModel|Document|Schema)\b[^)]*\)"
                      r"|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"`]?(\w+)"
                      r"|^model\s+(\w+)\s*\{"
                      r"|mongoose\.model\(\s*['\"](\w+)", re.M | re.I)
ENV_RE = re.compile(r"os\.getenv\(\s*['\"](\w+)|os\.environ\.get\(\s*['\"](\w+)|os\.environ\[\s*['\"](\w+)"
                    r"|process\.env\.(\w+)|import\.meta\.env\.(\w+)")
LAYERS = {"ui", "api", "logic", "data", "config"}

SYSTEM = """You are a senior software architect who explains unfamiliar codebases to students.
You receive facts extracted automatically by reading EVERY code file of a GitHub repo: README excerpt, tech stack, entry points, modules (folders or files) with purpose hints, the functions/classes, API routes and data models they define, detected connections between modules, the most depended-on modules, modules with no detected connections, environment variables, and external services. Code draws the diagram from these facts. Your job is to understand the project first, then label the diagram correctly.

Think in this order and write your notes for steps 1-3 in the "understanding" field:
1. Purpose and type: what the project is (web app, API, library, ML pipeline, CLI, ...) from the README, stack and purpose hints.
2. Entry points and flow: where execution starts and how a request or data moves through the listed connections. Most depended-on modules are usually the core.
3. For each module decide its role and its layer.
4. Check every connection against those roles. Label the important ones.
5. Only then write the description and the explanation.

Layers: ui = screens and components; api = HTTP routes or the server entry; logic = processing, business rules, models; data = database, storage, schemas; config = settings, build and tooling.

Rules:
- Use ONLY the given facts. Never invent modules, files, connections, technologies or features. If the facts are thin, say "appears to" instead of guessing.
- Use module names exactly as written in the facts.
- Modules with no detected connections may be scripts, tooling or unused code. Do not invent links for them.
- The explanation must name the entry point, follow the listed connections to describe the data flow, and mention external services and data models when present.
- Plain language a beginner can follow. No marketing words, no filler.
- Return ONLY one JSON object."""

SCHEMA = ('{"understanding": "max 80 words: project type, entry points, flow", '
          '"description": "2-3 sentences on what the project does and who it is for", '
          '"explanation": "one paragraph of 100-150 words: stack, entry point, how modules connect, data flow, data models, external services", '
          '"roles": {"<module>": "role, max 4 words"}, '
          '"layers": {"<module>": "ui|api|logic|data|config|other"}, '
          '"edge_labels": {"<from> -> <to>": "label, max 3 words"}}')


def symbols(src):
    return [n for n in (x[0] or x[1] or x[2] for x in SYM.findall(src)) if n and not n.startswith("_")][:8]


def doc_line(src):
    head = src[:800]
    for q in ('"""', "'''"):
        i = head.find(q)
        if i != -1:
            j = head.find(q, i + 3)
            if j != -1:
                return " ".join(head[i + 3:j].split())[:110]
    m = re.match(r"\s*(?:/\*+|//)\s*([^\n*]{6,110})", head)
    return m[1].strip() if m else ""


def first(t):
    return next(x for x in t if x)


def build_facts(o, r, g, edges, svc, txt, readme, tech):
    cap = max(350, 11000 // max(len(g), 1))
    lines, entry, env = [], [], []
    for k, fs in g.items():
        syms, rts, docs, models = [], [], [], []
        for p in fs:
            src = txt.get(p, "")
            syms += symbols(src)
            rts += [m[1] for m in ROUTE_RE.finditer(src)]
            models += [first(t) for t in MODEL_RE.findall(src)]
            env += [first(t) for t in ENV_RE.findall(src)]
            d = doc_line(src)
            if d:
                docs.append(d)
            if p.split("/")[-1] in ENTRY or "__main__" in src:
                entry.append(p)
        line = f"- {k} ({len(fs)} files): " + ", ".join(p.split("/")[-1] for p in fs[:6])
        if docs:
            line += " | purpose hints: " + "; ".join(docs[:2])
        if syms:
            line += " | defines: " + ", ".join(dict.fromkeys(syms))[:150]
        if rts:
            line += " | routes: " + ", ".join(dict.fromkeys(rts))[:110]
        if models:
            line += " | data models: " + ", ".join(dict.fromkeys(models))[:100]
        lines.append(line[:cap])
    schema = [first(t) for p, s in txt.items() if p.endswith((".sql", ".prisma")) for t in MODEL_RE.findall(s)]
    indeg = {}
    for a, b, _ in edges:
        indeg[b] = indeg.get(b, 0) + 1
    hubs = ", ".join(f"{k} ({n})" for k, n in sorted(indeg.items(), key=lambda x: -x[1])[:4])
    linked = {x for a, b, _ in edges for x in (a, b)}
    loose = ", ".join([k for k in g if k not in linked][:6])
    return (f"Repo: {o}/{r}\nStack: {', '.join(tech) or 'unknown'}\nREADME: {txt.get(readme, '')[:2000] or 'none'}\n"
            f"Entry points: {', '.join(entry[:6]) or 'not detected'}\nModules:\n" + "\n".join(lines) +
            "\nConnections: " + ("; ".join(f"{a} -> {b}" + (" (HTTP call)" if k == "http" else "") for a, b, k in edges) or "none detected") +
            f"\nMost depended-on modules: {hubs or 'none'}\nNo detected connections: {loose or 'none'}"
            f"\nEnvironment variables: {', '.join(dict.fromkeys(env))[:300] or 'none'}"
            f"\nSchema files define: {', '.join(dict.fromkeys(schema))[:200] or 'none'}"
            "\nExternal services: " + (", ".join(f"{n} (used by {', '.join(sorted(v))})" for n, v in svc.items()) or "none detected"))[:16000]


async def ask(o, r, g, edges, svc, txt, readme, tech):
    facts = build_facts(o, r, g, edges, svc, txt, readme, tech)
    user = f"FACTS:\n{facts}\n\nReturn JSON in exactly this shape. Fill every module in roles and layers, and label up to 15 key connections in edge_labels:\n{SCHEMA}"
    if not E("GROQ_API_KEY"):
        raise HTTPException(500, "GROQ_API_KEY is missing in backend/.env")
    model = E("GROQ_MODEL", "openai/gpt-oss-120b")
    body = {"model": model, "temperature": 0.2, "max_tokens": 2500,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "medium"
    url, hdr = "https://api.groq.com/openai/v1/chat/completions", {"Authorization": f"Bearer {E('GROQ_API_KEY')}"}
    async with httpx.AsyncClient(timeout=90) as c:
        x = await c.post(url, headers=hdr, json={**body, "response_format": {"type": "json_object"}})
        if x.status_code == 400:  # JSON mode failed: retry as plain text and parse it ourselves
            x = await c.post(url, headers=hdr, json=body)
    if x.status_code == 429:
        raise HTTPException(429, "The AI service is busy. Try again in a minute.")
    x.raise_for_status()
    try:
        text = x.json()["choices"][0]["message"]["content"] or ""
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (KeyError, IndexError, TypeError, ValueError):
        raise HTTPException(502, "The AI returned an unreadable answer. Try again.")


def mermaid(g, edges, svc, ai):
    d = lambda k: ai.get(k) if isinstance(ai.get(k), dict) else {}
    roles, layers = d("roles"), d("layers")
    labels = {re.sub(r"\s*(?:->|→)\s*", " -> ", str(k)).strip(): v for k, v in d("edge_labels").items()}
    clean = lambda s: re.sub(r'["<>\[\]()`{}|]', "", str(s))
    ids = {k: f"n{i}" for i, k in enumerate(g)}
    tops = {}
    for k in g:
        tops.setdefault(k.split("/")[0] if "/" in k else "", []).append(k)

    def node(k, strip=0):
        name = k[strip:]
        if len(g[k]) == 1 and g[k][0] == k:
            name = name.split("/")[-1]
        role = clean(roles.get(k, ""))
        return f'{ids[k]}["{clean(name)}' + (f"<br/>{role}" if role else "") + '"]'

    lines = ["flowchart LR"]
    for si, (t, ks) in enumerate(tops.items()):
        if t:
            lines.append(f'  subgraph s{si}["{clean(t)}"]')
            lines += ["    " + node(k, len(t) + 1) for k in ks]
            lines.append("  end")
        else:
            lines += ["  " + node(k) for k in ks]
    for a, b, kind in edges:
        lab = clean(labels.get(f"{a} -> {b}", ""))[:30]
        if kind == "http":
            lines.append(f"  {ids[a]} -.->|{'HTTP: ' + lab if lab else 'HTTP'}| {ids[b]}")
        else:
            lines.append(f"  {ids[a]} -->|{lab}| {ids[b]}" if lab else f"  {ids[a]} --> {ids[b]}")
    for j, (name, gs) in enumerate(svc.items()):
        lines.append(f'  sv{j}[("{clean(name)}")]')
        lines += [f"  {ids[a]} --> sv{j}" for a in sorted(gs)]
    lines += ["  classDef ui fill:#dbeafe,stroke:#12263f", "  classDef api fill:#fde68a,stroke:#12263f",
              "  classDef logic fill:#dcfce7,stroke:#12263f", "  classDef data fill:#fce7f3,stroke:#12263f",
              "  classDef config fill:#e5e7eb,stroke:#12263f", "  classDef ext fill:#ffffff,stroke:#12263f,stroke-dasharray:4 3"]
    by = {}
    for k in g:
        L = str(layers.get(k, "")).lower()
        if L in LAYERS:
            by.setdefault(L, []).append(ids[k])
    lines += [f"  class {','.join(v)} {L}" for L, v in by.items()]
    if svc:
        lines.append("  class " + ",".join(f"sv{j}" for j in range(len(svc))) + " ext")
    return "\n".join(lines)


class Pay(BaseModel):
    order_id: str
    payment_id: str
    signature: str


class Req(BaseModel):
    repo_url: str
    token: str | None = None


@app.get("/")
def health():
    return {"ok": True}


@app.post("/order")
async def order():
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.razorpay.com/v1/orders", auth=RZP, json={"amount": 100, "currency": "INR"})
    r.raise_for_status()
    return {**r.json(), "key": RZP[0]}


@app.post("/verify")
async def verify(p: Pay):
    sig = hmac.new(RZP[1].encode(), f"{p.order_id}|{p.payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, p.signature):
        raise HTTPException(400, "Payment could not be verified")
    await sb("POST", "payments", json={"payment_id": p.payment_id, "used": False})
    return {"token": p.payment_id}


@app.post("/analyze")
async def analyze(q: Req):
    m = re.match(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", q.repo_url.strip())
    if not m:
        raise HTTPException(400, "Enter a link like https://github.com/owner/repo")
    o, r = m[1], m[2]
    key = f"v3-{o}/{r}".lower()
    used = False
    if not FREE:
        if not q.token or not re.fullmatch(r"\w+", q.token):
            raise HTTPException(402, "Payment required")
        if not await sb("PATCH", f"payments?payment_id=eq.{q.token}&used=eq.false", json={"used": True}):
            raise HTTPException(402, "Payment not found or already used")
        used = True
    try:
        if SB_URL:
            hit = await sb("GET", f"analyses?repo=eq.{key}&select=result")
            if hit:
                return hit[0]["result"]
        paths, code, cfg, readme, txt = await gather(o, r)
        g, edges, svc = await asyncio.to_thread(graph, code, txt)
        if not g:
            raise HTTPException(422, "No supported code found (Python, JavaScript/TypeScript, Java, Kotlin or Go)")
        tech = stack(cfg, txt)
        ai = await ask(o, r, g, edges, svc, txt, readme, tech)
        langs = {}
        for p in code:
            n = LANG[os.path.splitext(p)[1]]
            langs[n] = langs.get(n, 0) + 1
        res = {"repo": f"{o}/{r}", "description": str(ai.get("description", "")), "explanation": str(ai.get("explanation", "")),
               "stack": tech, "mermaid": mermaid(g, edges, svc, ai),
               "stats": {"files": len(code), "languages": langs, "modules": len(g), "connections": len(edges)}}
        if SB_URL:
            try:
                await sb("POST", "analyses", json={"repo": key, "result": res})
            except Exception:
                pass
        return res
    except Exception as e:
        if used:  # give the payment back if analysis failed
            await sb("PATCH", f"payments?payment_id=eq.{q.token}", json={"used": False})
        if isinstance(e, (HTTPException, httpx.HTTPError)):
            raise
        raise HTTPException(500, f"{type(e).__name__}: {e}")