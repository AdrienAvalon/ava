"""Startup guards for principal-owned managed channel runtimes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from openjarvis.server.app import _restore_sendblue_bindings


def test_startup_never_selects_one_owner_into_global_sendblue_runtime() -> None:
    manager = MagicMock()
    manager.list_agents.return_value = [
        {"id": "owner-agent"},
        {"id": "other-agent"},
    ]
    manager.list_channel_bindings.side_effect = [
        [
            {
                "channel_type": "sendblue",
                "config": {
                    "api_key_id": "owner-key",
                    "api_secret_key": "owner-secret",
                },
            }
        ],
        [
            {
                "channel_type": "sendblue",
                "config": {
                    "api_key_id": "other-key",
                    "api_secret_key": "other-secret",
                },
            }
        ],
    ]
    app = FastAPI()
    app.state.agent_manager = manager
    existing_channel = object()
    existing_bridge = object()
    app.state.sendblue_channel = existing_channel
    app.state.channel_bridge = existing_bridge

    with patch("openjarvis.channels.sendblue.SendBlueChannel") as channel_class:
        _restore_sendblue_bindings(app)

    manager.list_agents.assert_not_called()
    manager.list_channel_bindings.assert_not_called()
    channel_class.assert_not_called()
    assert app.state.sendblue_channel is existing_channel
    assert app.state.channel_bridge is existing_bridge
