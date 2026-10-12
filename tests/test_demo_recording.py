"""The README's recorded demo must show what `memdebug demo` really prints, so it cannot go stale again.

`docs/demo.cast` is a recording of the demo, `docs/demo.gif` is drawn from it (both by `packaging/make_demo.py`), and the README says "that is the output of
`memdebug demo`". The 0.3.0 recording and its caption stayed as they were through six releases; these tests fail the day the output changes. What differs at every
run (the throwaway folder, git hashes, the backup id) is ignored."""
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_demo", ROOT / "packaging" / "make_demo.py")
make_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_demo)

REGENERATE = "Record it again with: python packaging/make_demo.py cast   and draw it with: python packaging/make_demo.py gif   (see docs/releasing.md)"


def recorded_lines():
    return (ROOT / "docs" / "demo.cast").read_text(encoding="utf-8").splitlines()


def real_output(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONUTF8="1")
    done = subprocess.run([sys.executable, "-m", "memdebug", "demo"], capture_output=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.decode("utf-8")


def test_the_recording_shows_what_the_demo_prints_now(tmp_path):
    recorded = make_demo.normalise(make_demo.cast_text(recorded_lines())).splitlines()
    real = make_demo.normalise(real_output(tmp_path)).splitlines()
    difference = "\n".join(difflib.unified_diff(recorded, real, "docs/demo.cast", "memdebug demo", lineterm="", n=1))
    assert recorded == real, f"docs/demo.cast no longer matches the output of `memdebug demo`:\n{difference}\n{REGENERATE}"


def test_the_gif_was_drawn_from_this_recording():
    found = re.search(rb"memdebug demo recording sha256:([0-9a-f]{64})", (ROOT / "docs" / "demo.gif").read_bytes())
    wanted = make_demo.digest(make_demo.cast_text(recorded_lines()))
    assert found is not None, f"docs/demo.gif does not say which recording it was drawn from. {REGENERATE}"
    assert found.group(1).decode("ascii") == wanted, f"docs/demo.gif was drawn from another recording than docs/demo.cast. {REGENERATE}"


def test_the_recording_is_a_well_formed_cast_without_a_version_number_to_go_stale():
    lines = recorded_lines()
    header = json.loads(lines[0])
    assert header["version"] == 2 and header["width"] > 0 and header["height"] > 0
    assert not re.search(r"\d+\.\d+\.\d+", header["title"]), "a version in the title would be wrong again at the next release"
    times = []
    for line in lines[1:]:
        when, kind, text = json.loads(line)
        assert kind == "o" and isinstance(text, str)
        times.append(when)
    assert times and times == sorted(times)


def test_the_recording_holds_no_real_folder_name_of_a_person():
    text = make_demo.cast_text(recorded_lines())
    assert make_demo.FAKE_FOLDER in text and not re.search(r"(?i)\\users\\|/home/|/users/", text)


def test_the_readme_caption_does_not_name_a_version_of_the_demo():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    caption = next(line for line in readme if line.startswith("That is") and "memdebug demo" in line)
    assert not re.search(r"\d+\.\d+\.\d+|as released", caption), "the caption of the recording must not name a version that will be old at the next release"
