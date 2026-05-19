"""Typer CLI for internship-hunter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .drafter import DrafterError, draft_listing
from .filter import load_profile, rank_listings
from .schema import Listing
from .sources import REGISTRY, Source
from .storage import (
    connect,
    get_listing,
    get_listings,
    set_status,
    update_enrichment,
    upsert_listing,
)
from .tracker import TrackerError
from .tracker import export_csv as _export_csv
from .tracker import followups as _followups
from .tracker import mark as _mark

app = typer.Typer(add_completion=False, help="internship-hunter CLI")
console = Console()


@app.callback()
def _root() -> None:
    """internship-hunter: discover, filter, rank, and draft applications."""


async def _run_source(src: Source) -> list[Listing]:
    """Fail-soft fetch for a single source."""
    return await src.fetch_safe()


def _persist(listings: list[Listing]) -> tuple[int, int]:
    """Upsert listings; return (new, refreshed) counts."""
    conn = connect()
    try:
        new = refreshed = 0
        for listing in listings:
            if upsert_listing(conn, listing):
                new += 1
            else:
                refreshed += 1
        return new, refreshed
    finally:
        conn.close()


def _fetch_sources(
    names: list[str],
) -> tuple[list[tuple[str, int, int, int, bool]], list[tuple[str, str]]]:
    """Fetch+persist each source. Returns per-source rows and blocked list.

    Row = (name, fetched, new, refreshed, blocked).
    """
    rows: list[tuple[str, int, int, int, bool]] = []
    blocked: list[tuple[str, str]] = []
    for name in names:
        src = REGISTRY[name]()
        listings = asyncio.run(_run_source(src))
        new, refreshed = _persist(listings)
        rows.append((name, len(listings), new, refreshed, src.blocked))
        if src.blocked:
            blocked.append((name, src.manual_url or "(no url)"))
    return rows, blocked


def _render_fetch(rows, blocked) -> None:
    table = Table(title="fetch results")
    table.add_column("source")
    table.add_column("fetched", justify="right")
    table.add_column("new", justify="right", style="green")
    table.add_column("refreshed", justify="right", style="yellow")
    for name, fetched, new, refreshed, was_blocked in rows:
        label = "[red]blocked[/red]" if was_blocked else name
        table.add_row(label, str(fetched), str(new), str(refreshed))
    console.print(table)
    if blocked:
        lines = "\n".join(f"  • {n}: {url}" for n, url in blocked)
        console.print(
            Panel(
                f"These sources block automated access. Check them manually:\n{lines}",
                title="⚠ manual check needed",
                border_style="yellow",
            )
        )


@app.command()
def fetch(
    source: str = typer.Option(
        None,
        "--source",
        "-s",
        help="Fetch a single source by name (e.g. remoteok).",
    ),
    all_: bool = typer.Option(
        False, "--all", help="Fetch every registered source."
    ),
) -> None:
    """Fetch listings from one or all sources and store them (idempotent)."""
    if not source and not all_:
        raise typer.BadParameter("pass --source <name> or --all")
    if source and source not in REGISTRY:
        raise typer.BadParameter(
            f"unknown source '{source}'. Known: {', '.join(sorted(REGISTRY))}"
        )
    names = sorted(REGISTRY) if all_ else [source]
    rows, blocked = _fetch_sources(names)
    _render_fetch(rows, blocked)


def _score_and_persist(conn):
    """Score all stored listings, persist enrichment, skip hard-rejects."""
    profile = load_profile()
    listings = get_listings(conn)
    kept, rejected = rank_listings(listings, profile)
    for listing, res in kept:
        update_enrichment(
            conn,
            listing.source,
            listing.external_id,
            fit_score=res.fit_score,
            matched_keywords=res.matched_keywords,
        )
    for listing, _ in rejected:
        set_status(conn, listing.source, listing.external_id, "skipped", only_if="new")
    return kept, rejected


@app.command()
def rank(
    top: int = typer.Option(10, "--top", "-n", help="How many to show."),
    show_rejected: bool = typer.Option(
        False, "--show-rejected", help="Also print why listings were dropped."
    ),
) -> None:
    """Score stored listings against profile.yaml and print the best matches.

    Persists fit_score + matched_keywords for kept listings, and flips
    hard-rejected listings from 'new' to 'skipped' (never overriding a
    status you set yourself).
    """
    conn = connect()
    try:
        kept, rejected = _score_and_persist(conn)
    finally:
        conn.close()

    table = Table(title=f"top {top} matches  (kept {len(kept)} / rejected {len(rejected)})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("score", justify="right", style="green")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("title")
    table.add_column("company")
    table.add_column("loc/remote")
    table.add_column("matched", style="magenta")

    for i, (listing, res) in enumerate(kept[:top], 1):
        loc = "remote" if listing.remote else (listing.location or "—")
        table.add_row(
            str(i),
            f"{res.fit_score:.0f}",
            f"{listing.source}:{listing.external_id}",
            listing.title[:44],
            listing.company[:22],
            loc[:18],
            ", ".join(res.matched_keywords[:5]),
        )
    console.print(table)

    if show_rejected and rejected:
        rt = Table(title=f"rejected ({len(rejected)})")
        rt.add_column("title")
        rt.add_column("company")
        rt.add_column("reason", style="red")
        for listing, res in rejected[:50]:
            rt.add_row(listing.title[:48], listing.company[:24], res.reject_reason or "")
        console.print(rt)


@app.command()
def draft(
    listing_id: str = typer.Argument(
        ..., help="Listing id as shown by `hunter rank` — e.g. remoteok:12345"
    ),
    model: str = typer.Option(None, "--model", help="Override the Claude model."),
    lang: str = typer.Option(
        None, "--lang", help="Force draft language: 'en' or 'fr' (default: by source)."
    ),
) -> None:
    """Draft a tailored cover letter + application notes for one listing.

    Reads your CV (PDF), the listing, and profile.yaml, then writes
    outputs/<company>_<role>/{cover_letter,application_notes}.md.
    Language and CV default to the listing's source (French for Tunisian
    boards, English otherwise); override with --lang.
    Review before sending — nothing is submitted.
    """
    if lang is not None and lang not in ("en", "fr"):
        raise typer.BadParameter("--lang must be 'en' or 'fr'")
    if ":" not in listing_id:
        raise typer.BadParameter("listing id must be <source>:<external_id>")
    source, _, external_id = listing_id.partition(":")

    profile = load_profile()
    conn = connect()
    try:
        listing = get_listing(conn, source, external_id)
        if listing is None:
            raise typer.BadParameter(f"no listing {listing_id} (run `hunter rank`?)")
        try:
            result = draft_listing(listing, profile, model=model, lang=lang)
        except DrafterError as exc:
            console.print(f"[red]draft failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        if listing.status in ("new", "shortlisted", "skipped"):
            set_status(conn, source, external_id, "drafted")
    finally:
        conn.close()

    console.print(
        Panel(
            f"[green]Cover letter:[/green] {result.cover_letter_path}\n"
            f"[green]Notes:[/green] {result.notes_path}\n"
            f"[dim]cache: {result.cache_read_tokens} read / "
            f"{result.cache_write_tokens} written tokens[/dim]\n"
            f"[yellow]Review before sending — nothing was submitted.[/yellow]",
            title=f"drafted: {listing.company} — {listing.title}"[:70],
            border_style="green",
        )
    )


def _split_id(listing_id: str) -> tuple[str, str]:
    if ":" not in listing_id:
        raise typer.BadParameter("listing id must be <source>:<external_id>")
    source, _, external_id = listing_id.partition(":")
    return source, external_id


@app.command()
def mark(
    listing_id: str = typer.Argument(..., help="Listing id, e.g. remoteok:12345"),
    status: str = typer.Argument(..., help="new status, e.g. applied"),
    note: str = typer.Option(None, "--note", "-m", help="Append a dated note."),
) -> None:
    """Update a listing's status (e.g. `hunter mark remoteok:123 applied`)."""
    source, external_id = _split_id(listing_id)
    conn = connect()
    try:
        try:
            _mark(conn, source, external_id, status, note=note)
        except TrackerError as exc:
            raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()
    console.print(f"[green]✓[/green] {listing_id} → [bold]{status}[/bold]")


@app.command()
def followups(
    days: int = typer.Option(7, "--days", "-d", help="No-response window."),
) -> None:
    """List applications older than N days with no response yet."""
    conn = connect()
    try:
        pending = _followups(conn, days=days)
    finally:
        conn.close()

    if not pending:
        console.print(f"No applications older than {days}d awaiting a response. 🎉")
        return

    table = Table(title=f"follow up ({len(pending)} waiting > {days}d)")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("applied", no_wrap=True)
    table.add_column("age", justify="right")
    table.add_column("company")
    table.add_column("title")
    now = datetime.now(UTC)
    for x in pending:
        applied = x.applied_at
        if applied and applied.tzinfo is None:
            applied = applied.replace(tzinfo=UTC)
        age = (now - applied).days if applied else "?"
        table.add_row(
            f"{x.source}:{x.external_id}",
            applied.date().isoformat() if applied else "?",
            f"{age}d",
            x.company[:24],
            x.title[:40],
        )
    console.print(table)


@app.command()
def export(
    out: str = typer.Option(
        "outputs/applications.csv", "--out", "-o", help="CSV path."
    ),
    status: str = typer.Option(None, "--status", help="Only this status."),
    stdout: bool = typer.Option(False, "--stdout", help="Print CSV instead."),
) -> None:
    """Dump listings to CSV for pasting into a Google Sheet."""
    conn = connect()
    try:
        csv_text = _export_csv(conn, status=status)
    finally:
        conn.close()

    if stdout:
        # Raw stdout — never through rich (it wraps lines and would treat
        # '[' in notes as markup, corrupting the CSV).
        typer.echo(csv_text)
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text, encoding="utf-8")
    rows = max(csv_text.count("\n") - 1, 0)
    console.print(f"[green]✓[/green] wrote {rows} rows → {path}")


@app.command()
def daily(
    top: int = typer.Option(10, "--top", "-n", help="How many new listings to show."),
    no_input: bool = typer.Option(
        False,
        "--no-input",
        help="Skip the interactive draft prompt (for cron/timers).",
    ),
) -> None:
    """Full pipeline: fetch all → dedupe → score → show top new → draft.

    With --no-input it only fetches, scores and prints (safe for cron).
    """
    console.rule("[bold]1/3 fetch")
    rows, blocked = _fetch_sources(sorted(REGISTRY))
    _render_fetch(rows, blocked)

    console.rule("[bold]2/3 score")
    conn = connect()
    try:
        kept, rejected = _score_and_persist(conn)
        # Top NEW listings only (not skipped/drafted/applied/...).
        fresh = [(x, r) for x, r in kept if x.status == "new"][:top]
    finally:
        conn.close()
    console.print(
        f"scored: kept {len(kept)} / rejected {len(rejected)} — "
        f"showing {len(fresh)} new"
    )

    if not fresh:
        console.print("No new listings to review today. 🎉")
        return

    table = Table(title=f"top {len(fresh)} new")
    table.add_column("#", justify="right", style="dim")
    table.add_column("score", justify="right", style="green")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("title")
    table.add_column("company")
    for i, (x, res) in enumerate(fresh, 1):
        table.add_row(
            str(i),
            f"{res.fit_score:.0f}",
            f"{x.source}:{x.external_id}",
            x.title[:46],
            x.company[:24],
        )
    console.print(table)

    console.rule("[bold]3/3 draft")
    if no_input:
        console.print("--no-input set; skipping drafting. Run `hunter draft <id>`.")
        return

    answer = typer.prompt(
        "Draft which? comma-separated #, or 'none'", default="none"
    ).strip().lower()
    if answer in ("", "none", "n"):
        console.print("Nothing drafted.")
        return

    try:
        picks = sorted({int(p) for p in answer.replace(" ", "").split(",") if p})
    except ValueError as exc:
        raise typer.BadParameter(f"could not parse selection: {answer}") from exc

    profile = load_profile()
    conn = connect()
    try:
        for idx in picks:
            if not 1 <= idx <= len(fresh):
                console.print(f"[yellow]skip {idx}: out of range[/yellow]")
                continue
            listing, _ = fresh[idx - 1]
            try:
                result = draft_listing(listing, profile)
            except DrafterError as exc:
                console.print(f"[red]#{idx} draft failed:[/red] {exc}")
                continue
            if listing.status in ("new", "shortlisted", "skipped"):
                set_status(conn, listing.source, listing.external_id, "drafted")
            console.print(
                f"[green]✓ #{idx}[/green] {listing.company} — "
                f"{result.cover_letter_path}"
            )
    finally:
        conn.close()
    console.print("[yellow]Review drafts before sending — nothing submitted.[/yellow]")


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
