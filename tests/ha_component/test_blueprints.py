"""Tests for the shipped automation blueprints.

A blueprint is the one artefact in this integration that Home Assistant validates only
when a USER imports it. Ship a broken one and nothing here notices — the first person to
find out is whoever pasted it into the import dialog and got a schema error with no line
number. So these tests run Home Assistant's own blueprint schema over every file, using
the real `homeassistant` package in `.venv_test`, and check the two kinds of drift that
schema cannot see:

* an input **nobody uses** — a field the user is asked to fill in that goes nowhere;
* a blueprint the **installers do not copy** — the file exists in the repo, is invisible
  on the user's machine, and there is no error anywhere. That is exactly what happened
  when access_notification_list.yaml was added: two installer scripts had to be edited
  by hand, and forgetting either one ships nothing.

The reverse direction (an `!input` naming something that was never declared) needs no
test: `Blueprint.__init__` already raises on it, so the schema check below covers it.
"""
import re
from pathlib import Path

import pytest
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.util import yaml as yaml_util

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "ekey_ha_app"
BLUEPRINT_DIR = COMPONENT / "blueprints"

BLUEPRINT_FILES = sorted(BLUEPRINT_DIR.glob("*.yaml"))


def test_there_are_blueprints_to_check():
    """Guard against the glob silently matching nothing and every test below passing."""
    assert BLUEPRINT_FILES, f"no blueprints found in {BLUEPRINT_DIR}"


@pytest.mark.parametrize("path", BLUEPRINT_FILES, ids=lambda p: p.name)
def test_blueprint_passes_home_assistants_own_schema(path: Path):
    """Load it exactly as Home Assistant would, tags and all.

    `yaml_util.load_yaml_dict` rather than PyYAML: `!input` is a Home Assistant tag and
    a plain safe_load raises on it, so a test that used PyYAML would be testing nothing
    but its own error handling.
    """
    blueprint = Blueprint(
        yaml_util.load_yaml_dict(str(path)),
        expected_domain="automation",
        path=str(path),
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert blueprint.name
    # A blueprint with no description is one the user has to read the YAML to understand.
    assert blueprint.metadata.get("description")


@pytest.mark.parametrize("path", BLUEPRINT_FILES, ids=lambda p: p.name)
def test_every_declared_input_is_actually_used(path: Path):
    """An input nothing references is a question asked for no reason."""
    data = yaml_util.load_yaml_dict(str(path))
    blueprint = Blueprint(
        data,
        expected_domain="automation",
        path=str(path),
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    used = yaml_util.extract_inputs(data)
    unused = set(blueprint.inputs) - used
    assert not unused, f"{path.name} declares inputs nothing uses: {sorted(unused)}"


def test_both_installers_copy_every_blueprint():
    """The installer lists are hand-maintained; this is what keeps them honest.

    Checked by reading the lists out of the scripts rather than by running them: the
    shell one cannot run on Windows and the PowerShell one cannot run on the CI image,
    while the list itself is the part that goes stale.
    """
    names = {path.name for path in BLUEPRINT_FILES}

    sh = (COMPONENT / "install_blueprints.sh").read_text(encoding="utf-8")
    sh_list = re.search(r'^BLUEPRINTS="([^"]*)"', sh, re.MULTILINE)
    assert sh_list, "could not find the BLUEPRINTS list in install_blueprints.sh"
    assert set(sh_list.group(1).split()) == names

    ps1 = (COMPONENT / "install_blueprints.ps1").read_text(encoding="utf-8")
    ps1_list = re.search(r"^\$Blueprints = @\((.*)\)", ps1, re.MULTILINE)
    assert ps1_list, "could not find the $Blueprints list in install_blueprints.ps1"
    assert set(re.findall(r'"([^"]+)"', ps1_list.group(1))) == names


def test_the_readme_documents_every_blueprint():
    """Each file gets a section, or it is shipped and undiscoverable."""
    readme = (BLUEPRINT_DIR / "README.md").read_text(encoding="utf-8")
    missing = [path.name for path in BLUEPRINT_FILES if path.name not in readme]
    assert not missing, f"blueprints missing from blueprints/README.md: {missing}"


def test_access_blueprints_trigger_on_the_resolved_event():
    """The name only exists on ekey_access_granted, not on ekey_fingerprint_matched.

    This is a regression guard with a specific history: welcome_notification.yaml used
    to trigger on ekey_fingerprint_matched, which carries the raw APID and no name, and
    its message was therefore the fixed string "Welcome home!" for everybody. Anything
    that wants to name a person has to use the resolved event.
    """
    for name in ("welcome_notification.yaml", "access_notification_list.yaml",
                 "toggle_relay_on_granted.yaml"):
        text = (BLUEPRINT_DIR / name).read_text(encoding="utf-8")
        assert "ekey_access_granted" in text, name
        # The unresolved event must not be a trigger in these three.
        assert not re.search(
            r"event_type:\s*ekey_fingerprint_matched", text
        ), f"{name} triggers on the event that has no person name"
