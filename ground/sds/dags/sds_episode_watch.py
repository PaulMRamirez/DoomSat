"""Phase A trigger: one `sds_forward` run per episode, as soon as the episode's end shows up in Yamcs.

Every minute this reads the episode events (EpisodeStarted, PlayerDied, LevelFinished) over the last six hours,
works out which episodes have closed (doomsat_sds.episodes explains the gaps it allows for), drops the ones
already in the catalog, and triggers `sds_forward` once per remaining episode with
`run_id = fwd__<episode id>`. Triggering a run id that exists is skipped, so an episode is never processed
twice however often it is seen.

Why polling and not an AssetWatcher: Airflow folds watcher events that arrive together into one run, and a run
per episode is the point here. A minute of latency is the price.

Read-only: it reads Yamcs and the catalog and sends nothing.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task

LOOKBACK_S = 6 * 3600


@dag(
    dag_id="sds_episode_watch",
    schedule=timedelta(minutes=1),
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(seconds=15)},
    tags=["doomsat-sds", "phase-a"],
    doc_md=__doc__,
)
def sds_episode_watch():
    @task
    def find_closed_episodes() -> list[dict]:
        from doomsat_sds import pipeline
        settings, archive, catalog = pipeline.open_env()
        with catalog:
            closed = pipeline.watch(settings, archive, catalog, lookback_s=LOOKBACK_S)
        for e in closed:
            print("closed: %s (%s%s)" % (e["episode_id"], e["outcome"], ", inferred" if e["inferred"] else ""))
        return [{"trigger_run_id": "fwd__" + e["episode_id"], "conf": e} for e in closed]

    TriggerDagRunOperator.partial(
        task_id="trigger_forward_runs",
        trigger_dag_id="sds_forward",
        logical_date=None,                 # runs keyed by run id alone, so two can never collide on a date
        skip_when_already_exists=True,
        wait_for_completion=False,
    ).expand_kwargs(find_closed_episodes())


sds_episode_watch()
