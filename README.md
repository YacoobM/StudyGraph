# StudyGraph

StudyGraph turns lecture slides and notes into a knowledge graph you can study from.
Upload your documents and StudyGraph finds the key concepts and how they connect.
From the graph you can write revision notes, take quizzes and ask questions about your material.

Built by group MCS03 for FIT3162 at Monash University.

## What it does

1. **Graph**: concepts and their relationships drawn as an interactive map, with a version for every upload.
2. **Inspector**: click a concept to see its connections, the documents and pages it came from, and how it changed week by week.
3. **Revision notes**: notes written from your documents, optionally split into a study plan over several days.
4. **Quiz**: questions generated from each graph version, with your progress saved.
5. **Ask StudyGraph**: chat that answers from your own documents and cites the source.

## How it is built

| Part | Technology |
|---|---|
| Frontend | React 18, Vite 5, D3 |
| Backend | Python 3.12, FastAPI |
| Database, sign in, file storage | Supabase |
| Language model | OpenAI |
| Hosting | Vercel (frontend) |

## Folders

| Folder | What is inside |
|---|---|
| `src` | The React app |
| `backend` | The FastAPI server |
| `supabase/migrations` | Database tables, run in order on a new Supabase project |
| `backend/eval` | RAGAS evaluation of answer quality |
| `tests/ui` | Browser test for the frontend |

## Frontend layout

Each component keeps its stylesheet in the same folder.

```
src/
  main.jsx              routes
  pages/
    Landing/            landing page and the animated graph demo
    Login/              sign in and sign up
    Workspace/          the main app: header, tabs, documents tab
    NotFound/
  components/
    graph/              concept graph, inspector panel
    notes/              revision notes
    quiz/               quiz tab and the question runner
    chat/               Ask StudyGraph
    library/            subject sidebar and uploads
  lib/                  auth, library state, notes data, helpers
  services/             API calls and the Supabase client
  styles/               theme tokens, global and shared styles
  assets/
```

## Getting started

See [SETUP.md](SETUP.md) for step by step instructions.

## System overview

See [OVERVIEW.md](OVERVIEW.md) for an explanation of how StudyGraph works.

## Future work

- **Automated quality checks:** The current setup for concept extraction is largely dependent on manual verification. In the future, this could be extended to create our own synthetic datasets/data as well as a comprehensive gold label dataset to allow automatic validation instead of double checking.
- **Student corrections:** Let students manually correct inaccurate concepts and links, and save those
  corrections.
- **Cross-subject maps:** Connect concepts across subjects, such as FIT1008, FIT2004, and FIT3155,
  in one map.
- **Class-wide graph:** Explore a shared graph for an entire class (collaboration features).
