"""MLflow 3.18.0's tracking server on SQLite, on loopback, for chapter 5's examples (plan section 6; K-2, K-5).

The server binds port 0, so the port is only in its log: `start` reads it from there and never prints it. Telemetry is
off, so nothing leaves the machine. The database and the artifact folder sit beside the log in the caller's temporary
folder, not in the working copy. `stop` signals the whole process group, since MLflow starts worker processes that a
signal to the parent alone leaves running (K-5).
"""
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

os.environ["MLFLOW_DISABLE_TELEMETRY"] = "true"  # set before any import of mlflow, here and in the examples
os.environ["DO_NOT_TRACK"] = "true"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "true"  # its import-time hint line goes to stderr

import mlflow  # noqa: E402  (the environment is set above, before the import)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from lib.otlp import loopback_only


_PORT_LINE = re.compile(r"http://127\.0\.0\.1:(\d+)")
POLL_S = 0.25
STARTUP_POLLS = 960  # 240 s of polling: the probe saw 9.8 to 20.3 s to start; the limit leaves room


def _port_from_log(log, process):
    """Wait until the server's log names its port, and return it. Raises if the server exits first.

    Why a count, not a clock: the wait is bounded by how many times it polls, so nothing here reads the time.
    """
    for _ in range(STARTUP_POLLS):
        match = _PORT_LINE.search(log.read_text(encoding="utf-8", errors="replace"))
        if match:
            return int(match.group(1))
        if process.poll() is not None:
            raise RuntimeError(f"the MLflow server exited with status {process.returncode} before it listened")
        time.sleep(POLL_S)
    raise RuntimeError("the MLflow server did not report its address in time")


def _healthy(port, process):
    """Wait until /health answers 200, which comes after the database is migrated."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, no proxy
    for _ in range(STARTUP_POLLS):
        if process.poll() is not None:
            raise RuntimeError(f"the MLflow server exited with status {process.returncode} during startup")
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
                if reply.status == 200:
                    return
        except OSError:
            pass  # not listening yet
        time.sleep(POLL_S)
    raise RuntimeError("the MLflow server did not answer /health in time")


def start(db):
    """Start a server on the SQLite file `db` and return (process, port) once it answers /health.

    The caller passes a path inside its temporary folder. The server's log and its artifacts go beside it.
    """
    loopback_only()
    db = Path(db).resolve()
    folder = db.parent
    log = folder / "mlflow-server.log"
    command = [
        sys.executable, "-m", "mlflow", "server",
        "--backend-store-uri", f"sqlite:///{db}",
        "--default-artifact-root", str(folder / "artifacts"),  # keeps mlruns/ out of the working copy
        "--host", "127.0.0.1", "--port", "0",
    ]
    env = {**os.environ, "MLFLOW_DISABLE_TELEMETRY": "true", "DO_NOT_TRACK": "true",
           "MLFLOW_DISABLE_AGENT_HINT": "true"}
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=str(folder),
                                   start_new_session=True)  # its own process group, for stop()
    port = _port_from_log(log, process)
    _healthy(port, process)
    return process, port


def stop(process):
    """Stop the server's whole process group with SIGINT, then SIGKILL anything left in it, and wait for the leader.

    Why the group: MLflow's job consumers are children of the server. Signalling only the parent leaves them running.
    """
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    try:
        os.killpg(process.pid, signal.SIGKILL)  # members that outlived the leader, if any
    except ProcessLookupError:
        pass


def _decoded(value):
    """One attribute value: MLflow stores each as JSON text, so 1000 comes back as "1000" (K-3)."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def stored_spans(traces):
    """Return the spans of every trace in a search_traces result, as plain dicts with hex IDs and decoded attributes.

    Why: MLflow returns span and trace IDs base64-encoded and attribute values as JSON text (K-3, B-57). The dicts
    keep MLflow's own mlflow.* attributes too, so a caller can see what the backend added.
    """
    spans = []
    for row in traces.itertuples():
        for span_row in row.spans:
            parent = span_row["parent_span_id"]
            spans.append({
                "trace": base64.b64decode(span_row["trace_id"]).hex(),
                "span": base64.b64decode(span_row["span_id"]).hex(),
                "parent": base64.b64decode(parent).hex() if parent else None,
                "name": span_row["name"],
                "attributes": {key: _decoded(value) for key, value in span_row["attributes"].items()},
            })
    return spans


@contextmanager
def running(folder, experiment_name):
    """Start a server in `folder`, make one experiment, and yield (tracking URI, experiment ID); stop it on exit.

    The client is pointed at the server here too, so search_traces reads the same store the OTLP route writes to.
    """
    process, port = start(Path(folder) / "mlflow.db")
    try:
        uri = f"http://127.0.0.1:{port}"
        mlflow.set_tracking_uri(uri)
        yield uri, mlflow.MlflowClient(tracking_uri=uri).create_experiment(experiment_name)
    finally:
        stop(process)


def trace_usage(traces):
    """The first trace's token usage from its metadata, as a dict: input, output, total and cache read counts (K-3)."""
    usage = json.loads(traces.iloc[0]["trace_metadata"]["mlflow.trace.tokenUsage"])
    return {"input": usage["input_tokens"], "output": usage["output_tokens"], "total": usage["total_tokens"],
            "cache_read": usage["cache_read_input_tokens"]}


def otlp_exporter(uri, experiment):
    """An OTLP/HTTP exporter that sends to this server, with the header that names the experiment (S9 in the research file).

    Why the header: MLflow's OTLP route requires it, and the spans land in the experiment it names.
    """
    return OTLPSpanExporter(endpoint=f"{uri}/v1/traces", headers={"x-mlflow-experiment-id": experiment})
