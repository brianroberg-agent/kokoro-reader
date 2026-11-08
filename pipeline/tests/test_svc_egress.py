"""deploy/rootless/svc-egress: the one place a rootless service account's network reach is set
on the host, its own nftables table (docs/pipeline.md, "Deploy, rootless Podman"). It runs
here against a fake `nft` that keeps its tables in a JSON file and logs every call, a fake
`id` and a fake `systemctl`. The live checks on the home server are under "Verified"."""

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
TABLE = f"inet svc_egress_{ACCOUNT}"
OTHER = "ip filter"  # Docker's and ufw's rules live elsewhere: never touched
# the LAN, the tailnet and link-local; "this network" (0.0.0.0 reaches the host itself),
# multicast (mDNS and SSDP answer from devices at home) and the reserved block with broadcast
PRIVATE = ("10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, "
           "0.0.0.0/8, 224.0.0.0/4, 240.0.0.0/4")
REACH = [
    "meta nfproto ipv6 reject",  # no IPv6 at all (Brian, 2026-11-07)
    "ct state established,related accept",  # replies, the agent's own ssh among them
    'oifname "lo" udp dport 53 accept',  # the host's own stub resolver, for the account's shell
    'oifname "lo" tcp dport 53 accept',
    # the upstream resolvers the conf names: a compose network's aardvark-dns forwards to them
    # (review R1), and to no others (security review S3)
    "ip daddr 192.168.50.1 udp dport 53 accept",
    "ip daddr 192.168.50.1 tcp dport 53 accept",
    "ip daddr 100.100.100.100 udp dport 53 accept",
    "ip daddr 100.100.100.100 tcp dport 53 accept",
    # the grants, before loopback: one to the server's own address goes out over lo (R4)
    "ip daddr 100.64.0.5 tcp dport 8770 accept",
    "ip daddr 192.168.50.20 tcp dport 13378 accept",
    'oifname "lo" reject',  # nothing else on the host's own loopback
    f"ip daddr {{ {PRIVATE} }} reject",
    "accept",  # the internet (Brian, 2026-11-07: accepted for this service)
]
OUTPUT = [
    "type filter hook output priority 0; policy accept;",
    f"meta skuid {UID} jump reach",  # the account
    f"meta skuid {RANGE} jump reach",  # its sub-ids: a process that breaks out of a container as one
]

FAKE_NFT = r'''
import json, os, re, sys
args = sys.argv[1:]
path = os.environ["FAKE_NFT_STATE"]
state = json.load(open(path))
tables = state["tables"]


def log(text):
    with open(os.environ["FAKE_NFT_LOG"], "a") as f:
        f.write(text)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def parse_block(lines):
    """table NAME { chain X { ... } ... } -> {chain: [rules]}; nested braces in a rule stay."""
    chains, current = {}, None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"chain (\w+) \{", line)
        if m:
            current = chains.setdefault(m.group(1), [])
        elif line == "}":
            current = None
        elif current is not None:
            current.append(line)
        else:
            raise ValueError(f"line outside a chain: {line}")
    return chains


def load(text):
    """One transaction: every statement takes, or none does."""
    work = json.loads(json.dumps(tables))
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"table (inet|ip|ip6) (\w+)( \{)?", line)
        d = re.fullmatch(r"delete table (inet|ip|ip6) (\w+)", line)
        if d:
            name = f"{d.group(1)} {d.group(2)}"
            if name not in work:
                fail(f"Error: No such file or directory; did you mean table {name}?")
            del work[name]
        elif m and not m.group(3):
            work.setdefault(f"{m.group(1)} {m.group(2)}", {})
        elif m:
            depth, block = 1, []
            while depth:
                inner = lines[i]
                i += 1
                depth += inner.count("{") - inner.count("}")
                if depth:
                    block.append(inner)
            work[f"{m.group(1)} {m.group(2)}"] = parse_block(block)
        else:
            fail(f"Error: syntax error: {line}")
    return work


if args[:1] == ["-f"]:
    text = sys.stdin.read() if args[1] == "-" else open(args[1]).read()
    log("nft -f\n" + "".join(f"  {line}\n" for line in text.splitlines()))
    if os.environ.get("FAKE_NFT_FAIL"):
        fail("Error: Could not process rule: Operation not supported")
    state["tables"] = load(text)
else:
    log("nft " + " ".join(args) + "\n")
    as_json = args[:1] == ["-j"]
    args = args[1:] if as_json else args
    if args[:2] == ["list", "table"]:
        name = f"{args[2]} {args[3]}"
        if name not in tables:
            fail(f"Error: No such file or directory; did you mean table {name}?")
        if as_json:
            rules = [{"rule": {"chain": c, "expr": r}} for c, rs in tables[name].items() for r in rs
                     if not r.startswith("type ")]
            print(json.dumps({"nftables": [{"metainfo": {}}, {"table": {"name": args[3]}}] + rules}))
        else:
            print(f"table {name} {{")
            for c, rs in tables[name].items():
                print(f"\tchain {c} {{")
                for r in rs:
                    print(f"\t\t{r}")
                print("\t}")
            print("}")
        sys.exit(0)
    if args[:2] == ["delete", "table"]:
        name = f"{args[2]} {args[3]}"
        if name not in tables:
            fail(f"Error: No such file or directory; did you mean table {name}?")
        del tables[name]
    else:
        fail("fake nft: unsupported " + " ".join(args))
json.dump(state, open(path, "w"))
'''


@pytest.fixture
def host(tmp_path):
    """A fake host: nft, id and systemctl on PATH, a conf dir, a subuid file and a state dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "nft"
    fake.write_text(f"#!{sys.executable}\n{FAKE_NFT}")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "ids").write_text(f"{ACCOUNT} {UID}\n")
    fake_id = bin_dir / "id"
    fake_id.write_text(f'#!/bin/sh\n[ "$1" = -u ] && grep "^$2 " {tmp_path / "ids"} | cut -d" " -f2 | grep . && exit 0\n'
                       'echo "id: $2: no such user" >&2; exit 1\n')
    fake_id.chmod(0o755)
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(f'#!/bin/sh\necho "systemctl $*" >> {tmp_path / "systemctl.log"}\n')
    fake_systemctl.chmod(0o755)
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / f"{ACCOUNT}.conf").write_text(conf())
    (tmp_path / "subuid").write_text(f"someoneelse:100000:65536\n{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n")
    tables = {OTHER: {"OUTPUT": ["-o lo -j ACCEPT"]}}
    (tmp_path / "state.json").write_text(json.dumps({"tables": tables}))
    (tmp_path / "log").write_text("")
    (tmp_path / "systemctl.log").write_text("")
    return tmp_path


def conf(grants: str = GRANTS, resolvers: str = RESOLVERS) -> str:
    return f'GRANTS="{grants}"\nRESOLVERS="{resolvers}"\n'


def run(host: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {**extra_env, "PATH": f"{host / 'bin'}:/usr/bin:/bin", "SVC_EGRESS_CONF_DIR": str(host / "conf"),
           "SVC_EGRESS_SUBUID": str(host / "subuid"), "SVC_EGRESS_STATE_DIR": str(host / "run"),
           "FAKE_NFT_STATE": str(host / "state.json"), "FAKE_NFT_LOG": str(host / "log")}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def tables(host: Path) -> dict:
    return json.loads((host / "state.json").read_text())["tables"]


def set_tables(host: Path, t: dict) -> None:
    (host / "state.json").write_text(json.dumps({"tables": t}))


def log(host: Path) -> str:
    return (host / "log").read_text()


def test_apply_writes_the_limit_in_order(host):
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 0, r.stderr
    assert tables(host)[TABLE]["reach"] == REACH


def test_the_account_and_its_subids_go_through_the_limit(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert tables(host)[TABLE]["output"] == OUTPUT


def test_the_account_gets_no_ipv6(host):
    # Brian, 2026-11-07: no IPv6 at all, before anything else is let through. Rejecting only
    # private v6 ranges would leave a home network's public IPv6 addresses reachable.
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert tables(host)[TABLE]["reach"][0] == "meta nfproto ipv6 reject"


def test_apply_loads_once_and_leaves_other_tables_alone(host):
    # Round 2, #10: its own table. A reject there holds whatever another table accepts, and
    # nothing that reloads the filter table (ufw, iptables-restore) touches it.
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert log(host).count("nft -f") == 1
    assert tables(host)[OTHER] == {"OUTPUT": ["-o lo -j ACCEPT"]}
    assert "flush ruleset" not in log(host)


def test_apply_twice_gives_the_same_table(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    first = tables(host)
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert tables(host) == first


def test_a_failed_load_leaves_the_table_as_it_was(host):
    # Review R3: a re-apply that fails must not leave the account unlimited.
    assert run(host, "apply", ACCOUNT).returncode == 0
    before = tables(host)
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=GRANTS + " 10.20.30.40:443"))
    r = run(host, "apply", ACCOUNT, FAKE_NFT_FAIL="1")
    assert r.returncode != 0 and tables(host) == before


def test_each_account_gets_its_own_table(host):
    # Round 2, #2: no fight between two accounts over who sits first.
    (host / "ids").write_text(f"{ACCOUNT} {UID}\nother {UID + 1}\n")
    (host / "subuid").write_text(f"{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\nother:400000:65536\n")
    (host / "conf" / "other.conf").write_text(conf(grants=""))
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert run(host, "apply", "other").returncode == 0
    t = tables(host)
    assert t[TABLE]["output"] == OUTPUT
    assert t["inet svc_egress_other"]["output"][1:] == [f"meta skuid {UID + 1} jump reach",
                                                          "meta skuid 400000-465535 jump reach"]


def test_a_hyphen_in_the_account_becomes_an_underscore_in_the_table(host):
    (host / "ids").write_text("svc-two 1003\n")
    (host / "subuid").write_text("svc-two:500000:65536\n")
    (host / "conf" / "svc-two.conf").write_text(conf())
    assert run(host, "apply", "svc-two").returncode == 0
    assert "inet svc_egress_svc_two" in tables(host)


@pytest.mark.parametrize("grant", ["host.example:80", "10.0.0.1", "10.0.0.1:99999", "10.0.0.1:0",
                                   "10.0.0.256:80", "10.0.0:80", "10.0.0.1:80;reboot"])
def test_a_bad_grant_stops_with_its_name_and_changes_nothing(host, grant):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=f"100.64.0.5:8770 {grant}"))
    before = tables(host)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and grant in r.stderr
    assert log(host) == "" and tables(host) == before


@pytest.mark.parametrize("text", ["", "GRANTS=100.64.0.5:8770\n", "# GRANTS=\"100.64.0.5:8770\"\n"])
def test_a_conf_without_a_quoted_grants_line_stops_and_changes_nothing(host, text):
    # An unquoted or missing GRANTS would otherwise read as "no grants" without a word.
    (host / "conf" / f"{ACCOUNT}.conf").write_text(text)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "GRANTS" in r.stderr
    assert log(host) == ""


def test_an_empty_grants_line_means_no_grants(host):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(grants=""))
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert tables(host)[TABLE]["reach"] == [r for r in REACH if "8770" not in r and "13378" not in r]


@pytest.mark.parametrize("resolver", ["resolver.example", "192.168.50.1:53", "192.168.50.300", "::1"])
def test_a_bad_resolver_stops_with_its_name_and_changes_nothing(host, resolver):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(conf(resolvers=f"192.168.50.1 {resolver}"))
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and resolver in r.stderr
    assert log(host) == ""


def test_a_conf_without_a_quoted_resolvers_line_stops_and_changes_nothing(host):
    (host / "conf" / f"{ACCOUNT}.conf").write_text(f'GRANTS="{GRANTS}"\n')
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "RESOLVERS" in r.stderr
    assert log(host) == ""


@pytest.mark.parametrize("lines", [
    f"{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n{ACCOUNT}:400000:65536\n",  # usermod --add-subuids
    f"{ACCOUNT}:{SUBUID_START}:{SUBUID_COUNT}\n{UID}:400000:65536\n",  # a range listed by uid number
])
def test_a_second_subid_range_stops_and_changes_nothing(host, lines):
    # One rule covers one range: a second would leave its sub-ids outside the limit.
    (host / "subuid").write_text(lines)
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "more than one sub-id range" in r.stderr
    assert log(host) == ""


def test_a_range_listed_by_uid_number_is_the_accounts(host):
    (host / "subuid").write_text(f"someoneelse:100000:65536\n{UID}:{SUBUID_START}:{SUBUID_COUNT}\n")
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert tables(host)[TABLE]["output"] == OUTPUT


def test_no_subuid_line_stops_and_changes_nothing(host):
    (host / "subuid").write_text("someoneelse:100000:65536\n")
    r = run(host, "apply", ACCOUNT)
    assert r.returncode == 1 and "subuid" in r.stderr
    assert log(host) == ""


@pytest.mark.parametrize("problem, needle", [("no conf", ".conf"), ("unknown account", "nobody-here")])
def test_a_missing_conf_or_account_stops_and_changes_nothing(host, problem, needle):
    account = ACCOUNT
    if problem == "no conf":
        (host / "conf" / f"{ACCOUNT}.conf").unlink()
    else:
        account = "nobody-here"
    r = run(host, "apply", account)
    assert r.returncode == 1 and needle in r.stderr
    assert log(host) == ""


def test_check_leaves_an_intact_table_alone(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    (host / "log").write_text("")
    r = run(host, "check", ACCOUNT)
    assert r.returncode == 0 and r.stderr == ""
    assert "nft -f" not in log(host)


@pytest.mark.parametrize("damage", ["gone", "emptied"])
def test_check_puts_back_what_a_full_flush_took(host, damage):
    # Review R2 and round 2 #1: someone else's `nft flush ruleset` takes the table; the check
    # loads what apply last loaded, without reading the conf, which may have gone bad since.
    assert run(host, "apply", ACCOUNT).returncode == 0
    t = tables(host)
    if damage == "gone":
        del t[TABLE]
    else:
        t[TABLE]["reach"] = []
    set_tables(host, t)
    (host / "conf" / f"{ACCOUNT}.conf").write_text('GRANTS="host.example:80"\n')
    r = run(host, "check", ACCOUNT)
    assert r.returncode == 0 and "loading it again" in r.stderr
    assert tables(host)[TABLE]["reach"] == REACH and tables(host)[TABLE]["output"] == OUTPUT
    assert (host / "systemctl.log").read_text() == ""


@pytest.mark.parametrize("trouble", ["nothing saved", "load fails"])
def test_a_check_that_cannot_put_the_table_back_stops_the_account(host, trouble):
    # Fail-closed: stopping svc-egress@ stops the account's user manager (it Requires= it),
    # and with it every container. --no-block: the check's own unit is bound to it.
    assert run(host, "apply", ACCOUNT).returncode == 0
    t = tables(host)
    del t[TABLE]
    set_tables(host, t)
    extra = {}
    if trouble == "nothing saved":
        for f in (host / "run").iterdir():
            f.unlink()
    else:
        extra["FAKE_NFT_FAIL"] = "1"
    r = run(host, "check", ACCOUNT, **extra)
    assert r.returncode == 1
    assert (host / "systemctl.log").read_text() == f"systemctl --no-block stop svc-egress@{ACCOUNT}.service\n"


def test_the_saved_copy_is_roots_alone(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    assert stat.S_IMODE((host / "run").stat().st_mode) == 0o700


def test_remove_deletes_the_table_and_the_saved_copy(host):
    assert run(host, "apply", ACCOUNT).returncode == 0
    r = run(host, "remove", ACCOUNT)
    assert r.returncode == 0, r.stderr
    assert TABLE not in tables(host) and tables(host)[OTHER] == {"OUTPUT": ["-o lo -j ACCEPT"]}
    assert not any((host / "run").iterdir())


def test_remove_works_once_the_account_and_its_range_are_gone(host):
    # Review R7: after userdel, remove must still take the table away.
    assert run(host, "apply", ACCOUNT).returncode == 0
    (host / "ids").write_text("")
    (host / "subuid").write_text("someoneelse:100000:65536\n")
    assert run(host, "remove", ACCOUNT).returncode == 0
    assert TABLE not in tables(host)


def test_remove_without_a_table_is_not_an_error(host):
    assert run(host, "remove", ACCOUNT).returncode == 0


def test_usage_errors_change_nothing(host):
    for args in ([], ["apply"], ["check"], ["frobnicate", ACCOUNT]):
        assert run(host, *args).returncode == 2
    assert log(host) == ""


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


def test_the_check_timer_runs_check_while_the_limit_is_on():
    timer = unit("rootless/svc-egress-check@.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "30s" and timer["Timer"]["OnBootSec"] == "30s"
    assert timer["Timer"]["Unit"] == "svc-egress-check@%i.service"
    assert timer["Install"]["WantedBy"] == "timers.target"
    service = unit("rootless/svc-egress-check@.service")
    assert service["Service"]["ExecStart"] == "/usr/local/sbin/svc-egress check %i"
    assert service["Service"]["Type"] == "oneshot"
    # only while svc-egress@ is on: disabling it for a rollback isn't undone 30 seconds later
    assert service["Unit"]["Requisite"] == "svc-egress@%i.service"
    assert service["Unit"]["After"] == "svc-egress@%i.service"


def test_the_users_manager_needs_the_limit():
    # The account's systemd user manager, and with it every container it runs, starts only
    # after the table is in, and stops if the limit's unit stops.
    u = unit("rootless/user-egress.conf")
    assert u["Unit"]["Requires"] == "svc-egress@narration.service"
    assert u["Unit"]["After"] == "svc-egress@narration.service"


def test_nothing_writes_the_nftables_service_or_its_conf():
    # The home server: nftables.service is never enabled, and /etc/nftables.conf is never
    # written. Its stock config begins with `flush ruleset`, which would wipe Docker's and
    # ufw's rules. The table is loaded by svc-egress@ alone.
    for path in (DEPLOY / "rootless").iterdir():
        code = [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]
        for line in code:
            assert not any(s in line for s in ("flush ruleset", "nftables.conf", "nftables.service")), \
                f"{path.name}: {line}"


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
