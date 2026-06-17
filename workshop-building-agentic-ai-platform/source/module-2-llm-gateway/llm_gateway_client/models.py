# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pydantic models for LiteLLM Proxy API responses."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    model: str = ""
    choices: list[Choice] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = ""


class ModelsListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo] = Field(default_factory=list)


class KeyResponse(BaseModel):
    key: str = ""
    token: str = ""
    key_name: str = ""
    team_id: str | None = None
    max_budget: float | None = None
    expires: str | None = None


class TeamResponse(BaseModel):
    team_id: str | None = None
    team_alias: str = ""
    max_budget: float | None = None
    models: list[str] = Field(default_factory=list)
