#!/usr/bin/env python

import argparse
import asyncio
import logging
import traceback
import urllib.parse
from multiprocessing import Event, Process
from pathlib import Path

import requests.exceptions
from transcribee_worker.config import settings
from watchfiles import watch

logging.basicConfig(level=logging.INFO)

settings.setup_env_vars()


def main():
    parser = argparse.ArgumentParser(
        description="The worker for the transcribee open source transcription platform"
    )
    parser.add_argument(
        "--coordinator",
        help="url to the task coordinator (aka the transcribee backend)",
        default="http://localhost:8000",
    )
    parser.add_argument(
        "--websocket-base-url",
        help=(
            "url to the websocket sync server (aka the transcribee backend), "
            "default: {coordinator}/sync/"
        ),
        default=None,
    )
    parser.add_argument("--token", help="Worker token", required=True)
    parser.add_argument("--run-once-and-dont-complete", action="store_true")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.websocket_base_url is None:
        sync_url = urllib.parse.urlparse(args.coordinator)
        sync_url = sync_url._replace(path=sync_url.path + "/api/v1/documents/sync/")
        assert sync_url.scheme in ["http", "https"]
        sync_url = sync_url._replace(
            scheme="ws" if sync_url.scheme == "http" else "wss"
        )
        args.websocket_base_url = urllib.parse.urlunparse(sync_url)

    if args.reload:
        path = Path(__file__).parent

        p, event = run_sync_in_process(args)
        for _ in watch(path):
            logging.info("Source code change detected, reloading worker")
            event.set()
            p.join()
            p, event = run_sync_in_process(args)

    else:
        run_sync(args, Event())


def run_sync_in_process(args):
    event = Event()
    p = Process(target=run_sync, args=(args, event))
    p.start()
    return p, event


def run_sync(args, event):
    asyncio.run(run(args, event))


async def run(args, event: Event):
    # Needs to be done after settings.setup_env
    from transcribee_worker.worker import Worker  # noqa

    worker = Worker(
        base_url=f"{args.coordinator}/api/v1/tasks",
        websocket_base_url=args.websocket_base_url,
        token=args.token,
    )
    while not event.is_set():
        try:
            no_work = await worker.run_task(
                mark_completed=not args.run_once_and_dont_complete
            )
            if no_work:
                event.wait(5)
            elif args.run_once_and_dont_complete:
                break
        except requests.exceptions.ConnectionError:
            logging.warn("could not connect to backend")
            event.wait(5)
        except Exception:
            logging.warn(
                f"an error occured during worker execution:\n{traceback.format_exc()}"
            )
            event.wait(5)


if __name__ == "__main__":
    main()
