# Setup

You need two terminals: one for the backend and one for the frontend.

## What to install first

1. **Node.js 20** or newer
2. **Python 3.12** (newer versions break some packages)
3. **Git**
4. On macOS only, run `brew install libmagic`

You also need the team's Supabase project URL and publishable key, and an OpenAI API key.

## 1. Backend

Open a terminal in the `backend` folder.

**Windows (PowerShell)**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

**macOS**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a file called `.env` inside the `backend` folder:

```
OPENAI_API_KEY=your_openai_key
SUPABASE_URL=https://yourproject.supabase.co
SUPABASE_PUBLISHABLE_KEY=your_publishable_key
```

`GEMINI_API_KEY` is optional. It is only used to read images and handwriting.
See `backend/.env.example` for every setting.

Start the server:

```bash
uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000/docs to check it is running.

## 2. Frontend

Open a second terminal in the main `StudyGraph` folder (the one with `package.json`).

Create a file called `.env` in that folder:

```
VITE_SUPABASE_URL=https://yourproject.supabase.co
VITE_SUPABASE_PUBLISHABLE_KEY=your_publishable_key
VITE_API_URL=http://localhost:8000
```

Then run:

```bash
npm install
npm run dev
```

Open http://localhost:3000 and sign in.

## Common problems

| What you see | What to do |
|---|---|
| White screen | The frontend `.env` is missing or in the wrong folder. It must sit next to `package.json`. Stop `npm run dev` and start it again after any change. |
| "Cannot reach the backend" | Start the backend and check it uses port 8000. |
| "Supabase is not configured" | The backend `.env` is missing, or a value has a typo or an extra space. |
| Error about libmagic on macOS | Run `brew install libmagic`, then reinstall the requirements. |
| Package install fails | Check you are using Python 3.12 inside the `.venv`. |

Never commit either `.env` file. Git already ignores them.

## Checking your changes

| Command | Where | What it checks |
|---|---|---|
| `npm run build` | main folder | The app builds for Vercel |
| `npm run test:ui` | main folder | Opens the app in a hidden browser with fake data and clicks through the graph, notes and quiz |
| `python -m pytest tests` | `backend` | Backend unit tests |

Before the first `npm run test:ui`, run `npx playwright-core install chromium` once.
The `test_pruned_reactivation` tests need a real `OPENAI_API_KEY`.

## Measuring concept extraction

`backend/eval/concept_eval.py` compares the concepts StudyGraph found in a document with a list you checked by hand, and reports precision, recall and F1.
The steps are in `backend/eval/README.md`.

## Deploying

The frontend deploys to Vercel. Add the three `VITE_` settings above in the Vercel project, with `VITE_API_URL` pointing at the hosted backend.
On the backend, set `FRONTEND_URL` to the Vercel address so the browser is allowed to call it.

## New Supabase project

Only needed if you are not using the team's project. Run the files in `supabase/migrations` in number order in the Supabase SQL editor.
