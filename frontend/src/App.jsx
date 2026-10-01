import { useState, useEffect } from "react";
import mermaid from "mermaid";

const API = import.meta.env.VITE_API_URL;
const FREE = import.meta.env.VITE_FREE_MODE === "1";
mermaid.initialize({ startOnLoad: false, theme: "neutral", htmlLabels: false, flowchart: { useMaxWidth: false, htmlLabels: false } });

const loadRzp = () =>
  new Promise((ok, no) => {
    if (window.Razorpay) return ok();
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = ok;
    s.onerror = () => no(new Error("Could not load the payment window"));
    document.body.appendChild(s);
  });

async function pay() {
  await loadRzp();
  const o = await (await fetch(`${API}/order`, { method: "POST" })).json();
  return new Promise((ok, no) =>
    new window.Razorpay({
      key: o.key, order_id: o.id, amount: o.amount, currency: "INR",
      name: "Repo Reader", description: "One repo analysis",
      handler: async (r) => {
        const v = await fetch(`${API}/verify`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ order_id: r.razorpay_order_id, payment_id: r.razorpay_payment_id, signature: r.razorpay_signature }),
        });
        v.ok ? ok((await v.json()).token) : no(new Error("Payment could not be verified"));
      },
      modal: { ondismiss: () => no(new Error("Payment cancelled")) },
    }).open()
  );
}

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function downloadSvg(svg, name) {
  saveBlob(new Blob([svg], { type: "image/svg+xml" }), name);
}

function downloadPng(svg, name) {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
  const vb = (doc.getAttribute("viewBox") || "").split(/\s+/).map(Number);
  const w = vb[2] || parseFloat(doc.getAttribute("width")) || 1200;
  const h = vb[3] || parseFloat(doc.getAttribute("height")) || 800;
  doc.setAttribute("width", w);
  doc.setAttribute("height", h);
  const img = new Image();
  img.onload = () => {
    const scale = Math.min(2, 8000 / Math.max(w, h));
    const c = document.createElement("canvas");
    c.width = w * scale;
    c.height = h * scale;
    const x = c.getContext("2d");
    x.fillStyle = "#ffffff";
    x.fillRect(0, 0, c.width, c.height);
    x.scale(scale, scale);
    x.drawImage(img, 0, 0, w, h);
    c.toBlob((b) => saveBlob(b, name));
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(doc));
}

export default function App() {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [res, setRes] = useState(null);
  const [svg, setSvg] = useState("");

  useEffect(() => {
    if (res) mermaid.render("d" + Date.now(), res.mermaid).then((o) => setSvg(o.svg)).catch(() => setSvg(""));
  }, [res]);

  async function run(e) {
    e.preventDefault();
    setErr(""); setRes(null); setSvg(""); setBusy(true);
    try {
      let token = sessionStorage.getItem("tok");
      if (!FREE && !token) {
        setMsg("Waiting for payment...");
        token = await pay();
        sessionStorage.setItem("tok", token);
      }
      setMsg("Reading the repo. The first request can take up to a minute while the server wakes up.");
      const r = await fetch(`${API}/analyze`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: url, token }),
      });
      const d = await r.json();
      if (r.status === 402) sessionStorage.removeItem("tok");
      if (!r.ok) throw new Error(d.detail || "Something went wrong");
      sessionStorage.removeItem("tok");
      setRes(d);
    } catch (x) {
      setErr(x.message);
    } finally {
      setBusy(false); setMsg("");
    }
  }

  return (
    <main>
      <h1>Repo Reader</h1>
      <p className="lede">Paste a public GitHub repo. Get its architecture diagram, what it does, and how the code fits together.</p>
      <form onSubmit={run}>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repo" required />
        <button disabled={busy}>{busy ? "Working..." : FREE ? "Analyze repo" : "Pay ₹1 and analyze"}</button>
      </form>
      {msg && <p className="note">{msg}</p>}
      {err && <p className="err" role="alert">{err}</p>}
      {res && (
        <section className="out">
          <h2>{res.repo}</h2>
          <p>{res.description}</p>
          {res.stats && (
            <p className="stats">
              Read {res.stats.files} code files ({Object.entries(res.stats.languages).map(([l, n]) => `${l} ${n}`).join(", ")}) and found {res.stats.modules} modules with {res.stats.connections} connections.
            </p>
          )}
          <h3>Architecture</h3>
          {svg && (
            <div className="actions">
              <button type="button" className="ghost" onClick={() => downloadPng(svg, res.repo.replace("/", "-") + "-architecture.png")}>Download PNG</button>
              <button type="button" className="ghost" onClick={() => downloadSvg(svg, res.repo.replace("/", "-") + "-architecture.svg")}>Download SVG</button>
            </div>
          )}
          <div className="legend">
            <span><i style={{ background: "#dbeafe" }} />UI</span>
            <span><i style={{ background: "#fde68a" }} />API</span>
            <span><i style={{ background: "#dcfce7" }} />Logic</span>
            <span><i style={{ background: "#fce7f3" }} />Data</span>
            <span><i style={{ background: "#e5e7eb" }} />Config</span>
            <span>Dotted arrow = HTTP call</span>
          </div>
          <div className="diagram" dangerouslySetInnerHTML={{ __html: svg }} />
          <h3>How the code works</h3>
          <p>{res.explanation}</p>
          {res.stack.length > 0 && (
            <div className="chips">{res.stack.map((s) => <span key={s}>{s}</span>)}</div>
          )}
        </section>
      )}
      <footer className="foot">
        Built by Chandan Murthy HJ. <a href="https://chandanmhj.in" rel="noopener">Meet the creator</a>
      </footer>
    </main>
  );
}