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
| `backend/eval` | Tools that measure extraction and answer quality |
| `tests/ui` | Browser test for the frontend |

## Getting started

See [SETUP.md](SETUP.md) for step by step instructions on Windows and macOS.
