"""The device map: duties are claims, and every claim is checked against the machine."""

from __future__ import annotations

import json
import stat

import pytest

from hotline_registry import admin
from hotline_registry.cli import main
from hotline_registry.devices import (
    Device,
    DeviceMap,
    Duty,
    Unreachable,
    check,
    discover,
    probe,
    render,
)
from hotline_registry.registry import RegistryError


@pytest.fixture()
def dmap(tmp_path) -> DeviceMap:
    m = DeviceMap(tmp_path / "devices.json")
    m.put_device(Device(name="archserver", kind="desktop", local=True, power="always-on"))
    m.put_device(Device(name="pigion", kind="pi", ssh="pigion", power="always-on"))
    m.put_device(Device(name="arch", kind="laptop", ssh="arch", power="when-he-uses-it"))
    return m


class FakeBoxes:
    """`runner` stand-in: per device, a function from command to (rc, output), or None = unreachable."""

    def __init__(self, boxes):
        self.boxes = boxes
        self.ran: list[tuple[str, str]] = []

    def __call__(self, device, command):
        self.ran.append((device.name, command))
        box = self.boxes.get(device.name)
        if box is None:
            raise Unreachable(f"{device.name} is asleep")
        return box(command)


def _unit_box(active: set[str]):
    def run(command):
        unit = command.split()[-1].strip("'")
        return (0, "active") if unit in active else (3, "inactive")
    return run


# ---- the store ------------------------------------------------------------


def test_round_trip_and_private_mode(dmap, tmp_path) -> None:
    dmap.claim(Duty(id="pigion/wake-server", device="pigion", title="wake server",
                    probe={"type": "unit", "unit": "wake-server.service", "scope": "user"},
                    if_down="no box wakes on schedule"))
    dmap.save()
    assert stat.S_IMODE(dmap.path.stat().st_mode) == 0o600
    again = DeviceMap(dmap.path)
    assert again.devices["pigion"].ssh == "pigion"
    assert again.duties["pigion/wake-server"].if_down == "no box wakes on schedule"


def test_claims_are_validated(dmap) -> None:
    with pytest.raises(RegistryError, match="no device"):
        dmap.claim(Duty(id="nowhere/x", device="nowhere", title="x"))
    with pytest.raises(RegistryError, match="probe type"):
        dmap.claim(Duty(id="arch/x", device="arch", title="x", probe={"type": "vibes"}))
    with pytest.raises(RegistryError, match="two devices"):
        dmap.claim(Duty(id="arch/k", device="arch", title="k",
                        probe={"type": "freshest", "path": "~/k", "among": ["arch"]}))
    with pytest.raises(RegistryError, match="no device"):
        dmap.claim(Duty(id="arch/k", device="arch", title="k",
                        probe={"type": "freshest", "path": "~/k", "among": ["arch", "ghost"]}))


def test_a_device_with_duties_is_not_dropped_silently(dmap) -> None:
    dmap.claim(Duty(id="pigion/x", device="pigion", title="x"))
    with pytest.raises(RegistryError, match="still has 1 duties"):
        dmap.drop_device("pigion")
    dmap.drop_device("pigion", force=True)
    assert "pigion/x" not in dmap.duties


def test_reclaiming_with_a_new_probe_forgets_the_old_result(dmap) -> None:
    d = dmap.claim(Duty(id="pigion/x", device="pigion", title="x",
                        probe={"type": "unit", "unit": "a.service"}))
    d.last = {"state": "verified", "at": "t"}
    same = dmap.claim(Duty(id="pigion/x", device="pigion", title="renamed",
                           probe={"type": "unit", "unit": "a.service"}))
    assert same.state == "verified"
    new = dmap.claim(Duty(id="pigion/x", device="pigion", title="x",
                          probe={"type": "unit", "unit": "b.service"}))
    assert new.last == {} and new.state == "unknown"


# ---- probes ---------------------------------------------------------------


def test_unit_probe_verified_contradicted_and_unknown(dmap) -> None:
    boxes = FakeBoxes({"pigion": _unit_box({"wake-server.service"})})
    up = Duty(id="pigion/w", device="pigion", title="w", probe={"type": "unit", "unit": "wake-server.service"})
    gone = Duty(id="pigion/g", device="pigion", title="g", probe={"type": "unit", "unit": "ghost.service"})
    asleep = Duty(id="arch/k", device="arch", title="k", probe={"type": "unit", "unit": "x.service"})
    assert probe(up, dmap, boxes)["state"] == "verified"
    assert probe(gone, dmap, boxes)["state"] == "contradicted"
    result = probe(asleep, dmap, boxes)
    assert result["state"] == "unknown" and "asleep" in result["detail"]
    assert boxes.ran[0] == ("pigion", "systemctl --user is-active wake-server.service")


def test_system_scope_has_no_user_flag(dmap) -> None:
    boxes = FakeBoxes({"pigion": lambda c: (0, "active")})
    probe(Duty(id="pigion/a", device="pigion", title="a",
               probe={"type": "unit", "unit": "apache2.service", "scope": "system"}), dmap, boxes)
    assert boxes.ran == [("pigion", "systemctl is-active apache2.service")]


def test_a_duty_without_a_probe_is_never_called_verified(dmap) -> None:
    d = Duty(id="arch/phone", device="arch", title="something physical")
    assert d.state == "unprobed"
    assert probe(d, dmap, FakeBoxes({}))["state"] == "unprobed"


def _stat_box(mtime):
    return lambda command: (0, str(mtime)) if mtime is not None else (0, "missing")


def test_freshest_follows_the_newest_copy(dmap) -> None:
    keyring = {"type": "freshest", "path": "~/.local/share/keyrings/login.keyring",
               "among": ["arch", "archserver"]}
    d = Duty(id="arch/keyring", device="arch", title="freshest keyring", kind="data", probe=keyring)
    # His example: the laptop's is from 10-28, archserver's from 08-27.
    fresh = FakeBoxes({"arch": _stat_box(1793050000), "archserver": _stat_box(1787000000)})
    assert probe(d, dmap, fresh)["state"] == "verified"
    stale = FakeBoxes({"arch": _stat_box(1787000000), "archserver": _stat_box(1793050000)})
    got = probe(d, dmap, stale)
    assert got["state"] == "contradicted" and "archserver has a newer copy" in got["detail"]
    # The laptop asleep: archserver's copy says nothing about whether arch's is newer.
    asleep = FakeBoxes({"archserver": _stat_box(1787000000)})
    assert probe(d, dmap, asleep)["state"] == "unknown"
    # Missing on the claimed device, present elsewhere: contradicted.
    missing = FakeBoxes({"arch": _stat_box(None), "archserver": _stat_box(1787000000)})
    assert probe(d, dmap, missing)["state"] == "contradicted"
    # The path is expanded on the device, not here.
    assert '"$HOME"/' in fresh.ran[0][1]


def test_check_reports_transitions_and_scopes_to_devices(dmap) -> None:
    dmap.claim(Duty(id="pigion/w", device="pigion", title="w", probe={"type": "unit", "unit": "w.service"}))
    dmap.claim(Duty(id="arch/x", device="arch", title="x", probe={"type": "unit", "unit": "x.service"}))
    boxes = FakeBoxes({"pigion": _unit_box({"w.service"})})
    changes = {d.id: (b, a) for d, b, a in check(dmap, runner=boxes)}
    assert changes == {"pigion/w": ("unknown", "verified"), "arch/x": ("unknown", "unknown")}
    only = check(dmap, ["pigion"], runner=FakeBoxes({"pigion": _unit_box(set())}))
    assert [(d.id, a) for d, _b, a in only] == [("pigion/w", "contradicted")]
    with pytest.raises(RegistryError):
        check(dmap, ["ghost"], runner=boxes)


def test_where_answers_which_box_runs_it(dmap) -> None:
    dmap.claim(Duty(id="archserver/cvoiced", device="archserver", title="hotline voice daemon",
                    probe={"type": "unit", "unit": "cvoiced.service"}))
    dmap.claim(Duty(id="pigion/wake-server", device="pigion", title="wake scheduler server"))
    assert [d.id for d in dmap.where("cvoiced")] == ["archserver/cvoiced"]
    assert [d.id for d in dmap.where("wake")] == ["pigion/wake-server"]
    assert [d.id for d in dmap.where("pigion wake")] == ["pigion/wake-server"]
    assert dmap.where("nothing-like-this") == []


def test_discover_hides_base_units_and_claimed_ones(dmap) -> None:
    dmap.claim(Duty(id="pigion/w", device="pigion", title="w",
                    probe={"type": "unit", "unit": "wake-server.service"}))
    listing = {
        "--user": "wake-server.service loaded active running x\nguarana.service loaded active running y\n"
                  "dbus.service loaded active running z\n",
        "system": "apache2.service loaded active running a\nsshd.service loaded active running b\n",
    }
    boxes = FakeBoxes({"pigion": lambda c: (0, listing["--user" if "--user" in c else "system"])})
    assert discover(dmap, "pigion", runner=boxes) == ["system apache2.service", "user guarana.service"]


# ---- the admin channel ----------------------------------------------------


def test_render_and_publish_carry_both_sections(dmap, registry, monkeypatch) -> None:
    dmap.claim(Duty(id="pigion/w", device="pigion", title="wake scheduler server",
                    probe={"type": "unit", "unit": "w.service"},
                    last={"state": "contradicted", "at": "2026-10-28T21:00:00Z", "detail": "w.service: inactive"}))
    dmap.save()
    text = "\n".join(render(dmap))
    assert "Device map — 3 devices, 1 duties" in text and "❌ wake scheduler server" in text
    assert "inactive" in text

    monkeypatch.setenv("HOTLINE_REGISTRY_DEVICES", str(dmap.path))
    posted: list[str] = []

    def fake(method, path, token, body=None):
        if method == "GET" and path.endswith("@me"):
            return {"id": "bot"}
        if method == "GET":
            return []
        if method == "POST":
            posted.append(body["content"])
        return {}

    monkeypatch.setattr(admin, "_request", fake)
    assert admin.publish(registry, channel_id="1", token="t") == len(posted) >= 1
    joined = "\n".join(posted)
    assert "Contact registry" in joined and "Device map" in joined
    assert joined.index("Contact registry") < joined.index("Device map")


def test_a_broken_map_does_not_stop_the_roster(registry, tmp_path, monkeypatch) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    monkeypatch.setenv("HOTLINE_REGISTRY_DEVICES", str(bad))
    posted: list[str] = []
    monkeypatch.setattr(admin, "_request", lambda m, p, t, b=None: (
        {"id": "bot"} if p.endswith("@me") else [] if m == "GET" else posted.append(b["content"]) or {}))
    assert admin.publish(registry, channel_id="1", token="t") >= 1
    assert "Contact registry" in posted[0]


# ---- the CLI, end to end on this machine ----------------------------------


def test_cli_claim_check_where(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(admin, "publish", lambda *a, **k: 1)
    m = str(tmp_path / "d.json")
    assert main(["devices", "--map", m, "add-device", "here", "--local", "--kind", "desktop"]) == 0
    assert main(["devices", "--map", m, "claim", "here", "ok", "thing that holds", "--command", "true"]) == 0
    assert main(["devices", "--map", m, "claim", "here", "bad", "thing that does not", "--command", "false"]) == 0
    assert main(["devices", "--map", m, "claim", "here", "manual", "a physical thing"]) == 0
    capsys.readouterr()
    assert main(["devices", "--map", m, "check"]) == 1  # one contradicted
    out = capsys.readouterr().out
    assert "here/ok" in out and "verified" in out and "here/bad" in out and "contradicted" in out
    assert main(["devices", "--map", m, "where", "holds", "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert [h["id"] for h in hits] == ["here/ok"] and hits[0]["state"] == "verified"
    assert main(["devices", "--map", m, "where", "nothing"]) == 1
    assert main(["devices", "--map", m, "claim", "here", "x", "y", "--command", "true", "--dry-run"]) == 0
    assert "here/x" not in DeviceMap(m).duties
    assert main(["devices", "--map", m, "claim", "nowhere", "x", "y"]) == 1


def test_check_republishes_only_when_something_changed(tmp_path, monkeypatch) -> None:
    """A daily check that changed nothing must not ping him every day."""
    calls: list[int] = []
    monkeypatch.setattr(admin, "publish", lambda *a, **k: calls.append(1) or 1)
    monkeypatch.setenv("HOTLINE_REGISTRY_DEVICES", str(tmp_path / "default.json"))
    flag = tmp_path / "up"
    flag.write_text("")
    assert main(["devices", "add-device", "here", "--local"]) == 0
    assert main(["devices", "claim", "here", "f", "flag exists", "--command", f"test -e {flag}"]) == 0
    calls.clear()
    assert main(["devices", "check"]) == 0   # unknown -> verified: publish
    assert main(["devices", "check"]) == 0   # verified -> verified: quiet
    assert len(calls) == 1
    flag.unlink()
    assert main(["devices", "check"]) == 1   # verified -> contradicted: publish
    assert len(calls) == 2
