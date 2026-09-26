import array
import math

SILENCE_DB = -120.0


def rms_dbfs(chunk: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
    if not samples:
        return SILENCE_DB
    mean_square = sum(s * s for s in samples) / len(samples)
    if mean_square == 0:
        return SILENCE_DB
    return max(SILENCE_DB, 10 * math.log10(mean_square / (32768.0 ** 2)))
