"""Narration: one Amazon Polly clip per storyboard sentence, cached by content.

    python3 demo/video/tts.py            # writes build/audio/<hash>.mp3 and build/audio/index.json

Each sentence is synthesized separately so its exact length is known; the timeline and the
captions are built from those lengths.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

import boto3
import imageio_ffmpeg

HERE = Path(__file__).parent
AUDIO = HERE / "build" / "audio"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def sentences(board: dict) -> list[str]:
    out: list[str] = []
    for scene in board["scenes"]:
        groups = [scene["say"]] if "say" in scene else scene["steps"].values()
        out += [s["text"] for group in groups for s in group]
    return out


def to_ssml(text: str, pronunciations: dict[str, str]) -> str:
    """Latin-script product names are read by the English voice model, spelled as given."""
    names = sorted(pronunciations, key=len, reverse=True)
    pattern = re.compile("|".join(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])" for n in names))
    parts, pos = [], 0
    for m in pattern.finditer(text):
        parts.append(escape(text[pos : m.start()]))
        parts.append(f'<lang xml:lang="en-US">{escape(pronunciations[m.group(0)])}</lang>')
        pos = m.end()
    parts.append(escape(text[pos:]))
    return f'<speak><prosody rate="96%">{"".join(parts)}</prosody></speak>'


def duration_seconds(path: Path) -> float:
    proc = subprocess.run([FFMPEG, "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)  # noqa: S603
    times = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not times:
        raise RuntimeError(f"cannot read the duration of {path}")
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def main() -> None:
    board = json.loads((HERE / "storyboard.json").read_text())
    voice = board["voice"]
    AUDIO.mkdir(parents=True, exist_ok=True)
    polly = boto3.client("polly", region_name="us-east-1")
    index: dict[str, dict] = {}
    for text in sentences(board):
        ssml = to_ssml(text, board.get("pronunciations", {}))
        key = hashlib.sha256(json.dumps([voice, ssml], sort_keys=True).encode()).hexdigest()[:16]
        path = AUDIO / f"{key}.mp3"
        if not path.exists():
            resp = polly.synthesize_speech(
                Text=ssml,
                TextType="ssml",
                VoiceId=voice["id"],
                Engine=voice["engine"],
                LanguageCode=voice["language"],
                OutputFormat="mp3",
                SampleRate="24000",
            )
            path.write_bytes(resp["AudioStream"].read())
        index[text] = {"file": path.name, "seconds": round(duration_seconds(path), 3)}
    (AUDIO / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))
    total = sum(v["seconds"] for v in index.values())
    print(f"{len(index)} sentences, {total:.1f} s of narration")


if __name__ == "__main__":
    main()
