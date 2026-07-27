"""
audio_mux.py
------------
Mux an audio track onto a video file using ffmpeg (bundled via imageio-ffmpeg,
a pure-pip dependency — no system package install needed on the host).

Meta's Graph API has no way to attach Instagram/Facebook's licensed music
catalog to a post programmatically, so "adding music" means baking a chosen
audio track directly into the video file before it's uploaded to Meta.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg


def mux_audio_into_video(video_bytes: bytes, audio_bytes: bytes) -> bytes:
    """
    Replace a video's audio track with the given audio, trimmed to whichever
    of the two is shorter. Returns the muxed video's bytes (MP4).
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = Path(tmpdir) / "input_video.mp4"
        audio_path = Path(tmpdir) / "input_audio"
        output_path = Path(tmpdir) / "output.mp4"

        video_path.write_bytes(video_bytes)
        audio_path.write_bytes(audio_bytes)

        result = subprocess.run(
            [
                ffmpeg_exe, "-y",
                "-i", str(video_path),
                "-i", str(audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                str(output_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"ffmpeg audio mux failed: {stderr}")

        return output_path.read_bytes()
