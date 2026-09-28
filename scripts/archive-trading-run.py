"""Archive a finalized trading directory, including files larger than 2 GiB."""
import argparse
import json
from pathlib import Path
import zipfile


def archive_run(source):
    source = Path(source).resolve(strict=True)
    if not json.loads((source / "summary.json").read_text(encoding="utf-8"))["finalized"]:
        raise ValueError("run is not finalized")
    destination = source.with_name(source.name + ".zip")
    # Exclusive creation avoids overwriting a previous evidence archive.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.write(path, arcname=path.relative_to(source.parent))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    print(archive_run(parser.parse_args().directory))
