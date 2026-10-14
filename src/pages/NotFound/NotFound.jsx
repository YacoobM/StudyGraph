import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <main
      style={{
        minHeight: "100dvh",
        display: "grid",
        placeItems: "center",
        padding: 24,
        background: "var(--hero-gradient)",
        color: "var(--brand-ink)",
      }}
    >
      <section style={{ textAlign: "center" }}>
        <p style={{ marginBottom: 12, color: "var(--brand-muted)" }}>
          404 · PAGE NOT FOUND
        </p>
        <h1 style={{ marginBottom: 12, font: "500 48px var(--font-serif)" }}>
          This page wandered off.
        </h1>
        <p style={{ marginBottom: 24, color: "var(--brand-muted)" }}>
          Let’s get you back to your ideas.
        </p>
        <Link className="btn btn-primary" to="/workspace">
          Go to workspace
        </Link>
      </section>
    </main>
  );
}
