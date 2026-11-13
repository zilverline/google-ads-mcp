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

"""Read-only Keyword Planner tools: ideas, metrics, forecasts, themes, geo targets and account checks."""

import datetime
from enum import Enum
import re
from typing import Any, Callable, Iterable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations
from pydantic import BaseModel

import ads_mcp.utils as utils

keyword_planner_mcp = FastMCP("keyword_planner")

# The API caps keyword seeds at 20 keywords per GenerateKeywordIdeas request.
MAX_SEED_KEYWORDS = 20
IDEAS_PAGE_SIZE = 1000
DEFAULT_FORECAST_DAYS = 30

_ACCESS_LEVEL_HINT = (
    "Keyword Planner requires Basic or Standard access for the developer "
    "token's Google Cloud project; Explorer and test access cannot call it."
)


class KeywordPlanNetwork(str, Enum):
    """Networks Keyword Planner can report on."""

    GOOGLE_SEARCH = "GOOGLE_SEARCH"
    GOOGLE_SEARCH_AND_PARTNERS = "GOOGLE_SEARCH_AND_PARTNERS"


class KeywordMatchType(str, Enum):
    """Keyword match types supported by forecasts."""

    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class ForecastBiddingStrategy(str, Enum):
    """Bidding strategies supported by keyword forecasts."""

    MANUAL_CPC = "MANUAL_CPC"
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"


class ForecastKeyword(BaseModel):
    """A keyword to include in a forecast."""

    text: str
    match_type: KeywordMatchType = KeywordMatchType.EXACT


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value).upper()


def _require_customer_id(customer_id: str | int) -> str:
    cleaned = utils.clean_customer_id(customer_id)
    if not cleaned:
        raise ToolError("Error: 'customer_id' must contain digits.")
    return cleaned


def _resource_name(collection: str, value: str | int) -> str:
    """Accepts either a bare ID ('2528') or a full resource name."""
    text = str(value).strip()
    if text.startswith(f"{collection}/"):
        return text
    if not text.isdigit():
        raise ToolError(
            f"Error: '{value}' is not a valid {collection} ID. Use digits, "
            f"e.g. '2528', or a resource name like '{collection}/2528'."
        )
    return f"{collection}/{text}"


def _geo_resource_names(geo_target_ids: Iterable[str | int]) -> list[str]:
    return [_resource_name("geoTargetConstants", g) for g in geo_target_ids]


def _language_resource_name(language_id: str | int | None) -> str | None:
    if language_id is None or str(language_id).strip() == "":
        return None
    return _resource_name("languageConstants", language_id)


def _micros_to_units(micros: int) -> float:
    return round(micros / 1_000_000, 2)


def _call(fn: Callable[[], Any], action: str) -> Any:
    """Runs an API call and converts Google Ads errors into ToolErrors."""
    try:
        return fn()
    except GoogleAdsException as ex:
        message = utils.format_google_ads_error(ex)
        if "explorer access" in message.lower():
            message = f"{message}\n{_ACCESS_LEVEL_HINT}"
        raise ToolError(f"Failed to {action}:\n{message}") from ex


def _historical_metrics_to_dict(
    metrics: Any, include_monthly_volumes: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "avg_monthly_searches": metrics.avg_monthly_searches,
        "competition": metrics.competition.name,
        "competition_index": metrics.competition_index,
        "low_top_of_page_bid": _micros_to_units(
            metrics.low_top_of_page_bid_micros
        ),
        "high_top_of_page_bid": _micros_to_units(
            metrics.high_top_of_page_bid_micros
        ),
    }
    if include_monthly_volumes:
        result["monthly_search_volumes"] = [
            {
                "year": volume.year,
                "month": volume.month.name,
                "monthly_searches": volume.monthly_searches,
            }
            for volume in metrics.monthly_search_volumes
        ]
    return result


def _compile_pattern(include_pattern: str | None) -> re.Pattern[str] | None:
    if not include_pattern:
        return None
    try:
        return re.compile(include_pattern, re.IGNORECASE)
    except re.error as ex:
        raise ToolError(f"Error: invalid 'include_pattern' regex: {ex}") from ex


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def generate_keyword_ideas(
    customer_id: str | int,
    geo_target_ids: list[str | int],
    language_id: str | int | None = None,
    keywords: list[str] | None = None,
    url: str | None = None,
    site: str | None = None,
    network: KeywordPlanNetwork = KeywordPlanNetwork.GOOGLE_SEARCH,
    include_adult_keywords: bool = False,
    include_monthly_volumes: bool = False,
    min_monthly_searches: int = 0,
    include_pattern: str | None = None,
    max_results: int = 200,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Generates keyword ideas with search volumes and bid ranges (Keyword Planner).

    Seed with `keywords` (max 20), a page `url`, both, or a whole `site`
    (domain, used on its own). Results are sorted by average monthly searches.
    Volumes are rounded buckets (minimum 10) for the given locations and
    language; bids are in the account currency. Call once per market when
    markets use different languages. Word-order variants (e.g. 'z wave hub'
    and 'hub for z wave') often report the same shared volume; do not add
    them up.

    Use `suggest_geo_targets` to find geo target IDs (e.g. Netherlands = 2528).
    Language IDs are language_constant IDs, e.g. 1000 English, 1001 German,
    1002 French, 1010 Dutch.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        geo_target_ids: Geo target constant IDs, e.g. ['2528', '2056'].
        language_id: Language constant ID, e.g. '1010' for Dutch. Omit for all languages.
        keywords: Seed keywords (max 20).
        url: Seed page URL; combined with `keywords` when both are given.
        site: Seed site/domain, e.g. 'example.com'. Cannot be combined with `keywords` or `url`.
        network: GOOGLE_SEARCH (default) or GOOGLE_SEARCH_AND_PARTNERS.
        include_adult_keywords: Include adult keyword ideas.
        include_monthly_volumes: Include the monthly search volume breakdown per idea.
        min_monthly_searches: Drop ideas below this average monthly volume.
        include_pattern: Case-insensitive regex an idea must match, e.g. 'z[ -]?wave|homekit'.
        max_results: Maximum number of ideas returned after filtering and sorting.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        A dict with `total_ideas` (before filtering), `returned` and `ideas`:
        text, avg_monthly_searches, competition, competition_index,
        low_top_of_page_bid, high_top_of_page_bid and optionally
        monthly_search_volumes.
    """
    cleaned_customer_id = _require_customer_id(customer_id)
    seed_keywords = [k.strip() for k in keywords or [] if k.strip()]

    if site and (seed_keywords or url):
        raise ToolError(
            "Error: 'site' cannot be combined with 'keywords' or 'url'."
        )
    if not (seed_keywords or url or site):
        raise ToolError(
            "Error: provide at least one seed: 'keywords', 'url' or 'site'."
        )
    if len(seed_keywords) > MAX_SEED_KEYWORDS:
        raise ToolError(
            f"Error: at most {MAX_SEED_KEYWORDS} seed keywords are allowed; "
            f"got {len(seed_keywords)}. Split them over several calls."
        )
    if not geo_target_ids:
        raise ToolError("Error: 'geo_target_ids' must not be empty.")
    if max_results < 1:
        raise ToolError("Error: 'max_results' must be at least 1.")
    pattern = _compile_pattern(include_pattern)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "KeywordPlanIdeaService"
    )

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = cleaned_customer_id
    request.geo_target_constants.extend(_geo_resource_names(geo_target_ids))
    language = _language_resource_name(language_id)
    if language:
        request.language = language
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum[
        _enum_value(network)
    ]
    request.include_adult_keywords = include_adult_keywords
    request.page_size = IDEAS_PAGE_SIZE

    if site:
        request.site_seed.site = site
    elif seed_keywords and url:
        request.keyword_and_url_seed.url = url
        request.keyword_and_url_seed.keywords.extend(seed_keywords)
    elif url:
        request.url_seed.url = url
    else:
        request.keyword_seed.keywords.extend(seed_keywords)

    def _fetch() -> list[Any]:
        return list(service.generate_keyword_ideas(request=request))

    results = _call(_fetch, "generate keyword ideas")

    ideas = []
    for result in results:
        metrics = result.keyword_idea_metrics
        if metrics.avg_monthly_searches < min_monthly_searches:
            continue
        if pattern and not pattern.search(result.text):
            continue
        ideas.append(
            {
                "text": result.text,
                **_historical_metrics_to_dict(metrics, include_monthly_volumes),
            }
        )

    ideas.sort(key=lambda idea: -idea["avg_monthly_searches"])
    returned = ideas[:max_results]
    return {
        "total_ideas": len(results),
        "returned": len(returned),
        "ideas": returned,
    }


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_keyword_historical_metrics(
    customer_id: str | int,
    keywords: list[str],
    geo_target_ids: list[str | int],
    language_id: str | int | None = None,
    network: KeywordPlanNetwork = KeywordPlanNetwork.GOOGLE_SEARCH,
    include_monthly_volumes: bool = False,
    login_customer_id: str | int | None = None,
) -> list[dict[str, Any]]:
    """Returns search volumes and bid ranges for a known list of keywords (Keyword Planner).

    Unlike `generate_keyword_ideas`, this does not suggest new keywords.
    Spelling variants (e.g. 'z wave homekit' and 'z-wave homekit') are
    grouped into one entry; `close_variants` lists the grouped texts.
    Word-order variants are returned separately but often share one volume.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Keywords to look up.
        geo_target_ids: Geo target constant IDs, e.g. ['2840', '2124'].
        language_id: Language constant ID, e.g. '1000' for English. Omit for all languages.
        network: GOOGLE_SEARCH (default) or GOOGLE_SEARCH_AND_PARTNERS.
        include_monthly_volumes: Include the monthly search volume breakdown.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        One entry per (grouped) keyword: text, close_variants and metrics.
    """
    cleaned_customer_id = _require_customer_id(customer_id)
    texts = [k.strip() for k in keywords if k.strip()]
    if not texts:
        raise ToolError("Error: 'keywords' must not be empty.")
    if not geo_target_ids:
        raise ToolError("Error: 'geo_target_ids' must not be empty.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "KeywordPlanIdeaService"
    )

    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = cleaned_customer_id
    request.keywords.extend(texts)
    request.geo_target_constants.extend(_geo_resource_names(geo_target_ids))
    language = _language_resource_name(language_id)
    if language:
        request.language = language
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum[
        _enum_value(network)
    ]

    response = _call(
        lambda: service.generate_keyword_historical_metrics(request=request),
        "get keyword historical metrics",
    )

    return [
        {
            "text": result.text,
            "close_variants": list(result.close_variants),
            **_historical_metrics_to_dict(
                result.keyword_metrics, include_monthly_volumes
            ),
        }
        for result in response.results
    ]


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def forecast_keywords(
    customer_id: str | int,
    keywords: list[ForecastKeyword],
    geo_target_ids: list[str | int],
    daily_budget: float,
    language_ids: list[str | int] | None = None,
    bidding_strategy: ForecastBiddingStrategy = ForecastBiddingStrategy.MAXIMIZE_CLICKS,
    max_cpc: float | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    currency_code: str | None = None,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Forecasts clicks, cost and conversions for a set of keywords (Keyword Planner).

    Models one new campaign with a single ad group; nothing is created in the
    account. The forecast period must lie in the future and defaults to the
    next 30 days.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Keywords with match type, e.g. [{"text": "z-wave hub", "match_type": "EXACT"}].
        geo_target_ids: Geo target constant IDs, e.g. ['2840'].
        daily_budget: Daily budget in the account (or `currency_code`) currency.
        language_ids: Language constant IDs, e.g. ['1000']. Omit for all languages.
        bidding_strategy: MANUAL_CPC, MAXIMIZE_CLICKS (default) or MAXIMIZE_CONVERSIONS.
        max_cpc: Max CPC bid; required for MANUAL_CPC, optional ceiling for MAXIMIZE_CLICKS.
        start_date: Forecast start, YYYY-MM-DD. Defaults to tomorrow.
        end_date: Forecast end, YYYY-MM-DD. Defaults to 30 days after the start.
        currency_code: ISO 4217 code for budget and results. Defaults to the account currency.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The forecast period and clicks, cost, average_cpc, conversions and
        average_cpa for the whole period.
    """
    cleaned_customer_id = _require_customer_id(customer_id)
    if not keywords:
        raise ToolError("Error: 'keywords' must not be empty.")
    if not geo_target_ids:
        raise ToolError("Error: 'geo_target_ids' must not be empty.")
    if daily_budget <= 0:
        raise ToolError("Error: 'daily_budget' must be greater than 0.")

    strategy = _enum_value(bidding_strategy)
    if strategy == ForecastBiddingStrategy.MANUAL_CPC.value and not max_cpc:
        raise ToolError("Error: 'max_cpc' is required for MANUAL_CPC.")

    start = (
        datetime.date.fromisoformat(start_date)
        if start_date
        else datetime.date.today() + datetime.timedelta(days=1)
    )
    end = (
        datetime.date.fromisoformat(end_date)
        if end_date
        else start + datetime.timedelta(days=DEFAULT_FORECAST_DAYS)
    )
    if end < start:
        raise ToolError("Error: 'end_date' must not be before 'start_date'.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "KeywordPlanIdeaService"
    )

    request = client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cleaned_customer_id
    if currency_code:
        request.currency_code = currency_code.upper()
    request.forecast_period.start_date = start.isoformat()
    request.forecast_period.end_date = end.isoformat()

    campaign = request.campaign
    campaign.geo_target_constants.extend(_geo_resource_names(geo_target_ids))
    for language_id in language_ids or []:
        campaign.language_constants.append(
            _resource_name("languageConstants", language_id)
        )

    budget_micros = int(round(daily_budget * 1_000_000))
    max_cpc_micros = int(round(max_cpc * 1_000_000)) if max_cpc else None
    bidding = campaign.bidding_strategy
    if strategy == ForecastBiddingStrategy.MANUAL_CPC.value:
        bidding.manual_cpc_bidding_strategy.daily_budget_micros = budget_micros
        bidding.manual_cpc_bidding_strategy.max_cpc_bid_micros = max_cpc_micros
    elif strategy == ForecastBiddingStrategy.MAXIMIZE_CLICKS.value:
        bidding.maximize_clicks_bidding_strategy.daily_target_spend_micros = (
            budget_micros
        )
        if max_cpc_micros:
            bidding.maximize_clicks_bidding_strategy.max_cpc_bid_ceiling_micros = (
                max_cpc_micros
            )
    else:
        bidding.maximize_conversions_bidding_strategy.daily_target_spend_micros = (
            budget_micros
        )

    ad_group = client.get_type("ForecastAdGroup")
    for keyword in keywords:
        info = client.get_type("KeywordInfo")
        info.text = keyword.text
        info.match_type = client.enums.KeywordMatchTypeEnum[
            _enum_value(keyword.match_type)
        ]
        ad_group.keywords.append(info)
    campaign.ad_groups.append(ad_group)

    response = _call(
        lambda: service.generate_keyword_forecast_metrics(request=request),
        "forecast keywords",
    )
    metrics = response.campaign_forecast_metrics
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "clicks": round(metrics.clicks, 1),
        "cost": _micros_to_units(metrics.cost_micros),
        "average_cpc": _micros_to_units(metrics.average_cpc_micros),
        "conversions": round(metrics.conversions, 2),
        "average_cpa": _micros_to_units(metrics.average_cpa_micros),
    }


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def generate_ad_group_themes(
    customer_id: str | int,
    keywords: list[str],
    ad_group_ids: list[str | int],
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Suggests which existing ad group each keyword fits best (Keyword Planner).

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Keywords to place.
        ad_group_ids: IDs of candidate ad groups in this account.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        `suggestions` (keyword, suggested_keyword, suggested_match_type,
        suggested_ad_group_id, suggested_campaign_id) and `unusable_ad_group_ids`.
    """
    cleaned_customer_id = _require_customer_id(customer_id)
    texts = [k.strip() for k in keywords if k.strip()]
    if not texts:
        raise ToolError("Error: 'keywords' must not be empty.")
    if not ad_group_ids:
        raise ToolError("Error: 'ad_group_ids' must not be empty.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "KeywordPlanIdeaService"
    )

    request = client.get_type("GenerateAdGroupThemesRequest")
    request.customer_id = cleaned_customer_id
    request.keywords.extend(texts)
    request.ad_groups.extend(
        _resource_name(f"customers/{cleaned_customer_id}/adGroups", a)
        for a in ad_group_ids
    )

    response = _call(
        lambda: service.generate_ad_group_themes(request=request),
        "generate ad group themes",
    )

    return {
        "suggestions": [
            {
                "keyword": s.keyword_text,
                "suggested_keyword": s.suggested_keyword_text,
                "suggested_match_type": s.suggested_match_type.name,
                "suggested_ad_group_id": (
                    utils.extract_id(s.suggested_ad_group)
                    if s.suggested_ad_group
                    else None
                ),
                "suggested_campaign_id": (
                    utils.extract_id(s.suggested_campaign)
                    if s.suggested_campaign
                    else None
                ),
            }
            for s in response.ad_group_keyword_suggestions
        ],
        "unusable_ad_group_ids": [
            utils.extract_id(u.ad_group) for u in response.unusable_ad_groups
        ],
    }


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def suggest_geo_targets(
    location_names: list[str],
    locale: str = "en",
    country_code: str | None = None,
    login_customer_id: str | int | None = None,
) -> list[dict[str, Any]]:
    """Looks up geo target constant IDs by location name, e.g. 'Belgium' -> 2056.

    Use the returned `id` values as `geo_target_ids` in the other
    Keyword Planner tools and as location criteria in campaigns.

    Args:
        location_names: Location names, e.g. ['Belgium', 'Ontario'].
        locale: Language of the names and results, e.g. 'en' or 'nl'.
        country_code: Optional ISO 3166-1 alpha-2 code to restrict matches, e.g. 'CA'.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        Suggestions with id, name, canonical_name, target_type, country_code,
        status, reach and the search_term they matched.
    """
    names = [n.strip() for n in location_names if n.strip()]
    if not names:
        raise ToolError("Error: 'location_names' must not be empty.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "GeoTargetConstantService"
    )

    request = client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = locale
    if country_code:
        request.country_code = country_code.upper()
    request.location_names.names.extend(names)

    response = _call(
        lambda: service.suggest_geo_target_constants(request=request),
        "suggest geo targets",
    )

    return [
        {
            "id": str(s.geo_target_constant.id),
            "name": s.geo_target_constant.name,
            "canonical_name": s.geo_target_constant.canonical_name,
            "target_type": s.geo_target_constant.target_type,
            "country_code": s.geo_target_constant.country_code,
            "status": s.geo_target_constant.status.name,
            "reach": s.reach,
            "search_term": s.search_term,
        }
        for s in response.geo_target_constant_suggestions
    ]


MAX_COMPARE_KEYWORDS = 1000
_TOKEN_SEPARATOR = re.compile(r"[^\w]+")


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SEPARATOR.split(text.lower()) if t)


def _spelling_key(text: str) -> str:
    # Joining the tokens makes 'z wave', 'z-wave' and 'zwave' compare equal.
    return "".join(_tokens(text))


def _negative_blocks(
    negative: tuple[str, ...], match_type: str, query: tuple[str, ...]
) -> bool:
    """Negative keywords, unlike positive ones, never match close variants."""
    if not negative:
        return False
    if match_type == "EXACT":
        return query == negative
    if match_type == "PHRASE":
        size = len(negative)
        return any(
            query[i : i + size] == negative
            for i in range(len(query) - size + 1)
        )
    return set(negative) <= set(query)


def _require_id(value: str | int, name: str) -> str:
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(f"Error: '{name}' must be a numeric ID; got '{value}'.")
    return text


def _search_rows(service: Any, customer_id: str, query: str) -> list[Any]:
    def _fetch() -> list[Any]:
        return [
            row
            for batch in service.search_stream(
                customer_id=customer_id, query=query
            )
            for row in batch.results
        ]

    return _call(_fetch, "query the account")


def _keyword_negatives(
    rows: Iterable[Any], criterion: str, level: str
) -> list[dict[str, Any]]:
    negatives = []
    for row in rows:
        keyword = getattr(row, criterion).keyword
        entry: dict[str, Any] = {
            "text": keyword.text,
            "match_type": keyword.match_type.name,
            "level": level,
        }
        if criterion == "shared_criterion":
            entry["list_name"] = row.shared_set.name
        negatives.append(entry)
    return negatives


def _shared_list_negatives(
    service: Any, customer_id: str, list_query: str, level: str
) -> list[dict[str, Any]]:
    list_ids = sorted(
        {
            str(row.shared_set.id)
            for row in _search_rows(service, customer_id, list_query)
        }
    )
    if not list_ids:
        return []
    rows = _search_rows(
        service,
        customer_id,
        "SELECT shared_set.name, shared_criterion.keyword.text, "
        "shared_criterion.keyword.match_type FROM shared_criterion "
        f"WHERE shared_set.id IN ({', '.join(list_ids)}) "
        "AND shared_criterion.type = 'KEYWORD'",
    )
    return _keyword_negatives(rows, "shared_criterion", level)


@keyword_planner_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def compare_keyword_ideas(
    customer_id: str | int,
    keywords: list[str],
    ad_group_id: str | int | None = None,
    campaign_id: str | int | None = None,
    login_customer_id: str | int | None = None,
) -> dict[str, Any]:
    """Checks keyword ideas against the account before adding them to an ad group or campaign.

    Give exactly one target: `ad_group_id` or `campaign_id`. For each keyword
    it reports:
    - `in_target`: keywords in the target with the same words, ignoring case,
      spaces and hyphens ('z wave hub' = 'z-wave hub' = 'zwave hub').
    - `elsewhere`: the same keyword in other ad groups or campaigns, e.g. a
      Brand ad group that already covers it.
    - `blocked_by`: negative keywords that stop it from serving in the target:
      ad group negatives (ad group target only), campaign negatives, shared
      negative keyword lists linked to the campaign, and account-level
      negative keyword lists.

    The keyword text is treated as the search query: a broad negative blocks
    when all its words occur, a phrase negative when its words occur in
    order, and an exact negative only on the identical query. Negatives do not
    match close variants, so 'homebridge' blocks 'homebridge z-wave' but
    'homekit' (exact) does not block 'z-wave homekit'.

    `status` is BLOCKED (a negative blocks it), EXISTS (already in the target)
    or NEW.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated).
        keywords: Keyword ideas to check (max 1000).
        ad_group_id: Target ad group ID.
        campaign_id: Target campaign ID (when no specific ad group is targeted).
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        `target`, `summary` (counts per status) and `results` per keyword.
    """
    cleaned_customer_id = _require_customer_id(customer_id)
    texts = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))
    if not texts:
        raise ToolError("Error: 'keywords' must not be empty.")
    if len(texts) > MAX_COMPARE_KEYWORDS:
        raise ToolError(
            f"Error: at most {MAX_COMPARE_KEYWORDS} keywords are allowed; "
            f"got {len(texts)}."
        )
    if (ad_group_id is None) == (campaign_id is None):
        raise ToolError(
            "Error: provide exactly one of 'ad_group_id' or 'campaign_id'."
        )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = utils.get_googleads_service_from_client(
        client, "GoogleAdsService"
    )

    if ad_group_id is not None:
        target_id = _require_id(ad_group_id, "ad_group_id")
        rows = _search_rows(
            service,
            cleaned_customer_id,
            "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name "
            f"FROM ad_group WHERE ad_group.id = {target_id}",
        )
        if not rows:
            raise ToolError(f"Error: ad group {target_id} was not found.")
        target = {
            "campaign_id": str(rows[0].campaign.id),
            "campaign_name": rows[0].campaign.name,
            "ad_group_id": target_id,
            "ad_group_name": rows[0].ad_group.name,
        }
    else:
        target_id = _require_id(campaign_id, "campaign_id")
        rows = _search_rows(
            service,
            cleaned_customer_id,
            "SELECT campaign.id, campaign.name FROM campaign "
            f"WHERE campaign.id = {target_id}",
        )
        if not rows:
            raise ToolError(f"Error: campaign {target_id} was not found.")
        target = {
            "campaign_id": target_id,
            "campaign_name": rows[0].campaign.name,
        }
    target_campaign_id = target["campaign_id"]

    existing_rows = _search_rows(
        service,
        cleaned_customer_id,
        "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
        "ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status "
        "FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = 'KEYWORD' "
        "AND ad_group_criterion.negative = FALSE "
        "AND ad_group_criterion.status != 'REMOVED' "
        "AND ad_group.status != 'REMOVED' "
        "AND campaign.status != 'REMOVED'",
    )
    existing_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in existing_rows:
        keyword = row.ad_group_criterion.keyword
        existing_by_key.setdefault(_spelling_key(keyword.text), []).append(
            {
                "text": keyword.text,
                "match_type": keyword.match_type.name,
                "status": row.ad_group_criterion.status.name,
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "ad_group_id": str(row.ad_group.id),
                "ad_group_name": row.ad_group.name,
            }
        )

    negatives: list[dict[str, Any]] = []
    if "ad_group_id" in target:
        negatives += _keyword_negatives(
            _search_rows(
                service,
                cleaned_customer_id,
                "SELECT ad_group_criterion.keyword.text, "
                "ad_group_criterion.keyword.match_type "
                "FROM ad_group_criterion "
                f"WHERE ad_group.id = {target['ad_group_id']} "
                "AND ad_group_criterion.type = 'KEYWORD' "
                "AND ad_group_criterion.negative = TRUE "
                "AND ad_group_criterion.status != 'REMOVED'",
            ),
            "ad_group_criterion",
            "AD_GROUP",
        )
    negatives += _keyword_negatives(
        _search_rows(
            service,
            cleaned_customer_id,
            "SELECT campaign_criterion.keyword.text, "
            "campaign_criterion.keyword.match_type FROM campaign_criterion "
            f"WHERE campaign.id = {target_campaign_id} "
            "AND campaign_criterion.type = 'KEYWORD' "
            "AND campaign_criterion.negative = TRUE "
            "AND campaign_criterion.status != 'REMOVED'",
        ),
        "campaign_criterion",
        "CAMPAIGN",
    )
    negatives += _shared_list_negatives(
        service,
        cleaned_customer_id,
        "SELECT shared_set.id FROM campaign_shared_set "
        f"WHERE campaign.id = {target_campaign_id} "
        "AND campaign_shared_set.status = 'ENABLED' "
        "AND shared_set.type = 'NEGATIVE_KEYWORDS' "
        "AND shared_set.status = 'ENABLED'",
        "SHARED_LIST",
    )
    negatives += _shared_list_negatives(
        service,
        cleaned_customer_id,
        "SELECT shared_set.id FROM shared_set "
        "WHERE shared_set.type = 'ACCOUNT_LEVEL_NEGATIVE_KEYWORDS' "
        "AND shared_set.status = 'ENABLED'",
        "ACCOUNT_LIST",
    )
    negative_tokens = [(_tokens(n["text"]), n) for n in negatives]

    in_target_scope = (
        "ad_group_id" if "ad_group_id" in target else "campaign_id"
    )
    results = []
    for text in texts:
        query = _tokens(text)
        matches = existing_by_key.get(_spelling_key(text), [])
        in_target = [
            m for m in matches if m[in_target_scope] == target[in_target_scope]
        ]
        elsewhere = [m for m in matches if m not in in_target]
        blocked_by = [
            negative
            for tokens, negative in negative_tokens
            if _negative_blocks(tokens, negative["match_type"], query)
        ]
        status = "BLOCKED" if blocked_by else "EXISTS" if in_target else "NEW"
        results.append(
            {
                "keyword": text,
                "status": status,
                "in_target": in_target,
                "elsewhere": elsewhere,
                "blocked_by": blocked_by,
            }
        )

    summary = {
        status: sum(1 for r in results if r["status"] == status)
        for status in ("NEW", "EXISTS", "BLOCKED")
    }
    return {"target": target, "summary": summary, "results": results}
