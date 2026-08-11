# StudyGraph —Frontend

This is the **frontend module** of StudyGraph: the landing page, login, and the
graph-processing workspace (upload, model selection, document table, and the
Generate / Preview controls).

It runs **completely standalone** — no backend required — because all backend
communication is stubbed with mock data. Your two teammates plug their modules
into the two clearly-marked integration points described below.

---

## ▶️ Run it

Requires **Node.js 18+**.

```bash
npm install
npm run dev
```

Open **http://localhost:3000**.

Sign in with anything (mock mode accepts any non-empty email/password), or use:

| Email | Password |
|---|---|
| `demo@graph.com` | `demo123` |

Then drag any file into the left panel, pick a model, and click **Generate
Graph** → **Preview Graph**. Everything works against mock data so you can see
the full flow without a backend.

---

## 🧩 The two integration points

This frontend was built so the other two modules slot in without touching the
UI code. Each teammate has exactly **one** place to work.

### ① Backend / Graph construction → `src/services/api.js`

This is the **only** file that talks to a backend. Every function currently
returns mock data. The backend teammate replaces each function body with a real
HTTP call, keeping the function signatures and return shapes identical.

Functions to implement:

| Function | Replaces | Returns |
|---|---|---|
| `login(email, password)` | `POST /api/auth/login` | `{ token, user }` |
| `register(...)` | `POST /api/auth/register` | `{ token, user }` |
| `listModels()` | `GET /api/models` | `{ providers, default }` |
| `listDocuments()` | `GET /api/documents` | `{ documents }` |
| `uploadDocument(file)` | `POST /api/upload` | `{ document }` |
| `deleteDocument(id)` | `DELETE /api/documents/:id` | `{ ok }` |
| `generateGraph(ids, provider, model)` | `POST /api/generate-graph` | `{ documents }` |
| `getGraph()` | `GET /api/graph` | `{ nodes, relationships, stats }` |
| `askQuestion(q)` | `POST /api/chat` | `{ answer, sources }` |

The file has an axios client ready to uncomment at the bottom, plus the exact
data shape documented above each function. Also uncomment the proxy in
`vite.config.js` so `/api/*` reaches the backend.

### ② Graph visualization → `src/components/GraphViewerPlaceholder.jsx`

This component receives the graph data (from `getGraph()`) in this shape:

```js
{
  nodes: [
    { id, label, color, properties: { name?, fileName?, mentions?, ... } }
  ],
  relationships: [
    { id, source, target, type, properties }
  ],
  stats: { total_nodes, total_relationships, label_counts, rel_counts }
}
```

The visualization teammate replaces the marked block (a big comment banner
inside the component) with the real renderer — D3.js, vis-network, neovis.js,
react-force-graph, etc. The component already fetches the data and handles the
modal shell; only the canvas needs to be built.

---

## 📁 Structure

```
studygraph-frontend/
├── index.html
├── package.json
├── vite.config.js
└── src/
    ├── main.jsx                 # routes
    ├── auth.jsx                 # login state + route guard
    ├── services/
    │   └── api.js               # ① INTEGRATION POINT — backend (mocked)
    ├── pages/
    │   ├── Landing.jsx/.css      # landing page  (frontend module)
    │   ├── Login.jsx/.css        # sign in       (frontend module)
    │   └── Builder.jsx/.css      # processing UI (frontend module)
    ├── components/
    │   ├── GraphViewerPlaceholder.jsx/.css   # ② INTEGRATION POINT — viz
    │   ├── ChatPanel.jsx/.css    # Q&A drawer (calls askQuestion)
    │   └── ModelPicker.jsx/.css  # model dropdown (calls listModels)
    └── styles/
        └── global.css           # theme / design tokens
```

---

## ✅ What works right now (mock mode)

- Landing page, login flow, route protection
- Drag-and-drop upload (files tracked in memory)
- Model picker populated from `listModels()`
- Generate Graph → marks docs Completed with sample counts
- Preview Graph → opens the viewer placeholder showing the sample graph data
- Ask StudyGraph → returns a mock answer

Everything above switches to real behavior automatically once the two
integration points are filled in — no other code needs to change.
