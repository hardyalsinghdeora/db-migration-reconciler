import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reconciler.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("demo")
    result = runner.invoke(app, ["demo", "--dir", str(directory)])
    assert result.exit_code == 0, result.output
    return directory


def _urls(demo_dir: Path) -> list[str]:
    return [str(demo_dir / "source.db"), str(demo_dir / "target.db")]


def _flat(output: str) -> str:
    """Rich wraps at 80 columns in a non-terminal, so compare whitespace-insensitively."""
    return " ".join(output.split())


def test_demo_creates_both_files_and_lists_the_injected_drift(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "--dir", str(tmp_path / "d")])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "d" / "source.db").exists() and (tmp_path / "d" / "target.db").exists()
    assert "order_lines 3000 rows" in _flat(result.output)
    assert "deleted rows 17, 418 and 1203" in _flat(result.output)
    assert "type family drift" in _flat(result.output)


def test_schema_reports_the_type_drift(demo_dir: Path) -> None:
    result = runner.invoke(app, ["schema", *_urls(demo_dir)])

    assert result.exit_code == 1
    assert "is_active" in result.output and "BOOLEAN [bool]" in _flat(result.output)


def test_counts_exit_codes(demo_dir: Path) -> None:
    assert runner.invoke(app, ["counts", *_urls(demo_dir), "--table", "customers"]).exit_code == 0
    result = runner.invoke(app, ["counts", *_urls(demo_dir)])
    assert result.exit_code == 1
    assert "MISMATCH" in result.output


def test_checksum_lists_differing_rows_and_writes_json(demo_dir: Path, tmp_path: Path) -> None:
    report = tmp_path / "report.json"

    result = runner.invoke(
        app,
        ["checksum", *_urls(demo_dir), "--table", "order_lines", "--json", str(report)],
    )

    assert result.exit_code == 1
    assert "2500" in result.output and "extra" in result.output
    payload = json.loads(report.read_text())
    assert payload["summary"] == {
        "ok": False,
        "chunks": 3,
        "mismatched_chunks": 1,
        "source_rows": 3000,
        "target_rows": 3001,
        "missing": 0,
        "extra": 1,
        "changed": 1,
        "truncated": False,
    }
    assert payload["diffs"][0]["columns"] == {"quantity": {"source": 3, "target": 4}}


def test_backfill_dry_run_then_write_then_clean_checksum(demo_dir: Path, tmp_path: Path) -> None:
    urls = _urls(demo_dir)
    checkpoint = tmp_path / "checkpoint.json"

    dry = runner.invoke(app, ["backfill", *urls, "--table", "orders"])
    assert dry.exit_code == 1
    assert "DRY RUN" in dry.output and "would write 3" in _flat(dry.output)

    wrote = runner.invoke(
        app, ["backfill", *urls, "--table", "orders", "--write", "--checkpoint", str(checkpoint)]
    )
    assert wrote.exit_code == 0, wrote.output
    assert "wrote 3" in _flat(wrote.output)
    assert not checkpoint.exists()

    assert runner.invoke(app, ["checksum", *urls, "--table", "orders"]).exit_code == 0


def test_where_filter_and_verbose_logging(demo_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["checksum", *_urls(demo_dir), "--table", "customers", "--where", "country = 'DE'", "-v"],
    )

    assert result.exit_code in (0, 1)
    assert "WHERE customers.country = 'DE'" in _flat(result.output)


def test_usage_errors_exit_2(demo_dir: Path, tmp_path: Path) -> None:
    source, target = _urls(demo_dir)

    missing = runner.invoke(app, ["counts", source, str(tmp_path / "nope.db")])
    assert missing.exit_code == 2 and "does not exist" in _flat(missing.output)

    bad_where = runner.invoke(
        app, ["checksum", source, target, "--table", "orders", "--where", "status = 'x' OR 1=1"]
    )
    assert bad_where.exit_code == 2 and "expected AND" in _flat(bad_where.output)

    bad_table = runner.invoke(app, ["checksum", source, target, "--table", "ghost"])
    assert bad_table.exit_code == 2

    assert runner.invoke(app, []).exit_code == 2


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "reconciler 0.1.0" in result.output
