#!/usr/bin/env python3
"""Victor-first local Resume Studio.

This is deliberately a local companion, not a hosted CV service.  It reads the
radar's public job snapshot and Victor's ignored ``CV/`` directory, then can
ask the installed first-party Codex CLI to work on a private
resume draft using their existing local authentication.

The service has two modes:

* ``strict`` selects only existing, human-approved source bullets and runs
  deterministic layout checks against the canonical one-page resume format.
* ``dream``/``unrestricted`` run a frontier draft, a synthesis pass, and a
  separate Codex Luna multi-role jury. Codex may apply critique in bounded
  revision rounds; critics never mutate or self-grade the plan, and the module
  reports separate quality gates instead of a composite craft score.
* ``generation`` adds a requirement-to-evidence gap pass before drafting. It
  may synthesize new bullets and tailored skill lines from authorized Markdown
  evidence while leaving unsupported requirements visible.

Run with::

    .venv/bin/python scripts/resume_studio.py

Then open http://127.0.0.1:4317/ .  Private run history stays below the
ignored ``CV/.resume_studio/`` directory. The newest primary PDFs are also
copied to the easy-to-find ``CV/tailored/`` folder.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import itertools
import json
import os
import re
import shutil
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import requests

SCRIPT_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from radar.resume_match import (MATCH_VERSION, build_evidence_graph,
                                evidence_context, job_match_hash,
                                posting_eligibility_blocks, score_resume_match)
from radar.company_research import dossier_for
from radar.evidence_review import (BLOCKING_STATUSES, REVIEW_STATUSES,
                                   add_question_hint as save_context_hint,
                                   answer_question as save_context_answer,
                                   dismiss_question_hint as dismiss_context_hint,
                                   load_reviews, review_path, review_summary,
                                   upsert_questions)
from radar.application_agent import (
    add_issue as add_application_issue,
    apply_confirmation as apply_application_confirmation,
    create_session as create_application_session,
    get_session as get_application_session,
    list_issues as list_application_issues,
    plan_form as plan_application_form,
    prepare_review as prepare_application_review,
    public_context as application_context,
    public_sessions as application_sessions,
    record_event as record_application_event,
    save_answer as save_application_answer,
    save_mapping as save_application_mapping,
    store_path as application_store_path,
    verify_submission_page as verify_application_submission_page,
)
from scripts import resume_evaluator
from scripts import resume_projects


ENGINE_SOURCE_PATH = Path(__file__).resolve()
ENGINE_EVALUATOR_SOURCE_PATH = Path(resume_evaluator.__file__).resolve()
ENGINE_RUNTIME_VERSION = "resume-studio-runtime-v4"


def _sha256_file(path: Path) -> str:
    """Return a stable source identity without shelling out to git.

    Resume Studio is commonly started by launchd from a dirty checkout.  A git
    commit is therefore not a sufficient runtime identity: the service can
    use file bytes to track versions.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, UnicodeDecodeError):
        # Fallback for text-based files or when path is a symbolic link chain
        return hashlib.sha256(path.read_text()).hexdigest()


class RequestHandler(BaseHTTPRequestHandler):
    """Serves the Master Board and handles resume studio logic over HTTP."""

    protocol_version = "HTTP/1.1"

    def _send_json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(json.dumps(data, indent=2).encode("utf-8")))
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

    def _send_html(self, html: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(html.encode("utf-8")))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_text(self, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(text.encode("utf-8")))
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_GET(self) -> None:
        """Handle incoming GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        if path == "":
            # Serve the Master Board index (default view)
            self.serve_master_board()
        elif path == "health":
            self._send_json({"status": "healthy", "version": ENGINE_RUNTIME_VERSION})
        elif path == "job/<id>":  # Dynamic path support if needed
            self._send_json({"message": f"Detail view for {path}"})
        elif path == "sessions":
            sessions = application_sessions()
            self._send_json({"sessions": sessions})
        elif path == "issues":
            issues = list_application_issues()
            self._send_json({"issues": issues})
        elif path.startswith("/files"):
            filename = path[len("/files/"):]
            p = SCRIPT_REPO_ROOT / filename
            if p.exists():
                self._send_json({"filename": str(filename), "hash": _sha256_file(p)})
            else:
                self._send_json({"filename": str(filename)}, 404)
        elif path.endswith(".json"):
            raw = self._read_body() if self.headers.get("Content-Length") else "{}"
            try:
                parsed_json = json.loads(raw)
                self._send_json(parsed_json)
            except json.JSONDecodeError:
                self._send_json({"error": "Bad JSON"}, 400)
        else:
            # Fallback to HTML serving
            self.serve_master_board()

    def serve_master_board(self) -> None:
        """Serve the HTML representation of the Master Board."""
        # Build the HTML body with the current job list
        header = "<h1>Resume Studio</h1><div class='board'><h2>Master Board</h2>"
        
        # Get current batch from context or defaults
        batch = 10
        jobs = application_context(limit=batch) if len(application_context(limit=batch)) else []
        
        if jobs:
            rows = []
            for j in jobs:
                rows.append(f"""
                    <div class='row' data-id="{html.escape(str(j.id))}">
                        <span class='company'>{html.escape(str(j.company))}</span>
                        <a href="{html.escape(str(j.url))}">Link</a>
                        <small>{html.escape(str(j.title))}</small>
                        <span class='score'>{html.escape(str(j.score))}</span>
                    </div>
                """)
            body = "".join(rows)
            footer = f"</div><footer>Served by {ENGINE_RUNTIME_VERSION}</footer></html>"
            
            self._send_html(header + body + footer)
        else:
            self._send_html(f"<h1>Master Board</h1><div class='board'><p>No active roles found.</p></div></html>")

    def do_POST(self) -> None:
        """Handle incoming POST requests (e.g., for updates)."""
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        if path == "sync" or path == "":
            # Trigger a sync of the application state
            if application_sessions():
                self._send_json({"action": "sync", "triggered": True})
            else:
                self._send_json({"action": "sync", "triggered": False})
        else:
            self._send_json({"message": f"Received {path}"})

    def log_message(self, format: str, *args) -> None:
        """Log with some context about the engine."""
        message = f"{self.address_string()} - {format % args}"
        print(message)

    def send_file(self, filename: str, mimetype: str = "application/octet-stream") -> None:
        """Send a file directly to the client."""
        filepath = SCRIPT_REPO_ROOT / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(filepath.stat().st_size))
            self.end_headers()
            with open(filepath, "rb") as f:
                self.wfile.write(f.read())


def _run_command(cmd: List[str]) -> Tuple[str, str]:
    """Execute a shell command and return stdout, stderr."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.stdout, proc.stderr


def _main() -> None:
    """Entry point for the Resume Studio server."""
    parser = argparse.ArgumentParser(description="Victor's Local Resume Studio")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address")
    parser.add_argument("--port", type=int, default=4317, help="Port number")
    parser.add_argument("--mode", choices=["strict", "dream", "generation"], default="strict", help="Processing mode")
    
    args = parser.parse_args()
    
    # Set environment variables for child processes
    os.environ["RUMUS_MODE"] = args.mode
    os.environ["RUMUS_HOST"] = args.host
    
    # Initialize context bank
    if application_sessions():
        record_application_event({"type": "server_start", "mode": args.mode})
    
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    server.timeout = 30  # Handle blocking clients better
    
    print(f"Resume Studio listening on http://{args.host}:{args.port}", flush=True)
    
    def shutdown(signum=None, frame=None):
        print("Shutting down...", flush=True)
        server.shutdown()
        server.server_close()
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    # Run the server
    try:
        server.handle_request()  # Handle the initial request if any
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
    except Exception:
        print(traceback.format_exc(), flush=True)


if __name__ == "__main__":
    _main()