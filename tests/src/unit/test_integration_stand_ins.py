"""No test stands in for a real integration with Core's test helpers.

``scripts/integration_stand_ins.py`` runs here on a small repository written
for each test, against the installed Home Assistant's integrations, and once
on this checkout.
"""

from __future__ import annotations

import pytest

from ._guard_run import run_guard
from ._integration_stand_ins_repo import SAMPLE, sample_repo
from ._integration_stand_ins_repo import guard as guard

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
    "alias-of-a-helper-read-off-its-module": (
        "import pytest_homeassistant.common as common\n"
        "register = common.mock_component\nregister(None, 'hassio')\n"
    ),
    "alias-of-components-add": (
        "def test_it(hass):\n    add = hass.config.components.add\n    add('hassio')\n"
    ),
    "two-lists-added": (
        "for name in ['made_up_probe'] + ['hassio']:\n    MockModule(name)\n"
    ),
    "list-chosen-by-a-condition": (
        "for name in (['hassio'] if flag else ['made_up_probe']):\n"
        "    MockModule(name)\n"
    ),
    "global-set-by-a-later-function": (
        "NAME = 'made_up_probe'\ndef test_it(hass):\n    MockModule(NAME)\n"
        "def change():\n    global NAME\n    NAME = 'hassio'\n"
    ),
    "nonlocal-set-by-a-nested-function": (
        "def test_it(hass):\n    name = 'made_up_probe'\n"
        "    def change():\n        nonlocal name\n        name = 'hassio'\n"
        "    change()\n    MockModule(name)\n"
    ),
    "starred-assignment-target-after-the-star": (
        "label, *extras, name = ('case', 'hassio')\nMockModule(name)\n"
    ),
    "parametrize-names-with-a-trailing-comma": (
        "@pytest.mark.parametrize('name,', ['hassio'])\n"
        "def test_it(hass, name):\n    MockModule(name)\n"
    ),
    "parametrize-names-list-with-a-name-in-it": (
        "FIRST = 'label'\n@pytest.mark.parametrize("
        "[FIRST, 'other', 'name'], [('a', 'made_up_probe', 'hassio')])\n"
        "def test_it(hass, label, other, name):\n    MockModule(name)\n"
    ),
    "assignment-replaced-by-an-import": (
        "def test_it(hass):\n    name = 'made_up_probe'\n"
        "    from homeassistant.components.hassio.const import DOMAIN as name\n"
        "    MockModule(name)\n"
    ),
    "loop-over-a-loops-variable": (
        "for names in [['hassio']]:\n    for name in names:\n        MockModule(name)\n"
    ),
    "loop-over-a-column-of-a-loops-rows": (
        "for label, names in [('a', ['hassio'])]:\n    for name in names:\n"
        "        MockModule(name)\n"
    ),
    "loop-over-a-dicts-lists": (
        "for key, names in {'a': ['hassio']}.items():\n    for name in names:\n"
        "        MockModule(name)\n"
    ),
    "loop-over-a-parametrized-list": (
        "@pytest.mark.parametrize('names', [['hassio']])\n"
        "def test_it(hass, names):\n    for name in names:\n"
        "        MockModule(name)\n"
    ),
    "comprehension-over-a-loops-variable": (
        "for names in [['hassio']]:\n    made = [MockModule(n) for n in names]\n"
    ),
    "second-iterable-of-a-comprehension": (
        "rows = [MockModule(name) for names in [['hassio']] for name in names]\n"
    ),
    "row-given-by-a-name": (
        "ROW = ('a', 'hassio')\nfor label, name in [ROW]:\n    MockModule(name)\n"
    ),
    "first-iterable-of-a-comprehension-read-outside-it": (
        "name = 'hassio'\nrows = [None for name in [MockModule(name)]]\n"
    ),
    "comprehension-target-named-like-its-iterable": (
        "names = ['hassio']\nrows = [MockModule(names) for names in names]\n"
    ),
    "callee-that-is-cores-helper-on-one-path": (
        "try:\n    from pytest_homeassistant.common import mock_component\n"
        "except ImportError:\n    def mock_component(hass, name):\n        pass\n"
        "mock_component(None, 'hassio')\n"
    ),
    "callee-imported-from-a-module-that-cannot-be-read": (
        "try:\n    from nowhere_at_all import mock_component\n"
        "except ImportError:\n    def mock_component(hass, name):\n        pass\n"
        "mock_component(None, 'hassio')\n"
    ),
    "earlier-value-after-a-loop-over-its-name": (
        "def test_it(hass):\n    name = 'hassio'\n"
        "    for name in ['made_up_probe']:\n        pass\n    MockModule(name)\n"
    ),
    "nested-loop-target": (
        "for (name, _), label in [(('hassio', 0), 'case')]:\n    MockModule(name)\n"
    ),
    "pair-from-enumerate": (
        "for i, (label, name) in enumerate([('x', 'hassio')]):\n    MockModule(name)\n"
    ),
    "pair-from-a-dicts-items": (
        "for key, (label, name) in {'a': ('x', 'hassio')}.items():\n"
        "    MockModule(name)\n"
    ),
    "fixtures-default-with-a-direct-call-too": (
        "@pytest.fixture\ndef made(name='hassio'):\n    MockModule(name)\n"
        "def test_other():\n    made('made_up_probe')\n"
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
    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert out.startswith(f"{SAMPLE}:")
    assert "'hassio' integration" in out.splitlines()[0]


def test_a_name_passed_to_a_helper_in_another_module_is_refused_at_the_call(
    guard, tmp_path, capsys
):
    helper = (
        "def register(hass, domain):\n    mock_integration(hass, MockModule(domain))\n"
    )
    test = "from ._helpers import register\n\n\nregister(hass, 'frontend')\n"
    repo = sample_repo(tmp_path, {"tests/src/unit/_helpers.py": helper, SAMPLE: test})

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        f"{SAMPLE}:4: a stand-in registered under Home Assistant's 'frontend' "
        "integration through register()"
    )


def test_a_helper_re_exported_under_another_name_is_refused(guard, tmp_path, capsys):
    helper = "def register(hass, name):\n    mock_component(hass, name)\n"
    test = "from ._reexport import install\n\n\ninstall(hass, 'hassio')\n"
    repo = sample_repo(
        tmp_path,
        {
            "tests/src/unit/_helpers.py": helper,
            "tests/src/unit/_reexport.py": "from ._helpers import register as install\n",
            SAMPLE: test,
        },
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        f"{SAMPLE}:4: a stand-in registered under Home Assistant's 'hassio' "
        "integration through register()"
    )


def test_a_constant_from_a_local_helper_module_is_refused(guard, tmp_path, capsys):
    test = "from ._names import NAME\n\n\nMockModule(NAME)\n"
    repo = sample_repo(
        tmp_path, {"tests/src/unit/_names.py": "NAME = 'hassio'\n", SAMPLE: test}
    )

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == (
        f"{SAMPLE}:4: a stand-in registered under Home Assistant's 'hassio' integration"
    )


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

    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert out.startswith(f"{SAMPLE}:3: more than ")


def test_a_helper_wrapping_its_own_name_without_end_fails(guard, tmp_path, capsys):
    source = (
        "def register(hass, prefix):\n    if len(prefix) < 6:\n"
        "        return register(hass, prefix + 'x')\n"
        "    mock_component(hass, f'{prefix}sio')\n"
        "register(hass, 'demo_')\n"
    )

    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert f"{SAMPLE}:3: more than 8 helpers add text to the name" in out


def test_too_many_names_through_a_helper_fail(guard, tmp_path, capsys):
    """Each argument stays under the limit; together they are 289 names."""
    values = ", ".join(f"'m{number}'" for number in range(17))
    source = (
        "def register(hass, a, b):\n    MockModule(a + b)\n"
        f"for a in ({values}):\n    for b in ({values}):\n"
        "        register(hass, a, b)\n"
    )

    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert f"{SAMPLE}:5: more than 256 possible names to check" in out


def test_a_name_too_many_names_from_its_value_fails(guard, tmp_path, capsys):
    """Without a bound the chain ends the check in a recursion error."""
    chain = "".join(f"domain_{n} = domain_{n - 1}\n" for n in range(1, 600))
    source = f"domain_0 = 'hassio'\n{chain}MockModule(domain_599)\n"

    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert f"{SAMPLE}:601: more than 256 possible names to check, or a name" in out


def test_unparseable_source_fails_with_its_line(guard, tmp_path, capsys):
    repo = sample_repo(tmp_path, {SAMPLE: "x = 1\ndef broken(:\n"})

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.startswith(f"{SAMPLE}:2: ")


ALLOWED = {
    "decorator-named-like-a-fixture": (
        "def fixture_label(function):\n    return function\n"
        "@fixture_label\ndef register(name='hassio'):\n    MockModule(name)\n"
        "register('made_up_probe')\n"
    ),
    "fixture-attribute-of-an-object-of-the-tests": (
        "class Tools:\n    @staticmethod\n    def fixture(function):\n"
        "        return function\ntools = Tools()\n"
        "@tools.fixture\ndef register(name='hassio'):\n    MockModule(name)\n"
        "register('made_up_probe')\n"
    ),
    "loop-target-inside-its-loop": (
        "def test_it(hass):\n    name = 'hassio'\n"
        "    for name in ['made_up_probe']:\n        MockModule(name)\n"
    ),
    "nested-loop-target": (
        "for (name, _), label in [(('made_up_probe', 0), 'case')]:\n"
        "    MockModule(name)\n"
    ),
    "parts-of-one-nested-row": (
        "for (a, b), c in [(('has', 'x'), 1), (('made_up_', 'sio'), 2)]:\n"
        "    MockModule(a + b)\n"
    ),
    "parametrized-test-also-called-directly": (
        "@pytest.mark.parametrize('name', ['made_up_probe'])\n"
        "def test_register(name):\n    MockModule(name)\n"
        "def test_other():\n    test_register('x_probe')\n"
    ),
    "first-iterable-of-a-comprehension-read-outside-it": (
        "name = 'made_up_probe'\nrows = [None for name in [MockModule(name)]]\n"
    ),
    "loop-over-a-loops-variable": (
        "for names in [['made_up_probe']]:\n    for name in names:\n"
        "        MockModule(name)\n"
    ),
    "loop-over-an-empty-inner-list": (
        "for names in [[]]:\n    for name in names:\n        MockModule(name)\n"
    ),
    "row-given-by-a-name": (
        "ROW = ('a', 'made_up_probe')\nfor label, name in [ROW]:\n"
        "    MockModule(name)\n"
    ),
    "argument-after-a-named-one-in-a-names-list": (
        "FIRST = 'label'\n@pytest.mark.parametrize("
        "[FIRST, 'other', 'name'], [('a', 'hassio', 'made_up_probe')])\n"
        "def test_it(hass, label, other, name):\n    MockModule(name)\n"
    ),
    "import-replaced-by-an-assignment": (
        "def test_it(hass):\n"
        "    from homeassistant.components.hassio.const import DOMAIN as name\n"
        "    name = 'made_up_probe'\n    MockModule(name)\n"
    ),
    "assignment-replaced-by-an-import": (
        "def test_it(hass):\n    name = 'hassio'\n"
        "    from homeassistant.const import ATTR_ENTITY_ID as name\n"
        "    MockModule(name)\n"
    ),
    "made-up-domain": "mock_integration(hass, MockModule('made_up_probe'))\n",
    "made-up-platform": "mock_platform(hass, 'made_up_probe.config_flow')\n",
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
    "helper-default-not-used": (
        "def register(hass, a, b='sio'):\n    MockModule(a + b)\n"
        "register(hass, 'has', '_probe')\n"
    ),
    "function-named-like-an-alias": (
        "def register(hass, name):\n    pass\nregister(hass, 'hassio')\n"
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
    "target-before-a-starred-one": (
        "for name, *extras, label in [('made_up_probe', 'hassio', 'x')]:\n"
        "    MockModule(name)\n"
    ),
    "local-named-like-a-global": (
        "NAME = 'made_up_probe'\ndef change():\n    NAME = 'hassio'\n"
        "def test_it(hass):\n    MockModule(NAME)\n"
    ),
    "with-as-of-another-name": (
        "def test_it(hass):\n    name = 'made_up_probe'\n"
        "    with ctx() as other:\n        MockModule(name)\n"
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
    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert (status, out) == (
        0,
        "No unlisted stand-in for a real integration; 0 allowlisted.\n",
    )


def test_no_test_stands_in_for_a_real_integration_unlisted(guard, capsys):
    assert guard.main([]) == 0, capsys.readouterr().out
