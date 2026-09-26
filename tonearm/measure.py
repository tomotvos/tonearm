import statistics
import subprocess
import sys

from .capture import CHUNK_BYTES, arecord_command
from .levels import rms_dbfs


def main(device: str, seconds: float) -> None:
    proc = subprocess.Popen(arecord_command(device) + ["-d", str(int(seconds))], stdout=subprocess.PIPE)
    levels = []
    while chunk := proc.stdout.read(CHUNK_BYTES):
        if len(chunk) == CHUNK_BYTES:
            levels.append(rms_dbfs(chunk))
    proc.wait()
    if not levels:
        sys.exit("no audio captured")
    levels.sort()
    print(f"chunks={len(levels)} min={levels[0]:.1f} p10={levels[len(levels) // 10]:.1f} "
          f"median={statistics.median(levels):.1f} p90={levels[len(levels) * 9 // 10]:.1f} max={levels[-1]:.1f} dBFS")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "plughw:1,0", float(sys.argv[2]) if len(sys.argv) > 2 else 10)
