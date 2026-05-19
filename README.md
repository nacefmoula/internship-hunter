# internship-hunter

[![CI](https://github.com/nacefmoula/internship-hunter/actions/workflows/ci.yml/badge.svg)](https://github.com/nacefmoula/internship-hunter/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A semi-automated pipeline that **discovers, deduplicates, filters, ranks, and
drafts applications** for summer-2026 DevOps / Cloud / SRE internships
(Tunisia on-site, or remote) — from eight job sources into one reviewed
shortlist.

> Design principle: the system **surfaces and drafts**; the human reviews and
> clicks. It never auto-submits an application.

`Python 3.11+` · `async httpx` · `SQLite` · `pydantic` · `typer` · **82 tests, all offline · `ruff` clean**

---

## Why this exists

Internship hunting is a high-volume, low-signal grind: the same role is
cross-posted across boards, 90% of listings are irrelevant, and tailoring an
application to each one is slow. This tool turns that into a daily 30-second
review: it pulls every source, drops anything that isn't a real internship
match, ranks the rest against a YAML profile, and drafts a tailored cover
letter + interview prep — in English or French depending on the source.

## Pipeline

```
 fetch ─────────► dedupe ─────► score / filter ─────► rank ─────► draft
 8 sources        (source,      hard rules +          best-fit    LLM cover
 fail-soft,       external_id)  keyword scoring       first       letter + notes
 rate-limited     idempotent    internship-only                   (EN / FR)
```

One command runs the whole thing:

```bash
hunter daily          # fetch → dedupe → score → show top new → draft
hunter daily --no-input   # unattended (cron/systemd): fetch + score + print
```

## Engineering highlights

These are the parts I'd point a reviewer at:

- **Pluggable source registry.** Eight adapters (RemoteOK, Remotive, We Work
  Remotely, Himalayas, Hacker News "Who is hiring", Keejob, EmploiTunisie,
  TanitJobs, plus a curated GitHub internships feed) behind one abstract
  `Source` base. Adding a board is one file + a fixture.
- **Fail-soft by construction.** `fetch_safe()` guarantees a broken or
  bot-walled source logs and yields `[]` — one dead board never crashes the
  run. Per-query isolation means one bad search term can't sink a source.
- **Polite scraping.** Descriptive User-Agent, robots.txt honored, ≥2s/request
  rate limiting via an async lock, Cloudflare/anti-bot detection that turns a
  challenge page into an actionable "check this manually" link.
- **Idempotent persistence.** SQLite upsert keyed on `(source, external_id)`
  refreshes source-derived fields but **never clobbers workflow state you own**
  (status, applied date, notes) or filter enrichment.
- **Language-aware drafting.** French sources auto-select a French CV and
  produce a French cover letter; everything else English. Per-draft override.
- **Provider abstraction.** Drafting runs on free **Groq** or paid
  **Anthropic** (with prompt-cached CV/profile prefix), selected by env or
  inferred from whichever key is set — no code change to switch.
- **Tested offline.** 82 tests run with zero network: HTTP and parsing are
  separated so recorded fixtures drive every source, and the LLM clients are
  stubbed.

## Architecture

```
src/hunter/
├── cli.py          typer CLI (fetch / rank / draft / mark / followups / export / daily)
├── sources/        one adapter per board + an abstract Source (HTTP, rate-limit, robots)
├── schema.py       pydantic Listing model + status state machine
├── storage.py      idempotent SQLite upsert; preserves user-owned state
├── filter.py       keyword scoring + hard rejects (internship-only, location, staleness)
├── drafter.py      Groq/Anthropic cover letter + interview-notes generation
└── tracker.py      status transitions, follow-up detection, CSV export
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp profile.example.yaml profile.yaml   # then edit it
```

Edit `profile.yaml` — your name, email, target keywords, hard filters, and
the path to your CV. `cv_path` can be a single file or one per language:

```yaml
cv_path:
  en: ./my_cv_en.pdf
  fr: ./my_cv_fr.pdf
```

(Drop your own CV PDFs in the project root — they are gitignored and never
committed.)

### Drafting provider

`hunter draft` needs an LLM; **everything else needs none and is always free.**

- **Groq — free, no credit card.** Get a key at
  <https://console.groq.com/keys>:
  ```bash
  export GROQ_API_KEY=...        # auto-selected when it's the only key set
  ```
  Default model: `llama-3.3-70b-versatile`.
- **Anthropic — paid.** `export ANTHROPIC_API_KEY=...` (default
  `claude-sonnet-4-6`, prompt-cached CV/profile prefix).

Provider = `HUNTER_PROVIDER` if set, else inferred. Override the model with
`HUNTER_MODEL` or `--model`.

## Commands

```bash
hunter fetch --all              # pull every source (idempotent, fail-soft)
hunter rank --top 10            # score against profile.yaml, show best
hunter draft remoteok:12345     # tailored cover letter + interview notes
hunter mark remoteok:12345 applied --note "via company site"
hunter followups --days 7       # applied >7d ago, no response yet
hunter export --out apps.csv    # CSV for a tracking sheet
hunter daily                    # the whole pipeline, interactive drafting
```

## Run it on a schedule

Scheduled runs use `--no-input` (fetch + dedupe + score + print; no LLM):

```cron
0 9 * * 1-5 cd /path/to/internship-hunter && \
  ./.venv/bin/hunter daily --no-input >> ~/.internship-hunter.log 2>&1
```

<details>
<summary>systemd user timer (alternative to cron)</summary>

`~/.config/systemd/user/internship-hunter.service`:

```ini
[Unit]
Description=internship-hunter daily run
[Service]
Type=oneshot
WorkingDirectory=%h/internship-hunter
ExecStart=%h/internship-hunter/.venv/bin/hunter daily --no-input
```

`~/.config/systemd/user/internship-hunter.timer`:

```ini
[Unit]
Description=Run internship-hunter every weekday morning
[Timer]
OnCalendar=Mon-Fri 09:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now internship-hunter.timer
loginctl enable-linger "$USER"   # for runs while logged out
```
</details>

## Constraints & ethics

- Never auto-submits — surfaces and drafts only; a human sends every application.
- No LinkedIn scraping; robots.txt respected; ≥2s/request rate limiting.
- Idempotent: re-running never duplicates rows.
- Fail-soft: a broken or blocked source is logged and skipped, never fatal.
- Drafts are grounded only in the real CV/profile — the prompt forbids
  inventing experience.

## Tests

```bash
pytest            # 82 tests, fully offline (recorded fixtures + stubbed LLMs)
ruff check .
```

## License

MIT — personal project, free to read and reuse.
