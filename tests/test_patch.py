import json
import subprocess

from dagdoctor.github import GitError, branch_name, create_fix_branch
from dagdoctor.patch import check_patch, inspect_patch

GOOD_PATCH = json.loads(
    (__import__("pathlib").Path(__file__).parent.parent / "examples/llm/missing_column.json").read_text()
)["patch"]


def test_inspect_patch_counts_lines():
    files, added, removed, added_lines = inspect_patch(GOOD_PATCH)
    assert files == ["sql/shop_daily_sales.sql"]
    assert (added, removed) == (1, 1)
    assert "amount" in added_lines[0]


def test_good_patch_passes(example_repo):
    check = check_patch(GOOD_PATCH, example_repo)
    assert check.ok, check.reasons


def test_good_patch_passes_when_repo_root_is_a_subfolder(example_repo):
    nested = example_repo / "nested"
    nested.mkdir()
    for name in ("dags", "sql"):
        (example_repo / name).rename(nested / name)
    check = check_patch(GOOD_PATCH, nested)
    assert check.ok, check.reasons


def test_patch_that_does_not_apply_is_refused(example_repo):
    stale = GOOD_PATCH.replace("sum(amont)", "sum(price)")
    check = check_patch(stale, example_repo)
    assert not check.ok
    assert any("does not apply" in r for r in check.reasons)


def test_patch_outside_allowed_folders_is_refused(example_repo):
    patch = GOOD_PATCH.replace("sql/shop_daily_sales.sql", "setup.py")
    check = check_patch(patch, example_repo)
    assert any("outside allowed folders" in r for r in check.reasons)


def test_patch_with_path_traversal_is_refused(example_repo):
    patch = GOOD_PATCH.replace("sql/shop_daily_sales.sql", "sql/../../etc/hosts")
    check = check_patch(patch, example_repo)
    assert any("escapes" in r for r in check.reasons)


def test_patch_adding_a_secret_is_refused(example_repo):
    patch = GOOD_PATCH.replace(
        "+    sum(amount)        AS revenue", '+    sum(amount) AS revenue -- password="hunter22"'
    )
    check = check_patch(patch, example_repo)
    assert any("secret" in r for r in check.reasons)


def test_large_patch_is_refused(example_repo):
    check = check_patch(GOOD_PATCH, example_repo, max_changed_lines=1)
    assert any("limit" in r for r in check.reasons)


def test_empty_patch_is_refused(example_repo):
    assert not check_patch("   ", example_repo).ok


def test_create_fix_branch_commits_patch(example_repo):
    sha = create_fix_branch(example_repo, "dagdoctor/test", GOOD_PATCH, "fix: typo")
    assert len(sha) == 40
    sql = (example_repo / "sql/shop_daily_sales.sql").read_text()
    assert "sum(amount)" in sql
    branch = subprocess.run(
        ["git", "-C", str(example_repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "dagdoctor/test"


def test_create_fix_branch_refuses_dirty_tree(example_repo):
    (example_repo / "dags/new.py").write_text("x = 1\n")
    try:
        create_fix_branch(example_repo, "dagdoctor/test", GOOD_PATCH, "fix")
    except GitError as exc:
        assert "uncommitted" in str(exc)
    else:
        raise AssertionError("expected GitError")


def test_branch_name_is_safe():
    from datetime import datetime, timezone

    name = branch_name("shop daily/sales", "build:rollup", datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
    assert name == "dagdoctor/shop-daily-sales-build-rollup-202610090300"
