"""A name ``scripts/integration_stand_ins.py`` cannot read is refused: one only
known at runtime, and one given to a helper handed on instead of called."""

from __future__ import annotations

import pytest

from ._guard_run import run_guard
from ._integration_stand_ins_repo import LISTED, SAMPLE, sample_repo
from ._integration_stand_ins_repo import guard as guard

# A line marked ``# unread: <callee>`` is refused as a name the check cannot
# read, and no other line is.
_UNREAD_SOURCE = """\
MockModule('made_up_probe')
MockModule(pick())  # unread: MockModule
def test_it(hass, name):
    mock_component(hass, name)  # unread: mock_component
    MockModule(name)  # unread: MockModule
MockModule(*names)  # unread: MockModule
MockModule(**options)  # unread: MockModule
def register(hass, name):
    mock_component(hass, name)
register(hass, 'made_up_probe')
register(*row)  # unread: register
def default(hass, name='made_up_probe'):
    mock_component(hass, name)
@pytest.mark.parametrize('name', ['made_up_probe'])
def test_each(hass, name):
    mock_component(hass, name)
rows = ['made_up_probe']
if flag:
    rows = load_rows()
for row in rows:
    MockModule(row)  # unread: MockModule
for none in []:
    MockModule(none)
@pytest.mark.parametrize('name', ['hassio'], indirect=True)
def test_indirect(hass, name):
    MockModule(name)  # unread: MockModule
for unbound in NOWHERE:
    MockModule(unbound)  # unread: MockModule
import functools
make = functools.partial(mock_component, None)  # unread: mock_component
helpers = [MockModule]  # unread: MockModule
apply(mock_platform, None, 'hassio')  # unread: mock_platform
def defaulted(name, make=MockModule): ...  # unread: MockModule
def maker():
    return mock_config_flow  # unread: mock_config_flow
chosen = MockModule if flag else dict  # unread: MockModule
def test_with(hass):
    name = 'hassio'
    with ctx() as name:
        MockModule(name)  # unread: MockModule
def test_except(hass):
    name = 'made_up_probe'
    try:
        pass
    except Exception as name:
        MockModule(name)  # unread: MockModule
def test_match(hass):
    name = 'made_up_probe'
    match pick():
        case str() as name:
            MockModule(name)  # unread: MockModule
rest = 'made_up_probe'
label, *rest = ('case', 'hassio')
MockModule(rest)  # unread: MockModule
for each in (n for n in ['hassio']):
    MockModule(each)  # unread: MockModule
for letter in 'ab':
    MockModule(letter)  # unread: MockModule
ROWS = {'a': ['hassio']}
for entry in ROWS['a']:
    MockModule(entry)  # unread: MockModule
for twice in ['hassio'] * 2:
    MockModule(twice)  # unread: MockModule
module = MockModule('made_up_probe')
assert isinstance(module, MockModule)
typed: MockModule = MockModule('made_up_probe')
alias = MockModule
alias('made_up_probe')
first, second = MockModule, mock_component
first('made_up_probe')
class Module(MockModule):
    pass
import pytest_homeassistant.common as common
made = functools.partial(common.mock_component, None)  # unread: common.mock_component
apply(hass.config.components.add, 'hassio')  # unread: hass.config.components.add
def test_with_pair(hass):
    name = 'made_up_probe'
    with ctx() as (name, other):
        MockModule(name)  # unread: MockModule
async def test_async_with(hass):
    name = 'made_up_probe'
    async with ctx() as name:
        MockModule(name)  # unread: MockModule
def test_match_rest(hass):
    others = found = 'made_up_probe'
    match pick():
        case [first, *others]:
            MockModule(others)  # unread: MockModule
        case {**found}:
            MockModule(found)  # unread: MockModule
def test_except_group(hass):
    name = 'made_up_probe'
    try:
        pass
    except* ValueError as name:
        MockModule(name)  # unread: MockModule
rest = 'made_up_probe'
*rest, = pick()
MockModule(rest)  # unread: MockModule
later = functools.partial(register, None)  # unread: register
list(map(register, rows))  # unread: register
def plain():
    pass
apply(plain)
"""


def test_a_name_the_check_cannot_read_is_refused_where_it_is_used(
    guard, tmp_path, capsys
):
    """Refused: a call's result, a fixture's value at each stand-in it reaches,
    ``*`` and ``**`` arguments, a collection one branch reads at runtime or
    the check cannot read, a parameter ``parametrize`` gives a fixture, a name
    bound by ``with``, ``except``, ``match`` or ``*rest``, and a helper (also
    one read off its module or ``hass.config.components``) given to
    ``partial``, put in a list, passed as an argument or a default, returned,
    or chosen by a condition.

    A helper of the tests' own that gives its name on to a stand-in is
    refused the same way when it is handed on.

    Not refused: a parameter a call, a default or ``parametrize`` fills, a
    loop over an empty list, a helper in ``isinstance`` or an annotation,
    under a plain alias, or as a base class, and a function handed on that
    registers nothing."""
    refused = [
        f"{SAMPLE}:{number}: {line.rpartition('# unread: ')[2]}() {guard.UNREAD}"
        for number, line in enumerate(_UNREAD_SOURCE.splitlines(), 1)
        if "# unread: " in line
    ]

    status, out = run_guard(
        guard, sample_repo(tmp_path, {SAMPLE: _UNREAD_SOURCE}), capsys
    )

    assert status == 1
    assert out.splitlines()[:-1] == refused


def test_no_entry_allows_a_name_the_check_cannot_read(guard, tmp_path, capsys):
    source = "mock_component(hass, 'hassio')\nMockModule(pick())\n"
    repo = sample_repo(tmp_path, {SAMPLE: source}, LISTED)

    status, out = run_guard(guard, repo, capsys)

    assert status == 1
    assert out.splitlines()[0] == f"{SAMPLE}:2: MockModule() {guard.UNREAD}"


# Refused as names the check cannot read, not as a real integration's.
UNREAD = {
    "runtime-name": "def test_it(hass, name):\n    mock_component(hass, name)\n",
    "call-named-like-param": (
        "for name in [make_param('hassio')]:\n    MockModule(name)\n"
    ),
    "function-named-param": (
        "def param(_):\n    return 'made_up_probe'\n"
        "for name in [param('hassio')]:\n    MockModule(name)\n"
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
    "spread-call-with-a-default": (
        "def register(hass, name='hassio'):\n    MockModule(name)\n"
        "register(hass, **{'name': 'made_up_probe'})\n"
    ),
    "starred-call-with-a-default": (
        "def register(a, b='sio'):\n    MockModule(a + b)\n"
        "register('has', *('_probe',))\n"
    ),
    "class-named-pytest": (
        "class pytest:\n    @staticmethod\n    def param(_):\n"
        "        return 'made_up_probe'\n"
        "for name in [pytest.param('hassio')]:\n    MockModule(name)\n"
    ),
    "string-looped-over": "NAME = 'hassio'\nfor ch in NAME:\n    MockModule(ch)\n",
    "helper-parameter-looped-over-twice": (
        "def register(groups):\n    for names in groups:\n"
        "        for name in names:\n            MockModule(name)\n"
        "register([['hassio']])\n"
    ),
    "row-too-short-for-its-targets": (
        "for a, b, name in [('x', 'y')]:\n    MockModule(name)\n"
    ),
    "row-that-is-a-call": "for label, name in [pick()]:\n    MockModule(name)\n",
    "loop-over-a-function": (
        "def rows():\n    return ['hassio']\nfor name in rows:\n    MockModule(name)\n"
    ),
    "method-that-registers-handed-on": (
        "class TestIt:\n    def register(self, name):\n        MockModule(name)\n"
        "    def test_it(self, hass):\n        self.register('made_up_probe')\n"
        "        apply(self.register, 'hassio')\n"
    ),
    "function-that-registers-returned-by-a-fixture": (
        "def register(name):\n    MockModule(name)\nregister('made_up_probe')\n"
        "@pytest.fixture\ndef maker():\n    return register\n"
    ),
    "test-with-a-fixture-also-called-directly": (
        "def test_register(name):\n    MockModule(name)\n"
        "def test_other():\n    test_register('made_up_probe')\n"
    ),
    "fixture-with-a-fixture-also-called-directly": (
        "@pytest.fixture\ndef made(name):\n    MockModule(name)\n"
        "def test_other():\n    made('made_up_probe')\n"
    ),
    "nested-loop-target-in-a-row-not-written-out": (
        "for (name, _), label in [(pick(), 'case')]:\n    MockModule(name)\n"
    ),
    "fixture-imported-by-name-also-called-directly": (
        "from pytest import fixture\n@fixture\ndef made(name):\n"
        "    MockModule(name)\ndef test_other():\n    made('made_up_probe')\n"
    ),
    "fixture-of-pytest-asyncio-also-called-directly": (
        "import pytest_asyncio\n@pytest_asyncio.fixture(scope='module')\n"
        "async def made(name):\n    MockModule(name)\n"
        "async def test_other():\n    await made('made_up_probe')\n"
    ),
    "item-written-into-a-list-from-a-call": (
        "names = ['made_up_probe']\nnames[0] = pick()\n"
        "for name in names:\n    MockModule(name)\n"
    ),
    "dict-updated-by-keyword": (
        "names = {'a': 'made_up_probe'}\nnames.update(b='hassio')\n"
        "for name in names.values():\n    MockModule(name)\n"
    ),
    "dict-updated-with-a-dict-and-a-keyword-values-read": (
        "names = {'a': 'made_up_probe'}\n"
        "names.update({'b': 'other_probe'}, c='hassio')\n"
        "for name in names.values():\n    MockModule(name)\n"
    ),
    "dict-updated-with-a-dict-and-a-keyword-keys-read": (
        "names = {'made_up_probe': 1}\nnames.update({'x_probe': 2}, hassio=3)\n"
        "for name in names:\n    MockModule(name)\n"
    ),
    "parametrize-names-that-cannot-be-read": (
        "@pytest.mark.parametrize([pick(), 'name'], [('a', 'hassio')])\n"
        "def test_it(hass, label, name):\n    MockModule(name)\n"
    ),
}


@pytest.mark.parametrize("source", UNREAD.values(), ids=UNREAD.keys())
def test_a_name_the_check_cannot_read_is_refused(guard, tmp_path, capsys, source):
    status, out = run_guard(guard, sample_repo(tmp_path, {SAMPLE: source}), capsys)

    assert status == 1
    assert "registered under" not in out
    assert f"() {guard.UNREAD}\n" in out
