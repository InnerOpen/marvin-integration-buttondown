"""What Buttondown teaches Marvin (SDK 0.9.0): its guide and the opt-in recipe."""

import pytest
from marvin_integration_sdk import check_playbook, check_recipe, load_guide
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


# --- the Playbook (0.10.0, SDK 0.15.0) ---------------------------------------------------------------------


def _playbook():
    (blueprint,) = [c for c in ButtondownProvider.content if c.kind == "playbook"]
    return blueprint


def test_the_provider_ships_the_newsletter_issue_playbook():
    blueprint = _playbook()
    assert (blueprint.slug, blueprint.required) == ("buttondown-newsletter-issue", False)
    assert sorted(blueprint.payload["files"]) == ["references/checklist.md", "references/example-issue.md"]
    assert check_playbook(blueprint, ButtondownProvider()) == []


def test_the_playbook_tells_the_agent_to_write_and_save_a_draft_itself():
    body = _playbook().payload["skill_md"]
    assert "`create_draft`" in body and "`update_draft`" in body
    assert "`compose_entry`" in body and "`revise_entry`" in body  # named as the ones not to use
    assert "never publish" in body.lower() or "never publishes" in body.lower() or "You never publish" in body


def test_the_playbook_names_the_workflow_by_its_slug_and_the_workflow_exists():
    from marvin_integration_buttondown.content import ISSUE_ON_PUBLISH

    assert "buttondown-issue-on-publish" in _playbook().payload["skill_md"]
    assert ISSUE_ON_PUBLISH.slug == "buttondown-issue-on-publish"


def test_the_playbook_files_are_package_data():
    from importlib import resources

    root = resources.files("marvin_integration_buttondown").joinpath("playbooks", "buttondown-newsletter-issue")
    assert root.joinpath("SKILL.md").is_file() and root.joinpath("references", "checklist.md").is_file()
