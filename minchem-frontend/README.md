# MinChem frontend

React + Vite. No dependencies beyond React.

## Run

1. Backend (from the Python project root): `uv run uvicorn app.main:app --reload`
2. Frontend (this folder):
   ```
   npm install
   npm run dev
   ```
3. Open http://localhost:5173 (the backend only allows this origin and 127.0.0.1:5173).

The backend URL defaults to http://localhost:8000. To change it, copy `.env.example` to `.env`.

## Screens

- Inbox: category and role badges, tender chip, search, category filter.
- Tenders: list, PDF upload box, tender detail (offers + price chart, documents by type, emails).
- Reviews: conflicts waiting for a decision.
- New message: sends through /send-email.
