"""The README's recorded demo: `docs/demo.cast` (the real output of `memdebug demo`) and `docs/demo.gif` (that output, paced for reading).

    python packaging/make_demo.py cast            record `python -m memdebug demo` into docs/demo.cast
    python packaging/make_demo.py gif             draw docs/demo.gif from docs/demo.cast (needs Pillow and a monospace font)

`tests/test_demo_recording.py` fails when the cast no longer matches what `memdebug demo` prints, or when the gif was drawn from another cast, so
the recording cannot go stale unnoticed. The folder name, the git hashes and the backup id change at every run and are ignored by that comparison.
The recording carries no version number for the same reason. Record on Linux (the README says how long a run takes there).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAST = ROOT / "docs" / "demo.cast"
GIF = ROOT / "docs" / "demo.gif"
TITLE = "memdebug demo"
COLUMNS, ROWS = 112, 40
FAKE_FOLDER = "/tmp/memdebug-demo-k3j9x2ab"  # the real folder is random and, on a person's computer, under their own name
COMMENT = "memdebug demo recording sha256:"

_BACKUP = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]+")
_HASH = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b")
_FOLDER = re.compile(r"^(\s*folder: ).*$", re.M)


def normalise(text: str) -> str:
    """The demo's text with what changes at every run replaced: line endings, the throwaway folder, the backup id and git hashes."""
    text = text.replace("\r\n", "\n")
    text = _FOLDER.sub(r"\1<folder>", text)
    text = _BACKUP.sub("<backup>", text)
    return _HASH.sub("<hash>", text)


def cast_text(lines: list[str]) -> str:
    """All the output of a cast file (its lines, header first), as one text."""
    return "".join(json.loads(line)[2] for line in lines[1:])


def digest(text: str) -> str:
    """The fingerprint of a demo output, after `normalise`: what the gif says it was drawn from."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def record() -> list[list[object]]:
    """Run `memdebug demo` and note when each piece of output arrived."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", NO_COLOR="1", COLUMNS=str(COLUMNS))
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-m", "memdebug", "demo"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    events: list[list[object]] = []
    assert process.stdout is not None
    while chunk := os.read(process.stdout.fileno(), 65536):
        text = chunk.decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        events.append([round(time.monotonic() - started, 3), "o", text])
    if process.wait() != 0:
        raise SystemExit("memdebug demo failed; nothing was written")
    return events


def write_cast(path: Path) -> None:
    """Record the demo and write it as an asciinema v2 file."""
    events = record()
    folder = re.compile(r"(?<=folder: )\S[^\r\n]*memdebug-demo-\w+")
    for event in events:
        event[2] = folder.sub(FAKE_FOLDER, str(event[2]))
    header = {"version": 2, "width": COLUMNS, "height": ROWS, "timestamp": int(time.time()), "title": TITLE,
              "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"}}
    lines = [json.dumps(header)] + [json.dumps(event, ensure_ascii=False) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path} ({len(events)} events)")


# -- the gif ------------------------------------------------------------------------------------------------------------------------

BACKGROUND, BAR, TEXT, DIM = (13, 17, 23), (22, 27, 34), (201, 209, 217), (139, 148, 158)
RED, GREEN = (255, 123, 114), (126, 231, 135)
DOTS = ((255, 95, 86), (255, 189, 46), (39, 201, 63))
FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf", "/System/Library/Fonts/Menlo.ttc")


def _colour(line: str) -> tuple[int, int, int]:
    stripped = line.strip()
    if "!!" in line or "PROBLEM" in line:
        return RED
    if stripped.startswith(("ok ", "the ledger: intact")) or stripped.startswith("+"):
        return GREEN
    if stripped.startswith("(") or stripped.startswith("Next:"):
        return DIM
    return TEXT


def _blend(back: tuple[int, int, int], front: tuple[int, int, int], part: float) -> tuple[int, int, int]:
    return (round(back[0] + (front[0] - back[0]) * part), round(back[1] + (front[1] - back[1]) * part), round(back[2] + (front[2] - back[2]) * part))


def _palette() -> list[tuple[int, int, int]]:
    """Every colour a frame can hold: the two backgrounds, and each text or dot colour in eight strengths (the edges of letters are blends)."""
    colours = [BACKGROUND, BAR]
    for front in (TEXT, DIM, RED, GREEN):
        colours += [_blend(BACKGROUND, front, k / 8) for k in range(1, 9)]
    colours += [_blend(BAR, DIM, k / 8) for k in range(1, 9)]
    colours += [_blend(BAR, dot, k / 8) for dot in DOTS for k in range(1, 9)]
    return colours + [BACKGROUND] * (256 - len(colours))


def draw_gif(cast: Path, gif: Path, font_path: str | None) -> None:
    """Draw the cast as an animated terminal: the command is typed, then each piece of output appears and stays long enough to read."""
    from PIL import Image, ImageDraw, ImageFont  # imported here: only drawing needs it

    lines = cast.read_text(encoding="utf-8").splitlines()
    text = cast_text(lines)
    pieces = [json.loads(line)[2] for line in lines[1:]]
    path = font_path or next((f for f in FONTS if Path(f).exists()), None)
    if path is None:
        raise SystemExit("no monospace font found; pass --font FILE.ttf")
    width, height, bar, margin = 940, 710, 31, 22
    font = ImageFont.truetype(path, 13.28)
    row = (height - bar - 14) // ROWS
    comment = (COMMENT + digest(text)).encode("ascii")

    def frame(shown: list[str], typed: str, cursor: bool) -> Image.Image:
        image = Image.new("RGB", (width, height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, bar - 1), fill=BAR)
        for x, colour in zip((22, 44, 66), DOTS):
            draw.ellipse((x - 6, 16 - 6, x + 6, 16 + 6), fill=colour)
        draw.text((width / 2, 16), TITLE, font=font, fill=DIM, anchor="mm")
        y = bar + 8
        prompt = "$ " + typed
        draw.text((margin, y), prompt, font=font, fill=TEXT)
        x_end = margin + font.getlength(prompt)
        for line in shown:
            y += row
            draw.text((margin, y), line, font=font, fill=_colour(line))
        if cursor:
            if not shown:
                draw.rectangle((x_end, y, x_end + 8, y + row - 3), fill=TEXT)
            else:
                draw.rectangle((margin, y + row, margin + 8, y + 2 * row - 3), fill=TEXT)
        return image

    frames: list[Image.Image] = []
    durations: list[int] = []

    def add(image: Image.Image, seconds: float) -> None:
        frames.append(image)
        durations.append(int(seconds * 1000))

    command = "memdebug demo"
    add(frame([], "", True), 0.9)
    for i in range(1, len(command) + 1):
        add(frame([], command[:i], True), 0.07)
    add(frame([], command, True), 0.5)
    shown: list[str] = []
    for piece in pieces:
        shown.extend(piece.replace("\r\n", "\n").split("\n")[:-1] if piece.endswith("\n") else piece.replace("\r\n", "\n").split("\n"))
        count = piece.count("\n")
        add(frame(shown, command, True), min(0.7 + 0.45 * count, 4.0))
    add(frame(shown, command, True), 5.0)
    reference = Image.new("P", (1, 1))
    reference.putpalette([channel for colour in _palette() for channel in colour])
    quantised = [f.quantize(palette=reference, dither=Image.Dither.NONE) for f in frames]  # one fixed palette: no colour is lost to a busy frame
    quantised[0].save(gif, save_all=True, append_images=quantised[1:], duration=durations, loop=0, optimize=True, comment=comment)
    print(f"wrote {gif} ({len(frames)} frames, {gif.stat().st_size // 1024} KB)")


def main() -> None:
    """Command line: `cast` records, `gif` draws."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["cast", "gif"])
    parser.add_argument("--font", help="a monospace .ttf file (gif only)")
    args = parser.parse_args()
    if args.what == "cast":
        write_cast(CAST)
    else:
        draw_gif(CAST, GIF, args.font)


if __name__ == "__main__":
    main()
