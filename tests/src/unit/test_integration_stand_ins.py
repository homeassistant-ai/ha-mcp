"""No test stands in for a real integration with Core's test helpers.

``scripts/integration_stand_ins.py`` runs here on a small repository written
for each test, against the installed Home Assistant's integrations, and once
on this checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from ._guard_run import run_guard

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "integration_stand_ins.py"
_SAMPLE = "tests/src/unit/test_sample.py"
_LISTED = f"- file: {_SAMPLE}\n  integration: hassio\n  reason: hassio only loads it\n"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("integration_stand_ins", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, files: dict[str, str], allowlist: str = "[]\n") -> Path:
    for tree in ("tests/src/unit", "tests/addon"):
        (tmp_path / tree).mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "tests/src/unit/integration_stand_in_allowlist.yml").write_text(
        allowlist, encoding="utf-8"
    )
    return tmp_path


REFUSED = {
    "mock-component": "mock_component(hass, 'hassio')\n",
    "mock-module": "mock_integration(hass, MockModule('hassio'))\n",
    "mock-module-keyword": "MockModule(domain='hassio')\n",
    "mock-platform": "mock_platform(hass, 'hassio.update')\n",
    "constant-f-string": "NAME = 'hassio'\nmock_platform(hass, f'{NAME}.config_flow')\n",
    "core-constant": (
        "from homeassistant.components.hassio.const import DOMAIN\nMockModule(DOMAIN)\n"
    ),
    "class-attribute": "class Agent:\n    domain = 'hassio'\nMockModule(Agent.domain)\n",
    "loop": "for name in ('made_up', 'hassio'):\n    MockModule(name)\n",
    "dict-keys": "FLOWS = {'hassio': 1}\nfor name in FLOWS:\n    MockModule(name)\n",
    "parametrize": (
        "@pytest.mark.parametrize('name', ['made_up', pytest.param('hassio')])\n"
        "def test_it(name):\n    MockModule(name)\n"
    ),
    "helper-parameter": (
        "def register(hass, name):\n    mock_component(hass, name)\n"
        "def test_it(hass):\n    register(hass, 'hassio')\n"
    ),
    "helper-keyword-through-helper": (
        "def inner(hass, *, integration='test'):\n"
        "    mock_platform(hass, f'{integration}.update')\n"
        "def outer(hass, name):\n    inner(hass, integration=name)\n"
        "outer(hass, 'hassio')\n"
    ),
    "helper-dict-keys": (
        "def platforms(hass, found):\n"
        "    for name, updates in found.items():\n"
        "        mock_platform(hass, f'{name}.update')\n"
        "def test_it(hass):\n    platforms(hass, {'hassio': []})\n"
    ),
    "helper-default": "def register(hass, name='hassio'):\n    MockModule(name)\n",
    "aliased-helper": (
        "from pytest_homeassistant.common import mock_component as mc\n"
        "mc(hass, 'hassio')\n"
    ),
    "module-constant": (
        "from homeassistant.components import hassio\nMockModule(hassio.DOMAIN)\n"
    ),
    "mock-component-keyword": "mock_component(hass, component='hassio')\n",
    "mock-platform-keyword": "mock_platform(hass, platform_path='hassio.update')\n",
    "mock-config-flow": "with mock_config_flow('hassio', Flow):\n    pass\n",
    "mock-config-flow-keyword": "mock_config_flow(domain='hassio', config_flow=Flow)\n",
    "components-add": "hass.config.components.add('hassio')\n",
    "dict-values": "FLOWS = {'made_up': 'hassio'}\nfor name in FLOWS.values():\n    MockModule(name)\n",
    "dict-items-value": (
        "FLOWS = {'made_up': 'hassio'}\n"
        "for key, name in FLOWS.items():\n    MockModule(name)\n"
    ),
    "parametrize-dict-values": (
        "FLOWS = {'made_up': 'hassio'}\n"
        "@pytest.mark.parametrize('name', FLOWS.values())\n"
        "def test_it(name):\n    MockModule(name)\n"
    ),
    "helper-dict-values": (
        "def register(found):\n"
        "    for name in found.values():\n        MockModule(name)\n"
        "register({'made_up': 'hassio'})\n"
    ),
    "enumerate": (
        "for index, name in enumerate(('made_up', 'hassio')):\n    MockModule(name)\n"
    ),
    "zip": "for number, name in zip((1, 2), ('made_up', 'hassio')):\n    MockModule(name)\n",
    "self-method": (
        "class TestIt:\n"
        "    def register(self, hass, name):\n        mock_component(hass, name)\n"
        "    def test_it(self, hass):\n        self.register(hass, 'hassio')\n"
    ),
    "self-static-method": (
        "class TestIt:\n"
        "    @staticmethod\n"
        "    def register(hass, name):\n        mock_component(hass, name)\n"
        "    def test_it(self, hass):\n        self.register(hass, 'hassio')\n"
    ),
    "cls-method-of-a-base": (
        "class Base:\n"
        "    @classmethod\n"
        "    def register(cls, name):\n        MockModule(name)\n"
        "class TestIt(Base):\n"
        "    @classmethod\n"
        "    def setup_class(cls):\n        cls.register('hassio')\n"
    ),
    "percent-format": "NAME = 'hassio'\nmock_platform(hass, '%s.update' % NAME)\n",
    "str-format": "NAME = 'hassio'\nmock_platform(hass, '{}.update'.format(NAME))\n",
    "str-join": "NAME = 'hassio'\nmock_platform(hass, '.'.join([NAME, 'update']))\n",
    "parametrize-named-table": (
        "CASES = ['made_up', pytest.param('hassio', id='real')]\n"
        "@pytest.mark.parametrize('name', CASES)\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "helper-tuple-column": (
        "def register(hass, rows):\n"
        "    for label, name in rows:\n        mock_component(hass, name)\n"
        "register(hass, [('case', 'hassio')])\n"
    ),
    "parametrize-names-constant": (
        "FIELDS = 'name'\n@pytest.mark.parametrize(FIELDS, ['hassio'])\n"
        "def test_it(hass, name):\n    mock_component(hass, name)\n"
    ),
    "join-named-sequence": "PARTS = ('has', 'sio')\nMockModule(''.join(PARTS))\n",
    "reassigned-to-real": (
        "def test_it(hass):\n    domain = 'made_up_probe'\n    domain = 'hassio'\n"
        "    mock_component(hass, domain)\n"
    ),
    "saved-by-tuple-unpack": (
        "def test_it(hass):\n    domain = 'hassio'\n    saved, other = domain, 1\n"
        "    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "saved-annotated": (
        "def test_it(hass):\n    domain = 'hassio'\n    saved: str = domain\n"
        "    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "saved-by-walrus": (
        "def test_it(hass):\n    domain = 'hassio'\n    if (saved := domain):\n        pass\n"
        "    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "saved-by-a-loop": (
        "def test_it(hass):\n    domain = 'hassio'\n    for saved in (domain,):\n        pass\n"
        "    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "saved-in-a-list": (
        "def test_it(hass):\n    domain = 'hassio'\n    names = [domain]\n"
        "    domain = 'made_up_probe'\n    for name in names:\n        MockModule(name)\n"
    ),
    "saved-in-a-list-for-a-helper": (
        "def register_all(hass, names):\n    for name in names:\n"
        "        mock_component(hass, name)\n"
        "def test_it(hass):\n    domain = 'hassio'\n    names = [domain]\n"
        "    domain = 'made_up_probe'\n    register_all(hass, names)\n"
    ),
    "saved-as-a-dict-key": (
        "def test_it(hass):\n    domain = 'hassio'\n    flows = {domain: 1}\n"
        "    domain = 'made_up_probe'\n    for name in flows:\n        MockModule(name)\n"
    ),
    "augmented-assignment": (
        "def test_it(hass):\n    domain = 'has'\n    domain += 'sio'\n"
        "    mock_component(hass, domain)\n"
    ),
    "helper-many-suffixes-one-real": (
        "def register(hass, base):\n"
        "    for s in ('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'io'):\n"
        "        mock_component(hass, f'{base}{s}')\n"
        "register(hass, 'hass')\n"
    ),
    "assigned-on-the-calls-line": (
        "def test_it(hass):\n    name = 'made_up_probe'\n"
        "    name = 'hassio'; MockModule(name)\n"
    ),
    "dict-spread-other-key": (
        "base = {'slot': 'hassio'}\n"
        "for name in {**base, 'other': 'made_up_probe'}.values():\n"
        "    MockModule(name)\n"
    ),
    "helper-default-used": (
        "def register(hass, a, b='sio'):\n    MockModule(a + b)\n"
        "register(hass, 'has')\n"
    ),
    "default-of-a-function-no-call-reaches": (
        "def test_it(hass, name='hassio'):\n    MockModule(name)\n"
    ),
    "alias-of-a-helper": "register = mock_component\nregister(hass, 'hassio')\n",
    "format-named-tuple": "PARTS = ('has', 'sio')\nMockModule('%s%s' % PARTS)\n",
    "param-imported-from-pytest": (
        "from pytest import param\nROWS = [param('hassio')]\n"
        "@pytest.mark.parametrize('name', ROWS)\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "alias-of-a-local-helper": (
        "def register(hass, name):\n    MockModule(name)\n"
        "install = register\ninstall(hass, 'hassio')\n"
    ),
    "parametrize-by-keyword": (
        "@pytest.mark.parametrize(argnames='name', argvalues=['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "loop-row-that-is-real": (
        "for a, b in [('has', 'sio'), ('made_up_', '_probe')]:\n    MockModule(a + b)\n"
    ),
    "positional-only-parameter": (
        "def register(name='made_up_probe', /, **options):\n    MockModule(name)\n"
        "register('hassio')\n"
    ),
    "core-helper-through-its-module": (
        "from pytest_homeassistant import common\n"
        "common.mock_component(hass, 'hassio')\n"
    ),
    "helper-of-cores-in-a-test-method": (
        "class TestIt:\n    async def test_it(self, hass, supervised):\n"
        "        if supervised:\n            mock_component(hass, 'hassio')\n"
    ),
    "wrapper-named-like-a-core-helper": (
        "def mock_component(hass, component):\n"
        "    hass.config.components.add(component)\n"
        "mock_component(hass, 'hassio')\n"
    ),
    "wrapper-of-the-core-helper-it-is-named-after": (
        "from pytest_homeassistant.common import mock_component as core_mock\n"
        "def mock_component(hass, component):\n    core_mock(hass, component)\n"
        "mock_component(hass, 'hassio')\n"
    ),
    "method-wrapping-a-core-helper": (
        "from pytest_homeassistant.common import mock_component as core_mock\n"
        "class Helpers:\n    @staticmethod\n"
        "    def mock_component(hass, component):\n"
        "        core_mock(hass, component)\n"
        "Helpers.mock_component(None, 'hassio')\n"
    ),
    "subclass-named-like-the-core-helper": (
        "from pytest_homeassistant.common import MockModule as CoreModule\n"
        "class MockModule(CoreModule):\n    pass\n"
        "mock_integration(hass, MockModule('hassio'))\n"
    ),
    "subclass-of-a-core-helper": (
        "class Module(MockModule):\n    pass\n"
        "class Mine(Module):\n    pass\n"
        "mock_integration(hass, Mine('hassio'))\n"
    ),
    "alias-reassigned-after-the-call": (
        "def unrelated(*args):\n    pass\n"
        "def test_it(hass):\n    register = mock_component\n"
        "    register(hass, 'hassio')\n    register = unrelated\n"
    ),
    "variadic-helper-parameter": (
        "def register(*names):\n    for name in names:\n        MockModule(name)\n"
        "register('made_up_probe', 'hassio')\n"
    ),
    "keywords-a-helper-collects": (
        "def register(**domains):\n    for name in domains.values():\n"
        "        MockModule(name)\n"
        "register(first='hassio')\n"
    ),
    "branch-assignment-a-loop-brings-round": (
        "def test_it(hass, flag):\n    name = 'made_up_probe'\n"
        "    for _ in range(2):\n        MockModule(name)\n"
        "        if flag:\n            name = 'hassio'\n"
    ),
    "keyword-named-like-a-positional-only-parameter": (
        "def register(name, /, **domains):\n"
        "    for domain in domains.values():\n        MockModule(domain)\n"
        "register('unused', name='hassio')\n"
    ),
    "format-field-in-another-scripts-digits": (
        "MockModule('{\u0660}'.format('hassio'))\n"
    ),
    "method-default-from-the-class-body": (
        "class Helpers:\n    DOMAIN = 'hassio'\n    @staticmethod\n"
        "    def register(name=DOMAIN):\n        MockModule(name)\n"
        "Helpers.register()\n"
    ),
    "parametrize-rows-from-the-class-body": (
        "class TestIt:\n    NAMES = ['hassio']\n"
        "    @pytest.mark.parametrize('name', NAMES)\n"
        "    def test_it(self, hass, name):\n        MockModule(name)\n"
    ),
    "pytest-under-another-name": (
        "import pytest as pt\n@pt.mark.parametrize('name', ['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "mark-under-another-name": (
        "from pytest import mark as m\n@m.parametrize('name', ['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "pytest-param-under-another-name": (
        "import pytest as pt\n"
        "for name in [pt.param('hassio', id='real')]:\n    MockModule(name)\n"
    ),
    "target-after-a-starred-one": (
        "for label, *extras, name in [('case', 'hassio')]:\n    MockModule(name)\n"
    ),
    "target-after-a-starred-one-in-a-longer-row": (
        "for label, *extras, name in [('case', 'x', 'y', 'hassio')]:\n"
        "    MockModule(name)\n"
    ),
    "zip-column-after-a-starred-target": (
        "for label, *rest, name in zip(['a'], ['b'], ['hassio']):\n"
        "    MockModule(name)\n"
    ),
    "dict-value-after-a-starred-target": (
        "for key, *rest, name in {'a': 'hassio'}.items():\n    MockModule(name)\n"
    ),
    "call-inside-a-comprehension": (
        "rows = [MockModule(name) for name in ['hassio']]\n"
    ),
    "comprehension-over-a-helpers-parameter": (
        "def register(names):\n    return [MockModule(n) for n in names]\n"
        "register(['hassio'])\n"
    ),
    "name-a-comprehension-assigns": (
        "def test_it(hass):\n    rows = [last := 'hassio' for _ in range(1)]\n"
        "    MockModule(last)\n"
    ),
    "helper-two-parameters": (
        "def register(hass, a, b):\n    mock_component(hass, f'{a}{b}')\n"
        "register(hass, 'hass', 'io')\n"
    ),
    "dict-spread": (
        "REAL = {'hassio': 1}\n"
        "for name in {**REAL, 'made_up_probe': 1}:\n    MockModule(name)\n"
    ),
    "join-one-binding-unknown": (
        "PARTS = ('has', 'sio')\nif flag:\n    PARTS = make()\n"
        "MockModule(''.join(PARTS))\n"
    ),
    "zip-set-column": (
        "for n, name in zip((1,), {'made_up_probe', 'hassio'}):\n    MockModule(name)\n"
    ),
    "zip-starred-length": (
        "for n, name in zip(['a', *MORE], ['made_up_probe', 'x_probe', 'hassio']):\n"
        "    MockModule(name)\n"
    ),
    "saved-before-reassignment": (
        "def test_it(hass):\n    domain = 'hassio'\n    saved = domain\n"
        "    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "saved-on-a-branch": (
        "def test_it(hass, flag):\n    domain = 'hassio'\n    if flag:\n"
        "        saved = domain\n    domain = 'made_up_probe'\n    MockModule(saved)\n"
    ),
    "reassigned-on-a-branch": (
        "def test_it(hass, flag):\n    domain = 'hassio'\n    if flag:\n"
        "        domain = 'made_up_probe'\n    mock_component(hass, domain)\n"
    ),
    "helper-prefix": (
        "def register(hass, prefix):\n    mock_component(hass, f'{prefix}sio')\n"
        "register(hass, 'has')\n"
    ),
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_a_real_integrations_name_is_refused(guard, tmp_path, capsys, source):
    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert status == 1
    assert out.startswith(f"{_SAMPLE}:")
    assert "'hassio' integration" in out.splitlines()[0]


def test_a_name_passed_to_a_helper_in_another_module_is_refused_at_the_call(
    guard, tmp_path, capsys
):
    helper = (
        "def register(hass, domain):\n    mock_integration(hass, MockModule(domain))\n"
    )
    test = "from ._helpers import register\n\n\nregister(hass, 'frontend')\n"
    repo = _repo(tmp_path, {"tests/src/unit/_helpers.py": helper, _SAMPLE: test})

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        f"{_SAMPLE}:4: a stand-in registered under Home Assistant's 'frontend' "
        "integration through register()"
    )


def test_a_helper_re_exported_under_another_name_is_refused(guard, tmp_path, capsys):
    helper = "def register(hass, name):\n    mock_component(hass, name)\n"
    test = "from ._reexport import install\n\n\ninstall(hass, 'hassio')\n"
    repo = _repo(
        tmp_path,
        {
            "tests/src/unit/_helpers.py": helper,
            "tests/src/unit/_reexport.py": "from ._helpers import register as install\n",
            _SAMPLE: test,
        },
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{_SAMPLE}:4: ")


def test_a_constant_from_a_local_helper_module_is_refused(guard, tmp_path, capsys):
    test = "from ._names import NAME\n\n\nMockModule(NAME)\n"
    repo = _repo(
        tmp_path, {"tests/src/unit/_names.py": "NAME = 'hassio'\n", _SAMPLE: test}
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{_SAMPLE}:4: ")


def test_too_many_alternatives_fail_instead_of_passing_unchecked(
    guard, tmp_path, capsys
):
    """One of 289 combinations is ``hassio``; none may be dropped unchecked."""
    prefixes = ", ".join(repr(p) for p in ["has", *"ABCDEFGHIJKLMNOP"])
    suffixes = ", ".join(repr(s) for s in ["sio", *"abcdefghijklmnop"])
    source = (
        f"for prefix in ({prefixes}):\n"
        f"    for suffix in ({suffixes}):\n"
        "        MockModule(f'{prefix}{suffix}')\n"
    )

    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert status == 1
    assert out.startswith(f"{_SAMPLE}:3: more than ")


def test_a_helper_wrapping_its_own_name_without_end_fails(guard, tmp_path, capsys):
    source = (
        "def register(hass, prefix):\n    if len(prefix) < 6:\n"
        "        return register(hass, prefix + 'x')\n"
        "    mock_component(hass, f'{prefix}sio')\n"
        "register(hass, 'demo_')\n"
    )

    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert status == 1
    assert f"{_SAMPLE}:3: more than 8 helpers add text to the name" in out


def test_too_many_names_through_a_helper_fail(guard, tmp_path, capsys):
    """Each argument stays under the limit; together they are 289 names."""
    values = ", ".join(f"'m{number}'" for number in range(17))
    source = (
        "def register(hass, a, b):\n    MockModule(a + b)\n"
        f"for a in ({values}):\n    for b in ({values}):\n"
        "        register(hass, a, b)\n"
    )

    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert status == 1
    assert f"{_SAMPLE}:5: more than 256 possible names to check" in out


def test_a_name_too_many_names_from_its_value_fails(guard, tmp_path, capsys):
    """Without a bound the chain ends the check in a recursion error."""
    chain = "".join(f"domain_{n} = domain_{n - 1}\n" for n in range(1, 600))
    source = f"domain_0 = 'hassio'\n{chain}MockModule(domain_599)\n"

    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert status == 1
    assert f"{_SAMPLE}:601: more than 256 possible names to check, or a name" in out


def test_unparseable_source_fails_with_its_line(guard, tmp_path, capsys):
    repo = _repo(tmp_path, {_SAMPLE: "x = 1\ndef broken(:\n"})

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{_SAMPLE}:2: ")


def test_all_lists_the_names_only_known_at_runtime_for_review(guard, tmp_path, capsys):
    """Listed: a call's result, a fixture's value at each stand-in it reaches,
    ``*`` and ``**`` arguments, a collection one branch reads at runtime, and
    a parameter ``parametrize`` gives a fixture, and a loop over a name nothing
    binds. Left out: a parameter a call
    fills, one with a default, one ``parametrize`` fills, and a loop over a
    list known to be empty."""
    source = (
        "MockModule('made_up_probe')\nMockModule(pick())\n"
        "def test_it(hass, name):\n    mock_component(hass, name)\n    MockModule(name)\n"
        "MockModule(*names)\nMockModule(**options)\n"
        "def register(hass, name):\n    mock_component(hass, name)\n"
        "register(hass, 'made_up_probe')\nregister(*row)\n"
        "def default(hass, name='made_up_probe'):\n    mock_component(hass, name)\n"
        "@pytest.mark.parametrize('name', ['made_up_probe'])\n"
        "def test_each(hass, name):\n    mock_component(hass, name)\n"
        "rows = ['made_up_probe']\nif flag:\n    rows = load_rows()\n"
        "for row in rows:\n    MockModule(row)\n"
        "for none in []:\n    MockModule(none)\n"
        "@pytest.mark.parametrize('name', ['hassio'], indirect=True)\n"
        "def test_indirect(hass, name):\n    MockModule(name)\n"
        "for unbound in NOWHERE:\n    MockModule(unbound)\n"
    )
    repo = _repo(tmp_path, {_SAMPLE: source})

    status = guard.main(["--repo", str(repo), "--all"])

    listed = [(2, "MockModule"), (4, "mock_component"), (5, "MockModule")]
    listed += [(6, "MockModule"), (7, "MockModule"), (11, "register")]
    listed += [(21, "MockModule"), (26, "MockModule"), (28, "MockModule")]
    assert (status, capsys.readouterr().out.splitlines()) == (
        0,
        [
            *(
                f"{_SAMPLE}:{line}: {callee}() gets a name only known at runtime"
                for line, callee in listed
            ),
            "No unlisted stand-in for a real integration; 0 allowlisted.",
        ],
    )


ALLOWED = {
    "made-up-domain": "mock_integration(hass, MockModule('made_up_probe'))\n",
    "made-up-platform": "mock_platform(hass, 'made_up_probe.config_flow')\n",
    "runtime-name": "def test_it(hass, name):\n    mock_component(hass, name)\n",
    "helper-made-up": (
        "def register(hass, name):\n    mock_component(hass, name)\n"
        "register(hass, 'made_up_probe')\n"
    ),
    "real-setup": "async_setup_component(hass, 'hassio', {})\n",
    "reassigned-to-made-up": (
        "def test_it(hass):\n    domain = 'hassio'\n    domain = 'made_up_probe'\n"
        "    mock_component(hass, domain)\n"
    ),
    "saved-before-reassignment-to-real": (
        "def test_it(hass):\n    domain = 'made_up_probe'\n    saved = domain\n"
        "    domain = 'hassio'\n    mock_component(hass, saved)\n"
    ),
    "reassigned-on-the-calls-line": (
        "def test_it(hass):\n    name = 'hassio'\n"
        "    name = 'made_up_probe'; MockModule(name)\n"
    ),
    "dict-key-overwritten-after-a-spread": (
        "base = {'slot': 'hassio'}\n"
        "for name in {**base, 'slot': 'made_up_probe'}.values():\n"
        "    MockModule(name)\n"
    ),
    "helper-parts-from-different-calls": (
        "def register(hass, a, b):\n    MockModule(f'{a}{b}')\n"
        "register(hass, 'hass', '_probe')\nregister(hass, 'made_up_', 'io')\n"
    ),
    "call-named-like-param": (
        "for name in [make_param('hassio')]:\n    MockModule(name)\n"
    ),
    "helper-default-not-used": (
        "def register(hass, a, b='sio'):\n    MockModule(a + b)\n"
        "register(hass, 'has', '_probe')\n"
    ),
    "function-named-like-an-alias": (
        "def register(hass, name):\n    pass\nregister(hass, 'hassio')\n"
    ),
    "function-named-param": (
        "def param(_):\n    return 'made_up_probe'\n"
        "for name in [param('hassio')]:\n    MockModule(name)\n"
    ),
    "function-named-like-a-core-helper": (
        "def mock_component(hass, component):\n    return component\n"
        "mock_component(hass, 'hassio')\n"
    ),
    "method-named-like-a-core-helper": (
        "class Helpers:\n    @staticmethod\n"
        "    def mock_component(hass, component):\n        return component\n"
        "Helpers.mock_component(None, 'hassio')\n"
    ),
    "subclass-of-a-core-helper-with-a-made-up-domain": (
        "class Module(MockModule):\n    pass\n"
        "mock_integration(hass, Module('made_up_probe'))\n"
    ),
    "alias-reassigned-before-the-call": (
        "def unrelated(*args):\n    pass\n"
        "def test_it(hass):\n    register = mock_component\n"
        "    register = unrelated\n    register(hass, 'hassio')\n"
    ),
    "branch-assignment-after-the-call": (
        "def test_it(hass, flag):\n    name = 'made_up_probe'\n"
        "    MockModule(name)\n    if flag:\n        name = 'hassio'\n"
    ),
    "decorator-named-like-parametrize": (
        "def no_parametrize(names, rows):\n    return lambda function: function\n"
        "@no_parametrize('name', ['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "format-field-that-is-not-a-number": "MockModule('{\u00b2}'.format())\n",
    "format-field-too-long-for-a-number": (
        "MockModule('{" + "9" * 5000 + "}'.format('hassio'))\n"
    ),
    "class-name-a-method-body-does-not-see": (
        "DOMAIN = 'made_up_probe'\nclass Helpers:\n    DOMAIN = 'hassio'\n"
        "    @staticmethod\n    def register():\n        MockModule(DOMAIN)\n"
        "Helpers.register()\n"
    ),
    "comprehension-target-after-the-comprehension": (
        "def test_it(hass):\n    name = 'made_up_probe'\n"
        "    rows = [name for name in ['hassio']]\n    MockModule(name)\n"
    ),
    "name-replaced-before-a-comprehension-reads-it": (
        "def test_it(hass):\n    name = 'hassio'\n    name = 'made_up_probe'\n"
        "    rows = [MockModule(name) for _ in range(2)]\n"
    ),
    "built-in-name-the-tests-define": (
        "def sorted(rows):\n    return ['made_up_probe']\n"
        "def test_it(hass):\n    for name in sorted(['hassio']):\n"
        "        MockModule(name)\n"
    ),
    "mark-parametrize-of-a-local-object": (
        "class Fake:\n    class mark:\n        @staticmethod\n"
        "        def parametrize(names, rows):\n"
        "            return lambda function: function\n"
        "fake = Fake()\n@fake.mark.parametrize('name', ['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "target-before-a-starred-one": (
        "for name, *extras, label in [('made_up_probe', 'hassio', 'x')]:\n"
        "    MockModule(name)\n"
    ),
    "spread-call-with-a-default": (
        "def register(hass, name='hassio'):\n    MockModule(name)\n"
        "register(hass, **{'name': 'made_up_probe'})\n"
    ),
    "starred-call-with-a-default": (
        "def register(a, b='sio'):\n    MockModule(a + b)\n"
        "register('has', *('_probe',))\n"
    ),
    "helper-called-per-loop-row": (
        "def register(hass, a, b):\n    MockModule(a + b)\n"
        "for a, b in [('has', '_probe'), ('made_up_', 'sio')]:\n    register(hass, a, b)\n"
    ),
    "loop-row-columns-joined": (
        "for a, b in [('has', '_probe'), ('made_up_', 'sio')]:\n    MockModule(a + b)\n"
    ),
    "default-named-before-a-reassignment": (
        "def test_it(hass):\n    domain = 'made_up_probe'\n"
        "    def register(name=domain):\n        MockModule(name)\n"
        "    domain = 'hassio'\n    register()\n"
    ),
    "branch-then-unconditional-assignment": (
        "def test_it(hass, flag):\n    name = 'hassio'\n    if flag:\n"
        "        name = 'other_probe'\n    name = 'made_up_probe'\n"
        "    MockModule(name)\n"
    ),
    "keyword-for-a-positional-only-parameter": (
        "def register(name='made_up_probe', /, **options):\n    MockModule(name)\n"
        "register(name='hassio')\n"
    ),
    "class-named-pytest": (
        "class pytest:\n    @staticmethod\n    def param(_):\n"
        "        return 'made_up_probe'\n"
        "for name in [pytest.param('hassio')]:\n    MockModule(name)\n"
    ),
    "helper-many-suffixes": (
        "def register(hass, base):\n"
        "    for s in ('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'):\n"
        "        mock_component(hass, f'{base}_{s}')\n"
        "register(hass, 'made_up')\n"
    ),
    "parameter-reassigned": (
        "def helper(hass, name):\n    name = 'made_up_probe'\n"
        "    mock_component(hass, name)\n"
        "helper(hass, 'hassio')\n"
    ),
    "zip-stops-short": (
        "for n, name in zip((0,), ('made_up_probe', 'hassio')):\n    MockModule(name)\n"
    ),
    "helper-prefix-made-up": (
        "def register(hass, prefix):\n    mock_component(hass, f'{prefix}sio')\n"
        "register(hass, 'demo_')\n"
    ),
    "other-helpers-arguments": (
        "def register(hass, name):\n    mock_component(hass, name)\n"
        "def unrelated(name):\n    pass\n"
        "register(hass, 'made_up_probe')\nunrelated('hassio')\n"
    ),
}


@pytest.mark.parametrize("source", ALLOWED.values(), ids=ALLOWED.keys())
def test_a_made_up_domain_or_a_real_setup_passes(guard, tmp_path, capsys, source):
    status, out = run_guard(guard, _repo(tmp_path, {_SAMPLE: source}), capsys)

    assert (status, out) == (
        0,
        "No unlisted stand-in for a real integration; 0 allowlisted.\n",
    )


def test_an_allowlisted_case_passes(guard, tmp_path, capsys):
    repo = _repo(tmp_path, {_SAMPLE: "mock_component(hass, 'hassio')\n"}, _LISTED)

    assert run_guard(guard, repo, capsys) == (
        0,
        "No unlisted stand-in for a real integration; 1 allowlisted.\n",
    )


def test_an_allowlist_entry_that_matches_nothing_fails(guard, tmp_path, capsys):
    repo = _repo(tmp_path, {_SAMPLE: "MockModule('made_up_probe')\n"}, _LISTED)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        "tests/src/unit/integration_stand_in_allowlist.yml: "
        f"{_SAMPLE} no longer registers 'hassio'; remove the entry"
    )


def test_an_entry_allows_only_its_own_file(guard, tmp_path, capsys):
    other = "tests/addon/test_other.py"
    repo = _repo(
        tmp_path,
        {_SAMPLE: "mock_component(hass, 'hassio')\n", other: "MockModule('hassio')\n"},
        _LISTED,
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{other}:1: ")


@pytest.mark.parametrize(
    "allowlist",
    [
        f"- file: {_SAMPLE}\n  integration: hassio\n",
        f"- file: {_SAMPLE}\n  integration: hassio\n  reason: ''\n",
        _LISTED + "  extra: 1\n",
        _LISTED + "  1: x\n",
        f"- file: {_SAMPLE}\n  integration: hassio\n  reason: '   '\n",
    ],
    ids=["no-reason", "empty-reason", "unknown-key", "non-string-key", "blank-reason"],
)
def test_an_entry_without_exactly_a_file_integration_and_reason_fails(
    guard, tmp_path, capsys, allowlist
):
    repo = _repo(tmp_path, {_SAMPLE: "mock_component(hass, 'hassio')\n"}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "entry 1 needs exactly a non-empty file, integration, reason" in out


def test_a_key_repeated_in_an_entry_fails(guard, tmp_path, capsys):
    allowlist = _LISTED.replace("  reason:", "  reason: ''\n  reason:")
    repo = _repo(tmp_path, {_SAMPLE: "mock_component(hass, 'hassio')\n"}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "integration_stand_in_allowlist.yml:4: repeats the key 'reason'" in out


def test_an_entry_cannot_silence_the_checks_own_failures(guard, tmp_path, capsys):
    """More names than are checked stays a failure with its marker allowlisted."""
    names = ", ".join(f"'{letter}'" for letter in "abcdefghijklmnopq")
    source = (
        f"A = [{names}]\nB = [{names}]\n"
        "for a in A:\n    for b in B:\n        MockModule(f'{a}{b}')\n"
    )
    allowlist = _LISTED.replace("integration: hassio", 'integration: "\\x01"')
    repo = _repo(tmp_path, {_SAMPLE: source}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert "entry 1 names no integration: '\\x01'" in out
    assert f"{_SAMPLE}:5: more than 256 possible names to check" in out


@pytest.mark.parametrize(
    ("allowlist", "problem"),
    [
        pytest.param("{}\n", "must be a list of entries", id="mapping"),
        pytest.param("null\n", "must be a list of entries", id="null"),
        pytest.param("~\n", "must be a list of entries", id="tilde"),
        pytest.param("&rows [*rows]\n", "cannot be read", id="holds-itself"),
    ],
)
def test_an_allowlist_that_is_not_a_list_of_entries_fails(
    guard, tmp_path, capsys, allowlist, problem
):
    repo = _repo(tmp_path, {_SAMPLE: "MockModule('made_up_probe')\n"}, allowlist)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert problem in out


def test_a_refused_entry_still_names_its_file(guard, tmp_path):
    """The ratchet compares two versions of the list, also when one is bad."""
    listed = tmp_path / "allowlist.yml"
    listed.write_text(f"- file: {_SAMPLE}\n  integration: hassio\n  reason: ''\n")
    problems: list[str] = []

    assert guard.load_allowlist(listed, problems) == {(_SAMPLE, "hassio"): ""}
    assert problems


def test_no_test_stands_in_for_a_real_integration_unlisted(guard, capsys):
    assert guard.main([]) == 0, capsys.readouterr().out
