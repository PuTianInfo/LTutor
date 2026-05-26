
# Import libraries
import subprocess, tempfile, os, shutil, platform, re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Global Variables
# ─────────────────────────────────────────────────────────────────────────────
# Detect platform so we can adjust executables and paths (e.g., python vs python3)
IS_WINDOWS = platform.system() == "Windows"
PYTHON_BIN = "python" if IS_WINDOWS else "python3"

# Docker images to use for each supported language when Docker is available
DOCKER_IMAGES = {
    "python": "python:3.11-alpine",
    "java":   "eclipse-temurin:21-jre-alpine",  # javac needs -jdk image; see note below
    "cpp":    "gcc:13.2.0"
}

# ─────────────────────────────────────────────────────────────────────────────
# Language configuration
# ─────────────────────────────────────────────────────────────────────────────
LANG_ALIASES = {
    "python": "python",
    "py": "python",
    "java": "java",
    "text/x-java": "java",
    "cpp": "cpp",
    "c++": "cpp",
    "text/x-c++src": "cpp",
}

LANGUAGE_CONFIGS = {
    "python": {
        "file": "main.py",
        "run": [PYTHON_BIN, "main.py"],
    },
    "java": {
        "file": "Main.java",
        "compile": ["javac", "-encoding", "UTF-8", "-d", "."],
        "run": ["java", "-cp", "."],
    },
    "cpp": {
        "file": "main.cpp",
        "compile": ["g++", "main.cpp", "-O2", "-std=c++17", "-o", "app"],
        "run": ["app.exe" if IS_WINDOWS else "./app"],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
# Map various language labels and MIME types into a canonical language key
def _normalize_language(lang: str) -> str:
    return LANG_ALIASES.get((lang or "").strip().lower(), (lang or "").strip().lower())

# Normalize an assignment UUID into a shorter key (asn_<first8>) used for folder names
def _assignment_key_for(aid: str) -> str:
    aid = (aid or "").strip().lower()
    return f"asn_{aid[:8]}" if aid else "asn_unknown"

# Stage assignment-specific shared_files into the temp directory for the code run
def copy_assignment_files_into(tmpdir: str | Path, assignment_id: str, app_root: Path) -> str:
    """
    Copy assignment files into: <tmpdir>/shared_files/<used_key>/
    Chooses the first EXISTING assignment folder (uuid or asn_<first8>), never the container dir.
    Returns the folder name actually used (used_key).
    """
    aid = (assignment_id or "").strip()
    asn_key = _assignment_key_for(aid)

    # Candidate folders
    candidates = [
        ("data/asn",             app_root / "data" / "shared_files" / asn_key, asn_key),
        ("data/uuid",            app_root / "data" / "shared_files" / aid,     aid),
        ("uploads/asn",          app_root / "uploads" / "assignments" / asn_key, asn_key),
        ("uploads/uuid",         app_root / "uploads" / "assignments" / aid,     aid),
        ("student/uuid",         app_root / "student" / "shared_files" / aid,         aid),
        ("student/asn",          app_root / "student" / "shared_files" / asn_key,     asn_key),
        ("student_sandbox/uuid", app_root / "student_sandbox" / "shared_files" / aid,     aid),
        ("student_sandbox/asn",  app_root / "student_sandbox" / "shared_files" / asn_key, asn_key),
    ]

    used_src = None
    used_key = None
    for label, src, key in candidates:
        if key and src.exists() and src.is_dir():
            used_src = src
            used_key = key
            break

    if not used_key:
        used_key = asn_key if aid else "assignment_unknown"

    dest = (Path(tmpdir) / "shared_files" / used_key).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if used_src and used_src.exists():
        shutil.copytree(used_src, dest, dirs_exist_ok=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)

    # Create alias so uuid and asn_<first8> both work
    def _mk_alias(alias_path: Path, target: Path):
        try:
            if alias_path.exists():
                return
            # Prefer symlink; fall back to copy if FS disallows symlinks
            alias_path.symlink_to(target, target_is_directory=True)
        except Exception:
            shutil.copytree(target, alias_path, dirs_exist_ok=True)

    uuid_key = aid if aid else None

    # Add a UUID alias if staged under asn_key
    if used_key == asn_key and uuid_key and uuid_key != asn_key:
        alias = (Path(tmpdir) / "shared_files" / uuid_key).resolve()
        _mk_alias(alias, dest)

    # Add a asn_key alias if staged under UUID
    if uuid_key and used_key == uuid_key and asn_key and asn_key != uuid_key:
        alias = (Path(tmpdir) / "shared_files" / asn_key).resolve()
        _mk_alias(alias, dest)

    # Debug
    print(f"[DBG] source -> dest: {used_src or '(none)'} -> {dest}")
    try:
        root = Path(tmpdir) / "shared_files"
        for p in root.rglob("*"):
            print("  -", p.relative_to(Path(tmpdir)))
    except Exception:
        pass

    return used_key

# Inspect Java source to detect the fully qualified class that contains main()
# This allows students to use packages and custom class names instead of a hard-coded Main
def _java_main_fqcn(source: str) -> str:
    """Detect the fully-qualified Java class containing main()."""
    pkg = None
    m = re.search(r'^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;', source, re.M)
    if m:
        pkg = m.group(1)

    class_pattern = r'^(?:public\s+)?class\s+([A-Za-z_]\w*)\b[^{]*\{'
    next_class_pattern = r'^(?:public\s+)?class\s+[A-Za-z_]\w*\b'

    for cm in re.finditer(class_pattern, source, re.M):
        cls = cm.group(1)
        start = cm.end()
        segment = source[start:]
        next_class = re.search(next_class_pattern, segment, re.M)
        end = start + next_class.start() if next_class else len(source)
        body = source[start:end]
        if re.search(r'public\s+static\s+void\s+main\s*\(\s*String\s*\[\]\s*\w*\s*\)', body):
            return f"{pkg}.{cls}" if pkg else cls

    return f"{pkg}.Main" if pkg else "Main"

# --- Docker helpers ---
# Probe whether the Docker CLI is available and usable on this host
def _check_docker_available() -> bool:
    """Return True if Docker CLI exists and can run a test command."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        return True
    except Exception:
        return False

USE_DOCKER = _check_docker_available()

# Convert a host path into a Docker-compatible mount path
def _docker_host_path(p: str | Path) -> str:
    p = Path(p).resolve()
    if IS_WINDOWS:
        # Turn "C:\Users\me\AppData\Local\Temp\xxx" into "/c/Users/me/AppData/Local/Temp/xxx"
        drive = p.drive.replace(":", "").lower()
        rest = [part for part in p.parts if part not in (p.drive,)]
        return "/" + drive + "/" + "/".join(rest)
    return p.as_posix()

# Build the base docker run arguments for our sandbox container
def _docker_base_args(temp_dir: str, env: dict[str, str]) -> list[str]:
    mount = ["-v", f"{_docker_host_path(temp_dir)}:/workspace:rw",
             "-w", "/workspace",
             "--network", "none",
             "--rm",
             "--pids-limit", "256",
             "--cpus", "1",
             "--memory", "512m"]
    envs = []
    for k in ("ASSIGNMENT_ROOT", "ASSIGNMENT_KEY"):
        if k in env:
            envs += ["-e", f"{k}={env[k]}"]
    return mount + envs

# ─────────────────────────────────────────────────────────────────────────────
# Main execution
# ─────────────────────────────────────────────────────────────────────────────
# Main entry point: execute the given code in the specified language
# Uses Docker when available, otherwise falls back to host execution with toolchain checks
def run_code(language, code, assignment_id: str = ""):
    lang = _normalize_language(language)
    if lang not in LANGUAGE_CONFIGS:
        return {"ok": False, "stdout": "", "stderr": f"Unsupported language: {language}", "exit_code": None}

    # Create an isolated temporary working directory for this run
    temp_dir = tempfile.mkdtemp(prefix="leettutor_")
    app_root = Path(__file__).resolve().parent
    result = {"ok": False, "stdout": "", "stderr": "", "exit_code": None}

    try:
        # stage and get the *actual* folder name (uuid or asn_<first8>)
        key_used = copy_assignment_files_into(temp_dir, assignment_id, app_root)

        env = os.environ.copy()
        env.setdefault("ASSIGNMENT_ROOT", "shared_files")
        env["ASSIGNMENT_KEY"] = key_used

        cfg = LANGUAGE_CONFIGS[lang]

        if USE_DOCKER:
            # ---------------- Docker path ----------------
            # Java with Docker
            if lang == "java":
                fqcn = _java_main_fqcn(code)
                cls = fqcn.split(".")[-1]
                src_file = Path(temp_dir) / f"{cls}.java"
                src_file.write_text(code, encoding="utf-8")

                base = ["docker", "run", *_docker_base_args(temp_dir, env)]

                # Compile (use JDK image)
                cproc = subprocess.run(
                    base + ["eclipse-temurin:21-jdk-alpine",
                            "sh", "-lc", "javac -encoding UTF-8 -d . *.java"],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )
                if cproc.returncode != 0:
                    result.update({"ok": False, "stdout": cproc.stdout, "stderr": cproc.stderr,
                                   "exit_code": cproc.returncode, "phase": "compile"})
                    return result

                # Run (use JRE image)
                rproc = subprocess.run(
                    base + [DOCKER_IMAGES["java"], "sh", "-lc", f"java -cp . {fqcn}"],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )

            # C++ with Docker
            elif lang == "cpp":
                src_file = Path(temp_dir) / cfg["file"]
                src_file.write_text(code, encoding="utf-8")
                base = ["docker", "run", *_docker_base_args(temp_dir, env)]

                cproc = subprocess.run(
                    base + [DOCKER_IMAGES["cpp"], "sh", "-lc", "g++ main.cpp -O2 -std=c++17 -o app"],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )
                if cproc.returncode != 0:
                    result.update({"ok": False, "stdout": cproc.stdout, "stderr": cproc.stderr,
                                   "exit_code": cproc.returncode, "phase": "compile"})
                    return result

                rproc = subprocess.run(
                    base + [DOCKER_IMAGES["cpp"], "sh", "-lc", "./app"],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )

            # Python with Docker
            else:
                src_file = Path(temp_dir) / cfg["file"]
                src_file.write_text(code, encoding="utf-8")
                base = ["docker", "run", *_docker_base_args(temp_dir, env)]
                rproc = subprocess.run(
                    base + [DOCKER_IMAGES["python"], "python", "main.py"],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )

        else:
            # --------------- Existing host path ---------------
            # Host-only: verify required toolchains exist
            needed_bins = []
            if "compile" in cfg and cfg["compile"]:
                needed_bins.append(cfg["compile"][0])
            if "run" in cfg and cfg["run"]:
                needed_bins.append(cfg["run"][0])
            for b in needed_bins:
                if shutil.which(b) is None:
                    result["stderr"] = f"Required runtime not found: {b}"
                    return result

            if lang == "java":
                fqcn = _java_main_fqcn(code)
                cls = fqcn.split(".")[-1]
                src_file = Path(temp_dir) / f"{cls}.java"
                src_file.write_text(code, encoding="utf-8")

                cproc = subprocess.run(
                    cfg["compile"] + [src_file.name],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20, env=env
                )
                if cproc.returncode != 0:
                    result.update({"ok": False, "stdout": cproc.stdout, "stderr": cproc.stderr,
                                   "exit_code": cproc.returncode, "phase": "compile"})
                    return result

                rproc = subprocess.run(
                    cfg["run"] + [fqcn],
                    cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20, env=env
                )

            else:
                src_file = Path(temp_dir) / cfg["file"]
                src_file.write_text(code, encoding="utf-8")

                if "compile" in cfg and cfg["compile"]:
                    cproc = subprocess.run(
                        cfg["compile"], cwd=temp_dir,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=20, env=env
                    )
                    if cproc.returncode != 0:
                        result.update({"ok": False, "stdout": cproc.stdout, "stderr": cproc.stderr,
                                       "exit_code": cproc.returncode, "phase": "compile"})
                        return result

                rproc = subprocess.run(
                    cfg["run"], cwd=temp_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20, env=env
                )


        result.update({
            "ok": (rproc.returncode == 0),
            "stdout": rproc.stdout,
            "stderr": rproc.stderr,
            "exit_code": rproc.returncode,
            "phase": "run"
        })
        return result

    # Timeout & Errors
    except subprocess.TimeoutExpired:
        result["stderr"] = "⏰ Execution timed out."
        result["phase"] = "timeout"
        return result
    except FileNotFoundError as e:
        result["stderr"] = f"Runtime not found: {e}"
        return result
    except Exception as e:
        result["stderr"] = f"⚠️ Internal error: {e}"
        return result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)