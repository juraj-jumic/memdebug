"""A relocation variable that points at a network or device path is refused: looking at it would reach out over the network (found by the security review before 1.0)."""
import os

import pytest

import memdebug.agents as ag

UNC = "\\\\fileserver\\share\\claude"
DEVICE = "\\\\.\\C:\\claude"
LONG_DEVICE = "\\\\?\\C:\\claude"


@pytest.mark.parametrize("value", [UNC, DEVICE, LONG_DEVICE, "//fileserver/share/claude"], ids=["unc", "device", "long-device", "slashes"])
def test_network_and_device_paths_are_never_followed(value):
    assert ag._usable_env_path(value) is False


@pytest.mark.skipif(os.name != "nt", reason="drive-letter paths are a Windows form")
def test_an_ordinary_local_windows_path_still_is_followed():
    assert ag._usable_env_path("D:\\config\\claude") is True and ag._usable_env_path("D:/config/claude") is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX form")
def test_an_ordinary_local_posix_path_still_is_followed():
    assert ag._usable_env_path("/opt/config/claude") is True


def test_a_network_path_in_the_variable_does_not_make_the_scan_look_there(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
    looked = []
    real = ag._real_folder
    monkeypatch.setattr(ag, "_real_folder", lambda path: (looked.append(str(path)), real(path))[1])
    found = ag.scan_agents(home, env={"CLAUDE_CONFIG_DIR": UNC})
    assert [f.agent.key for f in found] == ["claude-code"] and not any(p.startswith("\\\\") or p.startswith("//") for p in looked)
