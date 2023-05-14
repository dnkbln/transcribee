import subprocess
from pathlib import Path
from typing import Callable

import ffmpeg


def get_duration(input_path: Path):
    return float(ffmpeg.probe(input_path)["format"]["duration"])


def reencode(
    input_path: Path,
    output_path: Path,
    output_params: dict[str, str],
    progress_callback: Callable[[float, dict[str, str]], None],
    duration: float,
) -> float:
    cmd: subprocess.Popen = (
        ffmpeg.input(input_path)
        .output(
            filename=output_path,
            map="0:a",
            loglevel="quiet",
            stats=None,
            progress="-",
            map_metadata="-1",
            **output_params
        )
        .run_async(pipe_stdout=True)
    )
    assert cmd.stdout
    raw_line: bytes
    progress_dict = {}
    for raw_line in cmd.stdout:
        key, value = raw_line.decode().strip().split("=", maxsplit=1)
        progress_dict[key] = value.strip()

        if key == "progress":
            if "out_time_ms" in progress_dict:
                out_time_ms = int(progress_dict["out_time_ms"])
                out_time_s = out_time_ms / 1e6
                progress_callback(
                    out_time_s / duration,
                    extra_data=progress_dict,
                )
            progress_dict = {}

    return duration