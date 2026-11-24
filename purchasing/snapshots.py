"""Publish a complete generation with one atomic switch on a symlink-capable filesystem."""

from contextlib import contextmanager
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import uuid4


def adopt_flat_files(output, store, names, work):
    # Older builds have ordinary files at the public paths. Preserve their contents
    # before replacing any one of them with a link through the shared pointer.
    current = store / "current"
    legacy = [output / name for name in names
              if (output / name).is_file() and not (output / name).is_symlink()]
    if legacy and not current.exists():
        previous = store / f"legacy-{uuid4().hex}"
        previous.mkdir()
        for path in legacy:
            copyfile(path, previous / path.name)
        try:
            current.symlink_to(previous.name, target_is_directory=True)
        except FileExistsError:
            pass  # Another publisher finished the adoption first.

    for name in names:
        alias = output / name
        target = Path(store.name, "current", name)
        if alias.is_symlink():
            if alias.readlink() != target:
                raise ValueError(f"unexpected output symlink: {alias}")
            continue
        link = work / name
        link.symlink_to(target)
        link.replace(alias)


@contextmanager
def stage_snapshot(output, names):
    store = output / ".purchasing"
    store.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=store, prefix=".pending-") as folder:
        work = Path(folder)
        snapshot = store / f"run-{uuid4().hex}"
        staged = work / "data"
        staged.mkdir()
        yield staged, snapshot
        adopt_flat_files(output, store, names, work)
        staged.rename(snapshot)
        pointer = work / "next"
        pointer.symlink_to(snapshot.name, target_is_directory=True)
        pointer.replace(store / "current")
