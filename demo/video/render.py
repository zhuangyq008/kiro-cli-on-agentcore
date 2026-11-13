"""Build the demo video from the storyboard, the narration clips and the terminal recordings.

    python3 demo/video/tts.py        # narration (Amazon Polly)
    python3 demo/video/record.py     # terminal recordings (runs real tasks)
    python3 demo/video/render.py     # build/kiro-cli-on-agentcore-demo.mp4

    python3 demo/video/render.py --no-voice      # captions only
    python3 demo/video/render.py --still 42.5 out.png   # one frame, for checking a layout

The page (player.html) is a pure function of time: for every frame this script calls
renderAt(t) in headless Chromium and takes a screenshot. Frames whose picture did not change
are not captured again.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
BUILD = HERE / "build"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FPS = 30
SAMPLE_RATE = 24000

SCENE_LEAD = 0.7  # picture before the first sentence of a scene
SCENE_TAIL = 0.9  # picture after the last sentence
SENTENCE_GAP = 0.35
TYPE_CPS = 40.0  # typing speed, characters per second
TYPE_MIN, TYPE_MAX = 0.6, 3.2
OUTPUT_DELAY = 0.35  # between pressing Enter and the first output
GAP_CAP = 1.1  # a wait longer than this is shortened to this ...
STEP_TAIL = 1.0  # ... and the finished output stays on screen this long


def fmt_duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60} 分 {seconds % 60:02d} 秒" if seconds >= 60 else f"{seconds} 秒"


class Timeline:
    """Lays out sentences and scenes on one clock."""

    def __init__(self, audio: dict[str, dict]) -> None:
        self.audio = audio
        self.captions: list[dict] = []
        self.clips: list[tuple[float, str]] = []

    def say(self, sentences: list[dict], start: float) -> tuple[float, dict[int, float]]:
        """Schedule sentences back to back from `start`; return the end time and reveal times."""
        t = start
        reveals: dict[int, float] = {}
        for s in sentences:
            clip = self.audio[s["text"]]
            if "reveal" in s:
                reveals[s["reveal"]] = round(t - 0.15, 3)
            self.captions.append({"t0": round(t, 3), "t1": round(t + clip["seconds"] + 0.15, 3), "text": s["text"]})
            self.clips.append((t, clip["file"]))
            t += clip["seconds"] + SENTENCE_GAP
        return t - SENTENCE_GAP, reveals


def schedule_step(step: dict, t0: float, narration_end: float) -> dict:
    """Display schedule of one recorded command: typing, then output with long waits shortened."""
    cmd = step["cmd"]
    type_dur = min(TYPE_MAX, max(TYPE_MIN, len(cmd) / TYPE_CPS))
    out_start = t0 + type_dur + OUTPUT_DELAY

    real = [c[0] for c in step["chunks"]]
    gaps = [b - a for a, b in zip([0.0, *real], real, strict=False)]
    shown = [min(g, GAP_CAP) for g in gaps]
    # If the narration for this step runs longer than the shortened output, give the time back to
    # the waits that were shortened, so the output finishes with the narration instead of early.
    spare = (narration_end - 0.5) - (out_start + sum(shown))
    cut = [g - s for g, s in zip(gaps, shown, strict=True)]
    if spare > 0 and sum(cut) > 0:
        ratio = min(1.0, spare / sum(cut))
        shown = [s + c * ratio for s, c in zip(shown, cut, strict=True)]

    outs, t = [], out_start
    for gap, (_, text) in zip(shown, step["chunks"], strict=True):
        t += gap
        outs.append([round(t, 3), text])
    out_end = t if outs else out_start
    result = {
        "id": step["id"],
        "cmd": cmd,
        "t0": round(t0, 3),
        "typeDur": round(type_dur, 3),
        "outs": outs,
        "outEnd": round(out_end, 3),
    }
    saved = step["seconds"] - (out_end - out_start)
    if saved > 3:
        result["badge"] = {
            "t0": round(out_start, 3),
            "t1": round(out_end + STEP_TAIL, 3),
            "text": f"实际耗时 {fmt_duration(step['seconds'])}，等待过程已加速",
        }
    return result


def build_timeline(board: dict, audio: dict[str, dict], casts: dict[str, dict]) -> dict:
    tl = Timeline(audio)
    scenes = []
    t = 0.0
    for scene in board["scenes"]:
        start = t
        entry: dict = {"id": scene["id"], "start": round(start, 3), "reveals": {}}
        t += SCENE_LEAD
        if scene["kind"] == "terminal":
            steps = []
            for step in casts[scene["cast"]]["steps"]:
                narration_end, _ = tl.say(scene["steps"].get(step["id"], []), t)
                shown = schedule_step(step, t, narration_end)
                steps.append(shown)
                t = max(shown["outEnd"] + STEP_TAIL, narration_end + 0.4)
            entry["term"] = {"heading": scene["heading"], "steps": steps}
        else:
            t, entry["reveals"] = tl.say(scene["say"], t)
        t += SCENE_TAIL
        entry["end"] = round(t, 3)
        scenes.append(entry)
    return {"duration": round(t, 3), "scenes": scenes, "captions": tl.captions, "clips": tl.clips}


def write_audio(timeline: dict, path: Path) -> None:
    """Place every narration clip at its start time in one mono WAV track."""
    total = int(timeline["duration"] * SAMPLE_RATE) + SAMPLE_RATE
    track = bytearray(total * 2)
    for start, name in timeline["clips"]:
        pcm = subprocess.run(  # noqa: S603
            [FFMPEG, "-v", "error", "-i", str(BUILD / "audio" / name), "-f", "s16le", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, check=True,
        ).stdout
        offset = int(start * SAMPLE_RATE) * 2
        track[offset : offset + len(pcm)] = pcm
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(track[: total * 2]))


def open_page(playwright, timeline: dict):  # noqa: ANN001, ANN201
    svg = (HERE / "architecture.svg").read_text()
    public = {k: v for k, v in timeline.items() if k != "clips"}
    (BUILD / "timeline.js").write_text(
        f"window.TIMELINE = {json.dumps(public, ensure_ascii=False)};\n"
        f"window.ARCHITECTURE_SVG = {json.dumps(svg, ensure_ascii=False)};\n"
    )
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    page.add_init_script("window.__RENDERER__ = true;")
    page.goto((HERE / "player.html").as_uri())
    page.wait_for_function("typeof window.renderAt === 'function'")
    page.evaluate("document.fonts.ready")
    return browser, page


def render_frames(timeline: dict, frames: Path) -> int:
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    count = int(timeline["duration"] * FPS)
    with sync_playwright() as p:
        browser, page = open_page(p, timeline)
        last_key, last_file, unique = None, None, 0
        for i in range(count):
            key = page.evaluate("t => window.renderAt(t)", i / FPS)
            target = frames / f"{i:06d}.png"
            if key == last_key and last_file is not None:
                os.symlink(last_file.name, target)
            else:
                page.screenshot(path=str(target))
                last_key, last_file, unique = key, target, unique + 1
            if i % 600 == 0:
                print(f"  frame {i}/{count} ({unique} captured)", flush=True)
        browser.close()
    print(f"  {count} frames, {unique} captured")
    return count


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-voice", action="store_true", help="captions only, no narration track")
    ap.add_argument("--still", nargs=2, metavar=("SECONDS", "PNG"), help="render one frame and exit")
    ap.add_argument("--out", default=str(BUILD / "kiro-cli-on-agentcore-demo.mp4"))
    args = ap.parse_args()

    board = json.loads((HERE / "storyboard.json").read_text())
    audio = json.loads((BUILD / "audio" / "index.json").read_text())
    casts = {
        s["cast"]: json.loads((BUILD / "casts" / f"{s['cast']}.json").read_text())
        for s in board["scenes"] if s["kind"] == "terminal"
    }
    timeline = build_timeline(board, audio, casts)
    (BUILD / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1))
    print(f"timeline: {timeline['duration']:.1f} s, {len(timeline['scenes'])} scenes")

    if args.still:
        with sync_playwright() as p:
            browser, page = open_page(p, timeline)
            page.evaluate("t => window.renderAt(t)", float(args.still[0]))
            page.screenshot(path=args.still[1])
            browser.close()
        return

    frames = BUILD / "frames"
    render_frames(timeline, frames)
    cmd = [FFMPEG, "-y", "-v", "error", "-framerate", str(FPS), "-i", str(frames / "%06d.png")]
    if not args.no_voice:
        wav = BUILD / "narration.wav"
        write_audio(timeline, wav)
        # Polly clips are quiet; bring the narration to a usual online-video level.
        cmd += ["-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-t", f"{timeline['duration']:.3f}", args.out]
    subprocess.run(cmd, check=True)  # noqa: S603
    print(f"wrote {args.out} ({Path(args.out).stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
