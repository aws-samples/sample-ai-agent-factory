/**
 * Runtime configuration utilities including model filtering and token estimation.
 * Strands-only — all models organized by provider.
 */

import type { StrandsModelProvider } from '../types/components';

// ============================================================================
// Model Definitions
// ============================================================================

export interface ModelOption {
  provider: StrandsModelProvider;
  modelId: string;
  label: string;
  maxTokens: number;
}

/**
 * Derive the cross-region inference prefix from the deployment region.
 * US regions → "us.", EU regions → "eu.", AP regions → "ap.", default → "us."
 */
export function getRegionPrefix(): string {
  const region = import.meta.env.VITE_AWS_REGION || '';
  if (region.startsWith('eu-')) return 'eu';
  if (region.startsWith('ap-')) return 'ap';
  return 'us';
}

/**
 * Replace the `us.` prefix on cross-region Bedrock model IDs with the
 * correct prefix for the deployment region.
 */
function regionalize(models: ModelOption[]): ModelOption[] {
  const prefix = getRegionPrefix();
  return models.map((m) => {
    if (m.provider === 'bedrock' && m.modelId.startsWith('us.')) {
      return { ...m, modelId: `${prefix}.${m.modelId.slice(3)}` };
    }
    return m;
  });
}

/**
 * Available models organized by Strands model provider.
 * Bedrock models use cross-region inference IDs, auto-prefixed for the deployment region.
 * Non-Bedrock models use their native model IDs.
 */
export const AVAILABLE_MODELS: ModelOption[] = regionalize([
  // ============================================================================
  // Amazon Bedrock Models (provider: 'bedrock') — default, IAM-based, no API key
  // ============================================================================

  // Anthropic Claude 4.5 Series — Bedrock GA Sep–Nov 2025
  { provider: 'bedrock', modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'Claude Sonnet 4.5', maxTokens: 200000 },
  { provider: 'bedrock', modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5', maxTokens: 200000 },
  { provider: 'bedrock', modelId: 'us.anthropic.claude-opus-4-5-20251101-v1:0', label: 'Claude Opus 4.5', maxTokens: 200000 },
  // NOTE: Older models are intentionally excluded by policy. Only models
  // published on Amazon Bedrock between October 2025 and May 2026 are listed
  // here. Claude Sonnet 4 / Opus 4.1 (May–Aug 2025), Claude 3.x, Nova v1
  // (Pro/Lite/Micro), Titan Text Premier, Mistral Large 2407 / Small 2402,
  // Cohere Command R/R+, Llama 3.x have all been removed because Bedrock
  // flags pre-Q3-2025 models as Legacy and returns
  // `ResourceNotFoundException: Access denied. This Model is marked by
  // provider as Legacy and you have not been actively using the model in
  // the last 30 days.` See tasks/lessons.md Bug 113.

  // Amazon Nova 2 — Bedrock GA Q4 2025
  { provider: 'bedrock', modelId: 'us.amazon.nova-2-lite-v1:0', label: 'Amazon Nova 2 Lite', maxTokens: 300000 },
  { provider: 'bedrock', modelId: 'us.amazon.nova-premier-v1:0', label: 'Amazon Nova Premier', maxTokens: 300000 },

  // Meta Llama 4 — Bedrock GA Oct 2025
  { provider: 'bedrock', modelId: 'us.meta.llama4-maverick-17b-instruct-v1:0', label: 'Llama 4 Maverick 17B', maxTokens: 128000 },
  { provider: 'bedrock', modelId: 'us.meta.llama4-scout-17b-instruct-v1:0', label: 'Llama 4 Scout 17B', maxTokens: 128000 },

  // AI21 Jamba 1.5 — Bedrock-current; 256k context, retained
  { provider: 'bedrock', modelId: 'ai21.jamba-1-5-large-v1:0', label: 'AI21 Jamba 1.5 Large', maxTokens: 256000 },
  { provider: 'bedrock', modelId: 'ai21.jamba-1-5-mini-v1:0', label: 'AI21 Jamba 1.5 Mini', maxTokens: 256000 },

  // OpenAI OSS — Bedrock GA Q4 2025
  { provider: 'bedrock', modelId: 'openai.gpt-oss-120b-1:0', label: 'GPT OSS 120B', maxTokens: 128000 },
  { provider: 'bedrock', modelId: 'openai.gpt-oss-20b-1:0', label: 'GPT OSS 20B', maxTokens: 128000 },

  // DeepSeek — Bedrock GA Q4 2025 (R1) / Q1 2026 (V3.1)
  { provider: 'bedrock', modelId: 'us.deepseek.r1-v1:0', label: 'DeepSeek R1', maxTokens: 128000 },
  { provider: 'bedrock', modelId: 'deepseek.v3-v1:0', label: 'DeepSeek V3.1', maxTokens: 128000 },

  // ============================================================================
  // OpenAI (Direct API — requires OPENAI_API_KEY)
  // ============================================================================
  { provider: 'openai', modelId: 'gpt-4o', label: 'GPT-4o', maxTokens: 128000 },
  { provider: 'openai', modelId: 'gpt-4o-mini', label: 'GPT-4o Mini', maxTokens: 128000 },
  { provider: 'openai', modelId: 'gpt-4-turbo', label: 'GPT-4 Turbo', maxTokens: 128000 },
  { provider: 'openai', modelId: 'o1', label: 'OpenAI o1', maxTokens: 200000 },
  { provider: 'openai', modelId: 'o1-mini', label: 'OpenAI o1-mini', maxTokens: 128000 },
  { provider: 'openai', modelId: 'o3-mini', label: 'OpenAI o3-mini', maxTokens: 200000 },

  // ============================================================================
  // Anthropic Direct API (requires ANTHROPIC_API_KEY)
  // ============================================================================
  { provider: 'anthropic', modelId: 'claude-sonnet-4-5-20250929', label: 'Claude Sonnet 4.5 (Direct)', maxTokens: 200000 },
  { provider: 'anthropic', modelId: 'claude-opus-4-5-20251101', label: 'Claude Opus 4.5 (Direct)', maxTokens: 200000 },
  { provider: 'anthropic', modelId: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5 (Direct)', maxTokens: 200000 },

  // ============================================================================
  // Google Gemini (requires GOOGLE_API_KEY)
  // ============================================================================
  { provider: 'gemini', modelId: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash', maxTokens: 1000000 },
  { provider: 'gemini', modelId: 'gemini-1.5-pro', label: 'Gemini 1.5 Pro', maxTokens: 2000000 },
  { provider: 'gemini', modelId: 'gemini-1.5-flash', label: 'Gemini 1.5 Flash', maxTokens: 1000000 },

  // ============================================================================
  // Mistral Direct API (requires MISTRAL_API_KEY)
  // ============================================================================
  { provider: 'mistral', modelId: 'mistral-large-latest', label: 'Mistral Large', maxTokens: 128000 },
  { provider: 'mistral', modelId: 'mistral-small-latest', label: 'Mistral Small', maxTokens: 32000 },

  // ============================================================================
  // Ollama (local, no API key)
  // ============================================================================
  { provider: 'ollama', modelId: 'llama3', label: 'Llama 3 (Local)', maxTokens: 128000 },
  { provider: 'ollama', modelId: 'codellama', label: 'CodeLlama (Local)', maxTokens: 128000 },
  { provider: 'ollama', modelId: 'mistral', label: 'Mistral (Local)', maxTokens: 32000 },

  // ============================================================================
  // Groq (requires GROQ_API_KEY)
  // ============================================================================
  { provider: 'groq', modelId: 'llama-3.1-70b-versatile', label: 'Llama 3.1 70B (Groq)', maxTokens: 128000 },
  { provider: 'groq', modelId: 'mixtral-8x7b-32768', label: 'Mixtral 8x7B (Groq)', maxTokens: 32000 },

  // ============================================================================
  // DeepSeek Direct API (requires DEEPSEEK_API_KEY)
  // ============================================================================
  { provider: 'deepseek', modelId: 'deepseek-chat', label: 'DeepSeek Chat', maxTokens: 128000 },
  { provider: 'deepseek', modelId: 'deepseek-coder', label: 'DeepSeek Coder', maxTokens: 128000 },

  // ============================================================================
  // Together AI (requires TOGETHER_API_KEY, via LiteLLM)
  // ============================================================================
  { provider: 'together', modelId: 'meta-llama/Llama-3.1-70B-Instruct-Turbo', label: 'Llama 3.1 70B (Together)', maxTokens: 128000 },
  { provider: 'together', modelId: 'mistralai/Mixtral-8x7B-Instruct-v0.1', label: 'Mixtral 8x7B (Together)', maxTokens: 32000 },
]);

// ============================================================================
// Provider Definitions
// ============================================================================

export interface ProviderOption {
  value: StrandsModelProvider;
  label: string;
  description: string;
  requiresApiKey: boolean;
  envVar?: string;
}

export const PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'bedrock', label: 'Amazon Bedrock', description: 'AWS-native, uses IAM — no API key needed', requiresApiKey: false },
  { value: 'openai', label: 'OpenAI', description: 'GPT-4o, o1, o3 series', requiresApiKey: true, envVar: 'OPENAI_API_KEY' },
  { value: 'anthropic', label: 'Anthropic (Direct)', description: 'Claude via direct API', requiresApiKey: true, envVar: 'ANTHROPIC_API_KEY' },
  { value: 'gemini', label: 'Google Gemini', description: 'Gemini 2.0 Flash, 1.5 Pro', requiresApiKey: true, envVar: 'GOOGLE_API_KEY' },
  { value: 'mistral', label: 'Mistral', description: 'Mistral Large, Small', requiresApiKey: true, envVar: 'MISTRAL_API_KEY' },
  { value: 'ollama', label: 'Ollama (Local)', description: 'Local models — no API key', requiresApiKey: false },
  { value: 'groq', label: 'Groq', description: 'Ultra-fast inference', requiresApiKey: true, envVar: 'GROQ_API_KEY' },
  { value: 'deepseek', label: 'DeepSeek', description: 'DeepSeek Chat & Coder', requiresApiKey: true, envVar: 'DEEPSEEK_API_KEY' },
  { value: 'together', label: 'Together AI', description: 'Open-source models via Together', requiresApiKey: true, envVar: 'TOGETHER_API_KEY' },
  { value: 'sagemaker', label: 'SageMaker', description: 'Custom SageMaker endpoints', requiresApiKey: false },
  { value: 'litellm', label: 'LiteLLM', description: 'Universal proxy for 100+ providers', requiresApiKey: true, envVar: 'LITELLM_API_KEY' },
  { value: 'writer', label: 'Writer', description: 'Writer AI models', requiresApiKey: true, envVar: 'WRITER_API_KEY' },
];

// ============================================================================
// Model Filtering
// ============================================================================

/**
 * Get models available for a specific provider.
 */
export function getModelsForProvider(provider: StrandsModelProvider): ModelOption[] {
  return AVAILABLE_MODELS.filter((model) => model.provider === provider);
}

/**
 * Get model details by ID.
 */
export function getModelById(modelId: string): ModelOption | undefined {
  return AVAILABLE_MODELS.find((m) => m.modelId === modelId);
}

// ============================================================================
// Token Estimation
// ============================================================================

/**
 * Estimate token count for a given text.
 */
export function estimateTokenCount(text: string): number {
  if (!text || text.length === 0) {
    return 0;
  }

  const CHARS_PER_TOKEN = 4;
  const words = text.split(/\s+/).filter(Boolean);
  const wordCount = words.length;
  const charCount = text.length;
  const wordBasedEstimate = Math.ceil(wordCount * 1.3);
  const charBasedEstimate = Math.ceil(charCount / CHARS_PER_TOKEN);

  return Math.ceil((wordBasedEstimate + charBasedEstimate) / 2);
}

/**
 * Format token count for display.
 */
export function formatTokenCount(count: number): string {
  if (count >= 1000) {
    return `${(count / 1000).toFixed(1)}k`;
  }
  return count.toString();
}

// ============================================================================
// Default Configuration
// ============================================================================

import type { RuntimeConfiguration } from '../types/components';

export function createDefaultRuntimeConfig(): RuntimeConfiguration {
  return {
    name: '',
    entrypoint: 'agent.py',
    framework: 'strands_agents',
    model: {
      provider: 'bedrock',
      modelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
      temperature: 0.7,
      topP: 0.9,
    },
    systemPrompt: '',
    deploymentType: 'direct_code_deploy',
    pythonRuntime: 'PYTHON_3_13',
    protocol: 'HTTP',
    idleTimeout: 120,
    maxLifetime: 28800,
    enableOtel: false,
    modelProvider: 'bedrock',
    multiAgentPattern: 'none',
  };
}
