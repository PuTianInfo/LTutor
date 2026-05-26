# Import libraries
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
import json
import re
from memory_db import MemoryDB
from ai_provider import AIProvider
from models import StudentEvent
from db import db
from textwrap import dedent


# =============================================================================
# Progress Store (PostgreSQL)
# =============================================================================
# Stores student progress events in PostgreSQL
class StudentProgressDBStore:
    """Stores progress events in Postgres (student_events)."""
    def log(self, user_id: str, assignment_id: str, event: Dict[str, Any]):
        ev = StudentEvent(
            user_id=user_id,
            assignment_id=assignment_id,
            event_type=str(event.get("type") or "event"),
            payload={k: v for k, v in event.items() if k != "type"},
        )
        db.session.add(ev)
        db.session.commit()

    def summarize(self, user_id: str, assignment_id: str) -> Dict[str, Any]:
        # compute minimal summary with one SQL round-trip
        qs = StudentEvent.query.filter_by(
            user_id=user_id, assignment_id=assignment_id
        ).order_by(StudentEvent.created_at.asc())

        out = {
            "questions": 0, "answers": 0, "loops": 0,
            "last_question": None, "last_stage": None,
            "started_at": None, "ended_at": None,
        }
        for ev in qs:
            t = ev.event_type
            p = ev.payload or {}
            if t == "session_start":
                out["started_at"] = p.get("started_at") or ev.created_at.isoformat()
            elif t == "session_end":
                out["ended_at"] = p.get("ended_at") or ev.created_at.isoformat()
                out["loops"] = p.get("loops", out["loops"])
            elif t == "question":
                out["questions"] += 1
                out["last_question"] = p.get("text")
            elif t == "answer":
                out["answers"] += 1
                out["last_stage"] = p.get("stage")
        return out



# =============================================================================
# Student Session (core API)
# =============================================================================
@dataclass
class StudentSession:
    user_id: str
    assignment_id: str
    mdb: MemoryDB
    provider: AIProvider
    retrieval_k: int = 6                  # how many snippets to fetch from MemoryDB per question
    max_scaffold_loops: int = 10          # how many student questions before “full solution” is allowed
    loop_counter: int = 0
    flags: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    _progress: StudentProgressDBStore = field(default_factory=StudentProgressDBStore, repr=False)
    
    student_email: Optional[str] = None

    # Latest code submitted for this session
    submission_code: str = ""
    submission_lang: str = "text"

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    # Begin a new tutoring session and log the session_start event
    def start(self) -> None:
        if not self.assignment_id:
            raise ValueError("No assignment selected")
        self.loop_counter = 0
        self.flags = {"final_unlocked": False}
        self.started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self._progress.log(self.user_id, self.assignment_id, {
            "type": "session_start",
            "started_at": self.started_at
        })

    # End a tutoring session and log final data
    def end(self) -> None:
        self.ended_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self._progress.log(self.user_id, self.assignment_id, {
            "type": "session_end",
            "ended_at": self.ended_at,
            "loops": self.loop_counter
        })
    def _trim(self, s: str, limit: int = 1200) -> str:
        s = (s or "").strip()
        return s if len(s) <= limit else (s[:limit] + "\n... [truncated] ...")

    # Analyze compile/runtime errors using last_run and provide targeted Java debugging advice
    def diagnose(self, question: str = "") -> str:
        """Use latest compile/run output + code snippets to ask the AI for a focused fix."""
        if self.provider.active_backend() != "gemini":
            return "offline"
        lr = getattr(self, "last_run", None)
        if not lr or not (lr.get("javac_errors") or lr.get("stderr")):
            return ("No recent compile/run errors found.\n"
                    "Tip: run your code first (e.g., `run` or `run testExtends.java`).")

        errors = lr.get("javac_errors") or lr.get("stderr") or ""
        files = lr.get("files", {}) or {}

        # include up to 3 .java files, trimmed
        java_files = [(fn, src) for fn, src in files.items() if fn.endswith(".java")]
        java_files.sort()
        snippets = []
        for fn, src in java_files[:3]:
            snippets.append(f"// {fn}\n{self._trim(src, 800)}")

        system = dedent("""\
            You are a senior Java TA. Read the student's latest compiler/runtime output
            and the provided code snippets. Produce a concise, actionable diagnosis:
            • Identify root cause(s) citing exact error lines.
            • Point to the file/lines to fix.
            • Provide a minimal patch (small replacement snippet or diff), not a full rewrite.
            • Do NOT repeat the student's question.
        """).strip()

        snippets_joined = "\n\n".join(snippets) if snippets else "[no code captured]"

        user = dedent(f"""
            Student question: {question or "What's wrong with my code?"}

            Last compile/run output:
            ---- BEGIN ERRORS ----
            {self._trim(errors, 2000)}
            ---- END ERRORS ----

            Key source files (trimmed):
            {snippets_joined}
        """).strip()

        return self.provider.generate(system=system, user=user, max_tokens=650)


    def ask_ai(self, question: str) -> str:
        # Depreciated, references correct ask() to prevent reference failures
        return self.ask(question)

    # -------------------------------------------------------------------------
    # Code intake (file upload or paste)
    # -------------------------------------------------------------------------
    # Merge uploaded files and pasted code; detect language; log summary
    def add_code(self, files: Optional[List[str]] = None, pasted: Optional[str] = None) -> str:
        """
        Accept code from files and/or pasted text. Merges them, detects language,
        logs a summary event, and holds it for the next questions.
        """
        blobs: List[str] = []

        # Read any provided file paths (best-effort, tolerate read errors)
        if files:
            for p in files:
                try:
                    txt = Path(p).read_text(encoding="utf-8", errors="ignore")
                    blobs.append(txt)
                except Exception as e:
                    blobs.append(f"# [read_error] {p}: {e}")

        # Add pasted code (if any)
        if pasted:
            blobs.append(pasted)

        # Merge and store
        merged = "\n\n".join(x for x in blobs if x is not None).strip()
        if not merged:
            return "No code received."

        self.submission_code = merged
        self.submission_lang = self._detect_language_from_text(merged)

        # Log a short summary of the attached code
        summary = self._summarize_code(merged, self.submission_lang)
        self._progress.log(self.user_id, self.assignment_id, {
            "type": "code_attach",
            "lang": self.submission_lang,
            "lines": summary.get("lines", 0),
            "functions": len(summary.get("functions", [])),
            "classes": len(summary.get("classes", []))
        })

        return f"Code attached ({self.submission_lang}, {summary.get('lines', 0)} lines)."

    # -------------------------------------------------------------------------
    # Main tutoring entrypoint
    # -------------------------------------------------------------------------
    def ask(self, question: str) -> str:
        """
        Student Q&A pipeline with:
        • Student-first retrieval, then general assignment materials
        • Hint-first, cite-always, code-later (policy/loop gated)
        """
        # basic guards
        if not question or not question.strip():
            return "Please enter a question."
        if getattr(self, "provider", None) is None or self.provider.active_backend() != "gemini":
            return "offline"

        assignment_id = str(getattr(self, "assignment_id", "") or "").strip()
        has_assignment = bool(assignment_id)

        # policy unlock (default 6)
        unlock_after = 6
        if has_assignment:
            try:
                pol = self.mdb.search(
                    "POLICY: tutor_rules",
                    k=1,
                    where={"assignment_id": assignment_id, "source_type": "policy", "section": "tutor_rules"}
                ) or []
                if pol:
                    m = re.search(r"unlock_after_loops=(\d+)", pol[0].get("text") or "")
                    if m:
                        unlock_after = int(m.group(1))
            except Exception:
                pass

        allow_code = bool(self.flags.get("final_unlocked")) or (self.loop_counter >= unlock_after)

        # retrieval (student-first, then general)
        hits = []
        try:
            k = int(getattr(self, "retrieval_k", 6) or 6)
            if has_assignment:
                where_base = {"assignment_id": assignment_id}

                student_hits = []
                if getattr(self, "student_email", None):
                    where_student = dict(where_base)
                    where_student["source_type"] = "student_upload"
                    where_student["tags"] = [self.student_email]
                    try:
                        student_hits = self.mdb.search(question, k=4, where=where_student) or []
                    except Exception:
                        student_hits = []

                general_hits = self.mdb.search(question, k=max(6, k), where=where_base) or []

                # merge, de-dupe (keep student hits first)
                seen, merged = set(), []
                for h in (student_hits + general_hits):
                    key = (h.get("file_path"), h.get("section"), (h.get("text") or "")[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(h)
                hits = merged

                if not hits:  # broad fallback
                    hits = self.mdb.search(question, k=k) or []
            else:
                try:
                    hits = self.mdb.search(question, k=k, where={"namespace": "global"})
                except TypeError:
                    hits = self.mdb.search(question, k=k) or []
        except Exception as e:
            print(f"[Warn] Retrieval failed: {e}")
            hits = []

        # choose and redact snippets per policy
        chosen = self._select_snippets_with_policy(hits, max_snips=4)
        redacted: List[Dict[str, Any]] = [self._redact_code_if_needed(s, allow_code) for s in chosen]

        # summarize student code and quick completeness checks
        code_sig = {}
        compliance: List[str] = []
        if self.submission_code:
            code_sig = self._summarize_code(self.submission_code, self._detect_language_from_text(self.submission_code))
            compliance = self._quick_completeness_checks(self.submission_code, code_sig.get("lang","text"))

        # determine stage and build prompts
        stage = self._decide_stage(self.loop_counter, allow_code)
        system = self._build_system_prompt(stage)
        user = self._build_user_prompt(question, redacted, stage, code_sig, compliance)

        # generate, increment loop, log
        try:
            reply = self.provider.generate(system=system, user=user, max_tokens=700)
            self.loop_counter += 1
            self._progress.log(self.user_id, self.assignment_id, {"type": "answer", "stage": stage})
            return reply
        except Exception as e:
            print(f"[Error] Tutor generation failed: {e}")
            return "I'm having trouble generating a response right now. Please try again."


    # -------------------------------------------------------------------------
    # Public status method (for REPL `student.status`)
    # -------------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """
        Return a merged status view from the JSONL log + live session state.
        """
        s = self._progress.summarize(self.user_id, self.assignment_id)
        s.update({
            "user_id": self.user_id,
            "assignment_id": self.assignment_id,
            "loop_counter": self.loop_counter,
            "final_unlocked": self.flags.get("final_unlocked", False),
            "backend": self.provider.active_backend(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "has_code": bool(self.submission_code),
            "lang": self.submission_lang,
        })
        return s

    # =========================================================================
    # Helper policies and utilities (ranking, redaction, detection, prompts)
    # =========================================================================
    #Turn MemoryDB hits into a compact context string
    def _context_from_hits(self, hits, limit: int = 5) -> str:
        if not hits:
            return ""
        lines = []
        for i, h in enumerate(hits[:limit]):
            try:
                txt = (h.get("text") or "").strip()
            except Exception:
                txt = str(h)
            if txt:
                lines.append(f"[{i+1}] {txt}")
        if not lines:
            return ""
        return "\n".join(lines)
    
    # Rank and filter retrieval hits based on policy buckets
    def _select_snippets_with_policy(self, hits: List[Dict[str, Any]], max_snips: int = 4):
        """
        Rank MemoryDB hits with a simple priority:
          1) approved concepts
          2) working_solution comments
          3) instruction excerpts
          4) working_solution code_no_comments
        """

        def score(h: Dict[str, Any]):
            tags = set(h.get("tags") or [])
            st = h.get("source_type")
            sec = h.get("section")
            w = float(h.get("weight", 0.5))
            base = float(h.get("_score", 0.0))

            if "approved_concept" in tags:
                bucket = 4
            elif st == "working_solution" and (sec == "comments" or "comments" in tags or "benchmark" in tags):
                bucket = 3
            elif st == "instructions" and ("key_requirement" in tags or "approved_concept" in tags):
                bucket = 2
            elif st == "working_solution" and sec == "code_no_comments":
                bucket = 1
            else:
                bucket = 0

            if st == "student_upload":
                w += 0.15

            # Higher bucket wins; within bucket prefer higher weight and score
            return (bucket, w, base)

        ranked = sorted(hits, key=score, reverse=True)

        chosen: List[Dict[str, Any]] = []
        for h in ranked:
            txt = (h.get("text") or "").strip()
            if not txt:
                continue
            chosen.append({
                "text": txt,
                "meta": {
                    "source_type": h.get("source_type"),
                    "section": h.get("section"),
                    "file_path": h.get("file_path"),
                    "tags": h.get("tags") or [],
                    "weight": h.get("weight", 0.5),
                },
            })
            if len(chosen) >= max_snips:
                break

        return chosen

    # Remove code from a snippet when we’re still in “no direct answers” stages
    def _redact_code_if_needed(self, snip: Dict[str, Any], allow_code: bool) -> Dict[str, Any]:
        """
        Heuristics:
          - Strip fenced code blocks ```...```
          - Remove lines that look like function/class/import declarations
          - Remove short code-like lines ending with ';'
        """
        if allow_code:
            return snip

        text = snip["text"]

        # Remove fenced code blocks: ```...```
        text = re.sub(r"```[\s\S]*?```", "", text)

        # Remove indented code blocks (>=4 spaces or tabs across multiple lines)
        text = re.sub(r"(?:^|\n)(?: {4}|\t).*(?:\n(?: {4}|\t).*)+", "\n", text)

        # Filter out obvious code-ish lines
        cleaned: List[str] = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(("def ", "class ", "import ", "from ")):
                continue
            if s.endswith(";") and len(s.split()) <= 6:
                continue
            cleaned.append(ln)

        out = dict(snip)
        out["text"] = "\n".join(cleaned).strip()
        return out

    # Lightweight language detection for Python/C++/Java/JS/HTML
    def _detect_language_from_text(self, code: str) -> str:
        if not code:
            return "text"
        head = code[:500].lower()
        if "import " in head or "def " in head or "__name__" in head:
            return "python"
        if "#include" in head or "int main(" in head:
            return "c_cpp"
        if "public static void main" in head:
            return "java"
        if "function " in head or "console.log" in head:
            return "javascript"
        if "<html" in head or "<!doctype html" in head:
            return "html"
        return "text"

    # Produce a code signature (lines, imports, classes, functions)
    def _summarize_code(self, code: str, lang: str) -> Dict[str, Any]:
        if not code:
            return {}
        lines = code.splitlines()
        info: Dict[str, Any] = {
            "lang": lang,
            "lines": len(lines),
            "imports": [],
            "functions": [],
            "classes": [],
        }

        if lang == "python":
            info["imports"] = re.findall(
                r"^\s*(?:from\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s+import\s+[A-Za-z_]\w*|import\s+[A-Za-z_]\w*)",
                code,
                re.MULTILINE,
            )
            info["functions"] = re.findall(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", code, re.MULTILINE)
            info["classes"] = re.findall(r"^\s*class\s+([A-Za-z_]\w*)\s*[:\(]", code, re.MULTILINE)
        else:
            # Coarse heuristics for non-Python languages
            info["imports"] = [ln for ln in lines if ln.strip().startswith(("import ", "#include"))]
            info["functions"] = re.findall(r"\b([A-Za-z_]\w*)\s*\(", code)[:20]

        return info

    # Run heuristic completeness checks to identify missing structures
    def _quick_completeness_checks(self, code: str, lang: str) -> List[str]:
        """
        Heuristic “completeness” hints to drive tutoring:
          - presence of defs/classes/imports
          - unbalanced delimiters
          - tiny files likely incomplete
        """
        if not code:
            return ["No code attached."]

        checks: List[str] = []

        if lang == "python":
            if "def " not in code and "class " not in code:
                checks.append("No function or class definitions found.")
            if "import " not in code and "from " not in code:
                checks.append("No imports found; verify required libraries.")
            if "if __name__" not in code and "def main(" not in code:
                checks.append("No main entrypoint detected (e.g., if __name__ == '__main__': ...).")
            # Rough bracket balance checks
            if code.count("(") != code.count(")"):
                checks.append("Unbalanced parentheses.")
            if code.count("[") != code.count("]"):
                checks.append("Unbalanced brackets.")
            if code.count("{") != code.count("}"):
                checks.append("Unbalanced braces.")

        # Generic size check for all languages
        if len(code) < 40:
            checks.append("Code is very short; may be incomplete.")

        return checks

    # Map loop counter and policy unlocks to tutoring stage
    def _decide_stage(self, loop: int, allow_code: bool) -> str:
        if allow_code or loop >= self.max_scaffold_loops:
            return "solution"
        if loop == 0:
            return "hint"
        if loop == 1:
            return "steps"
        return "pseudocode"

    # Construct system-level teaching instructions for the AI provider
    def _build_system_prompt(self, stage: str) -> str:
        base = [
        "You are an encouraging and conversational AI tutor.",
        "Always use natural, friendly language — speak like a helpful TA, not a manual.",
        "Be concise, but use full sentences and positive reinforcement.",
        "Follow assignment-specific tutor rules: give hints first, cite materials, and avoid full code until allowed.",
        ]
        if stage == "early_guidance":
            base.append("Focus on helping the student think through the problem step-by-step.")
        elif stage == "mid_guidance":
            base.append("You may reveal small code fragments or formulas as examples, still maintaining a teaching tone.")
        elif stage == "final_solution":
            base.append("You can now show full working code or detailed solutions, while explaining your reasoning clearly.")
        return "\n".join(base)

    # Build user prompt including question, context, code summary, compliance
    def _build_user_prompt(
        self,
        question: str,
        snips: List[Dict[str, Any]],
        stage: str,
        code_sig: Dict[str, Any],
        compliance: List[str],
    ) -> str:
        """
        User-facing prompt content: student question + selected context + code summary.
        The tutor will reference snippets by [index] and tailor the guidance to the code.
        """
        # Assemble top-k context snippets as numbered blocks
        ctx_blocks: List[str] = []
        for i, s in enumerate(snips[:4], start=1):
            meta = s.get("meta", {})
            src  = meta.get("source_type") or ""
            sec  = meta.get("section") or ""
            fn   = ""
            try:
                fp = meta.get("file_path")
                if fp:
                    fn = f" — {Path(fp).name}"
            except Exception:
                pass
            label = f"({src}/{sec}{fn})"
            ctx_blocks.append(f"[{i}] {label} {s['text']}")
        context = "\n\n".join(ctx_blocks) if ctx_blocks else "(no context)"


        # Include a code summary + a short trimmed preview (helpful for grounding)
        code_section = ""
        if code_sig:
            preview = self.submission_code[:800]  # keep short to avoid flooding the model
            code_section = (
                f"\n\nStudent code summary: lang={code_sig.get('lang')}, "
                f"lines={code_sig.get('lines')}, "
                f"functions={code_sig.get('functions')[:6]}, "
                f"classes={code_sig.get('classes')[:6]}, "
                f"imports={code_sig.get('imports')[:6]}\n"
                f"Completeness checks: {compliance or ['ok']}\n"
                f"Code preview (trimmed):\n{preview}"
            )

        # Final composed prompt string
        return (
            f"Student question: {question}\n\n"
            f"Relevant context:\n{context}"
            f"{code_section}\n\n"
            f"Stage: {stage}. Short, precise response tailored to the student's code:"
        )


__all__ = ["StudentSession", "StudentProgressDBStore"]
