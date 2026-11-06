# Setup

These instructions use **Command Prompt (CMD)** on Windows. You will need one terminal for the
backend and another for the frontend.

## What to install first

1. **Node.js 20** or newer
2. **Python 3.12**
3. **Git**

You will also need the project's Supabase URL and publishable key, plus an OpenAI API key.

## Configure environment files

There are two `.env` files. Create each one by copying its corresponding example file:

- In the project root (next to `package.json`), copy `.env.example` to `.env`. Fill in
  `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
- In `backend/`, copy `backend/.env.example` to `backend/.env`. Fill in `OPENAI_API_KEY`,
  `SUPABASE_URL`, and `SUPABASE_PUBLISHABLE_KEY`.

Use the **publishable** Supabase key, not a secret/service-role key. If the app says **"Supabase is
not configured,"** the Supabase key is missing or empty in the relevant `.env` file. Add the
project's publishable key and URL to both files, and restart the frontend and backend after editing
them.

.env file is ignored by git, but do note to avoid revealing it publicly.

## 1. Backend

Open Command Prompt in the `backend` folder:

```cmd
cd path\to\StudyGraph\backend
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
fastapi run app/main.py --port 8000
```

Keep this terminal open while using the app. The backend is available at
http://localhost:8000, and its API docs are at http://localhost:8000/docs (via fastapi).

## 2. Frontend

Open a second Command Prompt in the project root (the folder containing `package.json`):

```cmd
cd path\to\StudyGraph (note: project root)
npm install
npm run dev
```

Open http://localhost:3000 (you will have to wait until the backend is running).

## Supabase migrations

If you are using the team's already-configured Supabase project, its migrations should already be
applied; do not run them again.

For a new Supabase project, open the Supabase SQL Editor and run the SQL files in `supabase/migrations/`
in numerical order, from `001` upward. These migrations create and update the database schema used by
the app. Make sure the `.env` files point to that same Supabase project. Do not apply migrations to
a shared or production project unless you have confirmed it is safe to do so.

## Build check

From the project root, run:

```cmd
npm run build
```

## Deploying

The frontend deploys to Vercel. Add `VITE_SUPABASE_URL` and
`VITE_SUPABASE_PUBLISHABLE_KEY` to the Vercel project settings. Set `VITE_API_URL` to the hosted
backend URL. On the backend, set `FRONTEND_URL` to the deployed frontend address so the browser is
allowed to call it.
