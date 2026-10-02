"""Cost, latency, and structured logging.

Every planning run produces a trace record so you can answer 'how much did this
decision cost and how long did it take?'. This is the #11 production-observability
signal. To add hosted tracing later, send these same records to Langfuse.
"""
import json
import logging
import sys
import time

logger = logging.getLogger("aegis")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def log(event: str, **fields):
    """Emit one structured JSON log line."""
    logger.info(json.dumps({"event": event, **fields}))


# Illustrative prices in USD per million tokens. Update to your model's real
# pricing. Keyed by a substring of the model name.
_PRICES = {
    "haiku": (0.80, 4.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
    "_default": (3.00, 15.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _PRICES["_default"]
    for key, prices in _PRICES.items():
        if key in model:
            in_price, out_price = prices
            break
    cost = (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    return round(cost, 6)


class Timer:
    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self._start) * 1000, 1)
