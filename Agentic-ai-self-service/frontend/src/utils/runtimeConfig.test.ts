/**
 * Property-based tests for runtime configuration utilities.
 * Updated for Strands-only with provider-based model filtering.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  getModelsForProvider,
  estimateTokenCount,
  AVAILABLE_MODELS,
  PROVIDER_OPTIONS,
} from './runtimeConfig';
import type { StrandsModelProvider } from '../types/components';

// ============================================================================
// Arbitraries (Test Data Generators)
// ============================================================================

const providerArb = fc.constantFrom<StrandsModelProvider>(
  'bedrock',
  'openai',
  'anthropic',
  'gemini',
  'mistral',
  'ollama',
  'groq',
  'deepseek',
  'together'
);

const textArb = fc.string({ minLength: 0, maxLength: 10000 });

const wordsArb = fc.array(
  fc.stringMatching(/^[a-zA-Z]+$/),
  { minLength: 0, maxLength: 500 }
).map((words) => words.join(' '));

// ============================================================================
// Provider-Based Model Filtering
// ============================================================================

describe('Provider-Based Model Filtering', () => {
  it('returns only models matching the selected provider', () => {
    fc.assert(
      fc.property(providerArb, (provider) => {
        const models = getModelsForProvider(provider);
        for (const model of models) {
          expect(model.provider).toBe(provider);
        }
      }),
      { numRuns: 50 }
    );
  });

  it('bedrock provider has the most models', () => {
    const bedrockModels = getModelsForProvider('bedrock');
    expect(bedrockModels.length).toBeGreaterThan(10);
  });

  it('non-bedrock providers with models return at least one', () => {
    const providersWithModels: StrandsModelProvider[] = ['openai', 'anthropic', 'gemini', 'mistral', 'ollama', 'groq', 'deepseek', 'together'];
    for (const provider of providersWithModels) {
      const models = getModelsForProvider(provider);
      expect(models.length).toBeGreaterThan(0);
    }
  });
});

// ============================================================================
// Token Count Estimation
// ============================================================================

describe('Token Count Estimation', () => {
  it('returns zero for empty text', () => {
    expect(estimateTokenCount('')).toBe(0);
    expect(estimateTokenCount('   ')).toBeGreaterThanOrEqual(0);
  });

  it('token count is proportional to text length', () => {
    fc.assert(
      fc.property(textArb, textArb, (text1, text2) => {
        if (text1.length === 0 || text2.length === 0) return true;
        const tokens1 = estimateTokenCount(text1);
        const tokens2 = estimateTokenCount(text2);
        if (text1.length > text2.length * 2) {
          expect(tokens1).toBeGreaterThanOrEqual(tokens2 * 0.5);
        }
        if (text2.length > text1.length * 2) {
          expect(tokens2).toBeGreaterThanOrEqual(tokens1 * 0.5);
        }
        return true;
      }),
      { numRuns: 100 }
    );
  });

  it('token count is always non-negative', () => {
    fc.assert(
      fc.property(textArb, (text) => {
        expect(estimateTokenCount(text)).toBeGreaterThanOrEqual(0);
      }),
      { numRuns: 100 }
    );
  });

  it('token count increases with word count', () => {
    fc.assert(
      fc.property(wordsArb, (text) => {
        const tokens = estimateTokenCount(text);
        const wordCount = text.split(/\s+/).filter(Boolean).length;
        if (wordCount > 0) {
          expect(tokens).toBeGreaterThanOrEqual(Math.floor(wordCount * 0.5));
          expect(tokens).toBeLessThanOrEqual(wordCount * 3);
        }
        return true;
      }),
      { numRuns: 100 }
    );
  });

  it('provides reasonable estimates for typical prompts', () => {
    const shortPrompt = 'You are a helpful assistant.';
    const mediumPrompt = `You are a helpful AI assistant specialized in customer support.
      You should be polite, professional, and provide accurate information.
      Always ask clarifying questions when needed.`;
    const longPrompt = mediumPrompt.repeat(10);

    const shortTokens = estimateTokenCount(shortPrompt);
    const mediumTokens = estimateTokenCount(mediumPrompt);
    const longTokens = estimateTokenCount(longPrompt);

    expect(shortTokens).toBeGreaterThan(3);
    expect(shortTokens).toBeLessThan(20);
    expect(mediumTokens).toBeGreaterThan(shortTokens);
    expect(longTokens).toBeGreaterThan(mediumTokens * 5);
    expect(longTokens).toBeLessThan(mediumTokens * 15);
  });
});

// ============================================================================
// Additional Unit Tests
// ============================================================================

describe('Runtime Configuration Utilities', () => {
  it('all providers with models are defined in PROVIDER_OPTIONS', () => {
    const providersInModels = new Set(AVAILABLE_MODELS.map((m) => m.provider));
    for (const provider of providersInModels) {
      const option = PROVIDER_OPTIONS.find((p) => p.value === provider);
      expect(option).toBeDefined();
      expect(option?.label).toBeTruthy();
    }
  });

  it('all models have required properties', () => {
    for (const model of AVAILABLE_MODELS) {
      expect(model.provider).toBeTruthy();
      expect(model.modelId).toBeTruthy();
      expect(model.label).toBeTruthy();
      expect(model.maxTokens).toBeGreaterThan(0);
    }
  });

  it('bedrock is the default provider and has no API key requirement', () => {
    const bedrock = PROVIDER_OPTIONS.find((p) => p.value === 'bedrock');
    expect(bedrock).toBeDefined();
    expect(bedrock?.requiresApiKey).toBe(false);
  });
});
