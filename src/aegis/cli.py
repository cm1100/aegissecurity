"""Typer CLI — talks to the same orchestrators the FastAPI routes use.

There is no business logic in this file. Every command is a thin shell
around `aegis.ingest`, `aegis.normalize`, `aegis.correlate`,
`aegis.classify`, `aegis.risk`, `aegis.policy`. The CLI exists so a
reviewer can drive the pipeline without booting the API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aegis.classify import classify_persist
from aegis.correlate import correlate_persist
from aegis.ingest import ingest_many
from aegis.normalize import normalize_pending
from aegis.policy import policy_persist
from aegis.risk import BaselineRegistry, risk_persist
from aegis.storage import (
    AgentRow,
    CorrelationEdgeRow,
    FindingRow,
    get_session,
    init_db,
    reset_db,
)

app = typer.Typer(
    name="aegis",
    help="Aegis Discovery — convert raw signals into one AI agent record + risk + policy.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

_TIER_STYLE = {"HIGH": "bold red", "MEDIUM": "bold yellow", "LOW": "bold green"}
_FRESH_DB_URL_ENV = "AEGIS_DB_URL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_payloads_from_file(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # could be a single event, or a manifest wrapping {"events": [...]}
        if "events" in data and isinstance(data["events"], list):
            return data["events"]
        return [data]
    raise typer.BadParameter(f"{path}: expected dict or list at top level")


def _load_payloads(path: Optional[Path], directory: Optional[Path]) -> list[dict]:
    payloads: list[dict] = []
    if path is not None:
        payloads.extend(_load_payloads_from_file(path))
    if directory is not None:
        if not directory.exists():
            raise typer.BadParameter(f"directory not found: {directory}")
        files = sorted(directory.glob("**/*.json"))
        if not files:
            console.print(f"[yellow]No *.json files under {directory}[/]")
        for f in files:
            payloads.extend(_load_payloads_from_file(f))
    return payloads


def _baseline_registry() -> Optional[BaselineRegistry]:
    path = Path.cwd() / "baselines.yaml"
    if path.exists():
        return BaselineRegistry.from_yaml(path)
    return None


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

@app.command()
def init() -> None:
    """Create the database tables (idempotent)."""
    init_db()
    console.print("[green]✓[/] aegis.db initialized")


@app.command()
def reset() -> None:
    """Drop and recreate every table. Destructive."""
    reset_db()
    console.print("[yellow]![/] database reset (all data dropped)")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    path: Optional[Path] = typer.Argument(None, exists=False,
                                          help="A JSON file (one event or a list)"),
    directory: Optional[Path] = typer.Option(None, "--dir", "-d",
                                             help="Directory of *.json files to ingest"),
) -> None:
    """Ingest events from a JSON file or a directory."""
    if path is None and directory is None:
        console.print("[red]Error:[/] pass a file path or --dir")
        raise typer.Exit(2)
    init_db()
    payloads = _load_payloads(path, directory)
    if not payloads:
        console.print("[yellow]Nothing to ingest.[/]")
        return
    with get_session() as s:
        results = ingest_many(s, payloads)
    deduped = sum(1 for r in results if r.deduped)
    fresh = len(results) - deduped
    console.print(f"[green]✓[/] ingested {fresh} new, {deduped} deduped (total submitted {len(results)})")


# ---------------------------------------------------------------------------
# Pipeline stages — direct
# ---------------------------------------------------------------------------

@app.command()
def normalize() -> None:
    """Normalize any raw events still lacking a canonical row."""
    init_db()
    with get_session() as s:
        n = normalize_pending(s)
    console.print(f"[green]✓[/] normalized {n} new event(s)")


@app.command()
def correlate() -> None:
    """Re-run correlation (delete-then-rewrite the agent table)."""
    init_db()
    with get_session() as s:
        r = correlate_persist(s)
    console.print(f"[green]✓[/] correlated → {len(r.agents)} agent(s), "
                  f"{sum(len(v) for v in r.edges_by_agent.values())} merge edge(s)")


@app.command()
def classify() -> None:
    """Re-run the fingerprint classifier."""
    init_db()
    with get_session() as s:
        n = classify_persist(s)
    console.print(f"[green]✓[/] classified {n} agent(s)")


@app.command()
def risk() -> None:
    """Re-run risk rules + scoring."""
    init_db()
    with get_session() as s:
        r = risk_persist(s, baselines=_baseline_registry())
    tiers = r["tiers"]
    console.print(
        f"[green]✓[/] scored {r['agents_scored']} agent(s), "
        f"{r['findings']} finding(s) → "
        f"[red]{tiers['HIGH']} HIGH[/], "
        f"[yellow]{tiers['MEDIUM']} MEDIUM[/], "
        f"[green]{tiers['LOW']} LOW[/]"
    )


@app.command()
def policy() -> None:
    """Re-run the policy recommender."""
    init_db()
    with get_session() as s:
        r = policy_persist(s)
    console.print(f"[green]✓[/] recommended policies for {r['agents_recommended']}/{r['agents_total']} agent(s): {r['by_policy']}")


@app.command()
def run() -> None:
    """Run the full pipeline: normalize → correlate → classify → risk → policy."""
    init_db()
    with get_session() as s:
        normalized = normalize_pending(s)
        corr = correlate_persist(s)
        classified = classify_persist(s)
        risk_res = risk_persist(s, baselines=_baseline_registry())
        policy_res = policy_persist(s)
    body = Text()
    body.append(f"normalized   {normalized}\n", style="dim")
    body.append(f"agents       {len(corr.agents)}\n")
    body.append(f"classified   {classified}\n", style="dim")
    body.append(f"findings     {risk_res['findings']}\n")
    body.append(f"policies     {policy_res['agents_recommended']}/{policy_res['agents_total']}\n")
    console.print(Panel(body, title="[bold]Pipeline complete[/]", title_align="left",
                        border_style="green"))


# ---------------------------------------------------------------------------
# Read-side commands
# ---------------------------------------------------------------------------

@app.command("list")
def list_cmd() -> None:
    """Tabular view of all agents — sorted by risk score, highest first."""
    init_db()
    # Materialize everything needed inside the session — SQLAlchemy detaches
    # rows once the session closes, so reading attributes outside the block
    # raises DetachedInstanceError.
    with get_session() as s:
        snapshots = [
            {
                "agent_id": r.agent_id,
                "risk_score": r.risk_score,
                "risk_tier": r.risk_tier,
                "payload": dict(r.payload),
            }
            for r in s.query(AgentRow).all()
        ]

    if not snapshots:
        console.print(
            "[yellow]No agents yet.[/] Try [cyan]aegis ingest --dir samples/[/] "
            "then [cyan]aegis run[/], or just [cyan]aegis demo[/]."
        )
        return

    snapshots.sort(key=lambda r: (-(r["risk_score"] or 0), r["agent_id"]))

    table = Table(
        title=f"Discovered Agents ({len(snapshots)})",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )
    table.add_column("Agent ID", style="cyan", no_wrap=True)
    table.add_column("NHI", style="dim")
    table.add_column("Workload", style="dim")
    table.add_column("Framework")
    table.add_column("Tier", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Policy", style="magenta", no_wrap=True)
    table.add_column("Events", justify="right", style="dim")

    for r in snapshots:
        p = r["payload"]
        tier = r["risk_tier"] or "-"
        style = _TIER_STYLE.get(tier, "white")
        policy_obj = p.get("recommended_policy")
        policy_str = policy_obj["policy"] if policy_obj else "-"
        table.add_row(
            r["agent_id"],
            p.get("nhi_id") or "-",
            p.get("workload_id") or "-",
            p.get("framework") or "-",
            f"[{style}]{tier}[/{style}]",
            str(r["risk_score"]) if r["risk_score"] is not None else "-",
            policy_str,
            str(len(p.get("event_ids", []))),
        )
    console.print(table)


def _render_factors(factors: list[dict]) -> Text:
    t = Text()
    for f in factors:
        t.append(f"  {f['name']:<12} ")
        t.append(f"{f['value']:>3}/{f['max_value']:<3}  ", style="bold")
        t.append(f"{f['rationale']}\n", style="dim")
    return t


def _render_findings(findings: list[dict]) -> Text:
    t = Text()
    for p in findings:
        style = _TIER_STYLE.get(p["severity"], "white")
        t.append("  ● ", style=style)
        t.append(f"[{p['severity']}] ", style=style)
        t.append(f"{p['rule_name']}\n", style="bold")
        t.append(f"     {p['description']}\n", style="dim")
        for ev in p["evidence"][:4]:
            src = f"  (event {ev['source_event_id']})" if ev.get("source_event_id") else ""
            t.append(f"      - {ev['claim']}{src}\n")
    return t


def _render_policy(policy: dict) -> Text:
    t = Text()
    t.append("Policy: ", style="bold")
    t.append(f"{policy['policy']}", style="magenta")
    t.append(f"   (confidence {policy['confidence']})\n", style="dim")
    t.append("Evidence:\n")
    for ev in policy["evidence"]:
        src = f"  (event {ev['source_event_id']})" if ev.get("source_event_id") else ""
        t.append(f"  • {ev['claim']}{src}\n")
    if policy.get("alternatives_considered"):
        t.append(f"\nAlternatives considered: {', '.join(policy['alternatives_considered'])}\n",
                 style="dim")
    return t


def _render_edges(edges: list[dict]) -> Text:
    t = Text()
    for e in edges:
        t.append(f"  {e['event_a']} ↔ {e['event_b']}   ")
        t.append(f"{e['reason']}", style="dim")
        t.append(f"   conf={e['confidence']}\n", style="bold")
    return t


@app.command()
def show(agent_id: str) -> None:
    """Full detail for one agent: identity, risk breakdown, findings, policy, edges."""
    init_db()
    with get_session() as s:
        row = s.get(AgentRow, agent_id)
        if row is None:
            console.print(f"[red]✗[/] agent {agent_id!r} not found")
            raise typer.Exit(1)
        a = dict(row.payload)
        edges = [
            {"event_a": e.event_a, "event_b": e.event_b,
             "reason": e.reason, "confidence": e.confidence}
            for e in s.query(CorrelationEdgeRow).filter_by(agent_id=agent_id).all()
        ]
        findings_data = [
            dict(f.payload) for f in
            s.query(FindingRow).filter_by(agent_id=agent_id).all()
        ]

    # Identity panel
    id_text = Text()
    id_text.append(f"agent_id      {a['agent_id']}\n", style="cyan")
    id_text.append(f"nhi_id        {a.get('nhi_id') or '-'}\n", style="dim")
    id_text.append(f"workload      {a.get('workload_id') or '-'}\n", style="dim")
    id_text.append(f"repo          {a.get('repo') or '-'}\n", style="dim")
    id_text.append(f"host_ids      {a.get('host_ids') or []}\n", style="dim")
    id_text.append(f"framework     {a.get('framework') or '-'}")
    fw_conf = a.get("framework_confidence", 0)
    fw_rule = a.get("framework_rule") or "-"
    id_text.append(f"   (conf {fw_conf}, rule {fw_rule})\n", style="dim")
    id_text.append(f"provider      {a.get('provider') or '-'}\n", style="dim")
    id_text.append(f"data_classes  {a.get('data_classes') or []}\n")
    id_text.append(f"tools         {a.get('tools') or []}\n")
    id_text.append(f"destinations  {a.get('destinations') or []}\n")
    id_text.append(f"has_aegislib  {a.get('has_aegislib')}\n",
                   style=("red" if not a.get("has_aegislib") else "green"))
    console.print(Panel(id_text, title="Identity", title_align="left"))

    # Risk panel
    tier = a.get("risk_tier") or "-"
    score = a.get("risk_score") or 0
    style = _TIER_STYLE.get(tier, "white")
    risk_text = Text()
    risk_text.append("  tier  ", style="bold")
    risk_text.append(f"{tier}", style=style)
    risk_text.append("     score  ", style="bold")
    risk_text.append(f"{score}/100\n\n", style=style)
    risk_text.append(_render_factors(a.get("risk_factors", [])))
    console.print(Panel(risk_text, title="Risk", title_align="left", border_style=style))

    # Findings
    if findings_data:
        console.print(Panel(_render_findings(findings_data), title="Findings",
                            title_align="left", border_style="red"))

    # Policy
    if a.get("recommended_policy"):
        console.print(Panel(_render_policy(a["recommended_policy"]),
                            title="Recommended Policy",
                            title_align="left", border_style="magenta"))

    # Correlation edges
    if edges:
        console.print(Panel(_render_edges(edges), title="Correlation Audit Trail",
                            title_align="left", border_style="cyan"))


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

_BUILTIN_DEMO_EVENTS = [
    {"event_id": "rt-1", "source": "runtime",
     "timestamp": "2026-05-20T10:15:30Z",
     "host_id": "host-prod-01", "pid": 4242,
     "workload_id": "claims-processor",
     "destination": "api.anthropic.com",
     "tools_called": ["aurora_read", "external_llm_call"]},
    {"event_id": "nhi-1", "source": "nhi",
     "timestamp": "2026-05-20T10:00:00Z",
     "nhi_id": "role-aegis-shadow-agent",
     "workload_id": "claims-processor",
     "permissions": ["aurora:read", "bedrock:invoke", "s3:GetObject"]},
    {"event_id": "repo-1", "source": "repo",
     "timestamp": "2026-05-20T09:00:00Z",
     "repo": "claims-processor", "workload_id": "claims-processor",
     "imports": ["langchain", "anthropic"],
     "framework_hint": "langchain", "has_aegislib": False},
    {"event_id": "saas-1", "source": "saas",
     "timestamp": "2026-05-20T10:16:00Z",
     "nhi_id": "role-aegis-shadow-agent",
     "data_classes": ["PHI", "claims_data"],
     "action": "read", "resource": "claims.patient_records"},
]


@app.command()
def demo(
    samples_dir: Path = typer.Option(
        Path("samples"), "--samples", "-s",
        help="Directory of sample scenarios to ingest before running."
    ),
    keep_db: bool = typer.Option(
        False, "--keep-db",
        help="Skip the DB reset (default is to reset for a clean demo)."
    ),
) -> None:
    """Reset DB, load sample scenarios, run the full pipeline, print a dashboard.

    Falls back to a built-in shadow-PHI scenario if `samples/` is missing.
    """
    if not keep_db:
        reset_db()
    else:
        init_db()

    if samples_dir.exists() and any(samples_dir.glob("**/*.json")):
        console.print(f"[bold]Ingesting samples from[/] [cyan]{samples_dir}/[/]…")
        payloads = _load_payloads(None, samples_dir)
    else:
        console.print("[bold]Ingesting built-in shadow-PHI scenario[/] "
                      f"([dim]{samples_dir} not found[/])…")
        payloads = list(_BUILTIN_DEMO_EVENTS)

    with get_session() as s:
        results = ingest_many(s, payloads)
    fresh = sum(1 for r in results if not r.deduped)
    console.print(f"[green]✓[/] {fresh} new event(s) ingested\n")

    # Run pipeline
    with get_session() as s:
        normalize_pending(s)
        corr = correlate_persist(s)
        classify_persist(s)
        risk_res = risk_persist(s, baselines=_baseline_registry())
        policy_res = policy_persist(s)

    body = Text()
    body.append(f"agents     {len(corr.agents)}\n")
    body.append(f"findings   {risk_res['findings']}\n")
    body.append("HIGH/MED/LOW   ", style="bold")
    body.append(f"{risk_res['tiers']['HIGH']}", style="red")
    body.append(" / ")
    body.append(f"{risk_res['tiers']['MEDIUM']}", style="yellow")
    body.append(" / ")
    body.append(f"{risk_res['tiers']['LOW']}", style="green")
    body.append(f"\npolicies   {policy_res['by_policy']}\n")
    console.print(Panel(body, title="[bold]Discovery summary[/]",
                        title_align="left", border_style="cyan"))

    console.print()
    list_cmd()

    # Show full detail for the highest-risk agent
    with get_session() as s:
        top = (
            s.query(AgentRow)
            .order_by(AgentRow.risk_score.desc().nullslast())
            .first()
        )
        top_id = top.agent_id if top is not None else None
    if top_id is not None:
        console.print()
        console.print(f"[bold]Drill-down — highest-risk agent:[/] [cyan]{top_id}[/]")
        show(top_id)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

@app.command()
def graph(
    agent_id: str,
    json_out: bool = typer.Option(False, "--json", help="Emit raw node-link JSON instead of ASCII tree"),
) -> None:
    """Render the agent's projection: Agent → Identity → Tools → Data Classes → Policy."""
    init_db()
    from aegis.graph import graph_for_agent_id, render_ascii_tree

    g = graph_for_agent_id(agent_id)
    if g is None:
        console.print(f"[red]✗[/] agent {agent_id!r} not found")
        raise typer.Exit(1)
    if json_out:
        import json as _json
        console.print(_json.dumps(g, indent=2, default=str))
        return
    console.print(Panel(render_ascii_tree(g), title=f"Graph: {agent_id}",
                        title_align="left", border_style="cyan"))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    """Start the FastAPI server (uvicorn)."""
    import uvicorn

    console.print(f"[green]Serving Aegis Discovery on http://{host}:{port}/docs[/]")
    uvicorn.run("aegis.api.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
