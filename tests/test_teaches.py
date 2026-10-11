"""What Buttondown teaches Marvin (SDK 0.9.0): its guide and the opt-in recipe."""

import pytest
from marvin_integration_sdk import check_recipe, load_guide
from marvin_integration_sdk.testing import IntegrationContract

from marvin_integration_buttondown import ButtondownProvider
from marvin_integration_buttondown.provider import ERROR_POLICY
from marvin_integration_buttondown.recipes import SUBSCRIBE_ON_OPT_IN


class TestButtondownContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return ButtondownProvider()


def test_the_guide_ships_with_the_package():
    guide = load_guide(ButtondownProvider)
    assert guide.startswith("# Buttondown") and "BUTTONDOWN_API_KEY" in guide and "create_issue_email" in guide


def test_the_guide_names_every_error_code_the_policy_handles():
    guide = load_guide(ButtondownProvider)
    assert all(f"`{code}`" in guide for code in ERROR_POLICY if code != "*")


def test_the_guide_names_every_action_and_the_recipe():
    guide = load_guide(ButtondownProvider)
    assert all(f"`{action.key}`" in guide for action in ButtondownProvider.actions)
    assert SUBSCRIBE_ON_OPT_IN.title in guide


def test_the_provider_offers_the_recipe():
    assert ButtondownProvider.recipes == (SUBSCRIBE_ON_OPT_IN,)
    assert check_recipe(SUBSCRIBE_ON_OPT_IN, ButtondownProvider()) == []


def test_the_opt_in_recipe_subscribes_only_ticked_unflagged_submissions_of_one_form():
    conditions = {c["field"]: c for c in SUBSCRIBE_ON_OPT_IN.definition["conditions"]}
    assert SUBSCRIBE_ON_OPT_IN.definition["trigger"] == {"type": "event", "event": "form_submission_received"}
    assert conditions["entry.entry_type"]["value"] == "{{form_entry_type}}"
    assert conditions["event.flagged"] == {"field": "event.flagged", "op": "neq", "value": True}
    assert conditions["event.submission_data.{{optin_field}}"]["value"] is True  # the checkbox must be ticked


def test_the_opt_in_recipe_subscribes_the_senders_address_through_the_connection():
    (step,) = SUBSCRIBE_ON_OPT_IN.definition["actions"]
    assert (step["action"], step["integration"]) == ("subscribe", "{{buttondown_integration}}")
    assert step["args"]["email"] == "${event.submission_data.{{email_field}}}"  # run-time template, filled when it runs
    assert step["args"]["ip_address"] == "${event.ip_address}"
    assert step["args"]["tags"] == ["{{tag}}"]
