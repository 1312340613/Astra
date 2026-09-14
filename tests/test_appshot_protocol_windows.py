import json
from pathlib import Path

import pytest

from agent.cli.appshots import AppshotValidationError, parse_appshot_manifest, parse_appshot_message
from agent.cli.appshot_protocol_windows import parse_windows_appshot_manifest, parse_windows_appshot_message

FIXTURES = Path(__file__).resolve().parents[1] / "native/appshot-core/Tests/Fixtures"
CASES = json.loads((FIXTURES / "appshot_protocol_cases_v2.json").read_text(encoding="utf8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_authoritative_windows_v2_contract(case):
    parser = parse_windows_appshot_manifest if case["kind"] == "manifest" else parse_windows_appshot_message
    if case["valid"]:
        value = parser(case["raw"])
        assert value["platform"] == "windows"
        legacy = parse_appshot_manifest if case["kind"] == "manifest" else parse_appshot_message
        with pytest.raises(AppshotValidationError):
            legacy(case["raw"])
    else:
        with pytest.raises(AppshotValidationError):
            parser(case["raw"])
