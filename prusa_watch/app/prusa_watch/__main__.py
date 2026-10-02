import argparse
import logging
import sys

from . import config
from .agent import Agent


def main() -> int:
    ap = argparse.ArgumentParser(prog="prusa_watch")
    ap.add_argument("--config", default="/data/options.json", help="JSON options file")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="never email or pause/stop, just log")
    args = ap.parse_args()

    try:
        cfg = config.load(args.config)
    except (OSError, ValueError, TypeError) as e:
        logging.basicConfig(level=logging.INFO)
        logging.error("Invalid configuration: %s", e)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("botocore", "urllib3", "httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    Agent(cfg, dry_run=args.dry_run).run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
