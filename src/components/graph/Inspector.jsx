import { useEffect, useMemo, useRef, useState } from "react";
import { fetchGraphConceptEvolution, getGraphDocumentSignedUrl } from "../../services/api.js";
import "./Inspector.css";

// Connections shown before "Show all".
const PREVIEW = 5;

const Chevron = ({ open }) => (
  <svg className={`insp-chev${open ? " open" : ""}`} width="14" height="14" viewBox="0 0 24 24"
       fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
       aria-hidden="true">
    <path d="m9 6 6 6-6 6" />
  </svg>
);

const FileIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5" />
  </svg>
);

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function pageLabel(pages) {
  if (!pages?.length) return "";
  const first = pages[0];
  const last = pages[pages.length - 1];
  if (pages.length === 1) return `p. ${first}`;
  return last - first === pages.length - 1 ? `pp. ${first}–${last}` : `pp. ${pages.slice(0, 4).join(", ")}${pages.length > 4 ? "…" : ""}`;
}

/** "Mutex, Race Condition and 4 more". */
function listSummary(names, limit = 5) {
  if (names.length <= limit) return names.join(", ");
  return `${names.slice(0, limit).join(", ")} and ${names.length - limit} more`;
}

/** A section heading that opens and closes its content. */
function Disclosure({ id, title, summary, open, onToggle, children }) {
  return (
    <section className="insp-sec">
      <button type="button" className="insp-sec-head" aria-expanded={open} aria-controls={id} onClick={onToggle}>
        <Chevron open={open} />
        <span className="insp-sec-title">{title}</span>
        {summary && <span className="insp-sec-sum">{summary}</span>}
      </button>
      {open && <div id={id} className="insp-sec-body">{children}</div>}
    </section>
  );
}

function SourceCard({ source, graphId }) {
  const [open, setOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");
  const canOpen = Boolean(graphId && source.documentId && !source.deleted);
  const pages = pageLabel(source.pages);

  async function openDocument() {
    setError("");
    setOpening(true);
    // Open the tab during the click so the browser does not block it as a popup.
    const tab = window.open("about:blank", "_blank");
    try {
      const { url } = await getGraphDocumentSignedUrl(graphId, source.documentId);
      const target = source.pages?.length && /\.pdf$/i.test(source.name) ? `${url}#page=${source.pages[0]}` : url;
      if (tab) {
        tab.opener = null;
        tab.location.href = target;
      } else {
        window.open(target, "_blank", "noopener,noreferrer");
      }
    } catch (e) {
      tab?.close();
      setError(e.message || "This document could not be opened.");
    } finally {
      setOpening(false);
    }
  }

  return (
    <li className={`insp-src${source.deleted ? " gone" : ""}`}>
      <div className="insp-src-row">
        <span className="insp-src-icon"><FileIcon /></span>
        <div className="insp-src-main">
          <div className="insp-src-name" title={source.name}>{source.name}</div>
          <div className="insp-src-meta">
            <span>{source.weekLabel}</span>
            {pages && <span>{pages}</span>}
            {source.deleted && <span className="insp-src-flag">Removed from this graph</span>}
            {source.unlinked && <span className="insp-src-flag quiet">No file link recorded</span>}
          </div>
        </div>
        {canOpen && (
          <button type="button" className="insp-src-open" onClick={openDocument} disabled={opening}
                  aria-label={`Open ${source.name}`}>
            {opening ? "Opening…" : "Open"}
          </button>
        )}
      </div>
      {source.excerpts.length > 0 && (
        <>
          <button type="button" className="insp-src-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            <Chevron open={open} />
            {open ? "Hide excerpt" : `Show ${source.excerpts.length === 1 ? "excerpt" : `${source.excerpts.length} excerpts`}`}
          </button>
          {open && source.excerpts.slice(0, 3).map((text, i) => (
            <blockquote className="insp-src-quote" key={i}>{text}</blockquote>
          ))}
        </>
      )}
      {error && <div className="insp-src-error" role="alert">{error}</div>}
    </li>
  );
}

/**
 * Right-hand concept panel. Everything here comes from the graph itself: the
 * concept's description, its relationships and the provenance of its sources.
 */
export default function Inspector({
  graphId,
  node,
  nodes = [],
  edges = [],
  maxWeek = 1,
  onClose,
  onSelectConcept,
}) {
  const canvasRef = useRef(null);
  // Open sections stay as the reader left them when another concept is picked.
  const [openSections, setOpenSections] = useState({ connections: true, sources: true, evolution: true });
  const [showAll, setShowAll] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [details, setDetails] = useState(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const toggle = (key) => setOpenSections((s) => ({ ...s, [key]: !s[key] }));

  // Escape closes the panel, unless the key belongs to a field, the chat or a dialog.
  useEffect(() => {
    if (!node || !onClose) return undefined;
    const onKey = (e) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target.closest?.("input, textarea, select")) return;
      if (document.querySelector('[aria-modal="true"], .chat-backdrop')) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [node, onClose]);

  const info = useMemo(() => {
    if (!node) return null;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const idOf = (end) => String(end?.id ?? end);

    const seen = new Set();
    const neighbours = [];
    edges.forEach((e) => {
      const outgoing = idOf(e.source) === node.id;
      if (!outgoing && idOf(e.target) !== node.id) return;
      const otherId = outgoing ? idOf(e.target) : idOf(e.source);
      const relation = e.label || "relates to";
      const key = `${otherId}|${relation}|${outgoing}`;
      if (seen.has(key)) return;
      seen.add(key);
      const other = byId.get(otherId);
      neighbours.push({
        id: otherId,
        name: other?.name ?? otherId,
        week: Math.max(other?.week ?? node.week, node.week),
        relation,
        outgoing,
      });
    });
    neighbours.sort((a, b) => a.week - b.week || a.name.localeCompare(b.name));

    const definition =
      node.description ||
      (neighbours.length
        ? `From your sources: ${node.name} ${neighbours.slice(0, 3).map((n) => `${n.relation} ${n.name}`).join(", ")}.`
        : `${node.name} appears in your material but has no recorded relationships yet.`);

    // One timeline entry per week rather than one per connection.
    const weeks = new Map();
    const at = (week) => {
      if (!weeks.has(week)) weeks.set(week, { week, introduced: [], connected: [] });
      return weeks.get(week);
    };
    (node.sources || []).forEach((s) => at(s.week).introduced.push(s.name));
    if (!weeks.size) at(node.week);
    neighbours.forEach((n) => {
      const entry = at(n.week);
      if (!entry.connected.includes(n.name)) entry.connected.push(n.name);
    });
    const timeline = [...weeks.values()].sort((a, b) => a.week - b.week);

    return { neighbours, definition, timeline, firstWeek: node.week };
  }, [node, nodes, edges]);

  const [evoWeek, setEvoWeek] = useState(1);
  useEffect(() => {
    if (info) setEvoWeek(maxWeek);
  }, [info, maxWeek]);

  useEffect(() => {
    setShowAll(false);
    setDetailsOpen(false);
    setDetails(null);
    setDetailsError("");
  }, [node?.id]);

  // The week slider only helps when the concept or its links change across weeks.
  const spansWeeks = Boolean(info && info.timeline.length > 1);

  async function showDetails() {
    if (details) {
      setDetailsOpen((open) => !open);
      return;
    }
    setDetailsOpen(true);
    setDetailsLoading(true);
    setDetailsError("");
    try {
      setDetails(await fetchGraphConceptEvolution(graphId, node.name));
    } catch (error) {
      setDetailsError(error.message || "Detailed evolution could not be loaded.");
    } finally {
      setDetailsLoading(false);
    }
  }

  // Radial view: neighbours appear as the slider reaches their week.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !info || !spansWeeks || !openSections.evolution) return;
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h / 2;
    const total = Math.max(1, info.neighbours.length);
    const radius = Math.min(w, h) * 0.34;
    const place = (i) => {
      const angle = (Math.PI * 2 * i) / total - Math.PI / 2;
      return { angle, x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
    };
    info.neighbours.forEach((n, i) => {
      if (n.week > evoWeek) return;
      const { x, y } = place(i);
      ctx.strokeStyle = "rgba(255,255,255,0.18)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(x, y);
      ctx.stroke();
    });
    info.neighbours.forEach((n, i) => {
      if (n.week > evoWeek) return;
      const { angle, x, y } = place(i);
      ctx.beginPath();
      ctx.fillStyle = n.week === evoWeek ? "#C1652F" : "#96897A";
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#F3EEDD";
      ctx.font = "10px Inter, sans-serif";
      ctx.textAlign = angle > -Math.PI / 2 && angle < Math.PI / 2 ? "left" : "right";
      const lx = x + (ctx.textAlign === "left" ? 9 : -9);
      ctx.fillText(n.name.length > 16 ? `${n.name.slice(0, 15)}…` : n.name, lx, y + 3);
    });
    ctx.beginPath();
    ctx.fillStyle = "#C1652F";
    ctx.arc(cx, cy, 8, 0, Math.PI * 2);
    ctx.fill();
  }, [info, evoWeek, spansWeeks, openSections.evolution]);

  if (!node || !info) return null;

  const conceptType = node.kind === "core" ? "Core concept" : "Supporting concept";
  const sources = node.sources || [];
  const shown = showAll ? info.neighbours : info.neighbours.slice(0, PREVIEW);

  return (
    <aside className="inspector insp-concept" id="inspector" aria-label={`Details for ${node.name}`}>
      <div className="insp-top">
        <div className="insp-eyebrow">Concept</div>
        {onClose && (
          <button type="button" className="insp-close" onClick={onClose} aria-label="Close the concept panel" title="Close">
            ×
          </button>
        )}
      </div>

      <h2 className="insp-name">{node.name}</h2>
      <p className="insp-def">{info.definition}</p>

      <div className="insp-meta">
        {node.status === "needs_regeneration" && <span className="insp-chip degraded">Needs update</span>}
        <span className="insp-chip">{conceptType}</span>
        <span className="insp-chip quiet">First seen week {node.week}</span>
        <span className="insp-chip quiet">{plural(sources.length, "source")}</span>
      </div>

      <Disclosure
        id="insp-connections"
        title="Connections"
        summary={plural(info.neighbours.length, "connection")}
        open={openSections.connections}
        onToggle={() => toggle("connections")}
      >
        {info.neighbours.length === 0 ? (
          <p className="insp-none">No relationship to another concept was extracted for this one.</p>
        ) : (
          <>
            <ul className="insp-rels">
              {shown.map((n) => (
                <li key={`${n.id}-${n.relation}-${n.outgoing}`}>
                  <span className="insp-rel-arrow" aria-label={n.outgoing ? "to" : "from"}>{n.outgoing ? "→" : "←"}</span>
                  {onSelectConcept ? (
                    <button type="button" className="insp-rel-name" onClick={() => onSelectConcept(n.id)}>{n.name}</button>
                  ) : (
                    <span className="insp-rel-name">{n.name}</span>
                  )}
                  <span className="insp-rel-type">{n.relation}</span>
                </li>
              ))}
            </ul>
            {info.neighbours.length > PREVIEW && (
              <button type="button" className="insp-more" onClick={() => setShowAll((v) => !v)} aria-expanded={showAll}>
                {showAll ? "Show fewer" : `Show all ${info.neighbours.length}`}
              </button>
            )}
          </>
        )}
      </Disclosure>

      <Disclosure
        id="insp-sources"
        title="Sources"
        summary={sources.length ? plural(sources.length, "document") : "None recorded"}
        open={openSections.sources}
        onToggle={() => toggle("sources")}
      >
        {sources.length === 0 ? (
          <p className="insp-none">No source evidence was recorded for this concept.</p>
        ) : (
          <ul className="insp-srcs">
            {sources.map((s) => <SourceCard key={s.key} source={s} graphId={graphId} />)}
          </ul>
        )}
      </Disclosure>

      <Disclosure
        id="insp-evolution"
        title="How it evolved"
        summary={info.timeline.length > 1 ? `Weeks ${info.timeline[0].week} to ${info.timeline[info.timeline.length - 1].week}` : `Week ${info.firstWeek}`}
        open={openSections.evolution}
        onToggle={() => toggle("evolution")}
      >
        {spansWeeks && (
          <div className="insp-evo-view">
            <div className="evo-canvas-wrap">
              <canvas ref={canvasRef} aria-label={`Connections of ${node.name} up to week ${evoWeek}`} role="img" />
            </div>
            <input
              type="range"
              min={info.firstWeek}
              max={maxWeek}
              value={evoWeek}
              step="1"
              aria-label="Show connections up to week"
              aria-valuetext={`Week ${evoWeek}`}
              onChange={(e) => setEvoWeek(Number(e.target.value))}
            />
            <div className="evo-ticks">
              <span>Week {info.firstWeek}</span>
              <span>Showing week {evoWeek}</span>
              <span>Week {maxWeek}</span>
            </div>
          </div>
        )}

        <ol className="insp-timeline">
          {info.timeline.map((entry) => (
            <li key={entry.week} className={spansWeeks && entry.week > evoWeek ? "later" : ""}>
              <div className="insp-tl-week">Week {entry.week}</div>
              {entry.introduced.length > 0 && (
                <p><b>{entry.week === info.firstWeek ? "Introduced in" : "Also in"}</b> {listSummary([...new Set(entry.introduced)], 3)}</p>
              )}
              {entry.connected.length > 0 && (
                <p><b>Connected to</b> {listSummary(entry.connected)}</p>
              )}
            </li>
          ))}
        </ol>

        {node.weeks?.length > 1 && graphId && (
          <div className="evo-details">
            <button type="button" className="insp-more" onClick={showDetails} aria-expanded={detailsOpen}>
              {detailsOpen ? "Hide weekly summaries" : "Read weekly summaries"}
            </button>
            {detailsOpen && (
              <div className="evo-details-body">
                {detailsLoading && <div className="insp-loading"><span className="insp-spinner" aria-hidden="true" />Writing the weekly summaries…</div>}
                {detailsError && (
                  <div className="insp-src-error" role="alert">
                    {detailsError} <button type="button" onClick={showDetails}>Try again</button>
                  </div>
                )}
                {details && (
                  <>
                    {details.comparison && <p className="evo-overall">{details.comparison}</p>}
                    {details.weeks_data?.map((weekData) => (
                      <div className="evo-weekly-item" key={weekData.week}>
                        <div className="insp-tl-week">{weekData.week}</div>
                        <p>{weekData.summary}</p>
                        {weekData.is_fallback && <span className="evo-fallback">Source excerpt used</span>}
                      </div>
                    ))}
                    {details.consecutive_comparisons?.map((c) => (
                      <div className="evo-weekly-item" key={`${c.from_week}-${c.to_week}`}>
                        <div className="insp-tl-week">{c.from_week} → {c.to_week}</div>
                        <p>{c.comparison}</p>
                      </div>
                    ))}
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </Disclosure>
    </aside>
  );
}
