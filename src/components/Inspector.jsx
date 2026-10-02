import React, { useEffect, useMemo, useRef, useState } from "react";
import { fetchGraphConceptEvolution } from "../services/api.js";

/**
 * Right-hand inspector — ported from the prototype's selectNode(), including
 * the per-concept evolution view.
 *
 * The prototype read a hand-written NAMED table with definitions, sources and
 * a week-by-week history. The backend does not return prose definitions, so
 * everything here is derived from the graph itself:
 *
 *   definition   the concept's own relationships, read as a sentence
 *   connected to its neighbours
 *   sources      the documents the concept was extracted from
 *   history      each neighbour, at the week that neighbour first appears
 *
 * That keeps the panel honest: it shows what the graph actually knows rather
 * than inventing an explanation the extraction never produced.
 */
export default function Inspector({
  graphId,
  node,
  nodes = [],
  edges = [],
  maxWeek = 1,
  onClose,
}) {
  const canvasRef = useRef(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [details, setDetails] = useState(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");

  const info = useMemo(() => {
    if (!node) return null;

    const byId = new Map(nodes.map((n) => [n.id, n]));
    const touching = edges.filter(
      (e) =>
        (e.source?.id ?? e.source) === node.id ||
        (e.target?.id ?? e.target) === node.id,
    );

    const neighbours = touching.map((e) => {
      const outgoing = (e.source?.id ?? e.source) === node.id;
      const otherId = outgoing
        ? (e.target?.id ?? e.target)
        : (e.source?.id ?? e.source);
      const other = byId.get(otherId);
      return {
        name: other?.name ?? String(otherId),
        week: other?.week ?? node.week,
        relation: e.label || "relates to",
        outgoing,
      };
    });

    // Read the outgoing relations back as a sentence; fall back to incoming.
    const out = neighbours.filter((n) => n.outgoing);
    const phrase = (out.length ? out : neighbours)
      .slice(0, 3)
      .map((n) => `${n.relation} ${n.name}`)
      .join(", ");

    // main.py sends a relation summary in node.description; use it when
    // present rather than re-deriving a worse one.
    const definition =
      node.description ||
      (phrase
        ? `From your sources: ${node.name} ${phrase}.`
        : `${node.name} appears in your material but has no recorded relationships yet.`);

    const sourceNames = [...new Set((node.sources || [])
      .map((source) => (typeof source === "string" ? source : source.name))
      .filter(Boolean))];
    const history = [
      {
        week: node.week,
        label: "First appears",
        detail: `Extracted from ${sourceNames.join(", ") || "your uploads"}.`,
      },
      ...[...neighbours]
        .sort((a, b) => a.week - b.week)
        .map((n) => ({
          week: Math.max(n.week, node.week),
          label: `Connected to ${n.name}`,
          detail: `${node.name} ${n.relation} ${n.name}.`,
        })),
    ];

    return { neighbours, definition, history, firstWeek: node.week };
  }, [node, nodes, edges]);

  const [evoWeek, setEvoWeek] = useState(1);
  useEffect(() => {
    if (info) setEvoWeek(info.firstWeek);
  }, [info]);

  useEffect(() => {
    setDetailsOpen(false);
    setDetails(null);
    setDetailsError("");
  }, [node?.id]);

  const showDetails = async () => {
    if (details) {
      setDetailsOpen((open) => !open);
      return;
    }

    setDetailsOpen(true);
    setDetailsLoading(true);
    setDetailsError("");
    try {
      const result = await fetchGraphConceptEvolution(graphId, node.name);
      setDetails(result);
    } catch (error) {
      setDetailsError(
        error.message || "Detailed evolution could not be loaded.",
      );
    } finally {
      setDetailsLoading(false);
    }
  };

  /* Radial evolution view: neighbours appear as their week is reached. */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !info) return;
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
      return {
        angle,
        x: cx + Math.cos(angle) * radius,
        y: cy + Math.sin(angle) * radius,
      };
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
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#F3EEDD";
      ctx.font = "10px Inter, sans-serif";
      ctx.textAlign =
        angle > -Math.PI / 2 && angle < Math.PI / 2 ? "left" : "right";
      const lx = x + (ctx.textAlign === "left" ? 10 : -10);
      ctx.fillText(
        n.name.length > 16 ? `${n.name.slice(0, 15)}…` : n.name,
        lx,
        y + 3,
      );
    });

    // the concept itself, at the centre
    ctx.beginPath();
    ctx.fillStyle = "#C1652F";
    ctx.arc(cx, cy, 9, 0, Math.PI * 2);
    ctx.fill();
  }, [info, evoWeek]);

  if (!node) return null;

  // The only "type" the data supports is how connected the concept is, which
  // Workspace already derives from node degree. Nothing else is invented.
  const conceptType = node.kind === "core" ? "Core concept" : "Supporting concept";

  return (
    <aside className="inspector insp-concept" id="inspector" role="complementary"
           aria-label={`Details for ${node.name}`}>
      <div className="insp-top">
        <div className="insp-eyebrow">Concept</div>
        {onClose && (
          <button type="button" className="insp-close" onClick={onClose}
                  aria-label="Close the concept panel" title="Close">
            ×
          </button>
        )}
      </div>

      <div className="insp-name">{node.name}</div>

      <div className="insp-meta">
        <span className="insp-chip">{conceptType}</span>
        <span className="insp-chip quiet">
          {info.neighbours.length} connection{info.neighbours.length === 1 ? "" : "s"}
        </span>
        <span className="insp-chip quiet">First seen week {node.week}</span>
      </div>

      <div className="insp-def">{info.definition}</div>

      <div className="insp-label">Connections</div>
      {info.neighbours.length === 0 ? (
        <div className="insp-none">
          No relationship to another concept was extracted for this one.
        </div>
      ) : (
        <ul className="insp-rels">
          {info.neighbours.map((n, i) => (
            <li key={i}>
              <span className="insp-rel-arrow" aria-hidden="true">
                {n.outgoing ? "→" : "←"}
              </span>
              <span className="insp-rel-name">{n.name}</span>
              <span className="insp-rel-type">{n.relation}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="insp-label">Sources</div>
      {(node.sources || []).length === 0 ? (
        <div className="insp-source">
          <div className="st">Not recorded</div>
        </div>
      ) : (
        node.sources.map((s, i) => (
          <div className="insp-source" key={i}>
            <div className="st">
              {typeof s === "string" ? s : s.name}
              {typeof s !== "string" && s.deleted && (
                <span className="sp-deleted">(deleted)</span>
              )}
            </div>
            <div className="sp">{typeof s === "string" ? `Week ${node.week}` : s.weekLabel}</div>
          </div>
        ))
      )}

      <div className="insp-evo">
        <div className="insp-label">How this concept evolved</div>
        <div className="evo-current-week">
          Viewing <strong>Week {evoWeek}</strong>
        </div>
        <div className="evo-canvas-wrap">
          <canvas ref={canvasRef} id="evoCanvas" />
        </div>
        <div className="evo-slider-wrap">
          <input
            type="range"
            min={info.firstWeek}
            max={maxWeek}
            value={evoWeek}
            step="1"
            onChange={(e) => setEvoWeek(Number(e.target.value))}
          />
          <div className="evo-ticks">
            <span>Week {info.firstWeek} · first appears</span>
            <span>Week {maxWeek} · now</span>
          </div>
        </div>

        {node.weeks?.length > 1 && (
          <div className="evo-details">
            <button
              className="evo-details-toggle"
              onClick={showDetails}
              aria-expanded={detailsOpen}
            >
              <span>
                {detailsOpen
                  ? "Hide detailed evolution"
                  : "View detailed evolution"}
              </span>
              <span aria-hidden="true">{detailsOpen ? "−" : "+"}</span>
            </button>

            {detailsOpen && (
              <div className="evo-details-body">
                {detailsLoading && (
                  <div className="evo-details-muted">
                    Preparing the weekly summaries…
                  </div>
                )}
                {detailsError && (
                  <div className="evo-details-error">
                    {detailsError}
                    <button onClick={showDetails}>Try again</button>
                  </div>
                )}
                {details && (
                  <>
                    <div className="evo-detail-block">
                      <div className="evo-detail-heading">
                        Overall evolution
                      </div>
                      <p>{details.comparison}</p>
                    </div>

                    <div className="evo-detail-block">
                      <div className="evo-detail-heading">Weekly summaries</div>
                      <div className="evo-weekly-list">
                        {details.weeks_data?.map((weekData) => (
                          <div className="evo-weekly-item" key={weekData.week}>
                            <div className="evo-weekly-week">
                              {weekData.week}
                            </div>
                            <p>{weekData.summary}</p>
                            {weekData.is_fallback && (
                              <span className="evo-fallback">
                                Source excerpt used
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>

                    {details.consecutive_comparisons?.length > 0 && (
                      <div className="evo-detail-block">
                        <div className="evo-detail-heading">What changed</div>
                        <div className="evo-weekly-list">
                          {details.consecutive_comparisons.map((comparison) => (
                            <div
                              className="evo-weekly-item"
                              key={`${comparison.from_week}-${comparison.to_week}`}
                            >
                              <div className="evo-weekly-week">
                                {comparison.from_week} → {comparison.to_week}
                              </div>
                              <p>{comparison.comparison}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}

        <div className="evo-label" style={{ marginBottom: 8 }}>
          Timeline
        </div>
        <div className="evo-log">
          {info.history.map((h, i) => {
            const state =
              h.week < evoWeek ? "past" : h.week === evoWeek ? "current" : "";
            return (
              <div className={`evo-log-item ${state}`} key={i}>
                <div className="evo-dot-col">
                  <span className="evo-dot" />
                  <span className="evo-line" />
                </div>
                <div className="evo-body">
                  <div className="evo-wk">WEEK {h.week}</div>
                  <div className="evo-label">{h.label}</div>
                  <div className="evo-detail">{h.detail}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </aside>
  );
}
