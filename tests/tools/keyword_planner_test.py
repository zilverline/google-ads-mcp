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

"""Test cases for the Keyword Planner tools."""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.credentials import AnonymousCredentials

from ads_mcp.tools import keyword_planner


class KeywordPlannerTestCase(unittest.TestCase):
    """Shares a real client for building requests and responses."""

    @classmethod
    def setUpClass(cls):
        cls.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        self.service = MagicMock()
        client_patcher = patch(
            "ads_mcp.utils.get_googleads_client", return_value=self.client
        )
        service_patcher = patch(
            "ads_mcp.utils.get_googleads_service_from_client",
            return_value=self.service,
        )
        self.mock_get_client = client_patcher.start()
        self.mock_get_service = service_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.addCleanup(service_patcher.stop)

    def idea(self, text, searches, low=0, high=0, competition="LOW"):
        result = self.client.get_type("GenerateKeywordIdeaResult")
        result.text = text
        metrics = result.keyword_idea_metrics
        metrics.avg_monthly_searches = searches
        metrics.low_top_of_page_bid_micros = low
        metrics.high_top_of_page_bid_micros = high
        metrics.competition = self.client.enums.KeywordPlanCompetitionLevelEnum[
            competition
        ]
        return result

    def google_ads_exception(self, message):
        failure = self.client.get_type("GoogleAdsFailure")
        error = self.client.get_type("GoogleAdsError")
        error.message = message
        failure.errors.append(error)
        return GoogleAdsException(
            error=None, call=None, failure=failure, request_id="req-1"
        )


class TestGenerateKeywordIdeas(KeywordPlannerTestCase):
    """Test cases for generate_keyword_ideas."""

    def test_keyword_seed_filters_sorts_and_converts(self):
        self.service.generate_keyword_ideas.return_value = [
            self.idea("z wave controller", 200, 70_000, 1_320_000),
            self.idea("z wave hub", 340, 50_000, 1_100_000, "HIGH"),
            self.idea("smart plug", 9000),
            self.idea("z wave knx", 0),
        ]

        result = keyword_planner.generate_keyword_ideas(
            customer_id="354-540-3315",
            geo_target_ids=["2528", "geoTargetConstants/2056"],
            language_id="1010",
            keywords=["z wave hub", " "],
            min_monthly_searches=10,
            include_pattern="z[ -]?wave",
        )

        request = self.service.generate_keyword_ideas.call_args.kwargs[
            "request"
        ]
        self.assertEqual(request.customer_id, "3545403315")
        self.assertEqual(
            list(request.geo_target_constants),
            ["geoTargetConstants/2528", "geoTargetConstants/2056"],
        )
        self.assertEqual(request.language, "languageConstants/1010")
        self.assertEqual(list(request.keyword_seed.keywords), ["z wave hub"])
        self.assertEqual(request.keyword_plan_network.name, "GOOGLE_SEARCH")

        self.assertEqual(result["total_ideas"], 4)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(
            [i["text"] for i in result["ideas"]],
            ["z wave hub", "z wave controller"],
        )
        self.assertEqual(
            result["ideas"][0],
            {
                "text": "z wave hub",
                "avg_monthly_searches": 340,
                "competition": "HIGH",
                "competition_index": 0,
                "low_top_of_page_bid": 0.05,
                "high_top_of_page_bid": 1.1,
            },
        )

    def test_max_results_truncates_after_sorting(self):
        self.service.generate_keyword_ideas.return_value = [
            self.idea("a", 10),
            self.idea("b", 30),
            self.idea("c", 20),
        ]

        result = keyword_planner.generate_keyword_ideas(
            customer_id="1",
            geo_target_ids=["2840"],
            keywords=["a"],
            max_results=2,
        )

        self.assertEqual([i["text"] for i in result["ideas"]], ["b", "c"])

    def test_keywords_and_url_use_combined_seed(self):
        self.service.generate_keyword_ideas.return_value = []

        keyword_planner.generate_keyword_ideas(
            customer_id="1",
            geo_target_ids=["2840"],
            keywords=["z wave homekit"],
            url="https://example.com/zwave",
        )

        request = self.service.generate_keyword_ideas.call_args.kwargs[
            "request"
        ]
        self.assertEqual(
            request.keyword_and_url_seed.url, "https://example.com/zwave"
        )
        self.assertEqual(
            list(request.keyword_and_url_seed.keywords), ["z wave homekit"]
        )
        self.assertEqual(request.language, "")

    def test_site_seed(self):
        self.service.generate_keyword_ideas.return_value = []

        keyword_planner.generate_keyword_ideas(
            customer_id="1", geo_target_ids=["2840"], site="example.com"
        )

        request = self.service.generate_keyword_ideas.call_args.kwargs[
            "request"
        ]
        self.assertEqual(request.site_seed.site, "example.com")

    def test_monthly_volumes(self):
        idea = self.idea("z wave hub", 340)
        volume = self.client.get_type("MonthlySearchVolume")
        volume.year = 2026
        volume.month = self.client.enums.MonthOfYearEnum.OCTOBER
        volume.monthly_searches = 390
        idea.keyword_idea_metrics.monthly_search_volumes.append(volume)
        self.service.generate_keyword_ideas.return_value = [idea]

        result = keyword_planner.generate_keyword_ideas(
            customer_id="1",
            geo_target_ids=["2840"],
            keywords=["z wave hub"],
            include_monthly_volumes=True,
        )

        self.assertEqual(
            result["ideas"][0]["monthly_search_volumes"],
            [{"year": 2026, "month": "OCTOBER", "monthly_searches": 390}],
        )

    def test_invalid_input(self):
        cases = [
            ({"keywords": ["a"], "site": "example.com"}, "'site' cannot"),
            ({}, "at least one seed"),
            ({"keywords": [f"k{i}" for i in range(21)]}, "at most 20"),
            ({"keywords": ["a"], "include_pattern": "("}, "invalid"),
            ({"keywords": ["a"], "geo_target_ids": ["Belgium"]}, "not a valid"),
            ({"keywords": ["a"], "geo_target_ids": []}, "must not be empty"),
            ({"keywords": ["a"], "max_results": 0}, "at least 1"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                arguments = {"customer_id": "1", "geo_target_ids": ["2840"]}
                arguments.update(kwargs)
                with self.assertRaisesRegex(ToolError, message):
                    keyword_planner.generate_keyword_ideas(**arguments)
        self.service.generate_keyword_ideas.assert_not_called()

    def test_explorer_access_error_explains_access_level(self):
        self.service.generate_keyword_ideas.side_effect = (
            self.google_ads_exception(
                "This method is not allowed for use with explorer access."
            )
        )

        with self.assertRaisesRegex(ToolError, "Basic or Standard access"):
            keyword_planner.generate_keyword_ideas(
                customer_id="1", geo_target_ids=["2840"], keywords=["a"]
            )

    def test_other_api_errors_are_passed_through(self):
        self.service.generate_keyword_ideas.side_effect = (
            self.google_ads_exception("Quota exceeded.")
        )

        with self.assertRaises(ToolError) as raised:
            keyword_planner.generate_keyword_ideas(
                customer_id="1", geo_target_ids=["2840"], keywords=["a"]
            )

        self.assertIn("Quota exceeded.", str(raised.exception))
        self.assertIn("req-1", str(raised.exception))
        self.assertNotIn("Basic or Standard", str(raised.exception))


class TestGetKeywordHistoricalMetrics(KeywordPlannerTestCase):
    """Test cases for get_keyword_historical_metrics."""

    def test_returns_metrics_with_close_variants(self):
        response = self.client.get_type(
            "GenerateKeywordHistoricalMetricsResponse"
        )
        result = self.client.get_type("GenerateKeywordHistoricalMetricsResult")
        result.text = "z wave hub"
        result.close_variants.append("z-wave hub")
        result.keyword_metrics.avg_monthly_searches = 4400
        result.keyword_metrics.high_top_of_page_bid_micros = 1_040_000
        response.results.append(result)
        self.service.generate_keyword_historical_metrics.return_value = response

        results = keyword_planner.get_keyword_historical_metrics(
            customer_id="1",
            keywords=["z wave hub", "z-wave hub"],
            geo_target_ids=["2840"],
            language_id=1000,
            network="GOOGLE_SEARCH_AND_PARTNERS",
        )

        request = (
            self.service.generate_keyword_historical_metrics.call_args.kwargs[
                "request"
            ]
        )
        self.assertEqual(list(request.keywords), ["z wave hub", "z-wave hub"])
        self.assertEqual(request.language, "languageConstants/1000")
        self.assertEqual(
            request.keyword_plan_network.name, "GOOGLE_SEARCH_AND_PARTNERS"
        )
        self.assertEqual(results[0]["close_variants"], ["z-wave hub"])
        self.assertEqual(results[0]["avg_monthly_searches"], 4400)
        self.assertEqual(results[0]["high_top_of_page_bid"], 1.04)

    def test_requires_keywords(self):
        with self.assertRaisesRegex(ToolError, "'keywords'"):
            keyword_planner.get_keyword_historical_metrics(
                customer_id="1", keywords=[" "], geo_target_ids=["2840"]
            )


class TestForecastKeywords(KeywordPlannerTestCase):
    """Test cases for forecast_keywords."""

    def forecast_response(self):
        response = self.client.get_type(
            "GenerateKeywordForecastMetricsResponse"
        )
        metrics = response.campaign_forecast_metrics
        metrics.clicks = 123.456
        metrics.cost_micros = 98_760_000
        metrics.average_cpc_micros = 800_000
        metrics.conversions = 1.234
        metrics.average_cpa_micros = 80_000_000
        return response

    def test_maximize_clicks_forecast(self):
        self.service.generate_keyword_forecast_metrics.return_value = (
            self.forecast_response()
        )

        result = keyword_planner.forecast_keywords(
            customer_id="1",
            keywords=[
                keyword_planner.ForecastKeyword(text="z-wave hub"),
                keyword_planner.ForecastKeyword(
                    text="z wave controller", match_type="PHRASE"
                ),
            ],
            geo_target_ids=["2840", "2124"],
            language_ids=["1000"],
            daily_budget=10,
            max_cpc=1.5,
            start_date="2026-12-01",
            end_date="2026-12-31",
            currency_code="eur",
        )

        request = (
            self.service.generate_keyword_forecast_metrics.call_args.kwargs[
                "request"
            ]
        )
        self.assertEqual(request.currency_code, "EUR")
        self.assertEqual(request.forecast_period.start_date, "2026-12-01")
        self.assertEqual(request.forecast_period.end_date, "2026-12-31")
        campaign = request.campaign
        self.assertEqual(
            list(campaign.geo_target_constants),
            ["geoTargetConstants/2840", "geoTargetConstants/2124"],
        )
        self.assertEqual(
            list(campaign.language_constants), ["languageConstants/1000"]
        )
        strategy = campaign.bidding_strategy.maximize_clicks_bidding_strategy
        self.assertEqual(strategy.daily_target_spend_micros, 10_000_000)
        self.assertEqual(strategy.max_cpc_bid_ceiling_micros, 1_500_000)
        keywords = campaign.ad_groups[0].keywords
        self.assertEqual(
            [(k.text, k.match_type.name) for k in keywords],
            [("z-wave hub", "EXACT"), ("z wave controller", "PHRASE")],
        )

        self.assertEqual(
            result,
            {
                "start_date": "2026-12-01",
                "end_date": "2026-12-31",
                "clicks": 123.5,
                "cost": 98.76,
                "average_cpc": 0.8,
                "conversions": 1.23,
                "average_cpa": 80.0,
            },
        )

    def test_manual_cpc_and_default_period(self):
        self.service.generate_keyword_forecast_metrics.return_value = (
            self.forecast_response()
        )

        result = keyword_planner.forecast_keywords(
            customer_id="1",
            keywords=[keyword_planner.ForecastKeyword(text="z-wave hub")],
            geo_target_ids=["2840"],
            daily_budget=5,
            bidding_strategy="MANUAL_CPC",
            max_cpc=0.75,
        )

        request = (
            self.service.generate_keyword_forecast_metrics.call_args.kwargs[
                "request"
            ]
        )
        manual = request.campaign.bidding_strategy.manual_cpc_bidding_strategy
        self.assertEqual(manual.daily_budget_micros, 5_000_000)
        self.assertEqual(manual.max_cpc_bid_micros, 750_000)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        self.assertEqual(result["start_date"], tomorrow.isoformat())
        self.assertEqual(
            result["end_date"],
            (tomorrow + datetime.timedelta(days=30)).isoformat(),
        )

    def test_maximize_conversions(self):
        self.service.generate_keyword_forecast_metrics.return_value = (
            self.forecast_response()
        )

        keyword_planner.forecast_keywords(
            customer_id="1",
            keywords=[keyword_planner.ForecastKeyword(text="z-wave hub")],
            geo_target_ids=["2840"],
            daily_budget=20,
            bidding_strategy="MAXIMIZE_CONVERSIONS",
        )

        request = (
            self.service.generate_keyword_forecast_metrics.call_args.kwargs[
                "request"
            ]
        )
        self.assertEqual(
            request.campaign.bidding_strategy.maximize_conversions_bidding_strategy.daily_target_spend_micros,
            20_000_000,
        )

    def test_invalid_input(self):
        keyword = keyword_planner.ForecastKeyword(text="z-wave hub")
        cases = [
            ({"keywords": []}, "'keywords'"),
            ({"daily_budget": 0}, "greater than 0"),
            ({"bidding_strategy": "MANUAL_CPC"}, "'max_cpc' is required"),
            (
                {"start_date": "2026-12-31", "end_date": "2026-12-01"},
                "before",
            ),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                arguments = {
                    "customer_id": "1",
                    "keywords": [keyword],
                    "geo_target_ids": ["2840"],
                    "daily_budget": 10,
                }
                arguments.update(kwargs)
                with self.assertRaisesRegex(ToolError, message):
                    keyword_planner.forecast_keywords(**arguments)


class TestGenerateAdGroupThemes(KeywordPlannerTestCase):
    """Test cases for generate_ad_group_themes."""

    def test_returns_suggestions_and_unusable_ad_groups(self):
        response = self.client.get_type("GenerateAdGroupThemesResponse")
        suggestion = self.client.get_type("AdGroupKeywordSuggestion")
        suggestion.keyword_text = "z wave homekit"
        suggestion.suggested_keyword_text = "z-wave homekit"
        suggestion.suggested_match_type = (
            self.client.enums.KeywordMatchTypeEnum.EXACT
        )
        suggestion.suggested_ad_group = "customers/1/adGroups/200"
        suggestion.suggested_campaign = "customers/1/campaigns/300"
        response.ad_group_keyword_suggestions.append(suggestion)
        unusable = self.client.get_type("UnusableAdGroup")
        unusable.ad_group = "customers/1/adGroups/201"
        response.unusable_ad_groups.append(unusable)
        self.service.generate_ad_group_themes.return_value = response

        result = keyword_planner.generate_ad_group_themes(
            customer_id="1",
            keywords=["z wave homekit"],
            ad_group_ids=["200", "customers/1/adGroups/201"],
        )

        request = self.service.generate_ad_group_themes.call_args.kwargs[
            "request"
        ]
        self.assertEqual(
            list(request.ad_groups),
            ["customers/1/adGroups/200", "customers/1/adGroups/201"],
        )
        self.assertEqual(
            result,
            {
                "suggestions": [
                    {
                        "keyword": "z wave homekit",
                        "suggested_keyword": "z-wave homekit",
                        "suggested_match_type": "EXACT",
                        "suggested_ad_group_id": "200",
                        "suggested_campaign_id": "300",
                    }
                ],
                "unusable_ad_group_ids": ["201"],
            },
        )


class TestSuggestGeoTargets(KeywordPlannerTestCase):
    """Test cases for suggest_geo_targets."""

    def test_returns_geo_target_ids(self):
        response = self.client.get_type("SuggestGeoTargetConstantsResponse")
        suggestion = self.client.get_type("GeoTargetConstantSuggestion")
        suggestion.search_term = "Belgium"
        suggestion.reach = 9_000_000
        geo = suggestion.geo_target_constant
        geo.id = 2056
        geo.name = "Belgium"
        geo.canonical_name = "Belgium"
        geo.target_type = "Country"
        geo.country_code = "BE"
        geo.status = self.client.enums.GeoTargetConstantStatusEnum.ENABLED
        response.geo_target_constant_suggestions.append(suggestion)
        self.service.suggest_geo_target_constants.return_value = response

        result = keyword_planner.suggest_geo_targets(
            location_names=["Belgium"], locale="nl", country_code="be"
        )

        request = self.service.suggest_geo_target_constants.call_args.kwargs[
            "request"
        ]
        self.assertEqual(list(request.location_names.names), ["Belgium"])
        self.assertEqual(request.locale, "nl")
        self.assertEqual(request.country_code, "BE")
        self.assertEqual(
            result,
            [
                {
                    "id": "2056",
                    "name": "Belgium",
                    "canonical_name": "Belgium",
                    "target_type": "Country",
                    "country_code": "BE",
                    "status": "ENABLED",
                    "reach": 9_000_000,
                    "search_term": "Belgium",
                }
            ],
        )

    def test_requires_names(self):
        with self.assertRaisesRegex(ToolError, "'location_names'"):
            keyword_planner.suggest_geo_targets(location_names=[])


if __name__ == "__main__":
    unittest.main()
