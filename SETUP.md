# StudyGraph — React app in the StudyGraph design

## 1. What this project is

**React 18 + Vite**, routed with `react-router-dom`, graphs drawn with `d3`.
Entry point is `index.html` → `src/main.jsx`. There is no standalone HTML page
any more — the design has been rebuilt as React components.

## 2. Run it

**Terminal 1 — backend**

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn python-multipart
python dev_server.py
```

**Terminal 2 — frontend**

```bash
npm install
npm run dev
```

Open **http://localhost:3000/** and hard-refresh (Cmd+Shift+R).

## 3. Using it

1. Sign in or create an account
2. You land on `/workspace` — sidebar on the left, five tabs across the top
3. **Documents** tab → drop in a PDF
4. Watch it go Queued → Running → Ready with a concept count
5. It switches to **Graph** automatically, drawn from what was extracted
6. **Chat** answers from your sources; **Notes** are written from the concepts;
   **Quiz** has practice questions

`+ New graph` opens the Documents tab. That is deliberate: a graph is whatever
your documents produce, so there is nothing to create before adding a source.

## 4. What the screens arecd

| Route | Component | What it is |
|---|---|---|
| `/` | `pages/Landing.jsx` | marketing page |
| `/login` | `pages/Login.jsx` | one screen, sign-in and sign-up |
| `/workspace` | `pages/Workspace.jsx` | **the app** — sidebar, five tabs |
| `/builder` | `pages/Builder.jsx` | your teammates' original page, untouched |

`/builder` still works. Nothing of theirs was deleted.

New files:

```
src/pages/Workspace.jsx        the workspace: sidebar, tabs, all five panes
src/pages/Workspace.css        its stylesheet
src/components/ConceptGraph.jsx  d3 force graph with the week slider
src/data/sampleData.js         quiz content carried over from the design
```

## 5. The design — ported, not approximated

`src/styles/studygraph.css` is the prototype's stylesheet, lifted verbatim
(478 lines). `Landing.jsx` is the prototype's marketing markup converted to
JSX — same elements, same class names — so it renders exactly as designed.
Only the inline `onclick` strings became React handlers wired to the router.

`ConceptGraph.jsx` is a direct port of the prototype's `gxBuild`, including
the physics, which are load-bearing rather than decoration:

- `velocityDecay(0.62)` — heavy friction, so nodes glide instead of jittering
- `alphaDecay(0.05)` — the layout cools in about two seconds
- `alphaMin(0.008)` — it stops early rather than drifting
- `forceCollide(...).strength(1).iterations(2)` — firm separation, no vibrating overlap
- `forceManyBody().strength(-520).distanceMax(420)` — distant nodes stop tugging

When the simulation ends, every node is pinned, so the graph sits still.
Dragging frees only the dragged node's direct neighbours, then re-pins
everything where it landed. Arrow markers, edge labels, dashed weak links,
search dimming, the zoom/re-layout toolbar and the week scrubber are all
carried over.

The graph draws on a **dark card inside the cream panel**, as the design does
— that is why its strokes and labels are light while the rest of the app is
paper.

## 6. Palette

`src/styles/global.css` holds the palette: warm off-white surfaces, ink text,
forest-green accent, copper for in-progress states, Inter and JetBrains Mono.
Every component reads those tokens, so changing one flows through the app.

Converting from the old dark theme surfaced three light-on-light bugs that
would have been invisible rather than merely wrong: graph labels were white
text with a dark halo, the selection ring was white, and link marks were white
at 50% opacity. All draw in ink now. Two tokens were darkened (`--text-3`,
`--warn`) because they failed WCAG AA for small text on the lighter
background; every text token now clears AA.

## 5b. The quiz system — generated from your documents

Ported from the prototype, not simplified. The model:

- questions are grouped into **topics** the student picks; progress counts per
  topic, not per quiz
- points are easy 10 / medium 20 / hard 30
- a quiz passes at **two thirds correct**, and a re-attempt keeps the better score
- a topic is **mastered** once every quiz in it has been passed
- the **study plan** gives each day one topic and a checkpoint quiz
- a **difficulty filter** narrows the list to easy / medium / hard

The runner shows one question at a time with a progress bar, a difficulty and
points tag, per-answer right/wrong marking, and the explanation after checking.
The result screen reports correct, points and topic progress, and offers the
next quiz in the topic or another attempt.

**The questions come from what you uploaded.** `GET /api/quiz`
(`backend/quiz_service.py`) builds them from the concepts and relationships
extracted out of your documents. Upload something new and the questions change
with it. Before anything is processed the pane says so rather than showing
samples.

Two generation paths:

- **With `OPENAI_API_KEY` set**, an LLM writes the questions from the extracted
  concepts and relations. Better prose, better distractors.
- **Without a key**, questions are built straight from the graph's relations by
  template — "what does X *begins with*?" — with wrong options drawn from other
  real concepts in the same document. Lower quality, but still genuinely from
  your material, and it needs no key and costs nothing.

Either way nothing is invented: every answer is a concept that appears in your
graph, and a concept named in the question is never offered as a wrong option.

Topics are the weeks your uploads were assigned to. A week with fewer than four
concepts is skipped, because a fair four-option question needs at least that
many.

Attempts post to `/api/quiz/results`, which keeps the best score. They are held
in memory: the generated quiz changes whenever you upload, so persisting scores
would leave them pointing at questions that no longer exist.

## 6. Verifying

```bash
npx vite build                                   # production build
npm run test:ui                                  # UI smoke test, see below
cd backend && python -m pytest tests             # backend unit tests
```

`npm run test:ui` builds the app, serves it and drives it in headless Chromium
against a fake API (`tests/ui/mock-api.mjs`), so it needs no backend, Supabase
or LLM key. It checks the graph and sidebar counts, the concept inspector
(collapsing, connections, sources and the Open button), revision notes
(day labels, plan days, saving completion) and quiz generation (progress,
success and a server error). Run `npx playwright-core install chromium` once first,
or set `CHROMIUM_PATH` to an installed Chrome.

The old `verify_*.mjs` scripts were removed. They imported `api.js` through a
`data:` URL, which fails now that `api.js` imports the Supabase client, and
they called functions (`register`, `uploadDocument`, `fetchGraph`) that no
longer exist since the move to Supabase.

To score concept extraction against a hand checked list, see
`backend/eval/README.md`.

## 7. Real extraction

`dev_server.py` fakes extraction — chat answers are tagged `[STUB ANSWER]` and
the graph is a fixed sample, the same whatever you upload. For graphs built
from your own PDFs:

```bash
cd backend && source .venv/bin/activate
pip install -r requirements.txt     # spacy, chromadb, torch — several GB
echo "OPENAI_API_KEY=your_openai_api_key" > .env
uvicorn app.main:app --port 8000 --reload
```

`GEMINI_API_KEY` is optional and is needed only to read images and handwriting.

## 8. Known limits

- **Notes are generated from the graph**, not written prose — each core concept
  with its relationships. The design prototype had hand-written sample notes;
  those would have been fiction once real documents were loaded.
- **Quiz difficulty is inferred from connectivity**, not from the material's
  actual difficulty: a concept in many relationships is treated as easier to
  recall than one mentioned once. It is a proxy, and a rough one.
- **`Builder.jsx` and the graph components under `src/components/` are
  untouched.** They are your teammates' work and still reachable at `/builder`.
- **No React component tests.** The repo has no test setup for React; the API
  layer and the backend are covered, the components are not.
