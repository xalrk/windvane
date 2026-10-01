"""A PEP 517 build backend in the standard library, so `pipx install`, `uv tool install` and `pip install` of
windvane need nothing downloaded to build (no setuptools: the check also runs offline).

The wheel puts prompts/, templates/, skills/ and hooks/ inside the `windvane` package, where `config.HOME` finds them
in an installed copy; a checkout keeps them at the top level. An editable install is a `.pth` to the checkout."""
import base64
import hashlib
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("prompts", "templates", "skills", "hooks")  # top-level dirs shipped as package data
SDIST = ("pyproject.toml", "README.md", "install.sh", "bin", "windvane", "tools/build_backend.py", *ASSETS)
EXECUTABLE = {"windvane/hooks/mention_hook.py"}


def _project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def _names():
    p = _project()
    return p["name"], p["version"], p["name"].replace("-", "_")


def _files(top):
    """Every file under `top` (a file or dir relative to ROOT), skipping caches."""
    path = ROOT / top
    if path.is_file():
        return [path]
    return sorted(f for f in path.rglob("*") if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc")


def _metadata():
    p = _project()
    lines = ["Metadata-Version: 2.4", f"Name: {p['name']}", f"Version: {p['version']}",
             f"Summary: {p['description']}", f"Requires-Python: {p['requires-python']}",
             f"License-Expression: {p['license']}"]
    lines += [f"Project-URL: {k}, {v}" for k, v in p.get("urls", {}).items()]
    lines.append("Description-Content-Type: text/markdown")
    return "\n".join(lines) + "\n\n" + (ROOT / p["readme"]).read_text()


def _wheel(directory, files):
    """Write a py3-none-any wheel of `files` ({archive name: bytes}) plus the dist-info, and return its name."""
    name, version, dist = _names()
    info = f"{dist}-{version}.dist-info"
    scripts = _project()["scripts"]
    files = {**files,
             f"{info}/METADATA": _metadata().encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: windvane build_backend\nRoot-Is-Purelib: true\n"
                              b"Tag: py3-none-any\n",
             f"{info}/entry_points.txt": ("[console_scripts]\n" + "".join(
                 f"{k} = {v}\n" for k, v in scripts.items())).encode()}
    record = []
    for arc, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{arc},sha256={digest},{len(data)}")
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
    out = f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(Path(directory) / out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in files.items():
            zi = zipfile.ZipInfo(arc, (2020, 1, 1, 0, 0, 0))
            zi.external_attr = (0o100755 if arc in EXECUTABLE else 0o100644) << 16  # a regular file: pip keeps +x
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return out


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    files = {f.relative_to(ROOT).as_posix(): f.read_bytes() for f in _files("windvane")}
    for top in ASSETS:
        files.update({f"windvane/{f.relative_to(ROOT).as_posix()}": f.read_bytes() for f in _files(top)})
    return _wheel(wheel_directory, files)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    """The checkout itself on sys.path: assets are found at its top level, as with bin/windvane."""
    return _wheel(wheel_directory, {f"{_names()[2]}.pth": f"{ROOT}\n".encode()})


def build_sdist(sdist_directory, config_settings=None):
    name, version, dist = _names()
    base = f"{dist}-{version}"
    out = f"{base}.tar.gz"
    with tarfile.open(Path(sdist_directory) / out, "w:gz", format=tarfile.PAX_FORMAT) as t:
        for top in SDIST:
            for f in _files(top):
                t.add(f, f"{base}/{f.relative_to(ROOT).as_posix()}")
        pkg_info = _metadata().encode()
        ti = tarfile.TarInfo(f"{base}/PKG-INFO")
        ti.size, ti.mode = len(pkg_info), 0o644
        t.addfile(ti, io.BytesIO(pkg_info))
    return out


def get_requires_for_build_wheel(config_settings=None):
    return []


get_requires_for_build_editable = get_requires_for_build_sdist = get_requires_for_build_wheel

