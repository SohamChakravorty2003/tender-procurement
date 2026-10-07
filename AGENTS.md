# AGENTS.md



Guidance for coding agents (OpenAI Codex) working in this repository. Where this file and the code disagree, the code wins. Items marked **[unverified]** or **[not built]** are not confirmed in the repository.



# Project Overview



MinChem AI Procurement & Supply-Chain system. MinChem (Minchem Impex) is a trader between customers and suppliers: a customer sends an enquiry, MinChem forwards it to one or more suppliers, suppliers quote, MinChem negotiates and confirms, then a sales contract, purchase orders and shipping documents follow.



The system:

- reads Gmail, classifies each email (category, role, tender id) with an LLM;

- reads PDFs (text layer plus OCR fallback), classifies them and extracts reference numbers (PO, contract, B/L, invoice, tender id);

- groups emails, documents and supplier offers into a **tender** (the primary grouping unit; "deal" in old notes means tender);

- extracts supplier offers into `bids`, marks duplicate offers and the offer whose price appears in the sales contract (`accepted`);

- shows all of it in a React UI.



The procurement team makes the final decision. Scoring and ranking of offers is **[not built]** (see Project-Specific Constraints).



# Technology Stack



- Backend: Python (version in `.python-version`), FastAPI, uv (`pyproject.toml`, `uv.lock`), pydantic v2.

- LLM: Groq via `langchain_groq.ChatGroq` and LangChain prompt/parser classes. Model `openai/gpt-oss-20b` by default.

- PDF: PyMuPDF (`pymupdf`) for text and page images; RapidOCR + onnxruntime for OCR.

- Gmail: `google-api-python-client`, `google-auth-oauthlib`, scope `gmail.readonly` only. Sending mail uses SMTP (`smtplib`, `email_service.py`), not the Gmail API.

- Storage: SQLite file `app/procurement.db`. No ORM.

- Frontend: React 18 + Vite. No router, no UI library, no chart library (custom SVG). Only React in dependencies.

- Environment: developer works on Windows PowerShell.



# Repository Structure



Only files an agent needs. Sample data folders are large and not code.



- `app/main.py`: FastAPI app and routes only.

- `app/models.py`: pydantic models and enums (email, bid, document, review).

- `app/classify_service.py`: email classification (`analyze_email`, `analyze_email_cached`), shared `llm` and `_parse_llm_json`.

- `app/gmail_service.py`: Gmail auth, `get_emails`, body and attachment-name extraction, `get_thread`.

- `app/email_service.py`: SMTP sending.

- `app/bid_service.py`: register pending Gmail bids, `extract_pending_bids(tender_id)`.

- `app/pdf_service.py`: PDF text extraction, per-page quality check, OCR, `ingest_pdf`.

- `app/document_service.py`: document classification and reference extraction (CLI).

- `app/link_service.py`: links documents into tenders (CLI).

- `app/thread_split.py`: splits an email-thread PDF into messages (no LLM).

- `app/offer_service.py`: extracts supplier offers from thread documents (CLI).

- `app/accept_service.py`: duplicate marking and accepted-offer marking (CLI, no LLM).

- `app/upload_service.py`: upload pipeline (extract, analyse, link) behind `POST /documents/upload`.

- `app/tender_store.py`: all SQL, schema, migrations.

- `app/eval_classifier.py`: classifier accuracy harness (writes nothing to the DB).

- `app/_cleanup.py`: one-off helper, hard-coded rows. Do not extend.

- `SQLite_ops/`: `viewDB.py`, `deleteDB.py`, still pointing at a removed old DB. Stale.

- `credentials/`: Gmail OAuth files. Never read, print or modify.

- `MINCHEM-DATA-RAW/`, `MINCHEM-DATA-TRAINING/`: sample PDFs and labelled test data (`pdf_labels.csv`, `test_emails.json`). Treat as read-only fixtures.

- `minchem-frontend/`: primary React/Vite frontend. Important files: `src/App.jsx`, `src/api.js`, `src/constants.js`, `src/index.css`, `src/main.jsx`, and `src/components/*`.

- `src/email_project/`: minimal Python package scaffold (`__init__.py`). The active application code lives in `app/`; do not move or duplicate application code into `src/email_project/` unless explicitly requested.

- `.env`: local backend secrets/configuration. Never read, print, modify, or commit it unless the user explicitly requests a specific configuration change.

- `minchem-frontend/.env.example`: safe reference for documented frontend environment-variable names; do not assume it contains secrets.

- `.gitignore`: repository ignore rules. Keep secrets, credentials, local databases, caches, virtual environments, `node_modules`, and build output out of version control.



# Architecture



Frontend (React, fetch) -> FastAPI (`main.py`) -> service modules -> `tender_store.py` -> SQLite. Services call Groq (LLM), Gmail API and PyMuPDF/RapidOCR.



- Routes handle HTTP only. Business logic is in services. All SQL is in `tender_store.py` (rule kept so a later move to PostgreSQL touches one file). `init_db()` runs on import of `tender_store`.

- Two input paths feed one tender model:

  - Gmail path: `/emails` -> classify -> tender id -> pending bid -> `extract_pending_bids`.

  - Document path: PDF (upload or CLI) -> `pdf_service` -> `document_service` -> `link_service` -> `offer_service` (thread documents) -> `accept_service`.



# Core Domain and Workflows



- **Tender**: key `tender_id`. Generated ids look like `TND-0001` (`id_origin='generated'`). Ids taken from documents or an enquiry code in a subject, such as `DOMSE/723/PASSE` (contains slashes), have `id_origin='extracted'`. Ids are normalised to upper case (`normalize_tender_id`).

- **Email classification** (`classify_service.py`): one LLM call returns `category` (quotation_request, follow_up, order_confirmation, shipment_update, document_collection, issue_resolution, other), `email_role` (tender_issue, vendor_bid, other), `tender_id`. Category is the topic, role is what the email does. The sender is passed to the prompt (senders at minchem.in are MinChem staff). Tender id is only an explicit labelled id or a bracketed code in the subject. Result is cached in `email_analysis`; a reply inherits the tender id of the earliest analysed email in its Gmail thread. Failures are stored as `status='failed'`, never guessed.

- **Gmail bids**: a `pending` row in `bids` is created only when `email_role == vendor_bid` and a tender id is known (`register_bid_if_applicable`, called from `get_emails`). Fields are filled later by `extract_pending_bids`. One bid per email.

- **Documents**: classified as purchase_order, sales_contract, email_thread, shipping_documents, quotation, tender_rfq, other, plus a `contains` list for bundles. References are stored in `tender_references`.

- **Linking** (`link_service.py`): union-find over shared normalised reference numbers (min length 5, must contain a digit, keys in more than 12 documents ignored). A group touching two tenders creates a `conflict` row in `link_reviews`; it is never merged automatically. Generated tenders are derived data and can be reset and rebuilt.

- **Thread offers**: `thread_split` splits a thread document; `offer_service` makes one small LLM call per non-MinChem message containing a digit, storing `vendor_offer` and `vendor_acceptance` rows in `bids` (`document_id`, `offer_seq`, `offered_at`). A document's offers are written only if every call succeeded.

- **Accepted offer** (`accept_service.py`): the offer whose `unit_price` appears in the tender's sales contract text. Duplicates are marked with `duplicate_of`, never deleted. The accepted offer shows the winning terms, not necessarily the cheapest. `accepted` and `duplicate_of` are derived data, replaced on each run.

- **Reviews**: `GET /reviews`, `POST /reviews/{id}/resolve` (`confirm` moves the document, its references and bids; `reject` changes nothing).



# Important Modules



Where to change what:

- Email prompt or categories: `classify_service.py` (`analysis_prompt`) and `models.py` (`EmailCategory`, `EmailRole`). The frontend `minchem-frontend/src/constants.js` mirrors these enums and must be updated with them.

- Gmail bid extraction fields or prompt: `bid_service.py` (`extraction_prompt`) and `models.BidExtraction`.

- Thread-offer extraction: `offer_service.py` and `models.ThreadOffer`.

- Document types or reference types: `models.DocumentType`, `ReferenceType`, `document_service.py`.

- Linking rules: `link_service.py`. Dedupe and accepted rules: `accept_service.py`.

- New queries or columns: `tender_store.py` only (add a migration step in `_migrate_existing_db`).

- New endpoint: `main.py` (thin) plus a service or store function.

- Shared LLM helpers (`llm`, `_parse_llm_json`) live in `classify_service.py`; `bid_service.py` imports them.



# API Conventions



Routes in `app/main.py`; CORS allows only `http://localhost:5173` and `http://127.0.0.1:5173`.



- `POST /send-email` body `{recipient, subject, body}`

- `GET /emails` newest 50 INBOX messages, newest first. Side effects: classifies new emails (LLM, slow), registers pending bids.

- `GET /threads/{thread_id}` full Gmail thread, both directions, oldest first, quoted history removed; read-only. Added during the demo work; not in older copies of `main.py`.

- `GET /tenders` summary rows (counts of documents, emails, bids, accepted).

- `GET /tenders/{tender_id:path}` full tender view. The `:path` converter is required because ids contain slashes. **This route must stay last in `main.py`.**

- `POST /documents/upload` multipart `file`, PDF only, max 25 MB; runs the whole upload pipeline.

- `GET /reviews[?status=all]`, `POST /reviews/{review_id}/resolve` body `{action: confirm|reject, tender_id}`.



Errors: `HTTPException` with a string `detail` (400 invalid input, 404 not found, 413 too large, 502 Gmail error). The frontend reads `detail`. Request/response models are pydantic classes in `models.py`; several tender endpoints return plain dicts.



# Database / Persistence



SQLite `app/procurement.db`, foreign keys on (`PRAGMA foreign_keys = ON` in `get_connection`). Tables:

- `tenders` (tender_id PK, id_origin, requirements_json unused)

- `email_analysis` (cache keyed by email_id; thread_id, category, email_role, tender_id, status)

- `bids` (email_id or document_id required; extracted_json, extraction_status pending/extracted/failed, accepted, duplicate_of, offered_at, offer_seq; partial unique index on document_id + offer_seq)

- `documents` (sha256 UNIQUE for dedupe, full_text, page_quality_json, document_type, contains_json, analysis_json, tender_id, link_method)

- `tender_references`, `link_reviews`.



Rules:

- Schema changes go through `_migrate_existing_db` in `tender_store.py`; it runs on every import and must stay idempotent. SQLite cannot drop NOT NULL, so such changes need a table rebuild.

- Any script that opens `procurement.db` with plain `sqlite3` must `import app.tender_store` first, or the migration has not run.

- Back up `procurement.db` before schema changes or bulk deletes. Do not commit it.

- Decision recorded: stay on SQLite; if it outgrows it, move to PostgreSQL, not MongoDB. User confirmation of this is still open.



# AI / LLM Usage



- Used for: email classification, Gmail bid extraction, document classification and reference extraction, per-message offer extraction.

- All output is parsed from JSON and validated with pydantic. Invalid output returns `None` and the record is marked `failed` (retried on the next run). Never substitute a guessed default.

- Extraction records facts only: `null` means not stated; never calculate values the vendor did not state; vendor wording is kept in `price_text` and `delivery_text`.

- Classification reads subject plus the first 2000 body characters; bid extraction reads up to 6000.

- `document_service` uses its own client (`doc_llm`) with model from env `DOC_MODEL` (default `openai/gpt-oss-20b`), `max_tokens=4096`, `reasoning_effort="low"`.

- LLM usage may be rate-limited or quota-limited. Do NOT run `--redo` over all documents unless explicitly requested; prefer targeted `--ids` runs. Do not change models solely to bypass a quota without user approval.

- A short `time.sleep(2)` follows fresh LLM calls to respect rate limits; keep it.

- Classifier prompt is frozen for the demo (91% category, 95% role on 22 labelled emails, 100% on the 13 clear ones; labels are not confirmed by MinChem). Do not tune it against those 22 emails.

- LLMs must not decide final rankings or scores.



# Frontend



- `minchem-frontend/src/App.jsx` holds a `view` state (inbox, compose, tenders, tender, reviews) instead of a router, polls `/emails` every 30 s (never overlapping calls), and keeps the pending-review count.

- Components: `Sidebar`, `InboxView` (list, filters, detail, conversation toggle), `ThreadView`, `ComposeView`, `TendersView`, `TenderDetail`, `PriceChart` (SVG, one line per vendor email address), `UploadBox`, `ReviewsView`, `Badges`.

- All HTTP goes through `minchem-frontend/src/api.js` (`request` wrapper, base URL `VITE_API_URL`, default `http://localhost:8000`). Tender ids go into URLs with `encodeURI` (keeps slashes).

- `minchem-frontend/src/constants.js` mirrors backend enums and holds label helpers; keep in sync with `models.py`.

- Styling: `minchem-frontend/src/index.css` with CSS variables and a dark-mode media query. Reuse existing classes (`card`, `table`, `badge`, `chip`, `btn`, `notice`) before adding new ones.

- Chart x-axis is offer order, not date: `offered_at` is stored as written in several formats.



# Development Commands



Backend (project root, PowerShell, uv):

- Start: `uv run uvicorn app.main:app --reload`

- Classifier accuracy (no DB writes): `uv run python -m app.eval_classifier` (options `--files`, `--ids`)

- Documents: `uv run python -m app.document_service [--redo | --ids N ...]`

- Link: `uv run python -m app.link_service` (dry run), `--apply`, `--reset-generated`, `--assign <doc_id> <tender_id>`, `--check "<root>" [--level N]`

- Thread offers: `uv run python -m app.offer_service [--ids N ...] [--redo] [--dry]`

- Accepted offers: `uv run python -m app.accept_service [--tender ID] [--apply]`

- Split a thread: `uv run python -m app.thread_split --id N`

- One PDF: `uv run python -m app.pdf_service "path\\to\\file.pdf" [--save]`

- Gmail bid extraction (no endpoint yet): `uv run python -c "from app.bid_service import extract_pending_bids; print(extract_pending_bids('TND-0001'))"`

- Python dependency install: managed by uv; exact command not documented **[unverified]** (`uv sync` is the usual one).



Frontend (`minchem-frontend/`): run `npm install`, `npm run dev` (port 5173, required by the current CORS configuration), and `npm run build` from that directory.



Tests and linting: no automated test suite or linter is configured or documented. Verify with the CLI commands above, `eval_classifier`, and `uv run python -c` checks. On Windows read JSON with `encoding='utf-8'`.



# Coding Conventions



- Python: type hints, `X | None` unions, StrEnum for enums, pydantic v2 models, small functions with a docstring stating behaviour and failure mode. Services return plain dicts or pydantic objects; routes translate `ValueError` to 400 and `LookupError` to 404.

- LLM calls: prompt template -> `llm` -> `StrOutputParser` -> `_parse_llm_json` -> pydantic validation; on failure print a short message and return `None`.

- Tolerant validators (`field_validator(mode="before")`) are used where models return a list instead of a string or a bare list instead of an object; follow that pattern instead of loosening the whole model.

- Idempotence: inserts use `INSERT OR IGNORE` with unique keys; CLIs are safe to re-run.

- Derived data (`accepted`, `duplicate_of`, generated tenders) is recomputed, not edited by hand.

- Frontend: function components with hooks, plain CSS, no new dependencies. Show backend `detail` text to the user.



# Rules for Coding Agents

1. Inspect existing implementations before changing anything; trace the flow end to end.

2. Prefer modifying existing modules over creating parallel ones; reuse existing models, helpers and CSS classes.

3. Make the smallest change that satisfies the request; no unrelated refactors or renames.

4. Preserve existing API contracts. If a response shape or schema changes, find and update every consumer (backend callers, `minchem-frontend/src/api.js`, components, `minchem-frontend/src/constants.js`).

5. Do not invent functions, routes, columns, environment variables or configuration; check the repository first.

6. Keep SQL in `tender_store.py` and HTTP in `main.py`. Keep `/tenders/{tender_id:path}` the last route.

7. Never read, print, hard-code, log, expose, or modify secret values in `.env`, `credentials/client_secret.json`, or `credentials/token.json` unless the user explicitly requests a narrowly scoped configuration action. Prefer `.env.example` files and code references when identifying environment-variable names. Never commit secrets, credential files, or `app/procurement.db`.

8. Respect `.gitignore`; do not force-add ignored secrets, credentials, databases, caches, virtual environments, `node_modules`, or build artifacts.

9. Do not run bulk LLM jobs (`--redo`, full re-analysis, evaluation over all emails) unless explicitly requested; prefer targeted runs to avoid unnecessary quota and cost.

10. Do not delete or rewrite rows in `app/procurement.db` unless asked; state what will change and back up the database first.

11. Remove code that a change fully supersedes and report what was removed and why; do not remove code still used elsewhere.

12. Validate imports and run the most relevant check after a change. Do not claim a check passed unless it was actually executed successfully; list checks that could not be run.

13. Report every file changed and why, plus assumptions and behaviour changes.

# Change Workflow



Before: read the relevant files, trace the flow, list callers, choose the smallest change.

During: follow existing structure, keep edits focused, reuse abstractions.

After: review the diff; check imports and interfaces; run the relevant command (CLI, `uv run python -c` check, `npm run build` for frontend); report files changed, behaviour changes, assumptions, and anything not run.



# Project-Specific Constraints



- Tender id is the primary key for grouping. Documents and emails with no explicit id are linked through shared reference numbers; a match with two tenders always goes to review, never auto-merged.

- Never take a product or grade name (e.g. RKCB 86), a quantity, a customer name or a contract or PO number as a tender id in email classification.

- Keep vendor-provided facts, extracted values, LLM interpretation, calculated scores and final ranking clearly separate. Missing information stays `null`/explicit; it is not silently scored as failure or zero.

- Scoring and ranking **[not built]**: when built, the LLM may only pick a rubric level plus an evidence quote; code applies weights and computes scores deterministically. Confirmed (placeholder) criteria: price 30 and required, delivery 20, compliance 20, quality 10, warranty 10, experience 5, payment 5; missing criteria are skipped and weights rescaled with a completeness indicator; no knock-outs; a bid without `unit_price` is "incomplete, not ranked". `evaluation_config.py` was proposed but is not confirmed to exist. Weights need confirmation from MinChem.

- Price comparability: keep `price_terms` (FOB/CIF/CFR) and ports as stated; never normalise them silently or compare across bases without a flag. The latest offer from a vendor is not the cheapest; the accepted offer comes from the contract.

- Known gaps (do not assume they work): Gmail attachments are read by name only (no download or text); `vendor_acceptance` does not exist in the Gmail bid path, and the accepted-offer step ignores Gmail bids (`document_id IS NOT NULL` only); a Gmail email listing several prices gets `unit_price` null with the wording in `price_text`; Gmail bids can duplicate offers extracted from a PDF thread of the same deal; `offered_at` is not parsed to real dates (`parse_day` in `accept_service.py` exists); vendors are not grouped by email address; the review `confirm` path is untested end to end; a brand-new PDF through `/documents/upload` was only partly verified.

- Open business decisions (MinChem): a `negotiation` category (currently counter-offers sit under `quotation_request`), confirmation of the labelled test emails, the SQLite decision.

- Gmail demo conventions: account A (connected to the backend) plays MinChem and its sent mail never appears in INBOX, so it is never analysed; keep the tender code in brackets in every subject of a demo thread (for example `(DEMO-0001)`); demo tenders `DEMO-0001` and `DEMO-0002` are test data, `TND-*` and `DOMSE/723/PASSE` come from the sample PDFs.
