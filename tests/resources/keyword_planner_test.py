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

"""Tests for the Keyword Planner resources."""

import json
import unittest
from unittest import mock

from fastmcp.exceptions import ResourceError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.credentials import AnonymousCredentials

from ads_mcp.resources import keyword_planner


class KeywordPlannerResourcesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        keyword_planner.get_keyword_planner_languages.cache_clear()
        keyword_planner.get_keyword_planner_countries.cache_clear()
        self.ga_service = mock.MagicMock()
        self.customer_service = mock.MagicMock()
        services = {
            "GoogleAdsService": self.ga_service,
            "CustomerService": self.customer_service,
        }
        patcher = mock.patch(
            "ads_mcp.utils.get_googleads_service",
            side_effect=lambda name, **kwargs: services[name],
        )
        self.mock_get_service = patcher.start()
        self.addCleanup(patcher.stop)

    def stub_rows(self, *rows):
        batch = mock.MagicMock()
        batch.results = list(rows)
        self.ga_service.search_stream.return_value = [batch]

    def language_row(self, language_id, code, name):
        row = self.client.get_type("GoogleAdsRow")
        row.language_constant.id = language_id
        row.language_constant.code = code
        row.language_constant.name = name
        return row

    def test_guide(self):
        guide = keyword_planner.get_keyword_planner_guide()

        self.assertIn("Basic or Standard", guide)
        self.assertIn("keywords_manage_keywords", guide)

    @mock.patch.dict(
        "os.environ", {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "354-540-3315"}
    )
    def test_languages_use_login_customer_and_are_cached(self):
        self.stub_rows(self.language_row(1010, "nl", "Dutch"))

        first = keyword_planner.get_keyword_planner_languages()
        second = keyword_planner.get_keyword_planner_languages()

        self.assertEqual(
            json.loads(first), [{"id": "1010", "code": "nl", "name": "Dutch"}]
        )
        self.assertEqual(first, second)
        self.ga_service.search_stream.assert_called_once()
        call = self.ga_service.search_stream.call_args.kwargs
        self.assertEqual(call["customer_id"], "3545403315")
        self.assertIn("language_constant.targetable = TRUE", call["query"])
        self.customer_service.list_accessible_customers.assert_not_called()

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_countries_fall_back_to_first_accessible_customer(self):
        self.customer_service.list_accessible_customers.return_value = (
            mock.MagicMock(resource_names=["customers/111", "customers/222"])
        )
        row = self.client.get_type("GoogleAdsRow")
        row.geo_target_constant.id = 2056
        row.geo_target_constant.country_code = "BE"
        row.geo_target_constant.name = "Belgium"
        self.stub_rows(row)

        result = json.loads(keyword_planner.get_keyword_planner_countries())

        self.assertEqual(
            result, [{"id": "2056", "country_code": "BE", "name": "Belgium"}]
        )
        call = self.ga_service.search_stream.call_args.kwargs
        self.assertEqual(call["customer_id"], "111")
        self.assertIn("target_type = 'Country'", call["query"])

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_no_accessible_customers(self):
        self.customer_service.list_accessible_customers.return_value = (
            mock.MagicMock(resource_names=[])
        )

        with self.assertRaisesRegex(ResourceError, "No accessible"):
            keyword_planner.get_keyword_planner_countries()

    @mock.patch.dict("os.environ", {"GOOGLE_ADS_LOGIN_CUSTOMER_ID": "1"})
    def test_api_errors_are_not_cached(self):
        failure = self.client.get_type("GoogleAdsFailure")
        error = self.client.get_type("GoogleAdsError")
        error.message = "Temporary failure."
        failure.errors.append(error)
        self.ga_service.search_stream.side_effect = [
            GoogleAdsException(
                error=None, call=None, failure=failure, request_id="req-1"
            ),
            [],
        ]

        with self.assertRaisesRegex(ResourceError, "Temporary failure"):
            keyword_planner.get_keyword_planner_languages()
        self.assertEqual(keyword_planner.get_keyword_planner_languages(), "[]")


if __name__ == "__main__":
    unittest.main()
