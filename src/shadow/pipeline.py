import argparse
import logging
from datetime import datetime, timezone

from shadow import match, quality, score
from shadow.collect import google, registry, sodra
from shadow.config import DB_PATH

log = logging.getLogger("shadow.pipeline")


def run() -> str:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log.info("Starting run %s", run_id)

    log.info("Registry rows: %s", registry.load_registry())
    log.info("Sodra rows: %s", sodra.load_sodra())
    log.info("Google listings: %s", google.collect_listings(run_id))
    log.info("Matched: %s", match.match_listings(run_id))
    log.info("Flagged leads: %s", score.score_leads(run_id))
    log.info("DQ passed: %s", quality.run_checks(run_id))

    log.info("Run %s finished", run_id)
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Shadow-economy monthly pipeline")
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help="Do nothing if a database from a previous run already exists",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.skip_if_exists and DB_PATH.exists():
        log.info("Existing database found at %s, skipping pipeline", DB_PATH)
        return
    run()


if __name__ == "__main__":
    main()
