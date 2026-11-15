"""Orbit invocation wrapper (A6 execution backend).

Pinned recipe from bionym evals/eval_orbit_grounded.py:
`node bin/orbit.js --print --session-dir <dir> "<prompt>"` in an
isolated workdir with isolated HOME, staged line-oriented inputs, a
terminal `REPRO_RESULT:` stdout marker, and a 20-minute wall budget.

Compat table: @galaxyproject/orbit 0.9.x, node >= 22.19. Detection:
`ORBIT_BIN` env (path to orbit.js or the orbit launcher) else `orbit`
or `loom` on PATH (loom is the published npm name pending the orbit
rename); `--version` must parse into the table or we refuse.

HOME is left alone by default — a normal install keeps provider config
in ~/.orbit/config.json and stripping it would break auth. Set
REPRO_ORBIT_HOME to isolate (evals).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .clients import ClientError

RUN_TIMEOUT_S = 20 * 60
COMPAT_MAJORS = {(0, 9)}  # (major, minor) pairs we parse output for

_MARKER = re.compile(r"REPRO_RESULT:\s*(\{.*)", re.S)

INSTALL_HINT = (
    "Orbit not found or incompatible — npm install -g "
    "@galaxyproject/loom@0.9 (published name; @galaxyproject/orbit "
    "post-rename) under node >= 22.19, or set ORBIT_BIN to the "
    "launcher path."
)


def runs_dir() -> Path:
    """Per-audit run workdirs live OUTSIDE any repo/IDE-watched path —
    ~300 session dirs under a workspace root once hit the inotify
    limit and Orbit's chokidar watcher died ENOSPC mid-run."""
    return Path(
        os.environ.get("REPRO_RUNS_DIR")
        or os.environ.get("ORBIT_RUNS_DIR")
        or Path.home() / ".cache" / "reprobouncer_runs"
    )


def find_orbit() -> tuple[list[str], str, str]:
    """-> (argv prefix incl. launcher, orbit version, pi version).
    Raises ClientError when missing or unparseable."""
    explicit = os.environ.get("ORBIT_BIN")
    if explicit:
        p = Path(explicit)
        argv = (["node", str(p)] if p.suffix == ".js" else [str(p)])
        return (argv, *_versions(argv))
    for name in ("orbit", "loom"):
        on_path = shutil.which(name)
        if on_path:
            return ([on_path], *_versions([on_path]))
    raise ClientError(INSTALL_HINT)


def _versions(argv: list[str]) -> tuple[str, str]:
    """--version -> (orbit_version, pi_version). loom prints a
    deprecation line then 'orbit X.Y.Z (pi-coding-agent A.B.C)'."""
    try:
        out = subprocess.run(
            [*argv, "--version"], env=_env(), capture_output=True,
            text=True, timeout=30,
        )
    except Exception as e:
        raise ClientError(f"{INSTALL_HINT} (--version failed: {e})")
    text = out.stdout + out.stderr
    mo = re.search(r"orbit (\d+\.\d+\.\d+)", text) or \
        re.search(r"(\d+\.\d+\.\d+)", text)
    if not mo or tuple(int(x) for x in mo.group(1).split(".")[:2]) \
            not in COMPAT_MAJORS:
        raise ClientError(
            f"{INSTALL_HINT} (got: {text.strip()[:120]!r})"
        )
    mp = re.search(r"pi-coding-agent (\d+\.\d+\.\d+)", text)
    return mo.group(1), (mp.group(1) if mp else "")


def _env() -> dict[str, str]:
    """Run env: real HOME (the user's ~/.orbit provider config IS the
    run config on a normal install — REPRO_ORBIT_HOME isolates for
    evals), NODE_BIN prepended for the pinned node, bionym/JEV creds
    stripped. ORBIT_ACTIVE_LLM_API_KEY is NOT forwarded from
    LLM_API_KEY — that key belongs to our generator's provider, and
    injecting it would break auth for the user's configured provider."""
    env = dict(os.environ)
    home = os.environ.get("REPRO_ORBIT_HOME")
    if home:
        env["HOME"] = str(Path(home))
        # isolated homes use env-keyed providers — forward the
        # generator key the way the bionym eval does; an explicit
        # ORBIT_ACTIVE_LLM_API_KEY still wins
        env["ORBIT_ACTIVE_LLM_API_KEY"] = env.get(
            "ORBIT_ACTIVE_LLM_API_KEY") or env.get("LLM_API_KEY", "")
    node_bin = os.environ.get("NODE_BIN")
    if node_bin:
        env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"
    for k in ("GALAXY_URL", "GALAXY_API_KEY", "TYPESAFE_API_KEY",
              "JEV_BASE_URL", "JEV_MODEL"):
        env.pop(k, None)
    return env


# rc!=0 output phrases that mean the LLM endpoint failed transiently —
# nginx 5xx pages, connection resets, rate limits. Phrase-level (not
# bare status codes) so agent transcripts that mention "504" don't
# trigger a retry. A real failure retried once or twice via --continue
# is cheap; a session killed mid-run by a flaky gateway is not.
_TRANSIENT = (
    "gateway time-out", "bad gateway", "service unavailable",
    "internal server error", "too many requests", "rate limit",
    "econnreset", "econnrefused", "etimedout", "socket hang up",
)

_RESUME_MSG = (
    "The run was interrupted by a transient API error. Continue the "
    "reproduction from where you left off — the workdir and staged "
    "files are preserved. Emit the final REPRO_RESULT JSON line when "
    "done."
)


def run_orbit(
    prompt: str,
    workdir: Path,
    session_dir: Path,
    *,
    argv: list[str],
    timeout_s: int = RUN_TIMEOUT_S,
    retries: int | None = None,
    retry_delay_s: int | None = None,
) -> dict:
    """One Orbit run -> {status, raw, result, wall_s, attempts}.

    result = parsed JSON after the LAST REPRO_RESULT: marker, or None.
    status: ok | timeout | rc!=0 | no_marker. On a transient-looking
    rc!=0 (LLM endpoint 5xx, connection failure, rate limit) the run
    re-invokes with --continue up to `retries` times so the agent
    resumes its session rather than restarting. `timeout_s` is the
    shared wall budget across attempts. Retries default to env
    REPRO_ORBIT_RETRIES (2) / REPRO_ORBIT_RETRY_DELAY_S (60)."""
    workdir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    if retries is None:
        retries = int(os.environ.get("REPRO_ORBIT_RETRIES", "2"))
    if retry_delay_s is None:
        retry_delay_s = int(
            os.environ.get("REPRO_ORBIT_RETRY_DELAY_S", "60"))
    # --dangerously-bypass-permissions: headless exec-guard denies any
    # command off its safe allowlist (conda, Rscript, ...) with "no
    # interactive session to approve". Reproduction IS arbitrary code
    # execution — our sandbox boundary is the isolated workdir + creds
    # stripping, not the guard. The launcher translates the flag to
    # ORBIT_/LOOM_DANGEROUSLY_BYPASS_PERMISSIONS and strips it before pi.
    t0 = time.monotonic()
    raw_all = ""
    transcript: list[str] = []

    def _save() -> None:
        """Full per-attempt stdout+stderr for post-hoc audit of the
        agent's claims. Best-effort — logging never fails the run."""
        try:
            (workdir / "orbit_stdout.log").write_text(
                "".join(transcript))
        except OSError:
            pass

    for attempt in range(1 + retries):
        remaining = timeout_s - (time.monotonic() - t0)
        if remaining <= 0:
            _save()
            return {"status": "timeout", "wall_s": round(
                time.monotonic() - t0, 1), "raw": raw_all[-2000:],
                "attempts": attempt}
        cmd = [*argv, "--print", "--session-dir", str(session_dir),
               "--dangerously-bypass-permissions"]
        if attempt:
            cmd += ["--continue", _RESUME_MSG]
        else:
            cmd.append(prompt)
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=_env(),
                capture_output=True, text=True, timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            transcript.append(
                f"===== attempt {attempt + 1}: killed at wall "
                f"{time.monotonic() - t0:.0f}s (timeout) =====\n")
            _save()
            return {"status": "timeout",
                    "wall_s": round(time.monotonic() - t0, 1),
                    "attempts": attempt + 1}
        transcript.append(
            f"===== attempt {attempt + 1}: rc={proc.returncode} =====\n"
            f"{proc.stdout or ''}\n{proc.stderr or ''}\n")
        _save()
        if proc.returncode != 0:
            chunk = proc.stderr[-2000:]
            raw_all += chunk
            low = raw_all.lower()
            if attempt < retries and any(
                    s in low for s in _TRANSIENT):
                time.sleep(min(
                    retry_delay_s,
                    max(0.0, timeout_s - (time.monotonic() - t0))))
                continue
            return {"status": f"rc={proc.returncode}",
                    "raw": raw_all[-2000:],
                    "wall_s": round(time.monotonic() - t0, 1),
                    "attempts": attempt + 1}
        raw = proc.stdout.strip()
        m = _MARKER.findall(raw)
        result = None
        if m:
            import json
            try:
                result = json.loads(m[-1])
            except json.JSONDecodeError:
                pass
        return {
            "status": "ok" if result is not None else "no_marker",
            "raw": raw[-8000:],
            "result": result,
            "wall_s": round(time.monotonic() - t0, 1),
            "attempts": attempt + 1,
        }
    raise AssertionError("unreachable")
