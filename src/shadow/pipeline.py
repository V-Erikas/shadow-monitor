import argparse
import logging
from datetime import datetime, timezone

from shadow import db, match, quality, score
from shadow.collect import google, registry, sodra
from shadow.config import DB_PATH

log = logging.getLogger("shadow.pipeline")

STEPS = {
    "registry": lambda run_id: registry.load_registry(),
    "sodra": lambda run_id: sodra.load_sodra(),
    "google": google.collect_listings,
    "match": match.match_listings,
    "score": score.score_leads,
    "quality": quality.run_checks,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run(steps: list[str], run_id: str | None = None) -> str:
    run_id = run_id or _utcnow().strftime("%Y%m%dT%H%M%SZ")
    with db.connect() as con:
        db.init_schema(con)
        con.execute(
            """INSERT INTO runs VALUES (?, ?, NULL, 'running')
               ON CONFLICT (run_id) DO UPDATE SET status = 'running', finished_at = NULL""",
            [run_id, _utcnow()],
        )
    log.info("Run %s: steps %s", run_id, ", ".join(steps))

    status = "failed"
    try:
        for name in steps:
            log.info("%s -> %s", name, STEPS[name](run_id))
        status = "success"
    finally:
        with db.connect() as con:
            con.execute("UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?", [_utcnow(), status, run_id])
        log.info("Run %s %s", run_id, status)
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Shadow-economy monthly pipeline")
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help="Do nothing if a database from a previous run already exists",
    )
    parser.add_argument("--steps", nargs="+", choices=list(STEPS), default=list(STEPS))
    parser.add_argument("--run-id", help="Re-run steps for an existing run instead of starting a new one")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.skip_if_exists and DB_PATH.exists():
        log.info("Existing database found at %s, skipping pipeline", DB_PATH)
        return
    run(args.steps, args.run_id)


if __name__ == "__main__":
    main()
