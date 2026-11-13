"""THE SELF-KICK (Nov 13 2026, Rob after another night of an archived box
session: "Can't kick… it's every night").

A pushed fix is inert on the box until someone pulls and restarts the daemon,
and the only way to do that was a Claude session living on the box that kept
going offline. Now the site asks the daemon to do it itself:

    /api/cellar/kick?key=…[&procs=money,tape]   →  machine_flags box_kick
                                                    {nonce, procs, at}

Every daemon (money, tape) watches that row. One whose CELLAR_PROC is named
and that has not handled this nonce:

  1. fetch origin/main
  2. if main is ahead of the working tree: check main out into a THROWAWAY
     worktree and run the selftest THERE, with this daemon's own python —
     the live tree is untouched until the new code passes
  3. pass → `git merge --ff-only` in the live tree; fail → nothing changes,
     the stamp carries the failing tail, a 🔴 goes out, the daemon keeps
     running on the code it has
  4. if the live tree is now on a commit the daemon did not boot on → stop
     cleanly (same path as SIGTERM); launchd KeepAlive starts it on the new
     code within ~10s

Every step stamps `exec_probe_runs kind=box_kick` (read it with
/api/cellar/kick?status=1). The nonce is remembered in
~/.kahla/box_kick_<proc> BEFORE anything runs, so a kick can never loop.
Git runs as the repo's owner (the money daemon may run as root, and a root
pull leaves root-owned files the next hand pull trips over). Two daemons
kicked together take a file lock so only one pulls at a time; the second
finds main already merged and just restarts.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("cellar.selfkick")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLL_S = 20.0
GIT_TIMEOUT_S = 120
SELFTEST_TIMEOUT_S = 1500
FLAG = "box_kick"
# The kick holds this while it runs — INCLUDING the selftest subprocess, whose own
# test_self_kick points LOCK_PATH elsewhere (Nov 12 2026: the first real kick
# deadlocked for its whole 25-minute selftest timeout — the child test waited on
# the lock its parent held).
LOCK_PATH = os.path.join(tempfile.gettempdir(), "kahla-box-kick.lock")


def proc_name() -> str:
    return (os.environ.get("CELLAR_PROC") or "money").strip() or "money"


def wants(flag_val, proc: str, done_nonce: Optional[str]) -> Optional[str]:
    """Pure. The nonce this daemon must act on, else None: a dict with a
    nonce this proc has not handled, naming this proc (procs missing =
    money only)."""
    if not isinstance(flag_val, dict):
        return None
    nonce = str(flag_val.get("nonce") or "")
    if not nonce or nonce == (done_nonce or ""):
        return None
    procs = flag_val.get("procs") or ["money"]
    if isinstance(procs, str):
        procs = [p.strip() for p in procs.split(",") if p.strip()]
    if proc not in procs and "all" not in procs:
        return None
    return nonce


def plan(head: str, target: str, boot: str) -> dict:
    """Pure. What a kick does given the live tree's HEAD, origin/main and the
    commit this process booted on."""
    head, target, boot = (head or ""), (target or ""), (boot or "")
    if not target:
        return {"pull": False, "restart": False, "why": "no_target"}
    if head != target:
        return {"pull": True, "restart": True, "why": "behind"}
    if boot and boot != head:
        return {"pull": False, "restart": True, "why": "pulled_not_running"}
    return {"pull": False, "restart": False, "why": "current"}


def _done_path(proc: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".kahla", f"box_kick_{proc}")


def _read_done(proc: str) -> Optional[str]:
    try:
        with open(_done_path(proc)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_done(proc: str, nonce: str) -> None:
    p = _done_path(proc)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(nonce)


def _as_owner(cmd: list) -> list:
    """Run git as the repo's owner when this daemon is root."""
    try:
        if os.geteuid() == 0:
            st = os.stat(REPO)
            if st.st_uid != 0:
                user = pwd.getpwuid(st.st_uid).pw_name
                return ["sudo", "-u", user, "-H"] + cmd
    except Exception:
        pass
    return cmd


def _git(*args, cwd: Optional[str] = None, timeout: int = GIT_TIMEOUT_S) -> tuple[int, str]:
    r = subprocess.run(_as_owner(["git", *args]), cwd=cwd or REPO, capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _selftest_in(tree: str) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "cellar", "--selftest"], cwd=tree,
                       capture_output=True, text=True, timeout=SELFTEST_TIMEOUT_S, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    tail = "\n".join(l for l in out.splitlines()[-40:] if "FAIL" in l or "passed" in l)[-1500:]
    return r.returncode == 0, tail or out[-800:]


def run_kick(nonce: str, boot_sha: str, stamp: Callable[[dict], None]) -> dict:
    """One kick. Returns the result (also stamped). `restart` tells the caller
    to stop the daemon."""
    out: dict = {"kind": FLAG, "box_kick": True, "nonce": nonce, "proc": proc_name(),
                 "boot_sha": boot_sha[:12], "at": datetime.now(timezone.utc).isoformat(),
                 "steps": []}
    t0 = time.time()
    lock_f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        rc, msg = _git("fetch", "-q", "origin", "main")
        out["steps"].append({"fetch": rc, "msg": msg[-300:]})
        if rc != 0:
            out.update(ok=False, restart=False, why="fetch_failed")
            return out
        _, head = _git("rev-parse", "HEAD")
        _, target = _git("rev-parse", "origin/main")
        pl = plan(head, target, boot_sha)
        out.update(head=head[:12], target=target[:12], plan=pl)
        if pl["pull"]:
            rc_anc, _ = _git("merge-base", "--is-ancestor", "HEAD", "origin/main")
            if rc_anc != 0:
                out.update(ok=False, restart=False, why="not_fast_forward")
                return out
            tree = tempfile.mkdtemp(prefix="kahla-kick-")
            os.rmdir(tree)
            rc, msg = _git("worktree", "add", "--detach", tree, target)
            out["steps"].append({"worktree": rc, "msg": msg[-200:]})
            if rc != 0:
                out.update(ok=False, restart=False, why="worktree_failed")
                return out
            try:
                st0 = time.time()
                passed, tail = _selftest_in(tree)
                out["steps"].append({"selftest": passed, "s": round(time.time() - st0, 1), "tail": tail})
            finally:
                _git("worktree", "remove", "--force", tree)
                shutil.rmtree(tree, ignore_errors=True)
            if not passed:
                out.update(ok=False, restart=False, why="selftest_failed")
                return out
            rc, msg = _git("merge", "--ff-only", "-q", target)
            out["steps"].append({"merge": rc, "msg": msg[-300:]})
            if rc != 0:
                out.update(ok=False, restart=False, why="merge_failed")
                return out
            _, head = _git("rev-parse", "HEAD")
            out["head"] = head[:12]
        out.update(ok=True, restart=bool(pl["restart"]), why=pl["why"])
        return out
    except Exception as e:
        out.update(ok=False, restart=False, why=f"error: {type(e).__name__}: {e}"[:200])
        return out
    finally:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
            lock_f.close()
        except Exception:
            pass
        out["s"] = round(time.time() - t0, 1)
        try:
            stamp(out)
        except Exception as e:
            log.warning("box kick stamp failed: %s", e)


class KickWatch:
    """The watcher thread. `read_flag()` returns the box_kick row's value;
    `on_restart()` stops the daemon."""

    def __init__(self, read_flag: Callable[[], object], stamp: Callable[[dict], None],
                 on_restart: Callable[[], None], stop: threading.Event, boot_sha: str):
        self.read_flag, self.stamp, self.on_restart = read_flag, stamp, on_restart
        self.stop, self.boot_sha = stop, boot_sha or ""
        self.proc = proc_name()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="cellar-selfkick", daemon=True).start()

    def _loop(self) -> None:
        # the first read waits a beat so a fresh boot never acts on a stale row
        # before its own boot stamp is written
        while not self.stop.wait(POLL_S):
            try:
                self.check()
            except Exception as e:
                log.warning("self-kick check: %s", e)

    def check(self) -> Optional[dict]:
        nonce = wants(self.read_flag(), self.proc, _read_done(self.proc))
        if not nonce:
            return None
        _write_done(self.proc, nonce)          # before anything runs: never a loop
        log.warning("SELF-KICK %s: pulling + selftest on %s", nonce, self.proc)
        res = run_kick(nonce, self.boot_sha, self.stamp)
        log.warning("SELF-KICK %s: %s", nonce, json.dumps({k: res.get(k) for k in
                                                           ("ok", "why", "head", "target", "restart", "s")}))
        if not res.get("ok"):
            try:
                import app as _app
                _app._send_fill_telegram(f"🔴 BOX KICK FAILED ({self.proc}): {res.get('why')} — "
                                         f"still running {self.boot_sha[:7]}")
            except Exception:
                pass
        if res.get("restart"):
            log.warning("SELF-KICK: restarting on %s (launchd brings it back)", res.get("head"))
            self.on_restart()
        return res
