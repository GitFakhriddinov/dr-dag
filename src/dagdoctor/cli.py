"""Command line interface: ``dagdoctor diagnose|fix|review|stats``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .context import DEFAULT_PATH_MAP, parse_path_map
from .diagnose import diagnose
from .github import GitError, branch_name, create_fix_branch, open_draft_pr, push_branch
from .llm import AnthropicClient, Completer, ReplayClient
from .report import render_diagnosis, render_review
from .review import review
from .stats import append_history, load_history, render_stats, summarize


def _read_log(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(errors="replace")


def _client(args: argparse.Namespace) -> Completer | None:
    if args.replay_llm:
        return ReplayClient(Path(args.replay_llm))
    if args.no_llm:
        return None
    return AnthropicClient.from_env()


def _run_diagnosis(args: argparse.Namespace):
    path_map = parse_path_map(args.path_map) if args.path_map else DEFAULT_PATH_MAP
    report = diagnose(
        _read_log(args.log),
        repo_root=Path(args.repo) if args.repo else None,
        path_map=path_map,
        client=_client(args),
    )
    if args.history:
        append_history(Path(args.history), report.diagnosis)
    return report


def cmd_diagnose(args: argparse.Namespace) -> int:
    report = _run_diagnosis(args)
    if args.json:
        print(json.dumps(report.diagnosis.to_dict(), indent=2))
    else:
        print(render_diagnosis(report))
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    report = _run_diagnosis(args)
    diagnosis = report.diagnosis
    print(render_diagnosis(report))

    if not diagnosis.patch:
        print(f"No patch proposed (action: {diagnosis.action}).")
        return 0
    if diagnosis.action != "patch":
        print("The proposed patch did not pass the guardrails; nothing was changed.")
        return 3
    if not args.apply:
        print("Dry run: the patch passes every check. Re-run with --apply to commit it on a branch.")
        return 0

    repo = Path(args.repo)
    branch = branch_name(diagnosis.dag_id, diagnosis.task_id)
    title = f"fix({diagnosis.dag_id or 'dag'}): {diagnosis.fix_summary[:60]}"
    try:
        sha = create_fix_branch(repo, branch, diagnosis.patch, f"{title}\n\n{diagnosis.root_cause}")
        print(f"Committed {sha[:10]} on branch {branch}.")
        if args.open_pr:
            token = os.environ.get("GITHUB_TOKEN")
            if not (token and args.github_repo):
                print("--open-pr needs GITHUB_TOKEN and --github-repo owner/name.", file=sys.stderr)
                return 2
            push_branch(repo, branch)
            url = open_draft_pr(args.github_repo, branch, args.base, title, render_diagnosis(report), token)
            print(f"Opened draft PR: {url}")
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    findings = review(Path(args.path))
    if args.format == "json":
        print(json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        print(render_review(findings, args.path))
    levels = {"never": set(), "error": {"error"}, "warning": {"error", "warning"}}
    return 1 if any(f.severity in levels[args.fail_on] for f in findings) else 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(render_stats(summarize(load_history(Path(args.history)), args.month)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dagdoctor",
        description="Diagnose failing Airflow tasks and review DAG code.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_diagnosis_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("log", help="task log file, or - for stdin")
        p.add_argument("--repo", help="repository root holding the DAG and SQL files")
        p.add_argument(
            "--path-map",
            action="append",
            metavar="SRC=DST",
            help="map worker paths to repo paths, e.g. /opt/airflow/dags=dags",
        )
        p.add_argument("--no-llm", action="store_true", help="rules only, never call an LLM")
        p.add_argument("--replay-llm", metavar="FILE", help=argparse.SUPPRESS)
        p.add_argument("--history", metavar="FILE", help="append the result to a JSONL history")

    p = sub.add_parser("diagnose", help="explain why a task failed")
    add_diagnosis_args(p)
    p.add_argument("--json", action="store_true", help="print the diagnosis as JSON")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("fix", help="diagnose and, if safe, commit the fix on a branch")
    add_diagnosis_args(p)
    p.add_argument("--apply", action="store_true", help="commit the patch on a new branch")
    p.add_argument("--open-pr", action="store_true", help="push and open a draft PR")
    p.add_argument("--github-repo", metavar="OWNER/NAME")
    p.add_argument("--base", default="main", help="base branch for the PR (default: main)")
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("review", help="static review of DAG and SQL files")
    p.add_argument("path", help="folder or file to review")
    p.add_argument("--format", choices=("md", "json"), default="md")
    p.add_argument(
        "--fail-on",
        choices=("error", "warning", "never"),
        default="error",
        help="exit non-zero when findings of this severity exist (default: error)",
    )
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("stats", help="digest of past diagnoses")
    p.add_argument("history", help="JSONL history written by --history")
    p.add_argument("--month", metavar="YYYY-MM")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "open_pr", False) and not getattr(args, "apply", False):
        args.apply = True
    if getattr(args, "apply", False) and not args.repo:
        print("--apply needs --repo", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
