"""
Background work queues.

Two separate queues, each with one worker:

  ingest   decoding an uploaded file into wav. I/O bound and quick.
  process  transcribe then summarize. CPU or GPU bound and slow.

They are separate so that dropping a file in while a lecture is transcribing
does not leave the upload sitting untouched for twenty minutes. Each queue
still runs one job at a time: whisper holds a model in memory, so two
concurrent transcriptions are slower than two sequential ones, and two
concurrent extractions just fight over the disk.
"""

import itertools
import queue
import threading
import time
from pathlib import Path


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind, stem, payload, label=None):
        self.id = next(Job._ids)
        self.kind = kind          # "ingest" | "process"
        self.stem = stem          # display name; for ingest this is the filename
        self.payload = payload    # whatever the runner needs
        self.label = label
        self.status = "queued"    # queued | running | done | failed | cancelled
        self.pct = 0.0
        self.message = "Waiting"
        self.error = None
        self.queued_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.result = None

    def as_dict(self):
        elapsed = None
        if self.started_at:
            elapsed = round((self.finished_at or time.time()) - self.started_at, 1)
        return {
            "id": self.id,
            "kind": self.kind,
            "stem": self.stem,
            "label": self.label,
            "status": self.status,
            "pct": self.pct,
            "message": self.message,
            "error": self.error,
            "elapsed": elapsed,
            "result": self.result,
        }


class JobQueue:
    """A single-worker queue. `runner(job, progress)` does the actual work."""

    def __init__(self, kind, runner, keep=40):
        self.kind = kind
        self.runner = runner
        self.keep = keep
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._worker = None

    def start(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, daemon=True, name=f"{self.kind}-worker"
            )
            self._worker.start()

    def submit(self, stem, payload, label=None, dedupe=True):
        with self._lock:
            if dedupe:
                for job in self._jobs.values():
                    if job.stem == stem and job.status in ("queued", "running"):
                        return job  # already in flight

            job = Job(self.kind, stem, payload, label=label)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()

        self._q.put(job.id)
        self.start()
        return job

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == "queued":
                job.status = "cancelled"
                job.message = "Cancelled"
                job.finished_at = time.time()
            return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def all(self):
        with self._lock:
            return [self._jobs[i].as_dict() for i in self._order if i in self._jobs]

    def _trim(self):
        finished = [
            i for i in self._order
            if self._jobs[i].status in ("done", "failed", "cancelled")
        ]
        while len(self._order) > self.keep and finished:
            victim = finished.pop(0)
            self._order.remove(victim)
            self._jobs.pop(victim, None)

    def _run(self):
        while True:
            job_id = self._q.get()
            job = self.get(job_id)
            if job is None or job.status == "cancelled":
                self._q.task_done()
                continue

            job.status = "running"
            job.started_at = time.time()
            job.message = "Starting"

            def progress(message, pct, _job=job):
                _job.message = (message or "").strip() or _job.message
                if pct is not None:
                    _job.pct = pct

            try:
                job.result = self.runner(job, progress)
                job.status = "done"
                job.pct = 100.0
                job.message = "Done"
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = "Failed"
            finally:
                job.finished_at = time.time()
                self._q.task_done()


class Queues:
    """The two queues plus the handoff between them."""

    def __init__(self, root=".", recordings_dir=None, auto_queue=True):
        self.root = root
        self.recordings_dir = recordings_dir
        self.auto_queue = auto_queue
        self.ingest = JobQueue("ingest", self._run_ingest)
        self.process = JobQueue("process", self._run_process)

    def start(self):
        self.ingest.start()
        self.process.start()

    def all(self):
        """Both queues, oldest first, so the UI can render one list."""
        jobs = self.ingest.all() + self.process.all()
        jobs.sort(key=lambda j: j["id"])
        return jobs

    def cancel(self, job_id):
        return self.ingest.cancel(job_id) or self.process.cancel(job_id)

    def submit_upload(self, temp_path, filename, label=None, auto_queue=None):
        payload = {
            "temp_path": str(temp_path),
            "filename": filename,
            "label": label,
            "auto_queue": self.auto_queue if auto_queue is None else auto_queue,
        }
        # No dedupe: uploading the same filename twice is a legitimate thing to do.
        return self.ingest.submit(filename, payload, label=label, dedupe=False)

    def submit_process(self, stem, audio_path, model_size="auto"):
        return self.process.submit(
            stem, {"audio_path": str(audio_path), "model_size": model_size}
        )

    # -- runners -------------------------------------------------------

    def _run_ingest(self, job, progress):
        from lecture_agent import ingest as ingest_mod

        payload = job.payload
        temp_path = Path(payload["temp_path"])
        try:
            result = ingest_mod.ingest(
                temp_path,
                self.recordings_dir,
                label=payload.get("label"),
                original_name=payload["filename"],
                keep_original=True,
                progress=progress,
            )
        finally:
            # On success the original was moved out; on failure it is still
            # sitting in .uploads and nothing will ever come back for it.
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        job.stem = result["stem"]
        if payload.get("auto_queue"):
            queued = self.submit_process(result["stem"], result["wav_path"])
            result["queued_job"] = queued.id
            progress("Extracted, queued for transcription", 100)
        return result

    def _run_process(self, job, progress):
        from lecture_agent import pipeline

        return pipeline.process(
            job.payload["audio_path"],
            model_size=job.payload["model_size"],
            root=self.root,
            progress=progress,
        )
