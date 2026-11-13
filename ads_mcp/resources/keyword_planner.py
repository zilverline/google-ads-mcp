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

"""Keyword Planner reference resources: usage guide, languages and countries."""

import functools
import json

from fastmcp.exceptions import ResourceError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.coordinator import mcp
import ads_mcp.utils as utils

GUIDE = """# Keyword Planner guide

## Access
Keyword Planner methods need Basic or Standard API access for the Google
Cloud project behind the developer token. Explorer and test access are
rejected with "not allowed for use with explorer access".

## Workflow
1. `keyword_planner_suggest_geo_targets` to find location IDs, and the
   `resource://keyword-planner/languages` resource for language IDs.
2. `keyword_planner_generate_keyword_ideas` with seed keywords, a URL or a
   site, once per market and language. Use `include_pattern` and
   `min_monthly_searches` to keep results small.
3. `keyword_planner_get_keyword_historical_metrics` for exact numbers on a
   shortlist, `keyword_planner_forecast_keywords` for expected clicks and cost.
4. `keyword_planner_generate_ad_group_themes` to pick the ad group.
5. `keyword_planner_compare_keyword_ideas` against the target ad group or
   campaign: it flags keywords that already exist and negatives that would
   block them.
6. `keywords_manage_keywords` to add, pause, remove or re-match keywords,
   after the user confirms the proposed changes.

## Reading the numbers
- Search volumes are rounded buckets; the minimum reported is 10. Small
  keywords often all show 10, 20 or 30.
- Word-order variants ('z wave hub', 'hub for z wave') often report the same
  shared volume. Do not add them up.
- Spelling variants ('z wave homekit', 'z-wave homekit') are grouped in
  historical metrics; `close_variants` lists them.
- Each request uses one language. Markets measured in a language that is not
  their own (e.g. Denmark in English) tend to mirror larger markets.
- Bids are top-of-page bid ranges in the account currency, not actual CPCs.
- Forecasts return clicks, cost, average CPC, conversions and average CPA,
  not impressions. Conversions are 0 without conversion history.

## Negative keywords
Negatives never match close variants, plurals or misspellings. A broad
negative blocks queries containing all its words, a phrase negative its words
in order, an exact negative only the identical query. Check shared and
account-level negative lists as well as campaign and ad group negatives.
"""


@mcp.resource(
    uri="resource://keyword-planner/guide",
    mime_type="text/markdown",
)
def get_keyword_planner_guide() -> str:
    """Retrieve the Keyword Planner usage guide.

    Explains the access level Keyword Planner needs, the recommended tool
    workflow from keyword ideas to adding keywords, how to read search
    volumes, bids and forecasts, and how negative keywords match.

    Returns:
        str: The guide in Markdown format.
    """
    return GUIDE


def _query_customer_id() -> str:
    """Constants are global, so any accessible customer can be queried."""
    login_customer_id = utils._get_login_customer_id()
    if login_customer_id:
        return login_customer_id
    customers = utils.get_googleads_service(
        "CustomerService"
    ).list_accessible_customers()
    if not customers.resource_names:
        raise ResourceError("No accessible Google Ads customers found.")
    return customers.resource_names[0].removeprefix("customers/")


def _query(query: str, fields: dict[str, str]) -> str:
    try:
        customer_id = _query_customer_id()
        service = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=customer_id
        )
        rows = []
        for batch in service.search_stream(
            customer_id=customer_id, query=query
        ):
            for row in batch.results:
                entry = {}
                for key, path in fields.items():
                    value = row
                    for part in path.split("."):
                        value = getattr(value, part)
                    entry[key] = str(value) if key == "id" else value
                rows.append(entry)
    except GoogleAdsException as ex:
        raise ResourceError(utils.format_google_ads_error(ex)) from ex
    return json.dumps(rows, ensure_ascii=False)


@mcp.resource(
    uri="resource://keyword-planner/languages",
    mime_type="application/json",
)
@functools.cache
def get_keyword_planner_languages() -> str:
    """Retrieve the targetable languages with their language constant IDs.

    Use the `id` as `language_id` / `language_ids` in the Keyword Planner
    tools, e.g. 1000 English, 1001 German, 1002 French, 1010 Dutch.

    Returns:
        str: A JSON list of {id, code, name}.
    """
    return _query(
        "SELECT language_constant.id, language_constant.code, "
        "language_constant.name FROM language_constant "
        "WHERE language_constant.targetable = TRUE "
        "ORDER BY language_constant.name",
        {
            "id": "language_constant.id",
            "code": "language_constant.code",
            "name": "language_constant.name",
        },
    )


@mcp.resource(
    uri="resource://keyword-planner/countries",
    mime_type="application/json",
)
@functools.cache
def get_keyword_planner_countries() -> str:
    """Retrieve all countries with their geo target constant IDs.

    Use the `id` in `geo_target_ids`, e.g. 2528 Netherlands, 2056 Belgium,
    2276 Germany, 2840 United States. For regions and cities use the
    `keyword_planner_suggest_geo_targets` tool.

    Returns:
        str: A JSON list of {id, country_code, name}.
    """
    return _query(
        "SELECT geo_target_constant.id, geo_target_constant.country_code, "
        "geo_target_constant.name FROM geo_target_constant "
        "WHERE geo_target_constant.target_type = 'Country' "
        "AND geo_target_constant.status = 'ENABLED' "
        "ORDER BY geo_target_constant.name",
        {
            "id": "geo_target_constant.id",
            "country_code": "geo_target_constant.country_code",
            "name": "geo_target_constant.name",
        },
    )
