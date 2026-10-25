import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "sukima"

sys.path.insert(0, str(ROOT / "tools"))

from package_addon import package  # noqa: E402


def test_the_manifest_names_the_add_on_sukima_for_anki():
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "Sukima for Anki"


def test_the_manifest_package_is_the_folder_name():
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package"] == SOURCE.name


def test_the_package_holds_the_add_on_without_caches_or_user_files(tmp_path):
    source = tmp_path / "sukima"
    for name in ["__init__.py", "manifest.json", "__pycache__/x.pyc", "user_files/p/state.json", "meta.json"]:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("x", encoding="utf-8")

    target = tmp_path / "out" / "sukima.ankiaddon"
    package(source, target)

    with zipfile.ZipFile(target) as zf:
        assert sorted(zf.namelist()) == ["__init__.py", "manifest.json"]


def test_the_release_check_accepts_the_real_package(tmp_path):
    from release import check

    target = tmp_path / "sukima.ankiaddon"
    package(SOURCE, target)
    check(target)
