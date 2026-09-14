import pytest

from agent.runtime.computer_forms import form_elements, image_actions, safe_action_receipt, verify_choice_goals
from agent.runtime.computer_protocol import ComputerAction


def test_deep_choices_are_exposed_with_current_refs_and_no_extra_values():
    root = {"role": "AXRadioButton", "element_ref": "current", "index": 35, "checked": False, "value": "private"}
    for i in range(34):
        root = {"role": "AXGroup", "label": f"Question group {i}", "children": [root]}
    controls = form_elements(root)
    assert controls["elements"][0]["element_ref"] == "current"
    assert controls["elements"][0]["checked"] is False
    assert "value" not in controls["elements"][0]
    assert not controls["truncated"]


def test_pixel_conversion_uses_actual_emitted_size_and_preserves_legacy_action():
    actions = [{"type": "click", "x": 501, "y": 251}]
    result = image_actions(actions, (1002, 502, 1500, 750))
    assert (result[0]["x"], result[0]["y"]) == (750, 375)
    assert actions[0]["x"] == 501
    for value in (-1, 1002, float("nan"), True):
        with pytest.raises(ValueError):
            image_actions([{"x": value}], (1002, 502, 1500, 750))


def test_final_choice_verification_detects_later_radio_state_changes_and_ambiguity():
    before = {"role": "AXRadioButton", "element_ref": "old", "index": 1, "label": "Answer", "checked": False}
    after = {**before, "element_ref": "fresh", "checked": True}
    actions = [ComputerAction(type="click", element_index=1, checked=True)]
    assert verify_choice_goals(before, after, actions)["status"] == "verified"
    after["checked"] = False
    assert verify_choice_goals(before, after, actions)["results"][0]["status"] == "not_satisfied"
    assert verify_choice_goals(before, {"children": [after, after]}, actions)["results"][0]["status"] == "unknown"


def test_durable_receipt_rejects_content_refs_and_unbounded_fields():
    assert (
        safe_action_receipt({"mode": "/private/file", "text": "secret", "snapshot_id": "ref", "action_count": True})
        == {}
    )
    assert safe_action_receipt({"channel": "native", "action_count": 10, "verified_count": 10}) == {
        "channel": "native",
        "action_count": 10,
        "verified_count": 10,
    }


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 1, "y": 2, "checked": True},
        {"type": "type", "text": "x", "element_ref": "ref", "checked": True},
        {"type": "click", "element_ref": "ref", "checked": 1},
    ],
)
def test_checked_requires_an_exact_boolean_choice_goal(action):
    with pytest.raises(ValueError):
        ComputerAction.from_mapping(action)


def test_choice_observation_identity_survives_layout_and_context_truncation():
    before = {
        "role": "AXRadioButton",
        "element_ref": "old",
        "index": 1,
        "observation_identity": "1:23",
        "label": "Answer",
        "checked": False,
        "context": "Long caption",
        "bounds": {"y": 0},
    }
    after = {
        **before,
        "element_ref": "new",
        "checked": True,
        "context": "Long caption truncated differently",
        "bounds": {"y": 100},
    }
    actions = [ComputerAction(type="click", element_index=1, checked=True)]
    assert verify_choice_goals(before, after, actions)["status"] == "verified"
    after["observation_identity"] = "2:23"
    assert verify_choice_goals(before, after, actions)["status"] == "not_verified"


def test_truncated_form_region_is_anchored_and_never_picks_between_web_forms():
    from agent.runtime.computer_forms import truncated_form_region

    form = {
        "role": "AXWebArea",
        "element_ref": "web",
        "children_truncated": True,
        "children": [{"role": "AXRadioButton", "element_ref": "choice"}],
    }
    root = {"observation_capabilities": ["subtree_v1"], "children": [form]}
    assert truncated_form_region(root) == "web"
    assert truncated_form_region({"children": [form]}) is None
    assert truncated_form_region({**root, "children": [form, form]}) is None
    assert form_elements(root)["truncated"] is True
