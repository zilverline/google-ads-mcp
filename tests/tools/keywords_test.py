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

"""Test cases for the manage_keywords tool."""

import json
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.credentials import AnonymousCredentials

from ads_mcp.tools import keywords
from ads_mcp.tools.keywords import KeywordChange


class TestManageKeywords(unittest.TestCase):
    """Test cases for manage_keywords."""

    @classmethod
    def setUpClass(cls):
        cls.client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        self.ga_service = MagicMock()
        self.ga_service.search_stream.return_value = []
        self.ad_group_service = MagicMock()
        self.campaign_service = MagicMock()
        services = {
            "GoogleAdsService": self.ga_service,
            "AdGroupCriterionService": self.ad_group_service,
            "CampaignCriterionService": self.campaign_service,
        }
        client_patcher = patch(
            "ads_mcp.utils.get_googleads_client", return_value=self.client
        )
        service_patcher = patch(
            "ads_mcp.utils.get_googleads_service_from_client",
            side_effect=lambda client, name: services[name],
        )
        client_patcher.start()
        service_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.addCleanup(service_patcher.stop)

    def existing_keywords(self, *criteria, criterion="ad_group_criterion"):
        rows = []
        for criterion_id, text, match_type, status, negative in criteria:
            row = self.client.get_type("GoogleAdsRow")
            item = getattr(row, criterion)
            item.criterion_id = criterion_id
            item.keyword.text = text
            item.keyword.match_type = self.client.enums.KeywordMatchTypeEnum[
                match_type
            ]
            item.status = getattr(
                self.client.enums,
                (
                    "AdGroupCriterionStatusEnum"
                    if criterion == "ad_group_criterion"
                    else "CampaignCriterionStatusEnum"
                ),
            )[status]
            item.negative = negative
            rows.append(row)
        batch = MagicMock()
        batch.results = rows
        self.ga_service.search_stream.return_value = [batch]

    def mutate_request(self, service, method="mutate_ad_group_criteria"):
        return getattr(service, method).call_args.kwargs["request"]

    def propose(self, **kwargs):
        return json.loads(keywords.manage_keywords(customer_id="1", **kwargs))

    def test_create_proposes_then_commits_with_checksum(self):
        changes = [
            KeywordChange(
                action="CREATE", text=" z-wave hub ", match_type="EXACT"
            ),
            KeywordChange(
                action="CREATE", text="best z-wave hub", match_type="PHRASE"
            ),
        ]

        proposal = self.propose(changes=changes, ad_group_id="200")

        self.assertEqual(proposal["status"], "PROPOSED")
        request = self.mutate_request(self.ad_group_service)
        self.assertTrue(request.validate_only)
        created = [op.create for op in request.operations]
        self.assertEqual(
            [(c.keyword.text, c.keyword.match_type.name) for c in created],
            [("z-wave hub", "EXACT"), ("best z-wave hub", "PHRASE")],
        )
        self.assertEqual(created[0].ad_group, "customers/1/adGroups/200")
        self.assertEqual(created[0].status.name, "ENABLED")
        self.assertFalse(created[0].negative)
        self.assertIn(
            {
                "operation": "CREATE",
                "entity_readable": "Keyword",
                "primary_key_readable": "ad_group 200: z-wave hub",
                "field_readable": "Match type",
                "value_readable": "EXACT",
            },
            proposal["change_items"],
        )

        response = MagicMock()
        response.results = [
            MagicMock(resource_name="customers/1/adGroupCriteria/200~11"),
            MagicMock(resource_name="customers/1/adGroupCriteria/200~12"),
        ]
        self.ad_group_service.mutate_ad_group_criteria.return_value = response

        result = keywords.manage_keywords(
            customer_id="1",
            changes=changes,
            ad_group_id="200",
            validate_only=False,
            checksum=proposal["checksum"],
        )

        self.assertFalse(
            self.mutate_request(self.ad_group_service).validate_only
        )
        self.assertIn("2 keyword operations", result)
        self.assertIn("200~11, 200~12", result)

    def test_api_errors_name_the_failing_keyword(self):
        failure = self.client.get_type("GoogleAdsFailure")
        error = self.client.get_type("GoogleAdsError")
        error.message = "A policy was violated."
        error.location.field_path_elements.append(
            type(error.location).FieldPathElement(
                field_name="operations", index=1
            )
        )
        violation = error.details.policy_violation_details
        violation.external_policy_name = "Trademarks in keywords"
        violation.key.violating_text = "iphone"
        failure.errors.append(error)
        self.ad_group_service.mutate_ad_group_criteria.side_effect = (
            GoogleAdsException(
                error=None, call=None, failure=failure, request_id="req-1"
            )
        )
        changes = [
            KeywordChange(action="CREATE", text="knx app", match_type="EXACT"),
            KeywordChange(
                action="CREATE", text="knx iphone app", match_type="EXACT"
            ),
        ]

        with self.assertRaises(ToolError) as raised:
            self.propose(changes=changes, ad_group_id="200")

        message = str(raised.exception)
        self.assertIn(
            "Operation 2 (CREATE 'knx iphone app' EXACT): A policy was "
            "violated.",
            message,
        )
        self.assertIn("violating text: 'iphone'", message)

    def test_commit_requires_matching_checksum(self):
        changes = [
            KeywordChange(
                action="CREATE", text="z-wave hub", match_type="EXACT"
            )
        ]
        proposal = self.propose(changes=changes, ad_group_id="200")
        other = [
            KeywordChange(
                action="CREATE", text="z-wave hub", match_type="BROAD"
            )
        ]
        self.ad_group_service.reset_mock()

        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            keywords.manage_keywords(
                customer_id="1",
                changes=other,
                ad_group_id="200",
                validate_only=False,
                checksum=proposal["checksum"],
            )
        self.ad_group_service.mutate_ad_group_criteria.assert_not_called()

    def test_change_match_type_creates_new_and_pauses_old(self):
        self.existing_keywords((55, "z wave hub", "BROAD", "ENABLED", False))

        proposal = self.propose(
            changes=[
                KeywordChange(
                    action="CHANGE_MATCH_TYPE",
                    criterion_id="55",
                    match_type="EXACT",
                    text="z-wave hub",
                )
            ],
            ad_group_id="200",
        )

        query = self.ga_service.search_stream.call_args.kwargs["query"]
        self.assertIn("WHERE ad_group.id = 200", query)
        self.assertIn("criterion_id IN (55)", query)
        operations = self.mutate_request(self.ad_group_service).operations
        self.assertEqual(operations[0].create.keyword.text, "z-wave hub")
        self.assertEqual(operations[0].create.keyword.match_type.name, "EXACT")
        self.assertEqual(
            operations[1].update.resource_name,
            "customers/1/adGroupCriteria/200~55",
        )
        self.assertEqual(operations[1].update.status.name, "PAUSED")
        self.assertEqual(list(operations[1].update_mask.paths), ["status"])
        self.assertIn(
            {
                "operation": "UPDATE",
                "entity_readable": "Keyword",
                "primary_key_readable": "ad_group 200: z wave hub (BROAD, ID 55)",
                "field_readable": "Status",
                "value_readable": "PAUSED",
                "original_value_readable": "ENABLED",
            },
            proposal["change_items"],
        )

    def test_change_match_type_keeps_text_and_skips_pausing_paused(self):
        self.existing_keywords((55, "z wave hub", "BROAD", "PAUSED", False))

        self.propose(
            changes=[
                KeywordChange(
                    action="CHANGE_MATCH_TYPE",
                    criterion_id="55",
                    match_type="PHRASE",
                )
            ],
            ad_group_id="200",
        )

        operations = self.mutate_request(self.ad_group_service).operations
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].create.keyword.text, "z wave hub")

    def test_change_match_type_without_change_is_rejected(self):
        self.existing_keywords((55, "z wave hub", "BROAD", "ENABLED", False))

        with self.assertRaisesRegex(ToolError, "already"):
            self.propose(
                changes=[
                    KeywordChange(
                        action="CHANGE_MATCH_TYPE",
                        criterion_id="55",
                        match_type="BROAD",
                    )
                ],
                ad_group_id="200",
            )

    def test_pause_enable_and_remove(self):
        self.existing_keywords(
            (1, "a", "EXACT", "ENABLED", False),
            (2, "b", "EXACT", "PAUSED", False),
            (3, "c", "BROAD", "ENABLED", False),
        )

        self.propose(
            changes=[
                KeywordChange(action="PAUSE", criterion_id="1"),
                KeywordChange(action="ENABLE", criterion_id="2"),
                KeywordChange(action="REMOVE", criterion_id="3"),
            ],
            ad_group_id="200",
        )

        operations = self.mutate_request(self.ad_group_service).operations
        self.assertEqual(operations[0].update.status.name, "PAUSED")
        self.assertEqual(operations[1].update.status.name, "ENABLED")
        self.assertEqual(
            operations[2].remove, "customers/1/adGroupCriteria/200~3"
        )

    def test_ad_group_negatives(self):
        self.propose(
            changes=[
                KeywordChange(
                    action="CREATE", text="sensor", match_type="BROAD"
                )
            ],
            ad_group_id="200",
            negative=True,
        )

        create = self.mutate_request(self.ad_group_service).operations[0].create
        self.assertTrue(create.negative)

    def test_campaign_negatives(self):
        self.existing_keywords(
            (15810911, "matter", "BROAD", "ENABLED", True),
            criterion="campaign_criterion",
        )

        proposal = self.propose(
            changes=[
                KeywordChange(
                    action="CREATE", text="manual", match_type="BROAD"
                ),
                KeywordChange(action="REMOVE", criterion_id="15810911"),
            ],
            campaign_id="300",
            negative=True,
        )

        query = self.ga_service.search_stream.call_args.kwargs["query"]
        self.assertIn("FROM campaign_criterion WHERE campaign.id = 300", query)
        request = self.mutate_request(
            self.campaign_service, "mutate_campaign_criteria"
        )
        self.assertEqual(
            request.operations[0].create.campaign, "customers/1/campaigns/300"
        )
        self.assertTrue(request.operations[0].create.negative)
        self.assertEqual(
            request.operations[1].remove,
            "customers/1/campaignCriteria/300~15810911",
        )
        self.assertEqual(
            proposal["change_items"][0]["entity_readable"], "Negative keyword"
        )

    def test_rejects_unknown_or_wrong_kind_criteria(self):
        self.existing_keywords((1, "sensor", "BROAD", "ENABLED", True))
        cases = [
            ("2", "not found"),
            ("1", "not positive keywords"),
        ]
        for criterion_id, message in cases:
            with self.subTest(criterion_id=criterion_id):
                with self.assertRaisesRegex(ToolError, message):
                    self.propose(
                        changes=[
                            KeywordChange(
                                action="PAUSE", criterion_id=criterion_id
                            )
                        ],
                        ad_group_id="200",
                    )
        self.ad_group_service.mutate_ad_group_criteria.assert_not_called()

    def test_invalid_input(self):
        create = KeywordChange(action="CREATE", text="a", match_type="EXACT")
        cases = [
            ({"changes": []}, "'changes'"),
            ({"changes": [create], "ad_group_id": None}, "exactly one"),
            (
                {"changes": [create], "campaign_id": "3", "ad_group_id": None},
                "only for",
            ),
            (
                {
                    "changes": [
                        KeywordChange(action="PAUSE", criterion_id="1")
                    ],
                    "campaign_id": "3",
                    "ad_group_id": None,
                    "negative": True,
                },
                "only CREATE and REMOVE",
            ),
            (
                {"changes": [KeywordChange(action="CREATE", text="a")]},
                "requires 'text' and 'match_type'",
            ),
            (
                {"changes": [KeywordChange(action="REMOVE")]},
                "requires 'criterion_id'",
            ),
            (
                {
                    "changes": [
                        KeywordChange(
                            action="CHANGE_MATCH_TYPE", criterion_id="1"
                        )
                    ]
                },
                "new 'match_type'",
            ),
            ({"changes": [create], "ad_group_id": "x"}, "numeric ID"),
            ({"changes": [create] * 1001}, "at most 1000"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                arguments = {"ad_group_id": "200"}
                arguments.update(kwargs)
                with self.assertRaisesRegex(ToolError, message):
                    self.propose(**arguments)
        self.ad_group_service.mutate_ad_group_criteria.assert_not_called()
        self.campaign_service.mutate_campaign_criteria.assert_not_called()


if __name__ == "__main__":
    unittest.main()
