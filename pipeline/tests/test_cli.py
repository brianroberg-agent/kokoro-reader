import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from narration_pipeline import __main__ as cli
from narration_pipeline.__main__ import main, make_deps
from narration_pipeline.discovery import Candidate
from narration_pipeline.night import RunInProgress
from narration_pipeline.record import Record
from night_fakes import NY, START, Script, World, restore_record_clock, source

REQUIRED = ["NARRATION_URL", "NARRATION_RUNNER_TOKEN", "AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_API_KEY",
            "NTFY_TOPIC_URL", "NTFY_TOKEN"]


@pytest.fixture(autouse=True)
def _clock():
    yield
    restore_record_clock()


@pytest.fixture
def env(tmp_path):
    (tmp_path / "sources.toml").write_text(Path(__file__).parent.parent.joinpath("sources.toml").read_text())
    shutil.copytree(Path(__file__).parent.parent / "covers", tmp_path / "covers")
    e = {k: f"value-of-{k}" for k in REQUIRED}
    e.update(PIPELINE_DATA=str(tmp_path / "data"), PIPELINE_CONFIG=str(tmp_path / "sources.toml"),
             HANDOFF_DIR=str(tmp_path / "handoff"), ARTICLE_ASSISTANT_DIR=str(tmp_path / "aa"))
    (tmp_path / "data").mkdir()
    return e


@pytest.mark.parametrize("name", REQUIRED)
def test_missing_env_names_the_variable(env, name):
    del env[name]
    with pytest.raises(SystemExit) as e:
        make_deps(env)
    assert name in str(e.value)
    assert not any(v in str(e.value) for k, v in env.items() if k in REQUIRED)  # no secret value


def test_make_deps_builds_the_runner(env, tmp_path):
    deps = make_deps(env)
    assert deps.record_dir == tmp_path / "data" and (tmp_path / "data" / "record.sqlite").exists()
    assert deps.handoff_dir == tmp_path / "handoff"
    assert deps.now().tzinfo is not None and deps.now().tzinfo.key == deps.settings.tz
    assert deps.sources and deps.settings.cap == 5


def test_make_deps_defaults(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "Record", lambda p: seen.setdefault("record", p))
    for k in ("PIPELINE_DATA", "HANDOFF_DIR", "ARTICLE_ASSISTANT_DIR"):
        del env[k]
    deps = make_deps(env)
    assert seen["record"] == Path("/data/record.sqlite") and deps.handoff_dir == Path("/handoff")
    assert deps.record_dir == Path("/data")


def test_run_dry_run_prints_a_report_and_exits_zero(tmp_path, capsys):
    world = World(tmp_path)
    world.add_article(1)
    assert main(["run", "--dry-run"], env={}, deps_factory=lambda env: world.deps()) == 0
    out = capsys.readouterr().out
    assert "Article 1" in out and "publish" in out
    assert world.abs.episodes == [] and world.push.sent == []


def test_run_only_a_url(tmp_path, capsys):
    world = World(tmp_path)
    url = world.add_article(1)
    world.add_article(2)
    assert main(["run", "--only", url], env={}, deps_factory=lambda env: world.deps()) == 0
    out = capsys.readouterr().out
    assert "Article 1" in out and "Article 2" not in out
    assert "published" in out


def test_status_prints_counts_and_recent_rows(env, capsys):
    rec = Record(Path(env["PIPELINE_DATA"]) / "record.sqlite")
    rec.add_discovered([Candidate(f"https://example.test/tna/s{i}", f"Status {i}", date(2026, 10, 1), 100 + i, None)
                        for i in range(12)], source())
    assert main(["status"], env=env) == 0
    out = capsys.readouterr().out
    assert "queued: 12" in out
    assert out.count("Status ") == 10


def test_status_without_a_record_says_so_and_creates_nothing(env, capsys):
    path = Path(env["PIPELINE_DATA"]) / "record.sqlite"
    assert main(["status"], env=env) == 0
    assert f"no record yet at {path}" in capsys.readouterr().out
    assert not path.exists()


def test_status_opens_read_only(env, capsys):
    path = Path(env["PIPELINE_DATA"]) / "record.sqlite"
    rec = Record(path)  # held open, so nothing else touches the file between the two stats
    before = path.stat().st_mtime_ns
    assert main(["status"], env=env) == 0
    assert path.stat().st_mtime_ns == before
    del rec


def test_run_in_progress_exits_nonzero_with_the_message(tmp_path):
    world = World(tmp_path)

    def busy(deps, **kw):
        raise RunInProgress("whatever the wording")
    import narration_pipeline.__main__ as m
    orig = m.run_night
    m.run_night = busy
    try:
        with pytest.raises(SystemExit) as e:
            main(["run"], env={}, deps_factory=lambda env: world.deps())
    finally:
        m.run_night = orig
    assert isinstance(e.value.code, str) and "another run is in progress" in e.value.code


@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_an_only_run_that_cannot_run_exits_non_zero_with_its_reason(tmp_path, dry):
    world = World(tmp_path)
    url = world.add_article(1)
    with pytest.raises(SystemExit) as e:
        main(["run", *dry, "--only", "https://example.test/nope"], env={}, deps_factory=lambda env: world.deps())
    assert e.value.code == "not discovered: https://example.test/nope"  # a message: exit status 1
    world.narration.down_until = datetime(2026, 11, 4, 0, 0, tzinfo=NY)
    with pytest.raises(SystemExit) as e:
        main(["run", *dry, "--only", url], env={}, deps_factory=lambda env: world.deps())
    assert e.value.code == "the narration server isn't reachable" and world.clock.sleeps == []


def test_other_run_failures_are_not_swallowed(tmp_path, monkeypatch):
    world = World(tmp_path)

    def broken(deps, **kw):
        raise ValueError("a bug")
    monkeypatch.setattr(cli, "run_night", broken)
    with pytest.raises(ValueError, match="a bug"):
        main(["run"], env={}, deps_factory=lambda env: world.deps())


def test_http_loggers_are_quiet(env, capsys):
    import logging
    main(["status"], env=env)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpx2").level == logging.WARNING


def test_abs_check_needs_only_the_abs_variables(monkeypatch, capsys, tmp_path):
    seen = {}

    class FakeAbs:
        def __init__(self, url, key):
            seen["args"] = (url, key)
    monkeypatch.setattr(cli, "Audiobookshelf", FakeAbs)
    monkeypatch.setattr(cli, "abs_check", lambda abs, podcast, mp3: seen.update(p=podcast, m=mp3) or "ep-1")
    env = {"AUDIOBOOKSHELF_URL": "http://abs.test", "AUDIOBOOKSHELF_API_KEY": "k"}
    assert main(["abs-check", "--podcast", "P", "--mp3", str(tmp_path / "x.mp3")], env=env) == 0
    assert capsys.readouterr().out.strip() == "ep-1"
    assert seen["args"] == ("http://abs.test", "k") and seen["p"] == "P" and seen["m"] == tmp_path / "x.mp3"


def test_abs_check_missing_key_names_it_not_its_value(capsys):
    with pytest.raises(SystemExit) as e:
        main(["abs-check", "--podcast", "P", "--mp3", "x"], env={"AUDIOBOOKSHELF_URL": "http://secret.test"})
    assert "AUDIOBOOKSHELF_API_KEY" in str(e.value) and "secret.test" not in str(e.value)


def test_schedule_hands_the_loop_its_clock_and_push(tmp_path, monkeypatch):
    world = World(tmp_path)
    got = {}
    monkeypatch.setattr(cli.schedule, "schedule_loop", lambda make, **kw: got.update(deps=make(), **kw))
    assert main(["schedule"], env={}, deps_factory=lambda env: world.deps()) == 0
    assert got["now"] == world.clock.now and got["push"] is world.push and got["sleep"] == world.clock.sleep
    assert got["nights"] is None  # without --nights the loop runs until the process is stopped


class Overslept(Exception):
    """The fake clock ran past the nights the test asked for."""


def test_schedule_nights_1_runs_one_night_and_its_summary_then_returns(tmp_path):
    # The runner container runs `schedule --nights 1` and is restarted by compose.
    world = World(tmp_path)
    url = world.add_article(1)

    def sleep(seconds):  # a loop that ignored --nights would run night after night: stop it
        world.clock.sleep(seconds)
        if world.clock.now() > START + timedelta(days=2):
            raise Overslept(world.clock.now())
    assert main(["schedule", "--nights", "1"], env={}, deps_factory=lambda env: world.deps(sleep=sleep)) == 0
    assert world.record.row(url)["status"] == "published"
    assert [title for title, _, _ in world.push.sent] == ["Narration: 2026-11-04"]
    assert world.clock.now() == datetime(2026, 11, 4, 7, 0, tzinfo=NY)  # the summary's time, then it returned


@pytest.mark.parametrize("value, message", [("0", "--nights must be at least 1"), ("-1", "--nights must be at least 1"),
                                            ("one", "--nights must be a whole number")])
def test_schedule_nights_must_be_at_least_one(tmp_path, capsys, value, message):
    world = World(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["schedule", "--nights", value], env={}, deps_factory=lambda env: world.deps())
    assert e.value.code == 2 and message in capsys.readouterr().err


@pytest.mark.parametrize("session,line", [("no_login", "stopped: Claude login failed"),
                                          ("api_down", "stopped: Claude API unreachable")])
def test_run_says_when_an_outside_failure_stopped_it(tmp_path, capsys, session, line):
    world = World(tmp_path)
    world.add_article(1, script=Script(session=session))
    world.add_article(2)
    assert main(["run"], env={}, deps_factory=lambda env: world.deps()) == 0
    out = capsys.readouterr().out
    assert line in out and "Article 2" not in out
