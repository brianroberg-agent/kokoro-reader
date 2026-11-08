"""deploy/rootless/svc-egress: the one chain that limits a rootless service account's network
reach on the host (docs/pipeline.md, "Deploy, rootless Podman"). It runs here against a fake
iptables and ip6tables that keep their chains in a JSON file and log every call, and a fake
`id` that knows one account. The live checks on the home server are under "Verified"."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_deploy_files import DEPLOY, unit

SCRIPT = DEPLOY / "rootless" / "svc-egress"
ACCOUNT, UID, SUBUID_START, SUBUID_COUNT = "narration", 1001, 231072, 65536
RANGE = f"{SUBUID_START}-{SUBUID_START + SUBUID_COUNT - 1}"  # 231072-296607
GRANTS = "100.64.0.5:8770 192.168.50.20:13378"
RESOLVERS = "192.168.50.1 100.100.100.100"  # the router, the tailnet's resolver
CHAIN = f"svc-{ACCOUNT}"
# the LAN, the tailnet and link-local; then "this network" (0.0.0.0 reaches the host itself),
# multicast (mDNS and SSDP answer from devices at home) and the reserved block with broadcast
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
           "0.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4"]
V4_RULES = [
    "-m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",  # replies, the agent's own ssh among them
    "-o lo -p udp --dport 53 -j RETURN",  # the host's own stub resolver, for the account's shell
    "-o lo -p tcp --dport 53 -j RETURN",
    # the upstream resolvers the conf names: a compose network's aardvark-dns forwards to them,
    # not to the loopback stub (review R1), and only to them (security review S3)
    "-d 192.168.50.1/32 -p udp --dport 53 -j RETURN",
    "-d 192.168.50.1/32 -p tcp --dport 53 -j RETURN",
    "-d 100.100.100.100/32 -p udp --dport 53 -j RETURN",
    "-d 100.100.100.100/32 -p tcp --dport 53 -j RETURN",
    # the grants, in the conf's order, before loopback: a grant to one of the server's own
    # addresses (a service on the home server itself) goes out over lo (review R4)
    "-d 100.64.0.5/32 -p tcp --dport 8770 -j RETURN",
    "-d 192.168.50.20/32 -p tcp --dport 13378 -j RETURN",
    "-o lo -j REJECT",  # nothing else on the host's own loopback
    *[f"-d {net} -j REJECT" for net in PRIVATE],  # the rest of the LAN and the tailnet
    "-j RETURN",  # the internet (Brian, 2026-11-07: accepted for this service)
]
JUMPS = [f"-m owner --uid-owner {UID} -j {CHAIN}", f"-m owner --uid-owner {RANGE} -j {CHAIN}"]

FAKE_IPTABLES = r'''
import json, os, sys
prog = os.path.basename(sys.argv[0])
family = "v6" if prog.startswith("ip6") else "v4"
args = [a for a in sys.argv[1:] if a != "-w"]
log = open(os.environ["FAKE_IPT_LOG"], "a")
path = os.environ["FAKE_IPT_STATE"]
state = json.load(open(path))


class Fail(Exception):
    pass


def run_op(chains, args):
    # One iptables command against chains, changed in place; Fail as iptables would.
    op, name, rest = args[0], args[1], args[2:]
    if op == "-N":
        if name in chains:
            raise Fail("iptables: Chain already exists.")
        chains[name] = []
    elif name not in chains:
        raise Fail("iptables: No chain/target/match by that name.")
    elif op == "-F":
        chains[name] = []
    elif op == "-X":
        if chains[name] or any(r.endswith(f"-j {name}") for c in chains.values() for r in c):
            raise Fail("iptables: Directory not empty.")
        del chains[name]
    elif op == "-A":
        chains[name].append(" ".join(rest))
    elif op == "-I":
        pos, rule = (int(rest[0]), rest[1:]) if rest and rest[0].isdigit() else (1, rest)
        chains[name].insert(pos - 1, " ".join(rule))
    elif op in ("-D", "-C"):
        rule = " ".join(rest)
        if rule not in chains[name]:
            raise Fail("iptables: Bad rule (does a matching rule exist in that chain?).")
        if op == "-D":
            chains[name].remove(rule)
    else:
        raise Fail(f"fake iptables: unsupported {op}")


try:
    if prog.endswith("-restore"):
        lines = sys.stdin.read().splitlines()
        log.write(" ".join([prog] + args) + "\n" + "".join(f"  {line}\n" for line in lines))
        if os.environ.get("FAKE_IPT_FAIL") == family:
            raise Fail(f"{prog}: line 3 failed")
        # One load: every line takes, or none does. Without --noflush the table is replaced.
        work = json.loads(json.dumps(state[family]))
        if "--noflush" not in args:
            work = {"INPUT": [], "OUTPUT": [], "FORWARD": []}
        committed = False
        for line in lines:
            if not line or line.startswith("#") or line == "*filter":
                continue
            if line == "COMMIT":
                committed = True
                break
            if line.startswith(":"):
                work[line[1:].split()[0]] = []  # declared: created, or flushed
                continue
            run_op(work, line.split())
        if not committed:
            raise Fail(f"{prog}: no COMMIT line")
        state[family] = work
    else:
        log.write(" ".join([prog] + args) + "\n")
        chains = state[family]
        if args[0] == "-S":
            for name in args[1:2] or list(chains):
                if name not in chains:
                    raise Fail("iptables: No chain/target/match by that name.")
                print(f"-P {name} ACCEPT" if name in ("INPUT", "OUTPUT", "FORWARD") else f"-N {name}")
                for rule in chains[name]:
                    print(f"-A {name} {rule}")
            sys.exit(0)
        run_op(chains, args)
except Fail as e:
    print(e, file=sys.stderr)
    sys.exit(1)
json.dump(state, open(path, "w"))
'''


def conf(grants: str = GRANTS, resolvers: str = RESOLVERS) -> str:
    return f'GRANTS="{grants}"\nRESOLVERS="{resolvers}"\n'


@pytest.fixture
def host(tmp_path):
    """A fake host: iptables, ip6tables and id on PATH, a conf dir and a subuid file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("iptables", "ip6tables", "iptables-restore", "ip6tables-restore"):
        fake = bin_dir / name
        fake.write_text(f"#!{sys.executable}\n{FAKE_IPTABLES}")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    fake_id = bin_dir / "id"
    fake_id.write_text(f'#!/bin/sh\n[ "$1" = -u ] && [ "$2" = {ACCOUNT} ] && echo {UID} && exit 0\n'
                       'echo "id: $2: no such user" >&2; exit 1\n')
    fake_id.chmod(0o755)
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / f"{ACCOUNT}.conf").write_text(conf())
    (tmp_path / "subuid").write_text(f"someoneelse:100000:65536\n{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n")
    state = {fam: {"INPUT": [], "OUTPUT": ["-o lo -j ACCEPT"], "FORWARD": []} for fam in ("v4", "v6")}
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "log").write_text("")
    return tmp_path


def run(host: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {**extra_env, "PATH": f"{host / 'bin'}:/usr/bin:/bin", "SVC_EGRESS_CONF_DIR": str(host / "conf"),
           "SVC_EGRESS_SUBUID": str(host / "subuid"), "FAKE_IPT_STATE": str(host / "state.json"),
           "FAKE_IPT_LOG": str(host / "log")}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def state(host: Path) -> dict:
    return json.loads((host / "state.json").read_text())


def test_apply_writes_the_v4_chain_in_order(host):
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 0, r.stderr
    assert state(host)["v4"][CHAIN] == V4_RULES


def test_the_account_gets_no_ipv6(host):
    # Brian, 2026-11-07: no IPv6 at all. Rejecting only private v6 ranges would leave a home
    # network's public IPv6 addresses reachable as "internet".
    assert run(host, "apply", ACCOUNT).returncode == 0
    v6 = state(host)["v6"]
    assert v6[CHAIN] == ["-j REJECT"]
    assert v6["OUTPUT"][:2] == JUMPS


def test_jumps_sit_at_the_top_of_output(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    # the account's uid first, then its sub-ids (a process that breaks out of a container as one)
    assert state(host)["v4"]["OUTPUT"] == JUMPS + ["-o lo -j ACCEPT"]


def test_apply_twice_leaves_one_pair_of_jumps(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert run(host, "apply", ACCOUNT).returncode == 0
    s = state(host)
    for fam in ("v4", "v6"):
        assert s[fam]["OUTPUT"] == JUMPS + ["-o lo -j ACCEPT"]
    assert s["v4"][CHAIN] == V4_RULES and s["v6"][CHAIN] == ["-j REJECT"]


@pytest.mark.parametrize("grant", ["host.example:80", "10.0.0.1", "10.0.0.1:99999", "10.0.0.1:0",
                                   "10.0.0.256:80", "10.0.0:80", "10.0.0.1:80;reboot"])
def test_a_bad_grant_stops_with_its_name_and_changes_nothing(host, grant):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=f"100.64.0.5:8770 {grant}"))
    before = state(host)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and grant in r.stderr
    assert (host / "log").read_text() == "" and state(host) == before


@pytest.mark.parametrize("text", ["", "GRANTS=100.64.0.5:8770\n", "# GRANTS=\"100.64.0.5:8770\"\n"])
def test_a_conf_without_a_quoted_grants_line_stops_and_changes_nothing(host, text):
    # An unquoted or missing GRANTS would otherwise read as "no grants" without a word.
    (host / "conf" / f"{ACCOUNT}.conf").write_text(text)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "GRANTS" in r.stderr
    assert (host / "log").read_text() == ""


def test_an_empty_grants_line_means_no_grants(host):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=""))
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert state(host)["v4"][CHAIN] == [r for r in V4_RULES if "--dport 8770" not in r and "13378" not in r]


@pytest.mark.parametrize("lines", [
    f"{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n{ACCOUNT}:400000:65536\n",  # usermod --add-subuids
    f"{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n{UID}:400000:65536\n",  # a range listed by uid number
])
def test_a_second_subid_range_stops_and_changes_nothing(host, lines):
    # One jump covers one range: a second would leave its sub-ids outside the chain.
    (host / "subuid").write_text(lines)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "more than one sub-id range" in r.stderr
    assert (host / "log").read_text() == ""


def test_a_range_listed_by_uid_number_is_the_accounts(host):
    (host / "subuid").write_text(f"someoneelse:100000:65536\n{UID}:{SUBUID_START}:{SUBUID_COUNT}\n")
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert state(host)["v4"]["OUTPUT"][:2] == JUMPS


@pytest.mark.parametrize("resolver", ["resolver.example", "192.168.50.1:53", "192.168.50.300", "::1"])
def test_a_bad_resolver_stops_with_its_name_and_changes_nothing(host, resolver):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(resolvers=f"192.168.50.1 {resolver}"))
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and resolver in r.stderr
    assert (host / "log").read_text() == ""


def test_a_conf_without_a_quoted_resolvers_line_stops_and_changes_nothing(host):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(f'GRANTS="{GRANTS}"\n')
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "RESOLVERS" in r.stderr
    assert (host / "log").read_text() == ""


def test_no_subuid_line_stops_and_changes_nothing(host):
    (host / "subuid").write_text("someoneelse:100000:65536\n")
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "subuid" in r.stderr
    assert (host / "log").read_text() == ""


@pytest.mark.parametrize("problem, needle", [("no conf", ".conf"), ("unknown account", "nobody-here")])
def test_a_missing_conf_or_account_stops_and_changes_nothing(host, problem, needle):
    account = ACCOUNT
    if problem == "no conf":
        (host / "conf" / f"{ACCOUNT}.conf").unlink()
    else:
        account = "nobody-here"
    r = run(host, "apply", account)
    assert r.returncode == 1 and needle in r.stderr
    assert (host / "log").read_text() == ""


def test_a_failed_load_leaves_that_family_as_it_was(host):
    # Review R3: a re-apply that fails part-way must not leave the account unlimited.
    assert run(host, "apply", ACCOUNT).returncode == 0
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=GRANTS + " 10.20.30.40:443"))
    r = run(host, "apply", ACCOUNT, FAKE_IPT_FAIL="v4")
    assert r.returncode != 0
    s = state(host)
    assert s["v4"][CHAIN] == V4_RULES and s["v4"]["OUTPUT"] == JUMPS + ["-o lo -j ACCEPT"]
    assert s["v6"][CHAIN] == ["-j REJECT"] and s["v6"]["OUTPUT"] == JUMPS + ["-o lo -j ACCEPT"]


def test_ipv6_goes_first_and_a_failure_there_stops_before_ipv4(host):
    r = run(host, "apply", ACCOUNT, FAKE_IPT_FAIL="v6")
    assert r.returncode != 0
    assert CHAIN not in state(host)["v4"] and state(host)["v4"]["OUTPUT"] == ["-o lo -j ACCEPT"]
    assert "iptables-restore" not in (host / "log").read_text().replace("ip6tables-restore", "")


def test_a_reapply_leaves_the_jumps_where_they_are(host):
    # Review R3: no moment without the jumps. The chain is reloaded in place; the jumps,
    # already the first two rules of OUTPUT, are neither deleted nor inserted again.
    assert run(host, "apply", ACCOUNT).returncode == 0
    (host / "log").write_text("")
    assert run(host, "apply", ACCOUNT).returncode == 0
    log = (host / "log").read_text()
    assert "-D OUTPUT" not in log and "-I OUTPUT" not in log
    assert state(host)["v4"][CHAIN] == V4_RULES


def test_jumps_moved_down_go_back_to_the_top(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    s = state(host)
    for fam in ("v4", "v6"):  # another tool inserted a rule above them
        s[fam]["OUTPUT"].insert(0, "-j ACCEPT")
    (host / "state.json").write_text(json.dumps(s))
    assert run(host, "apply", ACCOUNT).returncode == 0
    for fam in ("v4", "v6"):
        assert state(host)[fam]["OUTPUT"] == JUMPS + ["-j ACCEPT", "-o lo -j ACCEPT"]


def test_remove_leaves_no_chain_or_jump(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    r = run(host, "remove", ACCOUNT)
    assert r.returncode == 0, r.stderr
    for fam in ("v4", "v6"):
        assert CHAIN not in state(host)[fam]
        assert state(host)[fam]["OUTPUT"] == ["-o lo -j ACCEPT"]


def test_remove_works_once_the_account_and_its_range_are_gone(host):
    # Review R7: after userdel, remove must still take the chain and its jumps away.
    assert run(host, "apply", ACCOUNT).returncode == 0
    (host / "bin" / "id").write_text('#!/bin/sh\necho "id: $2: no such user" >&2; exit 1\n')
    (host / "subuid").write_text("someoneelse:100000:65536\n")
    r = run(host, "remove", ACCOUNT)
    assert r.returncode == 0, r.stderr
    for fam in ("v4", "v6"):
        assert CHAIN not in state(host)[fam] and state(host)[fam]["OUTPUT"] == ["-o lo -j ACCEPT"]


def test_remove_takes_a_jump_for_an_old_range_too(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    s = state(host)
    s["v4"]["OUTPUT"].append(f"-m owner --uid-owner 500000-565535 -j {CHAIN}")  # before a usermod
    (host / "state.json").write_text(json.dumps(s))
    assert run(host, "remove", ACCOUNT).returncode == 0
    assert CHAIN not in state(host)["v4"] and state(host)["v4"]["OUTPUT"] == ["-o lo -j ACCEPT"]


def test_remove_goes_on_to_ipv6_when_ipv4_fails(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    r = run(host, "remove", ACCOUNT, FAKE_IPT_FAIL="v4")
    assert r.returncode != 0
    assert CHAIN not in state(host)["v6"] and CHAIN in state(host)["v4"]


def test_remove_without_a_chain_is_not_an_error(host):
    assert run(host, "remove", ACCOUNT).returncode == 0


def test_usage_errors_change_nothing(host):
    for args in ([], ["apply"], ["frobnicate", ACCOUNT]):
        assert run(host, *args).returncode == 2
    assert (host / "log").read_text() == ""


def test_the_egress_unit_runs_apply_and_remove():
    u = unit("rootless/svc-egress@.service")
    assert u["Service"]["ExecStart"] == "/usr/local/sbin/svc-egress apply %i"
    assert u["Service"]["ExecStop"] == "/usr/local/sbin/svc-egress remove %i"
    # Review R8: a new grant goes in with `systemctl reload`, which re-applies in place. A
    # restart would restart the account's user manager too (it Requires= this unit), and
    # with it every container.
    assert u["Service"]["ExecReload"] == "/usr/local/sbin/svc-egress apply %i"
    assert u["Service"]["Type"] == "oneshot" and u["Service"]["RemainAfterExit"] == "yes"
    assert u["Install"]["WantedBy"] == "multi-user.target"
    # after the host's own firewall tools, so a full reload of theirs at boot comes first
    assert {"ufw.service", "netfilter-persistent.service"} <= set(u["Unit"]["After"].split())


def test_the_users_manager_needs_the_chain():
    # Fail-closed: the account's systemd user manager, and with it every container it runs,
    # starts only after the chain is in, and stops if the chain's unit stops.
    u = unit("rootless/user-egress.conf")
    assert u["Unit"]["Requires"] == "svc-egress@narration.service"
    assert u["Unit"]["After"] == "svc-egress@narration.service"


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
