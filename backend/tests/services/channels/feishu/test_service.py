# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from app.services.channels.feishu.service import (
    FeishuChannelProvider,
    _extract_event_payload,
)


class DummyChannel:
    def __init__(self, config):
        self.id = 1
        self.name = "feishu"
        self.channel_type = "feishu"
        self.is_enabled = True
        self.config = config
        self.default_team_id = 0
        self.default_model_name = ""


def test_provider_reads_feishu_native_config_keys():
    provider = FeishuChannelProvider(
        DummyChannel(
            {
                "app_id": "cli_a",
                "app_secret": "sec_a",
                "verification_token": "token_a",
                "encrypt_key": "encrypt_a",
            }
        )
    )

    assert provider.app_id == "cli_a"
    assert provider.app_secret == "sec_a"
    assert provider.verification_token == "token_a"
    assert provider.encrypt_key == "encrypt_a"


def test_provider_keeps_backward_compatibility_for_client_keys():
    provider = FeishuChannelProvider(
        DummyChannel(
            {
                "client_id": "cli_b",
                "client_secret": "sec_b",
                "token": "token_b",
            }
        )
    )

    assert provider.app_id == "cli_b"
    assert provider.app_secret == "sec_b"
    assert provider.verification_token == "token_b"


def test_extract_event_payload_unwraps_sdk_event_objects():
    payload = _extract_event_payload(
        SimpleNamespace(
            event=SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_x"),
                action=SimpleNamespace(value={"command": "/help"}),
            )
        )
    )

    assert payload["operator"]["open_id"] == "ou_x"
    assert payload["action"]["value"]["command"] == "/help"
