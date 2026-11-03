"""A static check of the deploy files: deploy/compose.yaml, the two Dockerfiles and the
env examples. It checks what agentbox needs outside its launcher, the egress, where each
service's secrets come from, and the volumes. The live checks (the images built and
started, and the containment) are recorded in docs/pipeline.md."""

import json
import re
from pathlib import Path

import pytest
import yaml

from narration_pipeline.__main__ import REQUIRED

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
SERVICES = ("narration-runner", "narration-agent")
MAC_URL = "http://${MAC_HOST:?set MAC_HOST in deploy/.env}:${MAC_PORT:-8770}"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text())


def environment(service: dict) -> dict:
    env = service["environment"]
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def env_files(service: dict) -> list:
    files = service.get("env_file", [])
    return [files] if isinstance(files, str) else files


def instructions(name: str) -> list[str]:
    """The Dockerfile's instructions, without comments, blank lines or line continuations."""
    text = (DEPLOY / name / "Dockerfile").read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def cmd(name: str) -> list[str]:
    [line] = [i for i in instructions(name) if i.startswith("CMD ")]
    return json.loads(line.removeprefix("CMD "))


def example_names(name: str) -> dict[str, str]:
    lines = (DEPLOY / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line.strip() and not line.startswith("#"))


@pytest.mark.parametrize("name", SERVICES)
def test_compose_has_agentbox_settings(compose, name):
    s = compose["services"][name]
    assert s["init"] is True
    assert sorted(s["cap_add"]) == ["NET_ADMIN", "NET_RAW"]
    assert s["security_opt"] == ["no-new-privileges:true"]
    assert s["restart"] == "unless-stopped"
    env = environment(s)
    assert env["AGENTBOX_NET"] == "restricted"
    assert env["AGENTBOX_GITHUB"] == "off"  # a string: an unquoted off would be YAML's false
    assert env["TZ"] == "America/New_York"


@pytest.mark.parametrize("name", SERVICES)
def test_no_service_is_reachable_or_outside_its_firewall(compose, name):
    s = compose["services"][name]
    assert "ports" not in s and "expose" not in s
    assert "network_mode" not in s and not s.get("privileged")


def test_agent_reaches_only_the_anthropic_api_and_the_mac(compose):
    s = compose["services"]["narration-agent"]
    env = environment(s)
    assert env["AGENTBOX_BASE"] == "off"
    assert "AGENTBOX_ALLOW" not in env
    assert env["AGENTBOX_LAN"] == "${MAC_HOST:?set MAC_HOST in deploy/.env}:${MAC_PORT:-8770}"
    assert env["NARRATION_AGENT_URL"] == MAC_URL + "/agent/mcp"


def test_agent_holds_only_the_claude_login(compose):
    s = compose["services"]["narration-agent"]
    assert env_files(s) == ["/srv/narration/agent.env"]
    assert example_names("agent.env.example") == {"CLAUDE_CODE_OAUTH_TOKEN": ""}


def test_runner_egress_and_secrets(compose):
    s = compose["services"]["narration-runner"]
    env = environment(s)
    assert env["AGENTBOX_BASE"] == "off"  # the runner hands sessions to the agent, and it holds the secrets
    assert env["AGENTBOX_ALLOW"] == "${RUNNER_ALLOW:?set RUNNER_ALLOW in deploy/.env}"
    # the Mac's grant comes from MAC_HOST, so it can't disagree with NARRATION_URL;
    # RUNNER_LAN adds Audiobookshelf when it's reached by address
    assert env["AGENTBOX_LAN"] == "${MAC_HOST:?set MAC_HOST in deploy/.env}:${MAC_PORT:-8770},${RUNNER_LAN:-}"
    assert env["NARRATION_URL"] == MAC_URL
    assert env_files(s) == ["/srv/narration/runner.env"]
    # the example names every secret the runner needs; compose sets NARRATION_URL itself
    assert example_names("runner.env.example") == {n: "" for n in REQUIRED if n != "NARRATION_URL"}


def test_volumes(compose):
    assert set(compose["volumes"]) == {"narration-data", "narration-handoff"}
    assert compose["services"]["narration-runner"]["volumes"] == ["narration-data:/data", "narration-handoff:/handoff"]
    assert compose["services"]["narration-agent"]["volumes"] == ["narration-handoff:/handoff"]


def test_compose_settings_come_from_deploy_env():
    text = (DEPLOY / "compose.yaml").read_text()
    used = re.findall(r"\$\{([A-Z_]+)([^}]*)\}", text)
    assert {name for name, _ in used} == set(example_names("env.example"))
    optional = {"MAC_PORT": ":-8770", "RUNNER_LAN": ":-"}
    for name, rest in used:  # compose stops, naming the setting, when a required one is unset
        assert rest == optional[name] if name in optional else rest.startswith(":?")


@pytest.mark.parametrize("name", SERVICES)
def test_images_build_on_agentbox(name):
    assert instructions(name)[0] == "FROM agentbox:latest"


def test_runner_runs_one_night_and_exits():
    # one night and its summary, then compose's restart starts the next and the firewall
    # resolves the allowed hosts again (it resolves them once, when the container starts)
    assert cmd("narration-runner") == ["uv", "run", "--frozen", "python", "-m", "narration_pipeline",
                                       "schedule", "--nights", "1"]


def test_runner_pins_article_assistant():
    assert any(re.fullmatch(r"ARG ARTICLE_ASSISTANT_REF=[0-9a-f]{7,40}", i) for i in instructions("narration-runner"))


def test_agent_pins_claude_code_and_turns_off_its_updater():
    ins = instructions("narration-agent")
    assert any(re.fullmatch(r"ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+", i) for i in ins)
    assert any("npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in i for i in ins)
    assert "ENV DISABLE_AUTOUPDATER=1" in ins
    assert cmd("narration-agent") == ["python3", "/opt/narration/session_service.py"]


def test_runner_runs_what_the_build_installed():
    # At start uv would find the project "modified" (it compares ctimes, which the chown
    # and the image's layers change) and rebuild it, fetching hatchling past the firewall.
    ins = instructions("narration-runner")
    last_sync = max(n for n, i in enumerate(ins) if "uv sync --frozen" in i)
    assert "ENV UV_NO_SYNC=1" in ins[last_sync + 1:]


def test_runner_leaves_the_dev_tools_out():
    # pytest, hypothesis and pyyaml have no place in the image that holds the secrets
    syncs = [i for i in instructions("narration-runner") if "uv sync" in i]
    assert len(syncs) == 2 and all("uv sync --frozen --no-dev" in i for i in syncs)


def test_runner_code_stays_roots():
    # node can run the code and its venvs but not change them; it owns only its data
    ins = instructions("narration-runner")
    assert not [i for i in ins if "chown" in i]
    assert [i for i in ins if "-o node" in i] == ["RUN install -d -o node -g node -m 0700 /data /handoff"]
