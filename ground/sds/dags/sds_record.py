"""Phase D, the file seam: the payload's own record of an episode, requested, downlinked and checked against L1.

    sds_record_watch (every 2 minutes)  -> one sds_record run per recent episode, run id rec__<episode id>
    sds_record:  plan_request -> send_SendFile_command -> wait_for_downlinked_file -> ingest_record -> compare_with_l1
                                                                               \\-> report_downlink_events (always)

This is the one place the data system commands anything, and the only command is FileDownlink's SendFile: the
task that sends it is named for it. It is off unless scripts/sds.sh runs with DOOMSAT_SDS_RECORDS=on, and it
only finds a file if the flight runs with RECORDS=on (payload/episode_record.py).

Only episodes whose payload ran with --records on (the episode's cataloged context says so) and that closed in
the last two hours are asked for: any other episode has no record to send, and the payload names records by
episode and last tic, with the episode number restarting with the payload, so a much older request could find a
different file (the comparison would say so, but there is no point asking).

The command is never retried by itself: a failed run is re-requested by hand (clear it in the UI). The tasks
around it retry, and report_downlink_events says why no file came: a FileOpenError on board (record_unavailable),
or a FileSent whose file never reached the mirror (record_not_mirrored, e.g. a full fprimeFilesIn bucket).
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ground/sds, however Airflow was started

import pendulum
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.standard.sensors.filesystem import FileSensor
from airflow.sdk import dag, task

ENABLED = os.environ.get("DOOMSAT_SDS_RECORDS", "off") == "on"
# for the idempotent tasks, not the command; the timeout turns a hung Yamcs read into a retry
RETRY = {"retries": 2, "retry_delay": timedelta(seconds=20), "execution_timeout": timedelta(minutes=15)}
MAX_AGE_S = 2 * 3600
REQUESTABLE = ("died", "level_finished", "reset")      # an interrupted episode never wrote a record


@dag(
    dag_id="sds_record_watch",
    schedule=timedelta(minutes=2),
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["doomsat-sds", "phase-d"],
    doc_md=__doc__,
)
def sds_record_watch():
    @task
    def episodes_without_record() -> list[dict]:
        if not ENABLED:
            print("record requests are off (DOOMSAT_SDS_RECORDS=on to enable)")
            return []
        from doomsat_sds import pipeline
        _, _, catalog = pipeline.open_env(archive=False)
        cutoff = pipeline.now_ms() - MAX_AGE_S * 1000
        with catalog:
            todo = [e["episode_id"] for e in catalog.episodes()
                    if e["outcome"] in REQUESTABLE and e["closing_ms"] >= cutoff
                    and e["context"].get("records") == "on"
                    and catalog.current(e["episode_id"], "l1_episode") is not None
                    and catalog.current(e["episode_id"], "l0_record") is None]
        print("records to ask for: %s" % todo)
        return [{"trigger_run_id": "rec__" + eid, "conf": {"episode_id": eid}} for eid in todo]

    TriggerDagRunOperator.partial(
        task_id="trigger_record_runs", trigger_dag_id="sds_record", logical_date=None,
        skip_when_already_exists=True, wait_for_completion=False,
    ).expand_kwargs(episodes_without_record())


@dag(
    dag_id="sds_record",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,                  # FprimeFilePacketService handles one transfer at a time
    tags=["doomsat-sds", "phase-d"],
    doc_md=__doc__,
)
def sds_record():
    @task(multiple_outputs=True, **RETRY)   # so the sensor can take p["mirror_path"] as its own XCom key
    def plan_request(**context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            p = record.plan(settings, catalog, context["dag_run"].conf["episode_id"])
        print(p)
        return p

    @task(execution_timeout=timedelta(minutes=2))     # never retried: a command is not sent twice by a machine
    def send_SendFile_command(p: dict) -> dict:
        """The only command the SDS sends: FileHandling.fileDownlink.SendFile(source, dest)."""
        if not ENABLED:
            raise RuntimeError("record requests are off (DOOMSAT_SDS_RECORDS=on)")
        from doomsat_sds import config, record
        out = record.request(config.Settings.from_env(), p)
        print(out)
        return out

    @task(trigger_rule="all_done", **RETRY)
    def report_downlink_events(p: dict, command: dict) -> list:
        """After the wait, whatever its outcome: what FileDownlink said, and whether the file reached the mirror."""
        import os
        from doomsat_sds import pipeline, record
        if not command:
            return []
        settings, archive, catalog = pipeline.open_env()
        events = record.downlink_events(archive, command["issued_ms"] - 2000, pipeline.now_ms() + 5000)
        for e in events:
            print(e)
        sent = any(e["type"] == "FileSent" for e in events)
        arrived = os.path.exists(p["mirror_path"])
        with catalog:
            if not sent:
                catalog.add_finding("record_unavailable", {"command": command, "events": events},
                                    episode_id=p["episode_id"])
            elif not arrived:
                catalog.add_finding("record_not_mirrored", {"command": command, "events": events,
                                                            "mirror": p["mirror_path"]}, episode_id=p["episode_id"])
        return events

    @task(**RETRY)
    def ingest_record(p: dict, command: dict, **context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            ref = record.ingest(settings, catalog, p, command, run_id=context["run_id"])
        ref["bucket_copy_deleted"] = record.forget_downlinked(settings, p)
        return ref

    @task(**RETRY)
    def compare_with_l1(p: dict, l0_ref: dict, **context) -> dict:
        from doomsat_sds import pipeline, record
        settings, _, catalog = pipeline.open_env(archive=False)
        with catalog:
            out = record.compare_and_register(settings, catalog, p, l0_ref, run_id=context["run_id"])
        print(out)
        return out

    p = plan_request()
    command = send_SendFile_command(p)
    wait = FileSensor(task_id="wait_for_downlinked_file", filepath=p["mirror_path"], fs_conn_id="fs_default",
                      deferrable=True, poke_interval=5, timeout=90)
    command >> wait >> report_downlink_events(p, command)
    l0 = ingest_record(p, command)
    wait >> l0
    compare_with_l1(p, l0)


sds_record_watch()
sds_record()
