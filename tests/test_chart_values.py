"""The chart's values must load into the settings they are rendered into.

Two production crash-loops came from this seam, and neither was visible from
either side alone - the chart rendered something the app refused, and every
test passed because no test crossed the boundary:

* ``stream.snapshotMaxBytes: 2097152`` reached the pod as ``"2.097152e+06"``.
  Helm parses values.yaml through JSON, so every number is a float64, and Go
  prints a float64 of 1e6 or more in scientific notation - which
  ``int`` refuses.
* ``build.history.success: 0`` was rejected by the ``ge=1`` floor on the field
  it fills, so a default install could not start. That floor mirrors kpack,
  which refuses a limit below 1 outright - so the chart was the side that was
  wrong, which is exactly the kind of mismatch only a test across the two can
  see.

So these read the templates, resolve what they render, and feed the result
through ``Settings`` the way the pod does.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from api.core.config import Settings
from controller.config import ControllerSettings
from provisioner.config import ProvisionerSettings

CHART = Path(__file__).resolve().parent.parent / "charts" / "serverless-api"
# Each Deployment against the settings class ITS process builds. Checking them
# all against the API's would pass vacuously for anything only the controller or
# the provisioner declares - every settings class ignores extra env, so the
# fields most likely to be mistyped are exactly the ones that would go unchecked.
TEMPLATES = [
    (CHART / "templates" / "deployment.yaml", Settings),
    (CHART / "templates" / "build-controller.yaml", ControllerSettings),
    (CHART / "templates" / "provisioner.yaml", ProvisionerSettings),
]

# `- name: SERVERLESS_FOO` followed by `value: {{ .Values.a.b [| int64] | quote }}`.
# Deliberately narrow: anything rendered through tpl/toJson/printf is a shape
# this test has no business guessing at, and is skipped.
_PAIR = re.compile(
    r"- name:\s*(?P<env>SERVERLESS_[A-Z0-9_]+)\s*\n\s*value:\s*\{\{\s*"
    r"\.Values\.(?P<path>[A-Za-z0-9_.]+)\s*(?P<int64>\|\s*int64\s*)?\|\s*quote\s*\}\}"
)


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def _resolve(values: dict, path: str):
    node = values
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _rendered_pairs():
    """Every ``(env, values-path, value, int64, settings)`` the Deployments render."""
    values = _values()
    for template, settings in TEMPLATES:
        for m in _PAIR.finditer(template.read_text()):
            value = _resolve(values, m["path"])
            if value is None:  # templated default, or not in values.yaml
                continue
            yield m["env"], m["path"], value, bool(m["int64"]), settings


def _go_v_float64(x) -> str:
    """Go's ``%v`` on a float64 - ``strconv.FormatFloat(x, 'g', -1, 64)``.

    Reimplemented rather than approximated with ``str()``, because the whole
    bug lives in the difference: Go's shortest ``'g'`` switches to scientific
    notation at an exponent of 6, so 999999 renders as itself and 1000000
    renders as ``1e+06``. Python's ``str()`` never does that, and a guard built
    on it would pass against the very bug it exists to catch.

    Verified against Go 1.24 across the range these values span.
    """
    f = float(x)
    if f == 0:
        return "0"
    d = Decimal(repr(f)).normalize()
    sign, digits, exp = d.as_tuple()
    e = len(digits) + exp - 1  # the exponent in d.ddd form
    body = "".join(map(str, digits))
    if e < -4 or e >= 6:  # shortest 'g' uses eprec = 6
        mant = body[0] + ("." + body[1:] if len(body) > 1 else "")
        return f"{'-' if sign else ''}{mant}e{'+' if e >= 0 else '-'}{abs(e):02d}"
    if e >= 0:
        whole, frac = body[: e + 1].ljust(e + 1, "0"), body[e + 1 :]
        return f"{'-' if sign else ''}{whole}" + (f".{frac}" if frac else "")
    return f"{'-' if sign else ''}0." + "0" * (-e - 1) + body


def _as_helm_renders(value, int64: bool) -> str:
    """The string the pod's env actually receives.

    Helm reads values.yaml through JSON, so every number reaches the template
    as a float64 - and ``| int64`` is what turns it back into something that
    prints as digits.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if int64 else _go_v_float64(value)
    return str(value)


def test_the_templates_and_values_still_line_up():
    """Guard the guard: a rename here would make the rest of this file vacuous."""
    pairs = list(_rendered_pairs())
    assert len(pairs) > 15, (
        f"only matched {len(pairs)} env/value pairs; has the chart changed shape?"
    )
    names = {env for env, *_ in pairs}
    assert "SERVERLESS_STREAM__SNAPSHOT_MAX_BYTES" in names
    # One per deployment, so a template dropping out of the list is visible.
    assert {"SERVERLESS_RESYNC_SECONDS", "SERVERLESS_ENSURE_WORKERS"} <= names


@pytest.mark.parametrize(
    ("env", "path", "value", "int64", "settings"),
    [pytest.param(*p, id=f"{p[4].__name__}-{p[0]}") for p in _rendered_pairs()],
)
def test_every_rendered_value_loads_into_settings(monkeypatch, env, path, value, int64, settings):
    """What the chart sends for one value, that process's settings must accept."""
    monkeypatch.setenv(env, _as_helm_renders(value, int64))
    settings()  # raises if the chart's value is not loadable


def test_an_integer_over_a_million_survives_the_render():
    """The snapshotMaxBytes crash, as a rule rather than one value.

    Any int-typed value can be overridden past 1e6 - a bigger snapshot cap, a
    longer queue - and every one of them has to keep working.
    """
    # Without int64 this is what the pod would receive - the original crash.
    assert _as_helm_renders(4_194_304, int64=False) == "4.194304e+06"
    assert _as_helm_renders(4_194_304, int64=True) == "4194304"

    for env in (
        "SERVERLESS_STREAM__SNAPSHOT_MAX_BYTES",
        "SERVERLESS_STREAM__QUEUE_SIZE",
        "SERVERLESS_STREAM__MAX_SECONDS",
    ):
        int64 = next(i for e, _, _, i, _s in _rendered_pairs() if e == env)
        assert int64, f"{env} is int-typed and must render through int64"


def test_the_build_history_the_chart_ships_can_actually_be_loaded():
    """A release shipped `history.success: 0`, and the pod refused to start.

    Correctly: kpack refuses a limit below 1 ("build history limit must be
    greater than 0"), and its defaulting only fills an absent one - so an
    explicit 0 reaches that check and no Image can be created at all. The floor
    mirrors kpack; the chart is what had to change.
    """
    values = _values()
    for field in ("success", "failed"):
        assert _resolve(values, f"build.history.{field}") >= 1, (
            f"build.history.{field} must be >= 1; 0 makes rebuilds a no-op"
        )
    Settings(
        build={
            "success_history_limit": _resolve(values, "build.history.success"),
            "failed_history_limit": _resolve(values, "build.history.failed"),
        }
    )


def test_zero_is_still_a_real_choice_for_the_gc():
    """Unlike the build history: this prunes registry tags, not Builds."""
    assert _resolve(_values(), "buildController.gc.keepBuilds") == 0


def test_the_seconds_that_may_be_fractional_are_not_forced_to_integers():
    """`| int64` truncates, so the float-typed bounds must not carry it.

    ``heartbeatSeconds: 0.5`` rendered through int64 is ``0``, which is a
    stream that heartbeats continuously rather than one that is misconfigured.
    """
    deployment = (CHART / "templates" / "deployment.yaml").read_text()
    for name in ("intervalSeconds", "minIntervalSeconds", "maxIntervalSeconds", "heartbeatSeconds"):
        line = next(ln for ln in deployment.splitlines() if f".Values.stream.{name}" in ln)
        assert "int64" not in line, f"stream.{name} is float-typed; int64 would truncate it"
