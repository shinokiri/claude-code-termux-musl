"""Reclaim only managed, inactive releases participating in the lease protocol."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat


@contextmanager
def hold_release(release):
    """Keep a managed Python command's files available until it finishes."""
    record = release / "runtime/runtime.json"
    if not (release / "installation.json").is_file() or not record.is_file():
        yield
        return
    if json.loads(record.read_text()).get("release_lease") != 1:
        yield
        return
    fd = os.open(release / "runtime/release.lease", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Release lease must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def collect(data, apply=False, protected=()):
    """Caller holds install.lock. Old installations without leases are retained."""
    releases = data / "releases"
    protected = {Path(__file__).resolve().parents[1], *(Path(p).resolve() for p in protected)}
    previous = None
    for name in ("current", "previous"):
        pointer = data / name
        if pointer.is_symlink():
            target = pointer.resolve(strict=name == "current")
            if target.parent != releases.resolve():
                raise RuntimeError("Refusing cleanup with an external " + name + " pointer")
            if name == "current":
                protected.add(target)
            else:
                previous = target
        elif os.path.lexists(pointer):
            raise RuntimeError("Refusing cleanup with an invalid " + name + " pointer")
    report = {"applied": apply, "removed": [], "eligible": [], "retained": [], "reclaimed_bytes": 0,
              "previous_cleared": False}
    if apply and previous is not None and not previous.exists():
        (data / "previous").unlink()
        report["previous_cleared"] = True
    for release in sorted(releases.iterdir()):
        reason = None
        if release.is_symlink() or not release.is_dir():
            reason = "unmanaged entry"
        elif release.resolve() in protected:
            reason = "current or running installation command"
        else:
            try:
                manifest = json.loads((release / "installation.json").read_text())
                record = json.loads((release / "runtime/runtime.json").read_text())
                if Path(manifest["data_dir"]).resolve() != data.resolve() or record.get("root") != str(release / "runtime"):
                    reason = "unrecognized installation"
                elif record.get("release_lease") != 1:
                    reason = "legacy release without process leases"
            except (OSError, ValueError, KeyError, TypeError):
                reason = "unrecognized installation"
        if reason:
            report["retained"].append({"release": release.name, "reason": reason})
            continue
        fd = None
        try:
            fd = os.open(release / "runtime/release.lease", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("lease is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size = sum(path.lstat().st_size for path in release.rglob("*") if not path.is_symlink() and path.is_file())
            report["eligible"].append({"release": release.name, "bytes": size})
            if apply:
                # Rename while locked, then remove using symlink-resistant rmtree.
                # Constructors cannot acquire a shared lease during this operation.
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise RuntimeError("Safe directory deletion is unavailable")
                garbage = data / ("gc-" + release.name)
                if os.path.lexists(garbage):
                    raise RuntimeError("A previous cleanup needs inspection: " + str(garbage))
                release.rename(garbage)
                try:
                    if previous == release.resolve():
                        (data / "previous").unlink()
                        report["previous_cleared"] = True
                except OSError:
                    garbage.rename(release)
                    raise
                shutil.rmtree(garbage)
                report["removed"].append(release.name)
                report["reclaimed_bytes"] += size
        except BlockingIOError:
            report["retained"].append({"release": release.name, "reason": "active process lease"})
        except (OSError, RuntimeError) as error:
            report["retained"].append({"release": release.name, "reason": str(error)})
        finally:
            if fd is not None:
                os.close(fd)
    return report
