# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool for adding, pausing, removing and re-matching keywords and negative keywords."""

from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

keywords_mcp = FastMCP("keywords")

MAX_KEYWORD_CHANGES = 1000


class KeywordAction(str, Enum):
    """Supported keyword changes."""

    CREATE = "CREATE"
    PAUSE = "PAUSE"
    ENABLE = "ENABLE"
    REMOVE = "REMOVE"
    CHANGE_MATCH_TYPE = "CHANGE_MATCH_TYPE"


class KeywordMatchType(str, Enum):
    """Keyword match types."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class KeywordChange(BaseModel):
    """One keyword change.

    CREATE needs `text` and `match_type`. PAUSE, ENABLE and REMOVE need
    `criterion_id`. CHANGE_MATCH_TYPE needs `criterion_id` and the new
    `match_type`, and optionally a new spelling in `text`.
    """

    action: KeywordAction
    text: str | None = None
    match_type: KeywordMatchType | None = None
    criterion_id: str | None = None


def _value(enum_value: Enum | str | None) -> str | None:
    if enum_value is None:
        return None
    return (
        enum_value.value
        if isinstance(enum_value, Enum)
        else str(enum_value).upper()
    )


def _require_id(value: str | int, name: str) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(f"Error: '{name}' must be a numeric ID; got '{value}'.")
    return text


def _validate_change(
    index: int, change: KeywordChange, campaign_scope: bool
) -> None:
    action = _value(change.action)
    label = f"Change {index + 1} ({action})"
    if campaign_scope and action not in ("CREATE", "REMOVE"):
        raise ToolError(
            f"Error: {label}: campaign negatives support only CREATE and REMOVE."
        )
    if action == "CREATE":
        if not (change.text and change.text.strip()) or not change.match_type:
            raise ToolError(f"Error: {label} requires 'text' and 'match_type'.")
        return
    if not change.criterion_id:
        raise ToolError(f"Error: {label} requires 'criterion_id'.")
    _require_id(change.criterion_id, "criterion_id")
    if action == "CHANGE_MATCH_TYPE" and not change.match_type:
        raise ToolError(f"Error: {label} requires the new 'match_type'.")


def _lookup_criteria(
    client: Any,
    customer_id: str,
    scope: str,
    scope_id: str,
    criterion_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not criterion_ids:
        return {}
    service = utils.get_googleads_service_from_client(
        client, "GoogleAdsService"
    )
    entity = "ad_group" if scope == "ad_group" else "campaign"
    criterion = f"{entity}_criterion"
    query = (
        f"SELECT {criterion}.criterion_id, {criterion}.keyword.text, "
        f"{criterion}.keyword.match_type, {criterion}.status, "
        f"{criterion}.negative FROM {criterion} "
        f"WHERE {entity}.id = {scope_id} "
        f"AND {criterion}.type = 'KEYWORD' "
        f"AND {criterion}.criterion_id IN ({', '.join(criterion_ids)})"
    )
    found = {}
    for batch in service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            item = getattr(row, criterion)
            found[str(item.criterion_id)] = {
                "text": item.keyword.text,
                "match_type": item.keyword.match_type.name,
                "status": item.status.name,
                "negative": item.negative,
            }
    return found


@keywords_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def manage_keywords(
    customer_id: str | int,
    changes: list[KeywordChange],
    ad_group_id: str | int | None = None,
    campaign_id: str | int | None = None,
    negative: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds, pauses, enables, removes or re-matches keywords in one atomic request.

    Scope: give exactly one of `ad_group_id` (keywords or, with
    `negative=True`, ad group negatives) or `campaign_id` (campaign negatives
    only; requires `negative=True`, supports CREATE and REMOVE).

    Actions per change:
    - CREATE: `text` and `match_type`.
    - PAUSE / ENABLE / REMOVE: `criterion_id` (the keyword's criterion ID).
    - CHANGE_MATCH_TYPE: `criterion_id` and the new `match_type`, optionally a
      new spelling in `text`. Google Ads cannot change a keyword's match type
      in place, so this creates the new keyword and pauses the old one.

    Check ideas with `keyword_planner_compare_keyword_ideas` first: it shows
    keywords that already exist or that negatives would block.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `manage_keywords` with `validate_only=True` (the default). After validation passes, the tool returns structured `change_items` and a `checksum` without committing changes.
    2. Present the `change_items` to the user and wait for explicit user approval.
    3. Only after user confirmation, call `manage_keywords` with `validate_only=False` along with the exact same parameters and the `checksum` returned by the previous `validate_only=True` call.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        changes: The changes to apply, e.g. [{"action": "CREATE", "text": "z-wave hub", "match_type": "EXACT"}].
        ad_group_id: Target ad group ID.
        campaign_id: Target campaign ID, for campaign negatives.
        negative: True to manage negative keywords.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
        validate_only: If True (default), validates and returns proposed `change_items` and a `checksum` without changing anything. If False, requires `checksum` from a prior `validate_only=True` call to commit.
        checksum: Checksum returned by a prior `validate_only=True` call. Required when `validate_only=False`.
    """
    cleaned_customer_id = utils.clean_customer_id(customer_id)
    if not cleaned_customer_id:
        raise ToolError("Error: 'customer_id' must contain digits.")
    if not changes:
        raise ToolError("Error: 'changes' must not be empty.")
    if len(changes) > MAX_KEYWORD_CHANGES:
        raise ToolError(
            f"Error: at most {MAX_KEYWORD_CHANGES} changes are allowed per "
            f"call; got {len(changes)}."
        )
    if (ad_group_id is None) == (campaign_id is None):
        raise ToolError(
            "Error: provide exactly one of 'ad_group_id' or 'campaign_id'."
        )
    if campaign_id is not None and not negative:
        raise ToolError(
            "Error: keywords live in ad groups; 'campaign_id' is only for "
            "negative keywords (negative=True)."
        )

    scope = "ad_group" if ad_group_id is not None else "campaign"
    scope_id = _require_id(
        ad_group_id if scope == "ad_group" else campaign_id, f"{scope}_id"
    )
    for index, change in enumerate(changes):
        _validate_change(index, change, scope == "campaign")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = client.enums
    if scope == "ad_group":
        service_name, operation_type = (
            "AdGroupCriterionService",
            "AdGroupCriterionOperation",
        )
        request_type, mutate_name = (
            "MutateAdGroupCriteriaRequest",
            "mutate_ad_group_criteria",
        )
        parent_field = "ad_group"
        parent = f"customers/{cleaned_customer_id}/adGroups/{scope_id}"
        collection = "adGroupCriteria"
    else:
        service_name, operation_type = (
            "CampaignCriterionService",
            "CampaignCriterionOperation",
        )
        request_type, mutate_name = (
            "MutateCampaignCriteriaRequest",
            "mutate_campaign_criteria",
        )
        parent_field = "campaign"
        parent = f"customers/{cleaned_customer_id}/campaigns/{scope_id}"
        collection = "campaignCriteria"

    def criterion_resource(criterion_id: str) -> str:
        return (
            f"customers/{cleaned_customer_id}/{collection}/"
            f"{scope_id}~{criterion_id}"
        )

    referenced_ids = sorted(
        {str(c.criterion_id).strip() for c in changes if c.criterion_id}
    )
    try:
        existing = _lookup_criteria(
            client, cleaned_customer_id, scope, scope_id, referenced_ids
        )
    except GoogleAdsException as exc:
        raise ToolError(
            "Failed to look up keywords:\n" + utils.format_google_ads_error(exc)
        ) from exc
    missing = [i for i in referenced_ids if i not in existing]
    if missing:
        raise ToolError(
            f"Error: criterion IDs not found in {scope} {scope_id}: "
            f"{', '.join(missing)}."
        )
    wrong_kind = [
        i for i in referenced_ids if existing[i]["negative"] != negative
    ]
    if wrong_kind:
        kind = "negative" if negative else "positive"
        raise ToolError(
            f"Error: criterion IDs are not {kind} keywords: "
            f"{', '.join(wrong_kind)}."
        )

    entity_readable = "Negative keyword" if negative else "Keyword"
    operations = []
    operation_labels: list[str] = []
    change_items: list[pac.ChangeItem] = []

    def create(text: str, match_type: str) -> None:
        operation = client.get_type(operation_type)
        criterion = operation.create
        setattr(criterion, parent_field, parent)
        criterion.keyword.text = text
        criterion.keyword.match_type = enums.KeywordMatchTypeEnum[match_type]
        if negative:
            criterion.negative = True
        elif scope == "ad_group":
            criterion.status = enums.AdGroupCriterionStatusEnum.ENABLED
        operations.append(operation)
        operation_labels.append(f"CREATE '{text}' {match_type}")
        change_items.extend(
            pac.build_change_items(
                operation="CREATE",
                entity_readable=entity_readable,
                primary_key_readable=f"{scope} {scope_id}: {text}",
                fields={"Text": text, "Match type": match_type},
            )
        )

    def set_status(criterion_id: str, status: str) -> None:
        operation = client.get_type(operation_type)
        operation.update.resource_name = criterion_resource(criterion_id)
        operation.update.status = enums.AdGroupCriterionStatusEnum[status]
        operation.update_mask.paths.append("status")
        operations.append(operation)
        current = existing[criterion_id]
        operation_labels.append(
            f"{status} '{current['text']}' {current['match_type']}, "
            f"ID {criterion_id}"
        )
        change_items.extend(
            pac.build_change_items(
                operation="UPDATE",
                entity_readable=entity_readable,
                primary_key_readable=(
                    f"{scope} {scope_id}: {current['text']} "
                    f"({current['match_type']}, ID {criterion_id})"
                ),
                fields={"Status": status},
                original_fields={"Status": current["status"]},
            )
        )

    def remove(criterion_id: str) -> None:
        operation = client.get_type(operation_type)
        operation.remove = criterion_resource(criterion_id)
        operations.append(operation)
        current = existing[criterion_id]
        operation_labels.append(
            f"REMOVE '{current['text']}' {current['match_type']}, "
            f"ID {criterion_id}"
        )
        change_items.extend(
            pac.build_change_items(
                operation="REMOVE",
                entity_readable=entity_readable,
                primary_key_readable=(
                    f"{scope} {scope_id}: {current['text']} "
                    f"({current['match_type']}, ID {criterion_id})"
                ),
                fields={"Resource Name": criterion_resource(criterion_id)},
            )
        )

    for change in changes:
        action = _value(change.action)
        criterion_id = (
            str(change.criterion_id).strip() if change.criterion_id else None
        )
        if action == "CREATE":
            create(change.text.strip(), _value(change.match_type))
        elif action == "PAUSE":
            set_status(criterion_id, "PAUSED")
        elif action == "ENABLE":
            set_status(criterion_id, "ENABLED")
        elif action == "REMOVE":
            remove(criterion_id)
        else:
            current = existing[criterion_id]
            new_text = (change.text or current["text"]).strip()
            new_match_type = _value(change.match_type)
            if (
                new_text == current["text"]
                and new_match_type == current["match_type"]
            ):
                raise ToolError(
                    f"Error: keyword {criterion_id} is already "
                    f"'{new_text}' ({new_match_type})."
                )
            create(new_text, new_match_type)
            if current["status"] != "PAUSED":
                set_status(criterion_id, "PAUSED")

    service = utils.get_googleads_service_from_client(client, service_name)
    mutate = getattr(service, mutate_name)
    request = client.get_type(request_type)
    request.customer_id = cleaned_customer_id
    request.operations.extend(operations)

    operation_payload = {
        "tool": "manage_keywords",
        "scope": scope,
        "scope_id": scope_id,
        "negative": negative,
        "changes": [c.model_dump(mode="json") for c in changes],
    }

    def _mutate(validate: bool):
        request.validate_only = validate
        try:
            return mutate(request=request)
        except GoogleAdsException as exc:
            raise ToolError(
                "Failed to manage keywords:\n"
                + utils.format_google_ads_error(exc, operation_labels)
            ) from exc

    def _validate() -> None:
        _mutate(validate=True)

    def _commit() -> str:
        response = _mutate(validate=False)
        ids = [utils.extract_id(r.resource_name) for r in response.results]
        return (
            f"Successfully applied {len(ids)} keyword operations in {scope} "
            f"{scope_id}. Resource IDs: {', '.join(ids)}"
        )

    return pac.propose_and_confirm(
        customer_id=cleaned_customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to manage keywords",
    )
