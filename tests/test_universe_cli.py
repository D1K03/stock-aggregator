from datetime import date

import pytest

from screener.universe.cli import main
from screener.universe.reconcile import Plan


def test_refresh_writes_into_the_data_directory(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "screener.universe.cli.refresh",
        lambda out_dir, **kw: seen.update(out_dir=out_dir) or _report(),
    )
    assert main(["refresh", "--data-dir", str(tmp_path)]) == 0
    assert seen["out_dir"] == tmp_path


def _report():
    from screener.universe.refresh import RefreshReport

    return RefreshReport(written=2, unresolved=0)


def test_load_passes_dry_run_and_force_through(monkeypatch, tmp_path):
    seen = {}

    def fake_load(path, *, as_of, dry_run, force):
        seen.update(path=path, as_of=as_of, dry_run=dry_run, force=force)
        return Plan()

    monkeypatch.setattr("screener.universe.cli.load", fake_load)
    (tmp_path / "universe.csv").write_text("")
    assert main(["load", "--data-dir", str(tmp_path), "--dry-run", "--force"]) == 0
    assert seen["dry_run"] is True and seen["force"] is True


def test_as_of_defaults_to_today(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "screener.universe.cli.load",
        lambda path, **kw: seen.update(kw) or Plan(),
    )
    (tmp_path / "universe.csv").write_text("")
    main(["load", "--data-dir", str(tmp_path)])
    assert seen["as_of"] == date.today()


def test_a_refused_load_exits_non_zero(monkeypatch, tmp_path):
    from screener.universe.load import DepartureCeilingExceeded

    def refuse(path, **kw):
        raise DepartureCeilingExceeded("too many departures")

    monkeypatch.setattr("screener.universe.cli.load", refuse)
    (tmp_path / "universe.csv").write_text("")
    assert main(["load", "--data-dir", str(tmp_path)]) == 1


def test_an_unknown_command_is_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        main(["demolish"])
