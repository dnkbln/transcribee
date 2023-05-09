import logging
import mimetypes
import shutil
import tempfile
import time
import urllib.parse
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Tuple

import automerge
import requests
import websockets
from pydantic import parse_raw_as
from transcribee_proto.api import AlignTask, AssignedTask
from transcribee_proto.api import Document as ApiDocument
from transcribee_proto.api import SpeakerIdentificationTask, TaskType, TranscribeTask
from transcribee_proto.document import Document as EditorDocument
from transcribee_proto.sync import SyncMessageType
from transcribee_worker.identify_speakers import identify_speakers
from transcribee_worker.torchaudio_align import align
from transcribee_worker.util import load_audio
from transcribee_worker.whisper_transcribe import transcribe_clean


def ensure_timing_invariant(doc: EditorDocument):
    prev_atom = None
    for atom in doc.iter_atoms():
        if prev_atom is not None:
            assert prev_atom.start <= atom.start, f"{prev_atom} < {atom}"
        prev_atom = atom


class Worker:
    base_url: str
    token: str
    tmpdir: Optional[Path]
    task_types: list[TaskType]

    def __init__(
        self,
        base_url: str,
        websocket_base_url: str,
        token: str,
        task_types: Optional[list[TaskType]] = None,
    ):
        self.base_url = base_url
        self.websocket_base_url = websocket_base_url
        self.token = token
        self.tmpdir = None
        if task_types is not None:
            self.task_types = task_types
        else:
            self.task_types = [
                TaskType.IDENTIFY_SPEAKERS,
                TaskType.ALIGN,
                TaskType.TRANSCRIBE,
            ]

    def _get_headers(self):
        return {"authorization": f"Worker {self.token}"}

    def claim_task(self) -> Optional[AssignedTask]:
        logging.info("Asking backend for new task")
        req = requests.post(
            f"{self.base_url}/claim_unassigned_task/",
            params={"task_type": self.task_types},
            headers=self._get_headers(),
        )
        req.raise_for_status()
        return parse_raw_as(Optional[AssignedTask], req.text)

    def _get_tmpfile(self, filename: str) -> Path:
        if self.tmpdir is None:
            raise ValueError("`tmpdir` must be set")
        return self.tmpdir / filename

    def get_document_audio_bytes(
        self, document: ApiDocument
    ) -> Optional[Tuple[bytes, str]]:
        logging.debug(f"Getting audio. {document=}")
        if not document.media_files:
            return
        # TODO: smarter selection of used media (seperate tag?)
        media_file = document.media_files[0]
        file_url = urllib.parse.urljoin(self.base_url, media_file.url)
        response = requests.get(file_url)
        response.raise_for_status()
        return response.content, media_file.content_type

    def get_document_audio(self, document: ApiDocument) -> Optional[BytesIO]:
        b = self.get_document_audio_bytes(document=document)
        if b is not None:
            return BytesIO(b[0])

    def get_document_audio_path(self, document: ApiDocument) -> Optional[Path]:
        b = self.get_document_audio_bytes(document=document)
        if b is not None:
            b, ct = b
            extension = mimetypes.guess_extension(ct)
            path = self._get_tmpfile(f"doc_audio.{extension}")
            with open(path, "wb") as f:
                f.write(b)
            return path

    def keepalive(self, task_id: str, progress: Optional[float]):
        body = {}
        if progress is not None:
            body["progress"] = progress
        logging.debug(f"Sending keepalive for {task_id=}: {body=}")
        req = requests.post(
            f"{self.base_url}/{task_id}/keepalive/",
            json=body,
            headers=self._get_headers(),
        )
        req.raise_for_status()

    async def perform_task(self, task: AssignedTask):
        logging.info(f"Running task: {task=}")

        if task.task_type == TaskType.IDENTIFY_SPEAKERS:
            await self.identify_speakers(task)
        elif task.task_type == TaskType.TRANSCRIBE:
            await self.transcribe(task)
        elif task.task_type == TaskType.ALIGN:
            await self.align(task)
        else:
            raise ValueError(f"Invalid task type: '{task.task_type}'")

    async def _init_doc(self, document_id: str, doc: automerge.Document):
        with automerge.transaction(doc, "Initialize Document") as d:
            if d.children is None:
                d.children = []
            if d.speaker_names is None:
                d.speaker_names = {}

        change = d.get_change()
        if change is not None:
            await self.send_change(document_id, change.bytes())

    async def get_document_state(self, document_id: str) -> automerge.Document:
        doc = automerge.init(EditorDocument)
        params = urllib.parse.urlencode(self._get_headers())
        async with websockets.connect(
            f"{self.websocket_base_url}{document_id}/?{params}"
        ) as websocket:
            while True:
                msg = await websocket.recv()
                if msg[0] == SyncMessageType.CHANGE:
                    automerge.apply_changes(doc, [msg[1:]])
                elif msg[0] == SyncMessageType.CHANGE_BACKLOG_COMPLETE:
                    break
                elif msg[0] == SyncMessageType.FULL_DOCUMENT:
                    doc = automerge.load(msg[1:])

        await self._init_doc(document_id, doc)
        return doc

    async def send_change(self, document_id: str, change: bytes):
        params = urllib.parse.urlencode(self._get_headers())
        async with websockets.connect(
            f"{self.websocket_base_url}{document_id}/?{params}"
        ) as websocket:
            while True:
                msg_type, *_ = await websocket.recv()
                if msg_type == SyncMessageType.CHANGE_BACKLOG_COMPLETE:
                    break

            await websocket.send(change)

    async def transcribe(self, task: TranscribeTask):
        if task.task_type != TaskType.TRANSCRIBE:
            return

        document_audio = self.get_document_audio(task.document)
        if document_audio is None:
            raise ValueError(
                f"Document {task.document} has no audio attached. Cannot transcribe."
            )
        audio = load_audio(document_audio)

        def progress_callback(_ctx, progress, _data):
            self._set_progress(task.id, "whisper", progress=progress / 100)

        doc = await self.get_document_state(task.document.id)

        with automerge.transaction(doc, "Reset Document") as d:
            d.children = []

        change = d.get_change()
        if change is not None:
            await self.send_change(task.document.id, change.bytes())

        async for paragraph in transcribe_clean(
            audio,
            task.task_parameters.model,
            task.task_parameters.lang,
            progress_callback,
        ):
            with automerge.transaction(doc, "Automatic Transcription") as d:
                p = paragraph.dict()
                for c in p["children"]:
                    c["text"] = automerge.Text(c["text"])
                d.children.append(p)

            change = d.get_change()
            if change is not None:
                await self.send_change(task.document.id, change.bytes())

    async def identify_speakers(self, task: SpeakerIdentificationTask):
        document_audio = self.get_document_audio_path(task.document)
        if document_audio is None:
            raise ValueError(
                f"Document {task.document} has no audio attached. Cannot identify speakers."
            )
        audio = load_audio(document_audio)
        doc = await self.get_document_state(task.document.id)

        self._set_progress(task.id, "identify speakers", progress=0)

        def progress_callback(step: str, progress: float):
            self._set_progress(task.id, step, progress=progress)

        with automerge.transaction(doc, "Speaker Identification") as d:
            identify_speakers(audio, d, progress_callback)
        change = d.get_change()
        if change is not None:
            await self.send_change(task.document.id, change.bytes())

        self._set_progress(task.id, "identify speakers", progress=1)

    async def align(self, task: AlignTask):
        document_audio = self.get_document_audio_path(task.document)
        if document_audio is None:
            raise ValueError(
                f"Document {task.document} has no audio attached. Cannot align."
            )
        audio = load_audio(document_audio)
        doc = await self.get_document_state(task.document.id)
        document = EditorDocument.parse_obj(automerge.dump(doc))

        aligned_para_iter = align(
            document,
            audio,
            # TODO(robin): this seems like a weird place to hardcode this parameter
            extend_duration=0.5,
            progress_callback=lambda progress, extra_data: self._set_progress(
                task.id, "torchaudio aligner", progress, extra_data
            ),
        )
        for i, al_para in enumerate(aligned_para_iter):
            with automerge.transaction(doc, "Alignment") as d:
                d_para = d.children[i]
                for d_atom, al_atom in zip(d_para.children, al_para.children):
                    d_atom.start = al_atom.start
                    d_atom.end = al_atom.end

            change = d.get_change()
            if change is not None:
                await self.send_change(task.document.id, change.bytes())

        document = EditorDocument.parse_obj(automerge.dump(doc))

    def mark_completed(self, task_id: str, additional_data: Optional[dict] = None):
        completion_data = {**self._result_data}
        if additional_data:
            completion_data.update(additional_data)
        body = {
            "completion_data": completion_data if completion_data is not None else {}
        }
        logging.debug(f"Marking task as completed {task_id=} {body=}")
        req = requests.post(
            f"{self.base_url}/{task_id}/mark_completed/",
            json=body,
            headers=self._get_headers(),
        )
        req.raise_for_status()

    def _set_progress(
        self, task_id: str, step: str, progress: Optional[float], extra_data: Any = None
    ):
        self._result_data["progress"].append(
            {
                "step": step,
                "progress": progress,
                "extra_data": extra_data,
                "timestamp": time.time(),
            }
        )
        self.keepalive(task_id, progress)

    async def run_task(self, mark_completed=True):
        self.tmpdir = Path(tempfile.mkdtemp())
        task = self.claim_task()

        no_work = False
        self._result_data = {"progress": []}

        try:
            if task is not None:
                task_result = await self.perform_task(task)
                logging.info(f"Worker returned: {task_result=}")
                if mark_completed:
                    self.mark_completed(task.id, {"result": task_result})
            else:
                logging.info("Got no task, not running worker")
                no_work = True
        except Exception as exc:
            logging.warning("Worker failed with exception", exc_info=exc)

        logging.debug(f"Cleaning tmpdir '{self.tmpdir}'")
        shutil.rmtree(self.tmpdir)
        self.tmpdir = None
        logging.debug("run_task() done :)")
        return no_work
