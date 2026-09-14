"""Notebook runner embedded into the selected sandbox's Python execution."""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def run_notebook(config):
    try:
        import nbformat
        from nbclient import NotebookClient
    except ImportError as exc:
        raise RuntimeError("Notebook execution requires nbclient, nbformat and a Jupyter kernel "
                           "in the selected execution environment; install Astra's notebook extra.") from exc

    source = Path(config["path"]).expanduser().resolve(strict=True)
    if source.suffix.lower() != ".ipynb" or source.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Expected an .ipynb file of at most 32 MiB")
    nb = nbformat.read(source, as_version=4)
    nbformat.validate(nb)
    count = len(nb.cells)
    start, end = config["start_cell"], config["end_cell"] or count
    skip = set(config["skip_cells"])
    if not 1 <= start <= end <= count or any(i < 1 or i > count for i in skip):
        raise ValueError("Cell indices must be within the notebook (1-based, inclusive)")
    chosen = [i for i in range(start - 1, end) if i + 1 not in skip and nb.cells[i].cell_type == "code"]
    if not chosen:
        raise ValueError("Selected range contains no executable code cells")
    output = (Path(config["output_path"]).expanduser().resolve() if config["output_path"]
              else source.with_name(source.stem + ".executed-" + uuid.uuid4().hex[:8] + ".ipynb"))
    if output == source or output.suffix.lower() != ".ipynb":
        raise ValueError("Output must be a different .ipynb file")
    # Discard stale outputs in the new copy, including cells outside the selection.
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None
            cell.metadata.pop("execution", None)
    nb.metadata.pop("widgets", None)
    report = {"source": str(source), "output_path": str(output), "status": "starting",
              "selected_cells": [i + 1 for i in chosen], "cells": [], "fresh_kernel": True}
    nb.metadata["astra_execution"] = report
    # Refuse existing destinations before launching a kernel or executing any code.
    with output.open("x", encoding="utf-8") as f:
        nbformat.write(nb, f)
    owned = output.stat()

    def save():
        nonlocal owned
        current = output.lstat()
        if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) != (
                owned.st_ino, owned.st_dev, owned.st_mtime_ns, owned.st_size):
            raise RuntimeError("Notebook output changed externally; refusing to overwrite it")
        nb.metadata["astra_execution"] = report
        fd, temporary = tempfile.mkstemp(prefix=".astra-notebook-", dir=output.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, output)
            owned = output.stat()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def emit(event, **values):
        print(json.dumps({"event": event, "output_path": str(output), **values},
                         ensure_ascii=False), flush=True)

    emit("notebook_started", selected_cells=report["selected_cells"])
    try:
        client = NotebookClient(nb, timeout=config["cell_timeout"],
                                kernel_name=config["kernel_name"] or nb.metadata.get("kernelspec", {}).get("name", "python3"),
                                resources={"metadata": {"path": str(source.parent)}},
                                allow_errors=False, force_raise_errors=True, store_widget_state=False)
        with client.setup_kernel():
            for number, index in enumerate(chosen, 1):
                entry = {"cell": index + 1, "status": "running"}
                report["cells"].append(entry)
                report["status"] = "running"
                save()
                emit("cell_started", cell=index + 1)
                began = time.monotonic()
                try:
                    client.execute_cell(nb.cells[index], index, execution_count=number)
                except BaseException as exc:
                    entry.update(status="failed", error_type=type(exc).__name__)
                    raise
                else:
                    entry["status"] = "completed"
                finally:
                    entry["duration_ms"] = int((time.monotonic() - began) * 1000)
                    save()
                    emit("cell_finished", **entry)
        report["status"] = "completed"
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        save()
        emit("notebook_finished", status=report["status"], cells=report["cells"])
    return report
