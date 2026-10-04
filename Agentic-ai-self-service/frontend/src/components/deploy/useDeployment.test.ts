/**
 * useDeployment hook unit tests.
 * Tests state transitions with mocked api module.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useDeployment } from "./useDeployment";
import type { RuntimeConfiguration } from "../../types/components";

// Mock authFetch
const mockAuthFetch = vi.fn();
vi.mock("../../auth/authFetch", () => ({
  authFetch: (...args: unknown[]) => mockAuthFetch(...args),
}));

// Mock workflowStore
const mockSetNodeExecutionStateByType = vi.fn();
const mockResetAllExecutionStates = vi.fn();
vi.mock("../../store/workflowStore", () => ({
  useWorkflowStore: () => ({
    setNodeExecutionStateByType: mockSetNodeExecutionStateByType,
    resetAllExecutionStates: mockResetAllExecutionStates,
  }),
}));

describe("useDeployment", () => {
  const mockConfig: RuntimeConfiguration = {
    name: "test-runtime",
    entrypoint: "agent.py",
    systemPrompt: "test prompt",
    model: {
      modelId: "test-model",
      provider: "bedrock",
      temperature: 0.7,
      topP: 0.9,
    },
    framework: "strands_agents",
    deploymentType: "direct_code_deploy",
    protocol: "HTTP",
    pythonRuntime: "PYTHON_3_12",
    idleTimeout: 900,
    maxLifetime: 28800,
    enableOtel: false,
    modelProvider: "bedrock",
    multiAgentPattern: "none",
  };

  const mockParams = {
    config: mockConfig,
    nodeId: "node-1",
    deploymentMode: "runtime" as const,
    connectedTools: [],
    gatewayConfig: null,
    externalMcpServers: undefined,
    gatewayTools: [],
    templateId: null,
    identityConfig: null,
    customTools: [],
    connectors: [],
    memoryConfig: null,
    evaluationConfig: null,
    policyConfig: null,
    guardrailsConfig: null,
    mcpServerConfig: null,
    knowledgeBaseConfig: null,
    observabilityConfig: null,
    a2aConfig: null,
    resourceTagState: {
      tags: {},
      profileName: null,
      profileUpdatedAt: null,
      explicitValues: {},
      policyRevision: "",
    },
    warmupRuntime: vi.fn(),
    onVersionsRefresh: vi.fn(),
    onTabChange: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("should initialize with idle state", () => {
    const { result } = renderHook(() => useDeployment(mockParams));
    expect(result.current.deploymentStatus.state).toBe("idle");
  });

  it("should transition to deploying state when handleDeploy is called", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    expect(result.current.deploymentStatus.state).toBe("idle");

    result.current.handleDeploy();

    await waitFor(() => {
      expect(mockResetAllExecutionStates).toHaveBeenCalled();
    });
  });

  it("should handle synchronous deployment success", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-123",
        endpoint: "https://test-endpoint.com",
        message: "Deployed successfully!",
      }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    await waitFor(() => {
      expect(result.current.deploymentStatus.state).toBe("deployed");
      expect(result.current.deploymentStatus.runtimeId).toBe("runtime-123");
      expect(result.current.deploymentStatus.endpoint).toBe(
        "https://test-endpoint.com",
      );
    });

    expect(mockParams.onTabChange).toHaveBeenCalledWith("chat");
    expect(mockParams.warmupRuntime).toHaveBeenCalledWith(
      "runtime-123",
      "https://test-endpoint.com",
    );
  });

  it("should handle deployment error", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: async () => "Internal Server Error",
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    await waitFor(() => {
      expect(result.current.deploymentStatus.state).toBe("error");
      expect(result.current.deploymentStatus.message).toContain("failed");
    });
  });

  it("should reset execution states when deploy starts", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));

    await result.current.handleDeploy();

    expect(mockResetAllExecutionStates).toHaveBeenCalled();
  });

  it("sends the selected target account and region to the live deploy route", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() =>
      useDeployment({
        ...mockParams,
        targetAccountId: "123456789012",
        targetRegion: "eu-west-1",
      }),
    );

    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    const body = JSON.parse((init as { body: string }).body);
    expect(body).toMatchObject({
      targetAccountId: "123456789012",
      targetRegion: "eu-west-1",
    });
  });

  it("does not invent target fields for a platform-default deploy", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() => useDeployment(mockParams));
    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    const body = JSON.parse((init as { body: string }).body);
    expect(body).not.toHaveProperty("targetAccountId");
    expect(body).not.toHaveProperty("targetRegion");
  });

  it("binds a governed deploy to the policy and profile revisions the user reviewed", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() =>
      useDeployment({
        ...mockParams,
        resourceTagState: {
          tags: { Environment: "production" },
          profileName: "regulated",
          profileUpdatedAt: "2026-09-23T10:00:00Z",
          explicitValues: {},
          policyRevision: "sha256:policy-v7",
        },
      }),
    );

    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    expect(JSON.parse((init as { body: string }).body)).toMatchObject({
      resourceTags: { Environment: "production" },
      tagProfile: "regulated",
      policyRevision: "sha256:policy-v7",
      tagProfileUpdatedAt: "2026-09-23T10:00:00Z",
    });
  });

  it("preserves an external IdP audience in the live deploy request", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() =>
      useDeployment({
        ...mockParams,
        identityConfig: {
          name: "Auth0",
          credentialType: "oauth2",
          oauth2Config: {
            provider: "auth0",
            clientId: "client-123",
            clientSecretRef:
              "arn:aws:secretsmanager:us-east-1:123456789012:secret:idp-client-AbCdEf",
            discoveryUrl:
              "https://tenant.example.com/.well-known/openid-configuration",
            scopes: ["gateway.invoke"],
            audience: "api://orders",
          },
        },
      }),
    );

    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    const body = JSON.parse((init as { body: string }).body);
    expect(body.identityConfig).toMatchObject({
      provider: "auth0",
      clientId: "client-123",
      audience: "api://orders",
    });
  });

  it("sends the saved flow id separately from the canvas node id", async () => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() =>
      useDeployment({
        ...mockParams,
        nodeId: "node-distinct-from-flow",
        flowId: "flow-distinct-from-node",
      }),
    );

    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    const body = JSON.parse((init as { body: string }).body);
    expect(body).toMatchObject({
      nodeId: "node-distinct-from-flow",
      flowId: "flow-distinct-from-node",
    });
  });

  it.each([
    ["an unsaved visual canvas", { flowId: null }],
    ["the harness", { deploymentMode: "harness" as const }],
  ])("omits flowId for %s", async (_description, overrides) => {
    mockAuthFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        runtimeId: "runtime-1",
        endpoint: "https://test.com",
      }),
    });

    const { result } = renderHook(() =>
      useDeployment({
        ...mockParams,
        ...overrides,
      }),
    );

    await result.current.handleDeploy();

    const [, init] = mockAuthFetch.mock.calls.find(
      ([url]) => url === "/api/deploy",
    )!;
    const body = JSON.parse((init as { body: string }).body);
    expect(body).not.toHaveProperty("flowId");
  });

  it("should allow manual state changes via setDeploymentStatus", () => {
    const { result } = renderHook(() => useDeployment(mockParams));

    result.current.setDeploymentStatus({ state: "idle" });

    expect(result.current.deploymentStatus.state).toBe("idle");
  });
});
